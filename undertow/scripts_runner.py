"""
Backs the webui's Scripts tab: discovers runnable one-off scripts in undertow/scripts/ and
runs them as subprocesses with their stdout/stderr captured for the browser to poll and
display as a terminal, using the same offset-cursor pattern as logtail.py's daemon log
tailing (read_since / /partials/log) so the frontend logic can be reused almost verbatim.

Only one run per script name is tracked at a time - starting a script again while it's still
running is a no-op (the frontend disables the pill while running) rather than something this
module needs to guard against with real concurrency control.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = PACKAGE_DIR / "scripts"

# Display metadata for the Scripts tab's cards - icon/label/description/group. A script with no
# entry here still runs fine (list_scripts() only reads the filesystem), it just renders as a
# bare pill with its filename, same as before this manifest existed.
SCRIPT_META: dict[str, dict[str, str]] = {
    "hydrus_health_check": {
        "icon": "\U0001fa7a", "label": "Hydrus Health Check",
        "desc": "Ping Hydrus + hydownloader; show file/inbox counts and daemon status.",
        "group": "Reports",
    },
    "inbox_triage_report": {
        "icon": "\U0001f4e5", "label": "Inbox Triage",
        "desc": "Bucket the Hydrus inbox by file age so backlog is visible at a glance.",
        "group": "Reports",
    },
    "untagged_files_report": {
        "icon": "\U0001f3f7️", "label": "Untagged Files",
        "desc": "List files with zero tags on the local tag service.",
        "group": "Reports",
    },
    "duplicate_tag_finder": {
        "icon": "\U0001f50d", "label": "Duplicate Tag Finder",
        "desc": "Find probable near-duplicate tags (casing/whitespace variants).",
        "group": "Reports",
    },
    "tag_namespace_summary": {
        "icon": "\U0001f4ca", "label": "Namespace Summary",
        "desc": "Tag + usage counts per common namespace (creator, character, ...).",
        "group": "Reports",
    },
    "subscription_health_report": {
        "icon": "\U0001f4e1", "label": "Subscription Health",
        "desc": "Flag paused, zero-download, or currently-failing subscriptions.",
        "group": "Reports",
    },
    "queued_urls_report": {
        "icon": "\U0001f5c2️", "label": "Queue Report",
        "desc": "Summarize hydownloader's queued URLs and flag the oldest entries.",
        "group": "Reports",
    },
    "disk_usage_report": {
        "icon": "\U0001f4be", "label": "Disk Usage",
        "desc": "Folder size breakdown plus free space on the install drive.",
        "group": "Reports",
    },
    "log_archiver": {
        "icon": "\U0001f5c3️", "label": "Log Archiver",
        "desc": "Zip logs older than 14 days; prune archives older than 90.",
        "group": "Housekeeping",
    },
    "empty_folder_sweep": {
        "icon": "\U0001f9f9", "label": "Empty Folder Sweep",
        "desc": "Remove empty leftover folders under hydownloader-data.",
        "group": "Housekeeping",
    },
    "tag_cleanup": {
        "icon": "\U0001f9fd", "label": "Tag Cleanup Wizard",
        "desc": "Interactive filename-tag splitter/cleaner, with preview before writing.",
        "group": "Interactive Wizards",
    },
    "performer_gazetteer": {
        "icon": "\U0001f575️", "label": "Performer Gazetteer",
        "desc": "Build the performer-name cache Tag Cleanup's name detection reads.",
        "group": "Interactive Wizards",
    },
    "tagrank_setup_hidden_tags_marker": {
        "icon": ">", "label": "Setup TagRank Hidden Tags Marker",
        "desc": "One-time setup: import marker image to Hydrus.",
        "group": "TagRank",
    },
    "sync_hidden_tags_to_marker": {
        "icon": ">", "label": "Sync TagRank Hidden Tags",
        "desc": "Sync hidden tags from TagRank config to Hydrus marker file.",
        "group": "TagRank",
    },
    "tagrank_demo_fix": {
        "icon": ">", "label": "TagRank Dashboard Demo",
        "desc": "Demo showing TagRank dashboard with 4 charts.",
        "group": "TagRank",
    },
    "tagrank_test_dashboard": {
        "icon": ">", "label": "TagRank Dashboard E2E Test",
        "desc": "End-to-end test for TagRank session summary dashboard.",
        "group": "TagRank",
    },
}

GROUP_ORDER = ["Reports", "Housekeeping", "Interactive Wizards", "TagRank", "Other"]


def meta_for(name: str) -> dict[str, str]:
    return SCRIPT_META.get(name) or {"icon": "▶️", "label": name, "desc": "", "group": "Other"}


@dataclass
class ScriptRun:
    lines: list[str] = field(default_factory=list)
    # Text written since the last completed line, e.g. an `input("prompt: ")` prompt that's
    # sitting on stdout with no trailing newline while the script blocks waiting for a reply.
    # Read line-by-line, that text would never surface until the script wrote a newline or
    # exited - which for an interactive prompt is never, so a raw byte-stream reader (see
    # start()) tracks it separately and every poll re-sends it (it's not "in" `lines` yet).
    partial: str = ""
    running: bool = False
    returncode: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    proc: subprocess.Popen | None = None


_runs: dict[str, ScriptRun] = {}


# Files under scripts/ that are support/library modules, not runnable scripts - imported by
# webui.py or other scripts rather than meant to be launched as a subprocess. They can't use
# the leading-underscore convention (_common.py) because other code imports them by this
# exact name (webui.py: `import tag_cleanup_lists`).
NOT_RUNNABLE = {"tag_cleanup_lists"}


def list_scripts() -> list[str]:
    """Names (without .py) of runnable scripts in undertow/scripts/, alphabetical."""
    if not SCRIPTS_DIR.exists():
        return []
    return sorted(
        p.stem for p in SCRIPTS_DIR.glob("*.py")
        if p.is_file() and not p.stem.startswith("_") and p.stem not in NOT_RUNNABLE
    )


def list_scripts_grouped() -> list[tuple[str, list[str]]]:
    """Script names bucketed by SCRIPT_META's "group" (GROUP_ORDER's order, alphabetical within
    each group), as (group_name, [script_name, ...]) pairs - what the Scripts tab renders as
    separate card sections instead of one flat pile of pills."""
    by_group: dict[str, list[str]] = {}
    for name in list_scripts():
        by_group.setdefault(meta_for(name)["group"], []).append(name)
    ordered_groups = GROUP_ORDER + sorted(g for g in by_group if g not in GROUP_ORDER)
    return [(g, by_group[g]) for g in ordered_groups if g in by_group]


def _script_path(name: str) -> Path | None:
    # Reject anything that isn't a bare filename stem - name comes from a URL segment.
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = SCRIPTS_DIR / f"{name}.py"
    return path if path.is_file() else None


def is_running(name: str) -> bool:
    run = _runs.get(name)
    return bool(run and run.running)


def start(name: str) -> bool:
    """Launches the named script in a background thread. Returns False if the name doesn't
    resolve to a real script or a run is already in progress."""
    path = _script_path(name)
    if path is None or is_running(name):
        return False

    run = ScriptRun(running=True)
    _runs[name] = run

    def worker() -> None:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(path)],
                cwd=str(PACKAGE_DIR.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            with run.lock:
                run.proc = proc
            assert proc.stdout is not None
            fd = proc.stdout.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                parts = chunk.decode("utf-8", errors="replace").split("\n")
                with run.lock:
                    if len(parts) == 1:
                        run.partial += parts[0]
                    else:
                        run.lines.append(run.partial + parts[0])
                        run.lines.extend(parts[1:-1])
                        run.partial = parts[-1]
            proc.wait()
            with run.lock:
                if run.partial:
                    run.lines.append(run.partial)
                    run.partial = ""
                run.returncode = proc.returncode
        except OSError as e:
            with run.lock:
                run.lines.append(f"[failed to launch: {e}]")
                run.returncode = -1
        finally:
            with run.lock:
                run.running = False
                run.proc = None

    threading.Thread(target=worker, daemon=True).start()
    return True


def stop(name: str) -> bool:
    """Force-kills the named script's process if it's currently running (for a hung/stuck
    script the Scripts tab needs to reset without waiting on it). Returns False if there's
    nothing running for that name."""
    run = _runs.get(name)
    if run is None:
        return False
    with run.lock:
        proc = run.proc
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.kill()
    except OSError:
        return False
    return True


def send_input(name: str, text: str) -> bool:
    """Writes a line to the running script's stdin (e.g. answering an input() prompt).
    Returns False if the script isn't currently running."""
    run = _runs.get(name)
    if run is None:
        return False
    with run.lock:
        proc = run.proc
    if proc is None or proc.stdin is None or proc.poll() is not None:
        return False
    try:
        proc.stdin.write(text + "\n")
        proc.stdin.flush()
    except OSError:
        return False
    return True


def read_since(name: str, since: int | None) -> tuple[list[str], int, bool, int | None, str]:
    """Returns (new_lines, offset, running, returncode, partial) for the named script's
    current/last run. offset is a line count, not a byte offset (output is captured in memory,
    not tailed off disk), but plays the same cursor role as logtail.read_since's offset.
    `partial` is always re-sent in full (not delta'd via offset) since it keeps growing in
    place until it either completes into a line or the run ends - see ScriptRun.partial."""
    run = _runs.get(name)
    if run is None:
        return [], 0, False, None, ""
    with run.lock:
        start_at = 0 if since is None else since
        new_lines = run.lines[start_at:]
        return new_lines, len(run.lines), run.running, run.returncode, run.partial
