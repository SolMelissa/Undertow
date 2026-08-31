"""
Startup/shutdown wiring - equivalent of the STARTUP section at the bottom of
Launch-HydrusPipeline.ps1. Makes sure Hydrus, the hydownloader daemon, and the systray are all
running (starting only whatever isn't already up), starts the background watchdog, then hands
off to the web dashboard (undertow/webui.py) as the primary interface - the console
window is hidden once it's up, since there's nothing left to interact with there day to day.

The Textual TUI (undertow/tui/) is still fully functional - it's the automatic fallback
if Flask isn't installed, and otherwise stays one click away via the web dashboard's own
"Console UI" button (which launches it in a fresh, visible console window without re-running
any of the startup steps below - see undertow/tui/__main__.py).

Does NOT reinstall anything - for first-time setup, use Setup-HydrusPipeline.ps1 (still
PowerShell; it's a one-time provisioning script, not part of daily use, so it wasn't
in scope for this port).
"""

from __future__ import annotations

import os
import threading

from . import services, subscriptions
from .watchdog import Watchdog

_shutdown_lock = threading.Lock()
_shutdown_done = False


def _shutdown_once() -> None:
    """Runs Stop-IdleComponents exactly once no matter how the process exits (normal quit, an
    unhandled exception, Ctrl+C, or the console window's X button) - equivalent of the PS1
    having both the [Q] menu handler AND a PowerShell.Exiting event registered so closing the
    window without picking Q still does the idle-shutdown check."""
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True
    try:
        services.stop_idle_components()
    except Exception:
        pass


def _install_exit_handlers() -> None:
    import atexit

    atexit.register(_shutdown_once)
    try:
        import win32api

        def console_handler(ctrl_type: int) -> bool:
            # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2, CTRL_LOGOFF_EVENT=5,
            # CTRL_SHUTDOWN_EVENT=6 - covers the window's X button, not just Ctrl+C, which
            # atexit alone doesn't reliably catch for console close on Windows.
            if ctrl_type in (0, 1, 2, 5, 6):
                _shutdown_once()
            return False

        win32api.SetConsoleCtrlHandler(console_handler, True)
    except ImportError:
        pass


def main() -> None:
    services.set_console_window_icon()

    print("Checking Hydrus pipeline services...")
    services.start_required_services()

    print("Making sure every subscription is grouped for parallel checking...")
    updated, total, restarted, error = subscriptions.ensure_all_subscriptions_parallel()
    if error:
        print(f"  couldn't check ({error}) - will retry next launch.")
    elif restarted:
        print(f"  regrouped {updated} of {total} subscription(s) and restarted the daemon to activate them.")
    else:
        print(f"  all {total} subscription(s) already parallel.")

    print("Prioritizing least-recently-succeeded subscriptions for the check queue...")
    prioritized, prio_total, prio_error = subscriptions.sync_priority_by_last_success()
    if prio_error:
        print(f"  couldn't check ({prio_error}) - will retry next launch.")
    elif prioritized:
        print(f"  updated queue priority for {prioritized} of {prio_total} subscription(s).")
    else:
        print(f"  all {prio_total} subscription(s) already correctly prioritized.")

    watchdog = Watchdog()
    watchdog.start()
    _install_exit_handlers()

    print()
    print("Ready - starting the web dashboard...")

    from . import webui

    # Set by UndertowLauncher.cs when it's about to show the dashboard in its own
    # WebView2 frame - skips opening a redundant browser tab alongside the app window.
    open_browser = os.environ.get("UNDERTOW_NO_BROWSER") != "1"
    port = webui.run_webui(open_browser=open_browser)
    if port is None:
        print("  'flask' isn't installed (pip install -r requirements.txt) - falling back to the console UI.")
        from .tui.app import PipelineApp

        try:
            PipelineApp().run()
        finally:
            # Covers every exit path out of the TUI (quit action, an unhandled exception
            # inside Textual, ...) in addition to the atexit/console-ctrl handlers above,
            # which also catch the window's X button closing the process out from under it.
            _shutdown_once()
        return

    print(f"  web dashboard running at http://127.0.0.1:{port} - this console window will now hide.")
    services.hide_console_window()

    try:
        # The web server and watchdog both run as daemon threads (see webui.run_webui and
        # Watchdog.start) - daemon threads don't keep the process alive on their own, so the
        # main thread has to block here for as long as the pipeline should keep running.
        # Normal shutdown now happens via the dashboard's own Shutdown button (which calls
        # os._exit directly, see webui.py) rather than this wait ever returning on its own;
        # this still catches Ctrl+C for the rare case this is run somewhere the console
        # wasn't hidden (e.g. Flask missing on a *different* machine than tested here).
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_once()


if __name__ == "__main__":
    main()
