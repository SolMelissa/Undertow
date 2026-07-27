"""
Background watchdog thread - restarts the hydownloader daemon or systray if either crashes.
Hydrus itself is NOT auto-restarted (closing it is usually deliberate). Equivalent of the
Register-ObjectEvent timer block at the bottom of the PS1.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from . import api_client, config, services, subscriptions


class Watchdog:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Ids already alerted-on as of the previous cycle, so a subscription stuck failing
        # doesn't spam a fresh "actions" entry every single 90s cycle - only a *newly* flagged
        # id gets logged. Reset (not persisted across app restarts) since the visible watchdog
        # log is itself only ever shown for the current session anyway.
        self._alerted_ids: set[int] = set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hydrus-pipeline-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        # .wait() with a timeout doubles as the sleep - it returns True immediately if
        # stop() is called, so shutdown doesn't have to wait out a full 90s interval.
        while not self._stop_event.wait(config.WATCHDOG_INTERVAL_SECONDS):
            try:
                self._check_once()
            except Exception:
                # A watchdog that dies on an unexpected exception is worse than one that
                # skips a cycle - never let this thread go silent.
                pass

    def _check_once(self) -> None:
        actions: list[str] = []
        status = services.get_service_status()

        if not status.daemon_running and config.HYDOWNLOADER_CONFIG_FILE.exists():
            killed = services.kill_orphaned_gallery_dl_processes()
            if killed:
                actions.append(f"cleared {killed} orphaned gallery-dl process(es) holding temp-file locks")
            services.start_daemon()
            actions.append("restarted hydownloader daemon (was down)")

        if not status.systray_running:
            systray_exe = config.find_systray_exe()
            if systray_exe and systray_exe.exists():
                services.start_systray()
                actions.append("restarted hydownloader systray (was down)")

        # Keeps "least recently successfully updated checked first" enforced on an ongoing
        # basis - see subscriptions.sync_priority_by_last_success for why this can't just be
        # set once. Silently skipped (not appended to `actions`) when the daemon is down or
        # nothing needs updating - this runs every cycle, and cluttering the visible watchdog
        # log with a routine no-op each time would bury the actually-interesting entries.
        if status.daemon_running:
            try:
                subscriptions.sync_priority_by_last_success()
            except Exception:
                pass

            # Proactive failure alerting: a subscription stuck failing was previously only
            # visible if someone happened to open its History modal - this surfaces it in the
            # watchdog log (and WATCHDOG_STATUS_FILE, which Diagnostics reads) the cycle it
            # first crosses the threshold, without repeating that same entry every cycle after.
            try:
                subs_resp = api_client.get_subscriptions()
                if subs_resp.success:
                    failure_status = subscriptions.get_failure_status(subs_resp.data or [])
                    flagged_now = {sid for sid, v in failure_status.items() if v["flagged"]}
                    for sid in sorted(flagged_now - self._alerted_ids):
                        v = failure_status[sid]
                        actions.append(
                            f"subscription #{sid} flagged: {v['consecutive_failures']} consecutive "
                            f"failure(s), last success "
                            + (f"{v['last_success_days_ago']:.1f} day(s) ago" if v["last_success_days_ago"] is not None else "never")
                        )
                    self._alerted_ids = flagged_now
            except Exception:
                pass

        status_obj = {
            "LastCheckLocal": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "HydrusRunning": services.get_service_status().hydrus_running,
            "Actions": actions,
        }
        try:
            config.WATCHDOG_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.WATCHDOG_STATUS_FILE.write_text(json.dumps(status_obj), encoding="utf-8")
        except OSError:
            pass

        if actions:
            ts = datetime.now().strftime("%H:%M:%S")
            print()
            for a in actions:
                print(f"[watchdog {ts}] {a}")
