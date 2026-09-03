"""
Native Windows toast notifications for watchdog-detected events (a subscription stuck failing,
a service that had to be restarted, a resource threshold crossed) - previously only visible if
someone happened to have the dashboard or console open at the right moment. Uses a classic
Shell_NotifyIcon balloon tip via pywin32 (already a required dependency - no new pip package
needed), not a modern Action Center toast; see send_windows_toast's docstring for why.
"""

from __future__ import annotations

import threading
import time

from . import settings

try:
    import win32api
    import win32con
    import win32gui
    HAVE_WIN32 = True
except ImportError:
    HAVE_WIN32 = False

_WNDCLASS_NAME = "UndertowAlertWnd"
_WM_TRAYICON = win32con.WM_USER + 20 if HAVE_WIN32 else 0
_class_registered = False
_class_lock = threading.Lock()


def _on_destroy(hwnd, msg, wparam, lparam) -> int:
    # Must return an int LRESULT, not PostQuitMessage's own return value (None) - pywin32
    # can't marshal None as an LRESULT and raises a TypeError from inside the WNDPROC dispatch
    # every time DestroyWindow() synchronously delivers WM_DESTROY here (i.e. every single
    # toast, since send_windows_toast() destroys its throwaway window on every call). That
    # crash was silent from the caller's perspective (it happens inside the win32 callback,
    # not in send_windows_toast's own try/except) but broke every Windows toast notification.
    win32gui.PostQuitMessage(0)
    return 0


def _ensure_window_class() -> None:
    global _class_registered
    with _class_lock:
        if _class_registered:
            return
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = _WNDCLASS_NAME
        wc.lpfnWndProc = {win32con.WM_DESTROY: _on_destroy}
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass  # already registered by an earlier call this session - fine, reuse it
        _class_registered = True


def send_windows_toast(title: str, message: str) -> bool:
    """Shows one Windows balloon-tip notification via Shell_NotifyIcon. This is the classic
    tray-icon balloon tip, not a modern Action Center toast - a nicer-looking modern toast
    needs a package like winotify/win11toast, which isn't a dependency this project already
    has; Shell_NotifyIcon works with pywin32 alone (already required) at the cost of the older
    visual style. That tradeoff, plus this being the one piece of the alerting feature that
    can't be exercised outside a real Windows desktop session, makes this the highest-risk part
    of the whole feature - if balloon tips don't actually appear on a given Windows build,
    swapping in winotify/win11toast here is the natural follow-up without touching any caller.

    Returns False (never raises) on any failure - pywin32 not installed, no desktop session to
    notify (e.g. running as a service), a transient shell API error - since a failed
    notification must never be allowed to take down the watchdog thread that's calling this.

    Runs synchronously (creating a throwaway hidden window, registering the icon, pumping
    messages briefly so the shell actually renders the balloon, then tearing the icon back
    down) - callers on a background thread already (see notify() below) should call this from
    there, not from anything latency-sensitive, since the whole round trip takes on the order
    of a second."""
    if not HAVE_WIN32:
        return False
    try:
        _ensure_window_class()
        hinst = win32api.GetModuleHandle(None)
        hwnd = win32gui.CreateWindow(_WNDCLASS_NAME, "Undertow Alert", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        try:
            hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP | win32gui.NIF_INFO
            nid = (hwnd, 0, flags, _WM_TRAYICON, hicon, "Undertow", message[:255], 10000, title[:63])
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
            # Give the shell a moment to actually render the balloon before the icon (and the
            # balloon riding on it) gets torn back down again.
            for _ in range(20):
                win32gui.PumpWaitingMessages()
                time.sleep(0.05)
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
        finally:
            win32gui.DestroyWindow(hwnd)
        return True
    except Exception:
        return False


def notify(events: list[str], title: str = "Undertow") -> None:
    """Fire-and-forget: dispatches `events` (already-formatted one-line strings - the same
    shape as watchdog.py's own `actions` list) as a single batched toast rather than one
    notification per event, so a cycle where several things break at once doesn't spam a burst
    of separate balloons. Runs the actual Shell_NotifyIcon work on its own throwaway daemon
    thread so the caller (the watchdog loop) isn't blocked for the ~1s a toast round trip
    takes. Respects settings.json's windows_toast_enabled - a no-op (not even a thread spawned)
    when toasts are turned off. Never raises."""
    if not events:
        return
    try:
        if not settings.load_settings().get("windows_toast_enabled", True):
            return
    except Exception:
        pass  # settings unreadable - fail open rather than silently losing an alert

    message = "\n".join(events[:5])
    if len(events) > 5:
        message += f"\n(+{len(events) - 5} more)"

    def _run() -> None:
        try:
            send_windows_toast(title, message)
        except Exception:
            pass  # a failed notification must never take down the watchdog thread

    threading.Thread(target=_run, daemon=True, name="hydrus-pipeline-toast").start()
