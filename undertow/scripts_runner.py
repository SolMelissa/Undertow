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


def list_scripts() -> list[str]:
    """Names (without .py) of runnable scripts in undertow/scripts/, alphabetical."""
    if not SCRIPTS_DIR.exists():
        return []
    return sorted(p.stem for p in SCRIPTS_DIR.glob("*.py") if p.is_file() and not p.stem.startswith("_"))


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
