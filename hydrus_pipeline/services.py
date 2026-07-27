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
    its process name (which is indistinguishable from any other python.exe)."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name not in ("python.exe", "pythonw.exe"):
                continue
            cmdline = proc.info["cmdline"] or []
            if any("hydownloader-daemon" in part for part in cmdline):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
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


def get_service_status() -> ServiceStatus:
    hydrus = find_process_by_name("hydrus_client.exe")
    daemon = find_hydownloader_daemon_proc()
    systray = find_process_by_name("hydownloader-systray.exe")
    return ServiceStatus(
        hydrus_running=hydrus is not None,
        hydrus_pid=hydrus.pid if hydrus else None,
        daemon_running=daemon is not None,
        daemon_pid=daemon.pid if daemon else None,
        systray_running=systray is not None,
        systray_pid=systray.pid if systray else None,
    )


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


def start_required_services() -> None:
    status = get_service_status()

    if not status.hydrus_running:
        if config.HYDRUS_EXE.exists():
            print("  starting Hydrus (minimized)...")
            _start_gui_minimized([str(config.HYDRUS_EXE)])
            time.sleep(12)
        else:
            print(f"  Hydrus not found at {config.HYDRUS_EXE} - has setup been run?")
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


def show_process_window(process_name: str) -> bool:
    """Brings a running process's main window to the front - the equivalent of
    Show-ProcessWindow (the PS1's P/Invoke SetForegroundWindow/ShowWindow/IsIconic calls)."""
    if not HAVE_WIN32:
        return False
    proc = find_process_by_name(f"{process_name}.exe") if not process_name.endswith(".exe") else find_process_by_name(process_name)
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


def get_host_stats() -> dict:
    """CPU/RAM/disk for the machine this pipeline runs on - real numbers straight from
    psutil, for the web dashboard's HOST widget.

    cpu_percent(interval=None) (the "compare against whenever this was last called anywhere"
    mode) is what the previous version of this function used, and it's exactly why CPU% sat
    pinned at 0% or 100%: several different endpoints (hoststats, top-procs priming, and any
    other code that happens to call a psutil cpu function) all share that one global last-call
    timestamp, so with multiple browser tabs/widgets polling concurrently the gap between calls
    could shrink to a few milliseconds - and a percentage computed over a few milliseconds is
    essentially just "was a core busy in this instant", i.e. 0 or 100, not a meaningful average.
    Passing a real interval makes this call take its own independent before/after measurement
    over that window regardless of what anything else is doing, which is what actually fixes it
    - at the cost of blocking this request for that long, which is fine at a several-second
    poll cadence."""
    cpu_pct = psutil.cpu_percent(interval=0.2)
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(config.INSTALL_ROOT.anchor)
    return {
        "cpu_pct": cpu_pct,
        "mem_pct": vm.percent, "mem_used_gb": vm.used / 1e9, "mem_total_gb": vm.total / 1e9,
        "disk_pct": disk.percent, "disk_used_gb": disk.used / 1e9, "disk_total_gb": disk.total / 1e9,
    }


_GPU_UNAVAILABLE_LOGGED = False


def get_gpu_stats() -> dict | None:
    """GPU utilization/memory/temperature via `nvidia-smi`, for the web dashboard's GPU
    widget. Returns None (not an error) when nvidia-smi isn't on PATH or the machine has no
    NVIDIA GPU - there's no cross-vendor equivalent psutil can fall back to (AMD/Intel expose
    this through their own separate tooling), so "not detected" is the honest answer rather
    than faking a number. Unlike CPU temperature (sensors_temperatures() is a no-op on
    Windows), nvidia-smi reliably reports GPU temp on Windows too, straight from the driver."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        util, mem_used, mem_total, temp = (p.strip() for p in result.stdout.strip().splitlines()[0].split(","))
        return {
            "util_pct": float(util), "mem_used_mb": float(mem_used), "mem_total_mb": float(mem_total),
            "temp_c": float(temp),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


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
            name = psutil.Process(c.pid).name()
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
