"""
Stash (stashapp) lifecycle - install check, start, stop, and status. Stash is a single
self-contained exe serving its own web UI on STASH_PORT, so unlike hydownloader there's no
API client here: Undertow only needs to know whether it's running and be able to start/stop it.
"""

from __future__ import annotations

import subprocess
import time

import psutil

from . import config


def is_installed() -> bool:
    return config.STASH_EXE.exists()


def find_process() -> psutil.Process | None:
    exe_name = config.STASH_EXE.name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() == exe_name:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def start() -> str | None:
    """Launches Stash hidden with output captured to log files. --nobrowser stops it opening a
    browser tab on every launch - the dashboard links to it instead. Returns None on success
    (or if already running), or an error string."""
    if not is_installed():
        return f"Stash not found at {config.STASH_EXE} - run Setup-HydrusPipeline.ps1 first."
    if find_process() is not None:
        return None
    config.STASH_LAUNCH_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(config.STASH_LAUNCH_STDOUT_LOG, "w", encoding="utf-8") as out, \
            open(config.STASH_LAUNCH_STDERR_LOG, "w", encoding="utf-8") as err:
        subprocess.Popen(
            [str(config.STASH_EXE), "--nobrowser"],
            cwd=str(config.STASH_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
        )
    return None


def stop(timeout: float = 10.0) -> None:
    proc = find_process()
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def restart(timeout: float = 10.0) -> str | None:
    stop(timeout)
    time.sleep(1)
    return start()
