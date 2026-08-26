"""
Service status/start/stop, window-focusing, and health checks - the Python equivalent of
Get-ServiceStatus, Start-RequiredServices, Show-ProcessWindow, Stop-IdleComponents,
Test-GalleryDlOnPath, and Invoke-HealthCheck in the PS1.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import config, hydrus_client
from .api_client import get_daemon_api_info, get_status_info, shutdown as api_shutdown

try:
    import win32con
    import win32gui
    import win32process
    HAVE_WIN32 = True
except ImportError:
    HAVE_WIN32 = False


@dataclass
class ServiceStatus:
    hydrus_running: bool
    hydrus_pid: int | None
    daemon_running: bool
    daemon_pid: int | None
    systray_running: bool
    systray_pid: int | None


def find_process_by_name(name: str) -> psutil.Process | None:
    name_lower = name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == name_lower:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def find_hydownloader_daemon_proc() -> psutil.Process | None:
    """Equivalent of Get-RunningHydownloaderProc -Match "hydownloader-daemon" - the daemon
    runs as python.exe/pythonw.exe, so it has to be found by matching its command line, not
    its process name (which is indistinguishable from any other python.exe).

    Requesting "cmdline" as a process_iter() attr forces psutil to fetch it for *every* process
    on the system before the name filter below ever runs - on a busy Windows box that's a
    multi-second scan (each cmdline read opens a process handle) just to find one process.
    Filtering by name first with a cheap process_iter(["pid", "name"]) and only reading
    .cmdline() on the handful of python.exe/pythonw.exe candidates cuts that from "every
    process" to "every Python interpreter" - the actual expensive part measured at 2s+ before
    this fix, now effectively instant on a normal process count."""
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name not in ("python.exe", "pythonw.exe"):
                continue
            cmdline = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any("hydownloader-daemon" in part for part in cmdline):
            return proc
    return None


def find_hydrus_proc() -> psutil.Process | None:
    """Hydrus now runs from our fork's source (see config.HYDRUS_ENTRY_SCRIPT) via its own
    venv's pythonw.exe, not a standalone hydrus_client.exe - so like the hydownloader daemon,
    it has to be found by matching its command line rather than its process name."""
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name not in ("python.exe", "pythonw.exe"):
                continue
            cmdline = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any("hydrus_client.pyw" in part or "hydrus_client.py" in part for part in cmdline):
            return proc
    return None


def kill_orphaned_gallery_dl_processes() -> int:
    """Kills any gallery-dl.exe processes still running with no hydownloader daemon alive.
    gallery-dl is only ever spawned by the daemon as a per-subscription subprocess, so if the
    daemon isn't running, any live gallery-dl.exe is an orphan left over from a daemon that
    crashed or was force-killed mid-run. An orphan like that can keep holding a lock on its
    own temp output file under hydownloader-data/temp, which makes every subsequent daemon
    startup crash on that same file (its startup cleanup deletes leftover temp files
    unconditionally and isn't written to tolerate one still being open) - a loop that repeats
    forever since restarting the daemon does nothing to the orphan holding the lock. Only
    meaningful to call once the daemon is confirmed down."""
    killed = 0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() != "gallery-dl.exe":
                continue
            proc.kill()
            proc.wait(timeout=5)
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            continue
    return killed


# The TUI's main tick and the web layout's /partials/status widgets both poll this every
# 1.5-2s. TTL is long enough that repeated callers within the same tick share one scan, but the
# real cost isn't the caching - it's that a cache miss used to mean THREE separate full
# psutil.process_iter() passes over every process on the box (one each for hydrus_client.exe,
# the daemon, hydownloader-systray.exe), with the daemon pass additionally opening a handle to
# read .cmdline() on every python.exe/pythonw.exe found. Process enumeration on Windows is
# genuinely expensive (per-process OpenProcess + query syscalls), so doing that 3x on every
# poll was a real, sustained CPU cost for something that's just "is this still running" - a
# single merged pass below finds all three in one scan. TTL is also longer than the old 1.5s:
# service up/down state doesn't change fast enough to need sub-3s freshness.
_service_status_cache: tuple[float, "ServiceStatus"] | None = None
_SERVICE_STATUS_TTL = 3.0


def get_service_status() -> ServiceStatus:
    global _service_status_cache
    now = time.monotonic()
    if _service_status_cache is not None and now - _service_status_cache[0] < _SERVICE_STATUS_TTL:
        return _service_status_cache[1]

    hydrus = daemon = systray = None
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name == "hydownloader-systray.exe":
            systray = proc
        elif name in ("python.exe", "pythonw.exe"):
            try:
                cmdline = proc.cmdline()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if any("hydownloader-daemon" in part for part in cmdline):
                daemon = proc
            elif any("hydrus_client.pyw" in part or "hydrus_client.py" in part for part in cmdline):
                hydrus = proc

    status = ServiceStatus(
        hydrus_running=hydrus is not None,
        hydrus_pid=hydrus.pid if hydrus else None,
        daemon_running=daemon is not None,
        daemon_pid=daemon.pid if daemon else None,
        systray_running=systray is not None,
        systray_pid=systray.pid if systray else None,
    )
    _service_status_cache = (now, status)
    return status


def _start_hidden(args: list[str], cwd: Path, stdout_path: Path, stderr_path: Path) -> None:
    """Launches a console process with stdout/stderr redirected to files and no window at
    all. Previously used CREATE_NEW_CONSOLE + SW_SHOWMINNOACTIVE (the equivalent of
    Start-Process -WindowStyle Minimized) to get a minimized window - but since output is
    fully redirected to files, that window never had anything to show anyway, and "minimized"
    isn't reliably honored by every terminal host (Windows Terminal in particular), so it
    could show up as a plain empty console window instead of tucking away. CREATE_NO_WINDOW
    skips allocating a console in the first place, which is what's actually wanted here."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "w", encoding="utf-8") as out, open(stderr_path, "w", encoding="utf-8") as err:
        subprocess.Popen(
            args,
            cwd=str(cwd),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
        )


def _start_gui_minimized(args: list[str], cwd: Path | None = None) -> None:
    """Launches a GUI application requesting it start minimized, without stealing focus - the
    same STARTF_USESHOWWINDOW/SW_SHOWMINNOACTIVE mechanism Windows exposes for exactly this.
    Used for Hydrus and the systray so "make sure these are running" doesn't also mean "and
    take over my screen" - they should start out of the way, available via [5]/[6] (or the
    taskbar) whenever actually wanted. This is a request via the process's initial nCmdShow,
    not a guarantee - well-behaved GUI toolkits (Qt/wxWidgets, which both Hydrus and the
    systray use) honor it, but nothing stops a given build from ignoring it."""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE
    subprocess.Popen(args, cwd=str(cwd) if cwd else None, startupinfo=startupinfo)


def start_daemon() -> None:
    _start_hidden(
        ["python", "-m", "poetry", "run", "hydownloader-daemon", "start", "--path", str(config.DATA_DIR)],
        cwd=config.HYDOWNLOADER_REPO_DIR,
        stdout_path=config.DAEMON_LAUNCH_STDOUT_LOG,
        stderr_path=config.DAEMON_LAUNCH_STDERR_LOG,
    )


def start_systray() -> None:
    systray_exe = config.find_systray_exe()
    if systray_exe and systray_exe.exists():
        _start_gui_minimized([str(systray_exe)], cwd=systray_exe.parent)


@dataclass
class RestartResult:
    success: bool
    error: str | None = None


def restart_daemon(timeout: float = 60.0) -> RestartResult:
    """Force-restarts the hydownloader daemon even if it's already running. Needed after
    reassigning subscription worker_ids (subscriptions.assign_worker_ids_by_downloader) since
    hydownloader only spawns subscription-checker threads at startup - editing worker_id on a
    running daemon has no effect until it's cycled. Not used for the routine "start whatever's
    down" path (start_required_services) - this always restarts, even a healthy daemon.

    Shuts down via the daemon's own /shutdown API first when it's reachable - that lets it
    finish whatever it's mid-write on and close its SQLite connections cleanly, instead of
    being force-killed while (for example) a subscription worker thread is partway through
    writing a check result. A hard kill only happens if the API doesn't respond, since a truly
    hung process needs something to actually unstick it.

    Then blocks until the API is reachable and answering again (or `timeout` elapses) and
    reports which happened - an earlier version fired the restart and declared success
    unconditionally, which meant a failed respawn (slow first-time startup, a stuck port, an
    actual crash) looked identical to success in the UI. Silence isn't acceptable for an
    action that can leave the pipeline down."""
    daemon = find_hydownloader_daemon_proc()
    if daemon:
        shutdown_resp = api_shutdown()
        graceful = False
        if shutdown_resp.success:
            try:
                daemon.wait(timeout=20)
                graceful = True
            except psutil.TimeoutExpired:
                pass
        if not graceful:
            try:
                daemon.kill()
                daemon.wait(timeout=10)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                pass
            # A force-killed daemon doesn't get a chance to clean up its own children - any
            # gallery-dl subprocess it had running is now orphaned and may still hold a lock
            # on its temp output file, which would make the respawn below crash immediately.
            kill_orphaned_gallery_dl_processes()

    # Give Windows a moment to actually free the port even after the process object is gone -
    # hydownloader's own check_can_bind() retries for a while if it can't, but no reason to
    # lean on that when a short sleep here usually avoids needing it.
    time.sleep(2)
    start_daemon()

    deadline = time.monotonic() + timeout
    last_error = f"daemon API still unreachable after {timeout:.0f}s - it may have failed to start; check daemon-launch-stderr.log"
    while time.monotonic() < deadline:
        time.sleep(2)
        status_resp = get_status_info()
        if status_resp.success:
            return RestartResult(True)
        last_error = status_resp.error or last_error
    return RestartResult(False, last_error)


def get_active_worker_ids() -> set[str] | None:
    """The set of subscription worker_ids that actually have a live thread right now (i.e.
    were present in the DB the moment the daemon last started) - keyed off
    /get_status_info's per-worker status dict, since hydownloader doesn't expose this more
    directly. Returns None if the API isn't reachable (caller should treat that as "unknown",
    not "empty"). A worker_id that was just assigned to a subscription but isn't in this set
    yet means that subscription won't actually be checked until the daemon restarts."""
    status_resp = get_status_info()
    if not status_resp.success or not status_resp.data:
        return None
    separate = status_resp.data.get("subscription_worker_status_separate")
    if not isinstance(separate, dict):
        return None
    return set(separate.keys())


def ensure_veracrypt_drive_mounted(timeout: float = 45.0) -> bool:
    """Makes sure the VeraCrypt volume holding the Hydrus media library is mounted at
    config.HYDRUS_VOLUME_DRIVE (A:) before anything tries to use it. Doesn't know or need the
    volume's path or password - it just asks VeraCrypt to mount its own configured "System
    Favorite Volumes" (/a favorites), which is what /q background does silently; VeraCrypt
    itself pops its password prompt if the volume isn't already cached. Returns True once the
    drive is confirmed accessible (including if it already was), False if it's still missing
    after `timeout` seconds."""
    drive_root = Path(config.HYDRUS_VOLUME_DRIVE + "\\")
    if drive_root.exists():
        return True

    veracrypt_exe = config.find_veracrypt_exe()
    if not veracrypt_exe:
        print(f"  {config.HYDRUS_VOLUME_DRIVE} isn't mounted and VeraCrypt wasn't found - mount it manually.")
        return False

    print(f"  {config.HYDRUS_VOLUME_DRIVE} isn't mounted - asking VeraCrypt to mount its favorite volumes...")
    subprocess.Popen([str(veracrypt_exe), "/q", "background", "/a", "favorites"])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if drive_root.exists():
            print(f"  {config.HYDRUS_VOLUME_DRIVE} is mounted.")
            return True
        time.sleep(2)

    print(f"  {config.HYDRUS_VOLUME_DRIVE} still isn't mounted after {timeout:.0f}s - "
          "check VeraCrypt for a password prompt or mount it manually.")
    return False


def dismount_veracrypt_drive(timeout: float = 20.0) -> bool:
    """Dismounts config.HYDRUS_VOLUME_DRIVE (A:) via `VeraCrypt.exe /q /dismount /force`.
    Used by the "close the app window" flow below, not by the dashboard's regular Shutdown
    button - that one deliberately leaves Hydrus and the volume alone. Returns True once the
    drive is confirmed gone (including if it was never mounted), False if it's still there
    after `timeout` seconds."""
    drive_root = Path(config.HYDRUS_VOLUME_DRIVE + "\\")
    if not drive_root.exists():
        return True

    veracrypt_exe = config.find_veracrypt_exe()
    if not veracrypt_exe:
        print(f"  VeraCrypt wasn't found - can't dismount {config.HYDRUS_VOLUME_DRIVE} automatically.")
        return False

    print(f"  Dismounting {config.HYDRUS_VOLUME_DRIVE}...")
    subprocess.Popen([str(veracrypt_exe), "/q", "/dismount", config.HYDRUS_VOLUME_DRIVE, "/force"])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not drive_root.exists():
            print(f"  {config.HYDRUS_VOLUME_DRIVE} dismounted.")
            return True
        time.sleep(1)

    print(f"  {config.HYDRUS_VOLUME_DRIVE} still mounted after {timeout:.0f}s - dismount it manually.")
    return False


def stop_everything() -> None:
    """Unlike stop_idle_components (which leaves a busy daemon and Hydrus itself running -
    Hydrus is never auto-closed there since that's meant to be a deliberate, separate choice),
    this stops the daemon unconditionally, closes the systray, force-closes Hydrus itself, and
    dismounts the VeraCrypt volume. Used only when the WebView2 app frame's window is closed -
    that's a stronger "I'm done for the day" signal than the dashboard's own Shutdown button."""
    print()
    print("Closing everything down...")

    daemon = find_hydownloader_daemon_proc()
    if daemon:
        print("  stopping hydownloader daemon (including any in-progress downloads)...")
        api_shutdown()  # Best-effort graceful request; force-killed below regardless of result.
        try:
            children = daemon.children(recursive=True)  # gallery-dl etc. worker subprocesses
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            children = []
        try:
            daemon.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            for proc in [daemon, *children]:
                try:
                    proc.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, alive = psutil.wait_procs([daemon, *children], timeout=5)
            for proc in alive:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    systray = find_process_by_name("hydownloader-systray.exe")
    if systray:
        print("  closing hydownloader systray...")
        try:
            systray.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    hydrus = find_hydrus_proc()
    if hydrus:
        print("  closing Hydrus...")
        try:
            hydrus.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        try:
            hydrus.wait(timeout=15)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

    dismount_veracrypt_drive()


def start_required_services() -> None:
    ensure_veracrypt_drive_mounted()

    status = get_service_status()

    if not status.hydrus_running:
        if config.hydrus_is_installed():
            print("  starting Hydrus (minimized)...")
            _start_gui_minimized(
                [str(config.HYDRUS_VENV_PYTHONW), str(config.HYDRUS_ENTRY_SCRIPT)],
                cwd=config.HYDRUS_DIR,
            )
            time.sleep(12)
        else:
            print(f"  Hydrus not found at {config.HYDRUS_DIR} - has setup been run?")
    else:
        print("  Hydrus already running.")

    if not status.daemon_running:
        if config.HYDOWNLOADER_CONFIG_FILE.exists():
            killed = kill_orphaned_gallery_dl_processes()
            if killed:
                print(f"  cleared {killed} orphaned gallery-dl process(es) from a previous crash...")
            print("  starting hydownloader daemon...")
            start_daemon()
            time.sleep(5)
        else:
            print("  hydownloader not set up yet - run Setup-HydrusPipeline.ps1 first.")
    else:
        print("  hydownloader daemon already running.")

    if not status.systray_running:
        systray_exe = config.find_systray_exe()
        if systray_exe and systray_exe.exists() and config.HYDOWNLOADER_CONFIG_FILE.exists():
            print("  starting hydownloader systray (minimized)...")
            start_systray()
            time.sleep(3)
        else:
            print("  hydownloader-systray not found - run Setup-HydrusPipeline.ps1 first.")
    else:
        print("  hydownloader systray already running.")


def restart_hydrus_service(timeout: float = 15.0) -> str | None:
    """Force-restarts Hydrus itself (terminate + relaunch minimized) - used by the dashboard's
    clickable service status pill. Returns None on success, or an error string."""
    hydrus = find_hydrus_proc()
    if hydrus:
        try:
            hydrus.terminate()
            hydrus.wait(timeout=timeout)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
    if not config.hydrus_is_installed():
        return f"Hydrus not found at {config.HYDRUS_DIR}"
    _start_gui_minimized(
        [str(config.HYDRUS_VENV_PYTHONW), str(config.HYDRUS_ENTRY_SCRIPT)],
        cwd=config.HYDRUS_DIR,
    )
    return None


def restart_systray_service(timeout: float = 10.0) -> str | None:
    """Force-restarts the hydownloader systray (terminate + relaunch minimized) - used by the
    dashboard's clickable service status pill. Returns None on success, or an error string."""
    systray = find_process_by_name("hydownloader-systray.exe")
    if systray:
        try:
            systray.terminate()
            systray.wait(timeout=timeout)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass
    systray_exe = config.find_systray_exe()
    if not systray_exe or not systray_exe.exists():
        return "hydownloader-systray not found"
    start_systray()
    return None


def show_process_window(process_name: str) -> bool:
    """Brings a running process's main window to the front - the equivalent of
    Show-ProcessWindow (the PS1's P/Invoke SetForegroundWindow/ShowWindow/IsIconic calls)."""
    if not HAVE_WIN32:
        return False
    if process_name in ("hydrus_client", "hydrus_client.exe"):
        proc = find_hydrus_proc()
    elif process_name.endswith(".exe"):
        proc = find_process_by_name(process_name)
    else:
        proc = find_process_by_name(f"{process_name}.exe")
    if not proc:
        return False
    target_pid = proc.pid

    found_hwnd = []

    def enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == target_pid:
            found_hwnd.append(hwnd)
            return False
        return True

    try:
        win32gui.EnumWindows(enum_handler, None)
    except Exception:
        pass

    if not found_hwnd:
        return False
    hwnd = found_hwnd[0]
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return True


def hide_console_window() -> None:
    """Hides this process's own console window - called once the web dashboard is up and
    running as the primary interface, so the process keeps running in the background exactly
    as before (services, watchdog, Flask server all unaffected) without a console window
    sitting on the taskbar. Deliberately only hides rather than detaching/killing the console
    entirely: startup errors that happen *before* this is called (service checks, Flask
    import failures, ...) still show up in a real, visible console window, which is why this
    is called late in menu.main() rather than switched to pythonw.exe from the start. Best-
    effort - a missing console handle or pywin32 not being available just leaves the window
    showing instead of crashing startup over a cosmetic step."""
    if not HAVE_WIN32:
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    except Exception:
        pass


def test_daemon_busy() -> bool | None:
    """Returns True (busy), False (idle), or None (couldn't tell - API unreachable)."""
    resp = get_status_info()
    if not resp.success:
        return None
    s = resp.data or {}
    return bool(s.get("urls_queued", 0) > 0 or s.get("subscriptions_due", 0) > 0 or s.get("autoimport_jobs_due", 0) > 0)


def stop_idle_components() -> None:
    print()
    print("Checking what's currently active before shutting anything down...")

    daemon = find_hydownloader_daemon_proc()
    if daemon:
        busy = test_daemon_busy()
        if busy is True:
            print("  hydownloader daemon is busy (downloads/subscriptions in progress) - leaving it running.")
        elif busy is False:
            print("  hydownloader daemon is idle - sending a graceful shutdown...")
            result = api_shutdown()
            if result.success:
                print("  shutdown requested (it'll finish anything genuinely in-flight, then exit).")
            else:
                print("  couldn't reach the daemon's API to shut it down cleanly - leaving it running.")
        else:
            print("  couldn't determine daemon status (API unreachable) - leaving it running, just in case.")
    else:
        print("  hydownloader daemon isn't running - nothing to do there.")

    systray = find_process_by_name("hydownloader-systray.exe")
    if systray:
        print("  closing hydownloader systray...")
        try:
            systray.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    print()
    print("Hydrus itself is never auto-closed - close it yourself whenever you're done with it.")


@dataclass
class GalleryDlPathInfo:
    on_path: bool
    resolved_path: str | None
    user_install_path: str | None
    hint: str | None


def test_gallery_dl_on_path() -> GalleryDlPathInfo:
    """Checks whether gallery-dl actually resolves on PATH, and if not, tries to explain why -
    specifically the "two different Python installs" trap where `pip install --user` lands
    gallery-dl.exe somewhere never added to PATH. Same diagnostic as the PS1 version."""
    found = shutil.which("gallery-dl")
    if found:
        return GalleryDlPathInfo(True, found, None, None)

    hint = None
    user_install_path = None
    try:
        result = subprocess.run(["python", "-m", "pip", "show", "gallery-dl"], capture_output=True, text=True, timeout=15)
        for line in result.stdout.splitlines():
            if line.startswith("Location:"):
                site_packages = line.split(":", 1)[1].strip()
                python_root = Path(site_packages).parent
                candidate_scripts = python_root / "Scripts"
                candidate_exe = candidate_scripts / "gallery-dl.exe"
                if candidate_exe.exists():
                    user_install_path = str(candidate_scripts)
                    hint = f"gallery-dl.exe exists at {candidate_exe} but that folder isn't on PATH."
                break
    except (subprocess.SubprocessError, OSError):
        pass

    return GalleryDlPathInfo(False, None, user_install_path, hint)


@dataclass
class HealthReport:
    status: ServiceStatus
    api_reachable: bool
    api_reason: str | None
    api_base_url: str | None
    hydrus_api_reachable: bool
    hydrus_api_reason: str | None
    gallery_dl: GalleryDlPathInfo
    multiple_python_paths: list[str]
    watchdog_last_check: str | None
    watchdog_actions: list[str]


def get_health_report() -> HealthReport:
    """Gathers everything the old console health check (option 7) printed, as data instead of
    print()/input() calls, so any UI (the TUI's health screen, tests, whatever comes next) can
    render it however it wants instead of being locked into a linear text dump."""
    status = get_service_status()

    # Process-running is not the same as API-reachable - the daemon can be up and still be
    # unreachable if hydownloader-config.json doesn't have working access-key settings. This
    # is what actually distinguishes "everything shows up/up/up" from "dashboard says
    # unreachable", so this gets checked and reported independently of process status.
    api, reason = get_daemon_api_info()
    api_reachable = False
    if api:
        status_resp = get_status_info()
        api_reachable = status_resp.success
        if not status_resp.success:
            reason = status_resp.error

    # Same "process running" vs "API actually answers" distinction as the daemon check above -
    # a Hydrus Client API key can exist locally (see api_keys.get_hydrus_key_status) without
    # Hydrus itself ever having accepted it (Client API turned off, key revoked/regenerated
    # since, wrong port). verify_access_key() is the one call that actually proves both "Hydrus
    # is up" and "this key still works" at once.
    hydrus_verify = hydrus_client.verify_access_key()
    hydrus_api_reachable = hydrus_verify.success
    hydrus_api_reason = None if hydrus_verify.success else hydrus_verify.error

    gdl = test_gallery_dl_on_path()

    python_on_path = shutil.which("python")
    multiple_python_paths: list[str] = []
    if python_on_path:
        path_dirs_with_python = {p for p in os.environ.get("PATH", "").split(os.pathsep) if re.search(r"Python\d", p)}
        if len(path_dirs_with_python) > 1:
            multiple_python_paths = sorted(path_dirs_with_python)

    watchdog_last_check = None
    watchdog_actions: list[str] = []
    if config.WATCHDOG_STATUS_FILE.exists():
        try:
            import json
            wd = json.loads(config.WATCHDOG_STATUS_FILE.read_text(encoding="utf-8"))
            watchdog_last_check = wd.get("LastCheckLocal")
            watchdog_actions = wd.get("Actions") or []
        except (OSError, ValueError):
            pass

    return HealthReport(
        status=status,
        api_reachable=api_reachable,
        api_reason=reason,
        api_base_url=api.base_url if api else None,
        hydrus_api_reachable=hydrus_api_reachable,
        hydrus_api_reason=hydrus_api_reason,
        gallery_dl=gdl,
        multiple_python_paths=multiple_python_paths,
        watchdog_last_check=watchdog_last_check,
        watchdog_actions=watchdog_actions,
    )


def get_hydrus_storage_disk_usage() -> dict | None:
    """Real free/used/total space across every drive Hydrus actually stores files on, not just
    the drive this app happens to be installed on. Hydrus supports splitting client_files
    across multiple locations (see /get_files/local_file_storage_locations), each of which can
    be its own physical drive - psutil.disk_usage() on a single fixed path (the old behavior,
    checking config.INSTALL_ROOT's drive) told you nothing about where the actual media
    library lives once that's the case. Dedupes by drive anchor (e.g. "A:\\") before summing so
    two Hydrus storage locations on the same physical drive aren't double-counted. Returns None
    if Hydrus's API isn't reachable (no key configured, Hydrus not running, ...) - callers
    should fall back to not showing a disk widget rather than a wrong one."""
    global _disk_usage_cache
    now = time.monotonic()
    if _disk_usage_cache is not None and now - _disk_usage_cache[0] < _DISK_USAGE_TTL:
        return _disk_usage_cache[1]
    result = hydrus_client.invoke_hydrus_api("/get_files/local_file_storage_locations")
    if not result.success or not result.data:
        _disk_usage_cache = (now, None)
        return None
    locations = result.data.get("locations") or []
    drives = {Path(loc["path"]).anchor for loc in locations if loc.get("path")}
    if not drives:
        _disk_usage_cache = (now, None)
        return None
    used = total = 0
    for drive in drives:
        try:
            du = psutil.disk_usage(drive)
        except OSError:
            continue  # drive not currently reachable (unplugged external, disconnected share, ...)
        used += du.used
        total += du.total
    if total == 0:
        _disk_usage_cache = (now, None)
        return None
    stats = {"disk_pct": used / total * 100, "disk_used_gb": used / 1e9, "disk_total_gb": total / 1e9}
    _disk_usage_cache = (now, stats)
    return stats


# Disk usage costs a live Hydrus Client API round-trip (see docstring above) - the web
# dashboard's hoststats widget polls this every 3s but disk usage doesn't change fast enough
# to need sub-second freshness, so a longer TTL than the other caches here is fine.
_disk_usage_cache: tuple[float, dict | None] | None = None
_DISK_USAGE_TTL = 10.0


def get_host_stats() -> dict:
    """RAM + real Hydrus-storage disk usage for the web dashboard's HOST widget. Deliberately
    does not include CPU% - on this machine CPU sits elevated most of the time regardless of
    what the pipeline is doing, so it was never a useful signal here and just added visual
    noise/false alarms to the resource-alert thresholds below."""
    vm = psutil.virtual_memory()
    stats = {
        "mem_pct": vm.percent, "mem_used_gb": vm.used / 1e9, "mem_total_gb": vm.total / 1e9,
        "disk_pct": None, "disk_used_gb": None, "disk_total_gb": None,
    }
    disk = get_hydrus_storage_disk_usage()
    if disk is not None:
        stats.update(disk)
    return stats


# get_host_stats() calls the RAM metric "mem_pct" (matching psutil's own virtual_memory().percent
# naming), but settings.py/the Settings page call the user-facing threshold "ram_pct" (reads
# better in a form label) - this is the one place that mapping has to be spelled out.
_THRESHOLD_TO_HOST_KEY = {"disk_pct": "disk_pct", "ram_pct": "mem_pct"}


def check_resource_thresholds(host_stats: dict, thresholds: dict) -> dict[str, str]:
    """Returns {metric_name: message} for every metric in `thresholds` currently at or above
    its configured percentage - pure logic over the already-existing get_host_stats() output,
    no new data-gathering. `metric_name` matches settings.json's resource_alert_thresholds keys
    (disk_pct/ram_pct), which is what watchdog.py's own per-metric dedup set keys on.
    A metric missing from `thresholds` or `host_stats` (e.g. GPU-only stats, which this doesn't
    cover) is silently skipped rather than treated as a breach."""
    breaches: dict[str, str] = {}
    for metric, host_key in _THRESHOLD_TO_HOST_KEY.items():
        threshold = thresholds.get(metric)
        value = host_stats.get(host_key)
        if threshold is None or value is None:
            continue
        if value >= threshold:
            label = metric.replace("_pct", "").upper()
            breaches[metric] = f"{label} usage at {value:.1f}% (threshold {threshold:.0f}%)"
    return breaches


_GPU_UNAVAILABLE_LOGGED = False


def get_gpu_stats() -> dict | None:
    """GPU utilization/memory/temperature via `nvidia-smi`, for the web dashboard's GPU
    widget. Returns None (not an error) when nvidia-smi isn't on PATH or the machine has no
    NVIDIA GPU - there's no cross-vendor equivalent psutil can fall back to (AMD/Intel expose
    this through their own separate tooling), so "not detected" is the honest answer rather
    than faking a number. Unlike CPU temperature (sensors_temperatures() is a no-op on
    Windows), nvidia-smi reliably reports GPU temp on Windows too, straight from the driver."""
    global _gpu_stats_cache
    now = time.monotonic()
    if _gpu_stats_cache is not None and now - _gpu_stats_cache[0] < _GPU_STATS_TTL:
        return _gpu_stats_cache[1]
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _gpu_stats_cache = (now, None)
            return None
        util, mem_used, mem_total, temp = (p.strip() for p in result.stdout.strip().splitlines()[0].split(","))
        stats = {
            "util_pct": float(util), "mem_used_mb": float(mem_used), "mem_total_mb": float(mem_total),
            "temp_c": float(temp),
        }
        _gpu_stats_cache = (now, stats)
        return stats
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        _gpu_stats_cache = (now, None)
        return None


# Spawning nvidia-smi is a real subprocess launch (50-150ms+ on Windows) - the hoststats widget
# polls this every few seconds, so too short a TTL means a fresh process spawn on nearly every
# request purely to refresh a gauge that doesn't need sub-5s freshness.
_gpu_stats_cache: tuple[float, dict | None] | None = None
_GPU_STATS_TTL = 20.0


# Persistent across polls (module-level, not re-created per call) so cpu_percent() has a
# prior sample to diff against - psutil.Process.cpu_percent(None) always returns 0.0 on a
# brand-new Process object, so a fresh psutil.process_iter() every poll would show every
# process pinned at 0%. Keeping the same Process objects around lets each one build up a
# real delta between polls.
_proc_cache: dict[int, psutil.Process] = {}
_cpu_count = psutil.cpu_count() or 1


def get_top_processes(limit: int = 8) -> list[dict]:
    """Top processes by CPU%, for the web dashboard's TOP_PROCS widget - real psutil data,
    not a fixed watchlist. A process's first appearance in the cache reads 0% CPU (nothing to
    diff against yet) and becomes meaningful on the poll after.

    psutil.Process.cpu_percent() reports usage relative to a SINGLE core - a process fully
    using 4 cores on an 8-core machine reads 400%, not 50%. That's correct but reads as
    nonsense on a dashboard where every other gauge is a plain 0-100% bar, so this divides by
    the core count to put it on the same normalized scale as everything else (matches what
    Windows' own Task Manager shows, unlike the raw psutil number)."""
    seen_pids = set()
    for proc in psutil.process_iter(["pid"]):
        try:
            pid = proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        seen_pids.add(pid)
        if pid not in _proc_cache:
            try:
                p = psutil.Process(pid)
                p.cpu_percent(None)  # prime the internal last-sample state
                _proc_cache[pid] = p
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    for pid in list(_proc_cache):
        if pid not in seen_pids:
            del _proc_cache[pid]

    rows = []
    for pid, p in list(_proc_cache.items()):
        try:
            rows.append({
                "pid": pid,
                "name": p.name(),
                "cpu": round(p.cpu_percent(None) / _cpu_count, 1),
                "mem_mb": p.memory_info().rss / (1024 * 1024),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: r["cpu"], reverse=True)
    return rows[:limit]


def get_network_connections(limit: int = 60) -> list[dict]:
    """Active inet connections grouped with their owning process name, for the web
    dashboard's scrollable NET_CONNECTIONS widget - real psutil data (psutil has no
    per-process bandwidth/byte-counter API on any platform, only whole-system counters via
    psutil.net_io_counters(), so "network stats per process" means the connection table
    itself: who's talking to what, not throughput). Connections whose owning process can't be
    read (permissions, or the process exited between the connection listing and the lookup)
    are skipped rather than shown with a blank name."""
    rows = []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return []
    for c in conns:
        if not c.pid or not c.raddr:
            continue  # skip listening/local-only sockets - "connections" implies a remote peer
        try:
            # Reuse get_top_processes' Process cache when it already has this pid, instead of
            # constructing (and opening a handle for) a brand-new Process object per connection
            # per poll - cheap when it hits, no behavior change when it doesn't.
            proc = _proc_cache.get(c.pid) or psutil.Process(c.pid)
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        rows.append({
            "pid": c.pid, "name": name,
            "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "-",
            "raddr": f"{c.raddr.ip}:{c.raddr.port}",
            "status": c.status,
        })
    rows.sort(key=lambda r: r["name"])
    return rows[:limit]
