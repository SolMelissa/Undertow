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
    running: bool = False
    returncode: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


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
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                with run.lock:
                    run.lines.append(line.rstrip("\n"))
            proc.wait()
            with run.lock:
                run.returncode = proc.returncode
        except OSError as e:
            with run.lock:
                run.lines.append(f"[failed to launch: {e}]")
                run.returncode = -1
        finally:
            with run.lock:
                run.running = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def read_since(name: str, since: int | None) -> tuple[list[str], int, bool, int | None]:
    """Returns (new_lines, offset, running, returncode) for the named script's current/last
    run. offset is a line count, not a byte offset (output is captured in memory, not tailed
    off disk), but plays the same cursor role as logtail.read_since's offset."""
    run = _runs.get(name)
    if run is None:
        return [], 0, False, None
    with run.lock:
        start_at = 0 if since is None else since
        new_lines = run.lines[start_at:]
        return new_lines, len(run.lines), run.running, run.returncode
