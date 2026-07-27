"""
Stops the hydownloader daemon and hydownloader-systray ONLY (leaves Hydrus itself running).
Equivalent of Stop-HydrusPipelineServices.ps1. Used when hydownloader-config.json changed
and the running daemon/systray need to pick up fresh values - editing the file alone doesn't
affect an already-running process. Run with: python -m hydrus_pipeline.stop_services
"""

from __future__ import annotations

import time

import psutil

from .services import find_hydownloader_daemon_proc, find_process_by_name


def run() -> None:
    print(">>> Stopping hydownloader daemon (if running)...")
    daemon = find_hydownloader_daemon_proc()
    if daemon:
        print(f"  stopping PID {daemon.pid}")
        try:
            daemon.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    print(">>> Stopping hydownloader-systray (if running)...")
    systray = find_process_by_name("hydownloader-systray.exe")
    if systray:
        print(f"  stopping PID {systray.pid}")
        try:
            systray.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(2)
    print()
    print("Done. Hydrus itself was left running. Re-run Setup-HydrusPipeline.ps1 to start fresh copies of the daemon and systray with the corrected config.")


if __name__ == "__main__":
    run()
    input("Press Enter to close this window")
