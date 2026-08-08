"""
Background watchdog thread - restarts the hydownloader daemon or systray if either crashes.
Hydrus itself is NOT auto-restarted (closing it is usually deliberate). Equivalent of the
Register-ObjectEvent timer block at the bottom of the PS1.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from . import alerts, api_client, config, services, settings, subscriptions


class Watchdog:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Ids already alerted-on as of the previous cycle, so a subscription stuck failing
        # doesn't spam a fresh "actions" entry every single 90s cycle - only a *newly* flagged
        # id gets logged. Reset (not persisted across app restarts) since the visible watchdog
        # log is itself only ever shown for the current session anyway.
        self._alerted_ids: set[int] = set()
        # Separate dedup set, keyed by metric name (not subscription id) - a resource breach
        # clears out of here once usage drops back under threshold, so it can re-alert on a
        # later re-breach rather than staying silenced forever after the first time.
        self._alerted_resource_metrics: set[str] = set()
        # Counts history writes so _maybe_truncate_history only stat()s the file every 50th
        # write instead of every single cycle - a rolling read-modify-write on every 90s tick
        # forever is wasted work for a file that only grows a line or two at a time.
        self._history_write_count = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hydrus-pipeline-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        # .wait() with a timeout doubles as the sleep - it returns True immediately if
        # stop() is called, so shutdown doesn't have to wait out a full 90s interval.
        while not self._stop_event.wait(config.get_watchdog_interval_seconds()):
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

        # Resource thresholds - independent of daemon/Hydrus status, so this runs every cycle
        # regardless of what the blocks above found.
        try:
            host_stats = services.get_host_stats()
            thresholds = settings.load_settings().get("resource_alert_thresholds", {})
            breaches = services.check_resource_thresholds(host_stats, thresholds)
            breached_now = set(breaches.keys())
            for metric in sorted(breached_now - self._alerted_resource_metrics):
                actions.append(breaches[metric])
            self._alerted_resource_metrics = breached_now
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
            # One batched toast per cycle (not one per action) - see alerts.notify's own
            # docstring for why a cycle where several things break at once shouldn't spam a
            # burst of separate balloons.
            alerts.notify(actions)
            self._append_history(actions)

    def _append_history(self, actions: list[str]) -> None:
        """Appends one JSON-Lines entry to WATCHDOG_HISTORY_FILE - unlike WATCHDOG_STATUS_FILE
        (overwritten every cycle, so it only ever shows the most recent run), this accumulates
        across cycles so Diagnostics can show a "recent incidents" list, not just a single
        snapshot."""
        entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "actions": actions}
        try:
            config.WATCHDOG_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(config.WATCHDOG_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            return
        self._history_write_count += 1
        if self._history_write_count % 50 == 0:
            self._maybe_truncate_history()

    @staticmethod
    def _maybe_truncate_history(max_bytes: int = 256 * 1024, keep_lines: int = 500) -> None:
        try:
            if config.WATCHDOG_HISTORY_FILE.stat().st_size <= max_bytes:
                return
            lines = config.WATCHDOG_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
            trimmed = lines[-keep_lines:]
            with open(config.WATCHDOG_HISTORY_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(trimmed) + ("\n" if trimmed else ""))
        except OSError:
            pass


def get_incident_history(limit: int = 20) -> list[dict]:
    """Returns up to `limit` most recent watchdog incident entries, newest first - each one the
    same {"ts", "actions"} shape Watchdog._append_history writes. Supplementary display data,
    same "degrade rather than raise" contract as subscriptions.py's get_latest_checks etc. -
    returns [] on any read/parse failure (missing file, a corrupt line) instead of erroring
    Diagnostics out over what's ultimately just a nice-to-have incident log."""
    try:
        lines = config.WATCHDOG_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    entries.reverse()
    return entries
