"""
Runtime version/freshness info for display in the app's titlebars (web dashboard tab title +
hero heading, TUI header). Answers "is the code that's actually running right now the same code
that's pushed to origin/master?" - useful because run.bat's git pull is best-effort (it silently
no-ops on diverged history, conflicts, or being offline), so a stale checkout can otherwise run
for a long time with no visible sign anything's out of date.

Git calls are best-effort and cheap but not free, so the result is computed once per process and
cached - it can't change without a restart (git pull happens only at launch, before this process's
Python even imports this module) other than someone pulling in a second terminal mid-session, which
is an edge case not worth polling for.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from . import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_FILE = _REPO_ROOT / "CHANGELOG.md"
_cache: Optional[dict] = None


def _git(*args: str, timeout: float = 3) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _relation_to_remote(head: str, remote: str) -> str:
    """Where HEAD sits relative to origin/master: "same", "behind" (remote has commits HEAD
    lacks - a real update is available), "ahead" (HEAD has local commits never pushed - normal
    day-to-day state for this repo, since CLAUDE.md has Claude commit locally and only push on
    explicit request; NOT the same thing as being out of date), or "diverged" (both - rare,
    happens after a force-push or rebase upstream)."""
    if head == remote:
        return "same"
    merge_base = _git("merge-base", head, remote)
    if merge_base == head:
        return "behind"
    if merge_base == remote:
        return "ahead"
    return "diverged"


def get_version_info() -> dict:
    """Returns {version, commit, dirty, status} where status is one of "current" (HEAD matches
    or is ahead of origin/master, no local changes to tracked files), "stale" (HEAD is behind or
    diverged from origin/master - an actual update is available/needed), or "unknown" (not a git
    checkout, or git/network info unavailable). Being ahead of origin/master (unpushed local
    commits) is deliberately NOT "stale" - see _relation_to_remote."""
    global _cache
    if _cache is not None:
        return _cache

    head = _git("rev-parse", "HEAD")
    remote = _git("rev-parse", "origin/master")
    # --untracked-files=no: this repo always has scratch untracked files lying around (settings.json,
    # __pycache__, dropped images, logs) that were never meant to affect version freshness - counting
    # them as "dirty" made the pill report "stale" essentially permanently, even on a checkout that
    # exactly matched origin/master.
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))

    if head is None:
        status = "unknown"
    elif remote is None:
        status = "unknown" if not dirty else "stale"
    else:
        relation = _relation_to_remote(head, remote)
        status = "stale" if (relation in ("behind", "diverged") or dirty) else "current"

    _cache = {
        "version": __version__,
        "commit": head[:7] if head else "unknown",
        "dirty": dirty,
        "status": status,
    }
    return _cache


def check_for_update() -> dict:
    """On-demand, uncached counterpart to get_version_info()'s cached "stale" status - run a
    real `git fetch` first so this reflects what's actually on the remote right now, not just
    whatever origin/master happened to point at when this process started. Returns
    {update_available, status, error}; status mirrors get_version_info()'s "current"/"stale"/
    "unknown" values so the caller can reuse the same button copy either way. "update_available"
    is only true when origin/master is genuinely ahead of (or diverged from) HEAD - unpushed
    local commits (HEAD ahead of origin/master) are never reported as an update being available."""
    # git fetch is a network round-trip, unlike the other (local, near-instant) git calls in this
    # module - a 3s timeout was clipping legitimate fetches (observed taking ~2.4s on a normal
    # connection) and surfacing as a bogus "couldn't reach origin" error on the dashboard pill.
    fetch_ok = _git("fetch", "--quiet", "origin", "master", timeout=15) is not None
    head = _git("rev-parse", "HEAD")
    remote = _git("rev-parse", "origin/master")

    if head is None:
        return {"update_available": False, "status": "unknown", "error": "not a git checkout"}
    if not fetch_ok or remote is None:
        return {"update_available": False, "status": "unknown", "error": "couldn't reach origin"}

    relation = _relation_to_remote(head, remote)
    update_available = relation in ("behind", "diverged")
    return {
        "update_available": update_available,
        "status": "stale" if update_available else "current",
        "error": None,
    }


def apply_update() -> dict:
    """Runs the actual update - `git pull --ff-only origin master` - invoked when the user clicks
    an "update available" pill. Fast-forward only, same as run.bat's launch-time pull, so it never
    clobbers local changes; a diverged/conflicting history fails cleanly and reports an error
    instead of doing anything destructive. Clears the cached get_version_info() result on success
    so the next page render reflects the new HEAD."""
    global _cache
    output = _git("pull", "--ff-only", "origin", "master", timeout=15)
    if output is None:
        return {"success": False, "error": "update failed - couldn't fast-forward (check for local changes or run `git pull` manually)"}
    _cache = None
    return {"success": True, "error": None}


# ------------------------------------------------------------------------------- changelog
# CHANGELOG.md format: "## <version>" headers (newest first) followed by "- " bullet lines,
# parsed with a plain regex rather than a Markdown library - the file only ever needs to be
# read by this one function, and the format is fully within this project's control.
_VERSION_HEADER_RE = re.compile(r"^##\s+(\S+)", re.MULTILINE)


def _parse_changelog() -> list[dict]:
    """Returns [{"version": "1.1.0", "entries": ["...", ...]}, ...], newest-first as they
    appear in the file. Empty list if CHANGELOG.md is missing or unparsable - a missing
    changelog should degrade to "nothing to show", not break the dashboard."""
    try:
        text = _CHANGELOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return []

    headers = list(_VERSION_HEADER_RE.finditer(text))
    sections = []
    for i, m in enumerate(headers):
        version = m.group(1)
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]
        entries = [
            line.strip().lstrip("-").strip()
            for line in body.splitlines()
            if line.strip().startswith("-")
        ]
        sections.append({"version": version, "entries": entries})
    return sections


def get_changelog() -> list[dict]:
    """Full changelog, newest version first - backs the Changelog tab."""
    return _parse_changelog()
