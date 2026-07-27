"""
Local web dashboard - a Flask + htmx + Alpine.js app serving the same "cockpit" controls as
the Textual TUI (subscribe/pause/resume/delete/force-check, diagnostics, API key setup),
backed by the exact same api_client/services/subscriptions/api_keys/logtail functions the TUI
uses (no daemon logic duplicated here - only the presentation differs). This is now the
primary interface - menu.main() launches this and hides its own console window once it's up,
falling back to the console TUI only if Flask isn't installed. The TUI is still one click away
via the "Console UI" button (see launch_tui() below), which opens it in a fresh console window
without re-running the startup sequence, since services are already running by the time this
dashboard exists at all. Reachable directly at http://127.0.0.1:8765.

Styling is Tailwind + daisyUI loaded from a CDN, interactivity is htmx (server-rendered HTML
fragments swapped into the page - polling for live data, hx-post for actions) plus a small
amount of Alpine.js for pure client-side UI state (the subscriptions filter box, the API keys
modal's tabs) that has no reason to round-trip to the server. No build step, no npm - every
route here returns a Jinja2-rendered template fragment straight from hydrus_pipeline/templates/.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from . import api_client, api_keys, hydrus_client, logtail, services, subscriptions
from .subscriptions import add_single_subscription

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from flask import Flask, jsonify, render_template, request
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

app = Flask(__name__) if HAVE_FLASK else None
if app is not None:
    # Auto-reload edited .html templates on the next request instead of serving Jinja's cached
    # compile of whatever was on disk at first render - editing templates/partials/*.html while
    # the dashboard is already running (this whole session's dev loop) used to need a full app
    # restart to show up otherwise. This only affects template *content*; it's not Werkzeug's
    # use_reloader (which re-execs the whole process to pick up *.py changes) - that one stays
    # off deliberately, since this Flask app runs inside a background thread of the console
    # process (see run_webui() below), not as the main process the reloader expects to own.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True

_ACTIVE_SUB_RE = re.compile(r"checking subscription:\s*(\d+)")

# Activity history for the sparkline/queue-graph widgets - one (urls_queued, subscriptions_due)
# sample per /partials/status poll (the page polls that every 2s, so this is a rolling ~96s
# window). Module-level and unguarded by a lock: the dev server thread model here is
# single-threaded per request in practice at this sample rate, and a torn read/append on a
# deque of int pairs is harmless cosmetic jitter at worst, not worth a lock for decorative charts.
_activity_history: deque[tuple[int, int]] = deque(maxlen=48)
_START_TIME = time.monotonic()


def _active_sub_id(status_data: dict | None) -> str | None:
    if not status_data:
        return None
    m = _ACTIVE_SUB_RE.search(str(status_data.get("subscription_worker_status") or ""))
    return m.group(1) if m else None


def _format_duration(seconds: float) -> str:
    """"14h 32m" / "45m" / "2d 3h" - shared by the LAST CHECK/INTERVAL/NEXT CHECK columns so
    they all read in the same units instead of mixing relative and absolute time."""
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{sign}{days}d {hours}h"
    if hours:
        return f"{sign}{hours}h {minutes}m"
    return f"{sign}{minutes}m"


def _format_last_check_column(sub: dict) -> str:
    last_check = sub.get("last_check")
    if last_check is None:
        return "never"
    return f"{_format_duration(time.time() - last_check)} ago"


def _format_check_interval_column(sub: dict) -> str:
    interval = sub.get("check_interval")
    if not interval:
        return "?"
    return _format_duration(interval)


def _format_next_check(sub: dict) -> str:
    """"NEXT CHECK" column for the main subscriptions table - hydownloader itself only exposes
    last_check + check_interval (seconds), not a precomputed next-check timestamp, so this
    derives it the same way hydownloader's own "is_due" check does (last_check + check_interval
    <= now). Shown as a countdown ("in 3h 12m") rather than a clock time, to match LAST CHECK's
    "Xh Ym ago" and CHECK INTERVAL's plain duration on either side of it."""
    if sub.get("paused"):
        return "(paused)"
    last_check = sub.get("last_check")
    interval = sub.get("check_interval")
    if last_check is None:
        return "pending (first check)"
    if not interval:
        return "?"
    remaining = (last_check + interval) - time.time()
    if remaining <= 0:
        return "due now"
    return f"in {_format_duration(remaining)}"


def _format_last_check(check: dict | None) -> dict | None:
    if not check:
        return None
    finished = check.get("time_finished")
    when = datetime.fromtimestamp(finished).strftime("%Y-%m-%d %H:%M") if finished else "?"
    return {
        "when": when,
        "new_files": check.get("new_files"),
        "already_seen_files": check.get("already_seen_files"),
    }


def _format_history_row(check: dict) -> dict:
    started = check.get("time_started")
    finished = check.get("time_finished")
    when = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S") if started else "?"
    duration = f"{finished - started:.0f}s" if started and finished else "?"
    status = str(check.get("status") or "")
    return {
        "when": when,
        "duration": duration,
        "new_files": check.get("new_files"),
        "already_seen_files": check.get("already_seen_files"),
        "status": status,
        "ok": status.lower() == "ok" or not status,
    }


_SPARK_W, _SPARK_H, _SPARK_PAD = 240, 40, 4


def _spark_points(history: list[int], width: float, height: float, pad: float, lo: int, hi: int) -> list[tuple[float, float]]:
    span = max(hi - lo, 1)
    n = len(history)
    step = width / max(n - 1, 1)
    return [
        (round(i * step, 1), round(pad + (height - 2 * pad) * (1 - (v - lo) / span), 1))
        for i, v in enumerate(history)
    ]


def _build_sparkline(pairs: list[tuple[int, int]]) -> dict | None:
    """Precomputes SVG polyline coordinates server-side (fixed viewBox, one hue, 2px line -
    see the dataviz skill's mark spec) so the template just drops in a ready points string
    instead of doing coordinate math in Jinja. Returns None with fewer than 2 samples - a
    single point isn't a line. Plots the combined (queued + due) total as one series - see
    _build_dual_line for the two-series breakdown."""
    if len(pairs) < 2:
        return None
    totals = [q + d for q, d in pairs]
    lo, hi = min(totals), max(totals)
    pts = _spark_points(totals, _SPARK_W, _SPARK_H, _SPARK_PAD, lo, hi)
    last_x, last_y = pts[-1]
    return {
        "points": " ".join(f"{x},{y}" for x, y in pts),
        "last_x": last_x, "last_y": last_y,
        "width": _SPARK_W, "height": _SPARK_H,
        "peak": hi, "now": totals[-1],
    }


_GRAPH_W, _GRAPH_H, _GRAPH_PAD = 380, 110, 6


def _build_dual_line(pairs: list[tuple[int, int]]) -> dict | None:
    """Two-series line chart (queued URLs vs. due subscriptions) sharing one axis - both are
    plain counts, so a shared scale is correct here (never a dual-axis chart - see the dataviz
    skill's anti-patterns: two different-unit measures would need two charts, not two scales
    on one plot). Categorical color (cyan/orange) since these are two distinct named series,
    not a magnitude ramp - both get a direct end-label instead of a legend box per the skill's
    "1-3 series: color alone is comfortable, direct-label" guidance."""
    if len(pairs) < 2:
        return None
    queued = [q for q, _ in pairs]
    due = [d for _, d in pairs]
    lo = min(min(queued), min(due))
    hi = max(max(queued), max(due), 1)
    q_pts = _spark_points(queued, _GRAPH_W, _GRAPH_H, _GRAPH_PAD, lo, hi)
    d_pts = _spark_points(due, _GRAPH_W, _GRAPH_H, _GRAPH_PAD, lo, hi)
    return {
        "queued_points": " ".join(f"{x},{y}" for x, y in q_pts),
        "due_points": " ".join(f"{x},{y}" for x, y in d_pts),
        "queued_last": q_pts[-1], "due_last": d_pts[-1],
        "width": _GRAPH_W, "height": _GRAPH_H,
        "queued_now": queued[-1], "due_now": due[-1],
        "peak": hi,
    }


if HAVE_FLASK:
    # Routes are only registered when Flask actually imported - `app` is None otherwise, and
    # @app.route on None would blow up at import time for every other module in this package
    # too, not just this one. See run_webui() for the friendly fallback message.

    @app.route("/")
    def index():
        return render_template("index.html")

    # ---------------------------------------------------------------- polled partials

    @app.route("/partials/status")
    def partial_status():
        svc = services.get_service_status()
        status_resp = api_client.get_status_info()
        if status_resp.success and status_resp.data:
            d = status_resp.data
            urls_queued = d.get("urls_queued") or 0
            subs_due = d.get("subscriptions_due") or 0
            ctx = dict(
                status=svc, api_ok=True,
                sub_status=d.get("subscription_worker_status") or "",
                url_status=d.get("url_worker_status") or "",
                urls_queued=urls_queued,
                subs_due=subs_due,
            )
            _activity_history.append((urls_queued, subs_due))
        else:
            ctx = dict(status=svc, api_ok=False, sub_status="", url_status="", urls_queued=0, subs_due=0)
            _activity_history.append((0, 0))
        # Decorative "signature" readout for the header - a real hash of the current worker
        # state (not random), so it changes exactly when something real changes rather than
        # ticking pointlessly every poll. Pure flavor, but grounded in real state.
        sig_input = f"{ctx['sub_status']}{ctx['url_status']}{ctx['urls_queued']}{ctx['subs_due']}{svc.hydrus_pid}{svc.daemon_pid}"
        ctx["sig"] = hashlib.sha1(sig_input.encode()).hexdigest()[:8]
        return render_template("partials/status.html", **ctx)

    @app.route("/partials/fleet")
    def partial_fleet():
        subs_resp = api_client.get_subscriptions()
        subs = sorted(subs_resp.data or [], key=lambda s: s.get("id", 0)) if subs_resp.success else []
        counts = subscriptions.fleet_counts(subs)
        uptime_s = int(time.monotonic() - _START_TIME)
        h, rem = divmod(uptime_s, 3600)
        m, sec = divmod(rem, 60)

        svc = services.get_service_status()
        procs = [
            {"name": "hydrus_client.exe", "up": svc.hydrus_running, "pid": svc.hydrus_pid},
            {"name": "hydownloader-daemon", "up": svc.daemon_running, "pid": svc.daemon_pid},
            {"name": "hydownloader-systray", "up": svc.systray_running, "pid": svc.systray_pid},
        ]

        return render_template(
            "partials/fleet.html",
            total=counts["total"], active=counts["active"], paused=counts["paused"], due=counts["due"],
            uptime=f"{h:02}:{m:02}:{sec:02}", procs=procs,
        )

    @app.route("/partials/sector")
    def partial_sector():
        subs_resp = api_client.get_subscriptions()
        subs = subs_resp.data or [] if subs_resp.success else []
        ranked = subscriptions.top_downloaders(subs)
        max_count = max((c for _, c in ranked), default=1) or 1
        sector = [{"name": name, "count": count, "pct": round(count / max_count * 100, 1)} for name, count in ranked]
        return render_template("partials/sector_scan.html", sector=sector)

    @app.route("/partials/sparkline")
    def partial_sparkline():
        return render_template("partials/sparkline.html", spark=_build_sparkline(list(_activity_history)))

    @app.route("/partials/queue-graph")
    def partial_queue_graph():
        return render_template("partials/queue_graph.html", graph=_build_dual_line(list(_activity_history)))

    @app.route("/partials/netstat")
    def partial_netstat():
        return render_template("partials/netstat.html", stats=api_client.get_call_stats())

    @app.route("/partials/hoststats")
    def partial_hoststats():
        return render_template("partials/hoststats.html", host=services.get_host_stats(), gpu=services.get_gpu_stats())

    @app.route("/partials/topprocs")
    def partial_topprocs():
        return render_template("partials/topprocs.html", procs=services.get_top_processes())

    @app.route("/partials/netconn")
    def partial_netconn():
        return render_template("partials/netconn.html", conns=services.get_network_connections())

    @app.route("/partials/hydrus")
    def partial_hydrus():
        return render_template("partials/hydrus_stats.html", stats=hydrus_client.get_hydrus_stats())

    @app.route("/partials/subscriptions")
    def partial_subscriptions():
        subs_resp = api_client.get_subscriptions()
        status_resp = api_client.get_status_info()
        active_id = _active_sub_id(status_resp.data if status_resp.success else None)
        if subs_resp.success:
            subs = sorted(subs_resp.data or [], key=lambda s: s.get("id", 0))
            for s in subs:
                s["last_check_display"] = _format_last_check_column(s)
                s["check_interval_display"] = _format_check_interval_column(s)
                s["next_check_display"] = _format_next_check(s)
            return render_template("partials/subs_table.html", subs=subs, active_id=active_id, error=None)
        return render_template("partials/subs_table.html", subs=[], active_id=None, error=subs_resp.error)

    @app.route("/partials/new-files")
    def partial_new_files():
        subs_resp = api_client.get_subscriptions()
        subs = subs_resp.data or [] if subs_resp.success else []
        ids = [s.get("id") for s in subs if s.get("id") is not None]
        latest = subscriptions.get_latest_checks(ids) if ids else {}
        totals = subscriptions.get_total_downloads(ids) if ids else {}
        failure_status = subscriptions.get_failure_status(subs) if subs else {}
        return render_template("partials/new_files_oob.html", latest=latest, totals=totals, all_ids=ids, failure_status=failure_status)

    @app.route("/partials/log")
    def partial_log():
        since = request.args.get("since", type=int)
        lines, offset = logtail.read_since(since)
        return jsonify({"lines": lines, "offset": offset})

    # ---------------------------------------------------------------- subscriptions: add

    @app.route("/subscriptions/quick-add", methods=["POST"])
    def quick_add():
        url = (request.form.get("url") or "").strip()
        hours_raw = (request.form.get("hours") or "").strip()
        hours = float(hours_raw) if hours_raw else None  # None -> fuzzed standard interval
        if not url:
            return render_template("partials/message.html", message="Enter a URL.", error=True)
        result = add_single_subscription(url, hours)
        headers = {}
        if result.status == "Added":
            headers["HX-Trigger"] = "refreshSubs"
            msg = f"Subscribed: {result.detail}"
            if result.restarted_daemon:
                msg += " (new site - daemon restarted automatically)"
            elif result.restart_error:
                msg += f" (added, but auto-restart failed: {result.restart_error} - check Diagnostics)"
            return render_template("partials/message.html", message=msg, error=False), 200, headers
        return render_template("partials/message.html", message=f"{result.status}: {result.detail}", error=(result.status == "Failed")), 200, headers

    @app.route("/subscriptions/add-modal")
    def add_subscription_modal():
        return render_template("partials/add_subscription_modal.html", results=None)

    @app.route("/subscriptions/add", methods=["POST"])
    def add_subscription():
        raw = (request.form.get("urls") or "").strip()
        hours_raw = (request.form.get("hours") or "").strip()
        hours: float | None
        try:
            hours = float(hours_raw) if hours_raw else None
            if hours is not None and hours <= 0:
                hours = None
        except (TypeError, ValueError):
            hours = None
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        results = []
        added_any = False
        for u in urls:
            # hours=None here means each URL in the batch draws its own fuzzed interval
            # (see add_single_subscription) instead of all landing on the same one - that's
            # what actually prevents a bulk add from stacking every check together.
            result = add_single_subscription(u, hours)
            if result.status == "Added":
                added_any = True
            results.append({
                "url": u, "status": result.status, "detail": result.detail,
                "restarted_daemon": result.restarted_daemon, "restart_error": result.restart_error,
            })
        headers = {"HX-Trigger": "refreshSubs"} if added_any else {}
        return render_template("partials/add_subscription_modal.html", results=results, urls_value="", hours_value=hours), 200, headers

    # ---------------------------------------------------------------- one-off downloads

    @app.route("/downloads/add-modal")
    def add_download_modal():
        return render_template("partials/add_download_modal.html", message=None)

    @app.route("/downloads/add", methods=["POST"])
    def add_download():
        raw = (request.form.get("urls") or "").strip()
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            return render_template("partials/add_download_modal.html", message="Enter at least one URL.", message_error=True)
        resp = api_client.add_or_update_urls(urls, file_filter=subscriptions.DEFAULT_FILE_FILTER)
        if resp.accepted:
            return render_template("partials/add_download_modal.html", message=f"Queued {len(urls)} URL(s) for download.", message_error=False)
        return render_template("partials/add_download_modal.html", message=f"Failed to queue: {resp.error or 'daemon rejected the request'}", message_error=True)

    # ---------------------------------------------------------------- subscription row actions

    def _require_sub(sub_id: int):
        """Shared by every route below keyed on an existing subscription id - returns (sub,
        None) on success, or (None, response) with the standard "no longer exists" message if
        it's already been deleted out from under this request (e.g. a stale row still showing
        in another browser tab)."""
        sub = subscriptions.get_subscription_by_id(sub_id)
        if sub:
            return sub, None
        return None, render_template("partials/message.html", message=f"Subscription #{sub_id} no longer exists.", error=True)

    def _render_actions_modal(sub: dict, sub_id: int, message: str | None, message_error: bool = False, headers: dict | None = None):
        latest = subscriptions.get_latest_checks([sub_id])
        return render_template(
            "partials/actions_modal.html", sub=sub, last_check=_format_last_check(latest.get(sub_id)),
            message=message, message_error=message_error,
        ), 200, (headers or {})

    @app.route("/subscriptions/<int:sub_id>/actions")
    def subscription_actions(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        return _render_actions_modal(sub, sub_id, message=None)

    @app.route("/subscriptions/<int:sub_id>/edit-modal")
    def edit_subscription_modal(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        return render_template("partials/edit_subscription_modal.html", sub=sub, message=None)

    @app.route("/subscriptions/<int:sub_id>/edit", methods=["POST"])
    def edit_subscription(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp

        keywords = (request.form.get("keywords") or "").strip()
        if not keywords:
            return render_template("partials/edit_subscription_modal.html", sub=sub, message="Keywords can't be empty.", message_error=True)

        hours_raw = (request.form.get("hours") or "").strip()
        try:
            hours = float(hours_raw)
            if hours <= 0:
                raise ValueError
        except ValueError:
            return render_template("partials/edit_subscription_modal.html", sub=sub, message="Check interval must be a positive number of hours.", message_error=True)

        def parse_cap(field: str) -> int | None:
            raw = (request.form.get(field) or "").strip()
            return int(raw) if raw else None

        file_filter = (request.form.get("filter") or "").strip()

        ok, error = subscriptions.update_subscription(
            sub_id, keywords=keywords, check_interval_hours=hours,
            max_files_initial=parse_cap("max_files_initial"), max_files_regular=parse_cap("max_files_regular"),
            file_filter=file_filter,
        )
        if not ok:
            return render_template("partials/edit_subscription_modal.html", sub=sub, message=f"Failed: {error}", message_error=True)

        updated_sub = subscriptions.get_subscription_by_id(sub_id) or sub
        return _render_actions_modal(
            updated_sub, sub_id, message=f"Subscription #{sub_id} updated.",
            headers={"HX-Trigger": "refreshSubs"},
        )

    @app.route("/subscriptions/<int:sub_id>/toggle-pause", methods=["POST"])
    def toggle_pause(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        new_paused = not bool(sub.get("paused"))
        resp = api_client.add_or_update_subscriptions([{"id": sub_id, "paused": new_paused}])
        if resp.accepted:
            sub["paused"] = new_paused
            return _render_actions_modal(
                sub, sub_id, message=f"Subscription #{sub_id} {'paused' if new_paused else 'resumed'}.",
                headers={"HX-Trigger": "refreshSubs"},
            )
        return _render_actions_modal(sub, sub_id, message=f"Failed: {resp.error or 'daemon rejected the request'}", message_error=True)

    @app.route("/subscriptions/<int:sub_id>/force-check", methods=["POST"])
    def force_check(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        ok, error = subscriptions.force_recheck(sub_id)
        if ok:
            return _render_actions_modal(
                sub, sub_id, message=f"Subscription #{sub_id} marked due - its worker thread will pick it up within a few seconds.",
                headers={"HX-Trigger": "refreshSubs"},
            )
        return _render_actions_modal(sub, sub_id, message=f"Failed: {error}", message_error=True)

    @app.route("/subscriptions/<int:sub_id>/history")
    def subscription_history(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        rows, error = subscriptions.get_check_history(sub_id)
        formatted = [_format_history_row(r) for r in rows] if not error else []
        # the gallery-dl log only reflects the most recent run, so only the newest row can be
        # explained from it - older "http error" rows just show the raw status, as before
        if formatted and not formatted[0]["ok"]:
            formatted[0]["error_detail"] = subscriptions.explain_check_error(sub_id)
        return render_template(
            "partials/history_modal.html", sub=sub, error=error, rows=formatted,
        )

    @app.route("/subscriptions/<int:sub_id>/confirm-delete")
    def confirm_delete(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        return render_template("partials/confirm_delete_modal.html", sub=sub)

    @app.route("/subscriptions/<int:sub_id>/delete", methods=["POST"])
    def delete_subscription(sub_id: int):
        resp = api_client.delete_subscriptions([sub_id])
        headers = {"HX-Trigger": "refreshSubs, closeModal"}
        if resp.accepted:
            return "", 200, headers
        return render_template("partials/message.html", message=f"Failed: {resp.error or 'daemon rejected the request'}", error=True), 200, headers

    # ---------------------------------------------------------------- diagnostics

    def _diagnostics_ctx() -> dict:
        flagged_count, flagged_error = subscriptions.get_flagged_subscription_count()
        return dict(report=services.get_health_report(), flagged_count=flagged_count, flagged_error=flagged_error)

    @app.route("/diagnostics")
    def diagnostics():
        return render_template("partials/diagnostics_modal.html", **_diagnostics_ctx(), cap_message=None, restart_message=None)

    @app.route("/diagnostics/restart-services", methods=["POST"])
    def diagnostics_restart():
        services.start_required_services()
        return render_template(
            "partials/diagnostics_modal.html", **_diagnostics_ctx(),
            cap_message=None, restart_message="Restart attempted - see status above.",
        ), 200, {"HX-Trigger": "refreshSubs"}

    def _render_diagnostics(updated: int, total: int, error: str | None, success_message: str) -> str:
        cap_message, cap_error = (f"Failed: {error}", True) if error else (success_message, False)
        return render_template(
            "partials/diagnostics_modal.html", **_diagnostics_ctx(),
            cap_message=cap_message, cap_error=cap_error, restart_message=None,
        )

    @app.route("/diagnostics/cap-existing", methods=["POST"])
    def diagnostics_cap_existing():
        updated, total, error = subscriptions.cap_existing_subscription_file_limits()
        return _render_diagnostics(
            updated, total, error,
            f"Capped {updated} of {total} subscription(s). Takes effect on each one's next check - no daemon restart needed.",
        )

    @app.route("/diagnostics/fuzz-intervals", methods=["POST"])
    def diagnostics_fuzz_intervals():
        force_all = (request.form.get("force_all") or "") == "true"
        updated, total, error = subscriptions.fuzz_existing_intervals(force_all=force_all)
        scope = "every" if force_all else "the standard-band"
        return _render_diagnostics(
            updated, total, error,
            f"Re-fuzzed {updated} of {total} subscription(s) ({scope} interval(s) touched, 12-24h spread). "
            f"Takes effect on each one's next check - no daemon restart needed.",
        )

    @app.route("/diagnostics/block-video", methods=["POST"])
    def diagnostics_block_video():
        updated, total, error = subscriptions.block_video_on_existing_subscriptions()
        return _render_diagnostics(
            updated, total, error,
            f"Applied the video block to {updated} of {total} subscription(s). Takes effect on each one's "
            f"next check - no daemon restart needed.",
        )

    # ---------------------------------------------------------------- API keys

    @app.route("/api-keys")
    def api_keys_view():
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx())

    def _api_keys_ctx(**overrides):
        reddit_ok, reddit_cid = api_keys.get_reddit_status()
        hydrus_ok, hydrus_key = api_keys.get_hydrus_key_status()
        ctx = dict(
            reddit_ok=reddit_ok, reddit_cid_masked=api_keys.mask_secret(reddit_cid),
            hydrus_ok=hydrus_ok, hydrus_key_masked=api_keys.mask_secret(hydrus_key),
            services=api_keys.list_service_key_statuses(),
            message=None,
        )
        ctx.update(overrides)
        return ctx

    @app.route("/api-keys/service/<service_id>", methods=["POST"])
    def api_keys_service(service_id: str):
        entry = next((e for e in api_keys.SERVICE_KEY_REGISTRY if e["id"] == service_id), None)
        if entry is None:
            return render_template("partials/api_keys_modal.html", **_api_keys_ctx(
                message=f"unknown service: {service_id}", message_error=True, default_tab="services",
            ))
        values = {f["key"]: request.form.get(f["key"], "") for f in entry["fields"]}
        ok, msg = api_keys.apply_service_key(service_id, values)
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(
            message=msg, message_error=not ok, default_tab="services",
        ))

    @app.route("/api-keys/hydrus", methods=["POST"])
    def api_keys_hydrus():
        ok, msg = api_keys.apply_hydrus_key((request.form.get("api_key") or "").strip())
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(message=msg, message_error=not ok, default_tab="hydrus"))

    @app.route("/api-keys/reddit/test-shared", methods=["POST"])
    def api_keys_reddit_test():
        ok, output = api_keys.run_reddit_shared_test_capture(request.form.get("subreddit") or "pics")
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(test_output=output, default_tab="services"))

    @app.route("/api-keys/reddit/configure", methods=["POST"])
    def api_keys_reddit_configure():
        client_id = (request.form.get("client_id") or "").strip()
        username = (request.form.get("username") or "").strip()
        ok, msg = api_keys.apply_reddit_app_config(client_id, username)
        if not ok:
            return render_template("partials/api_keys_modal.html", **_api_keys_ctx(message=msg, message_error=True, default_tab="services"))
        _, output = api_keys.run_reddit_oauth_capture()
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(oauth_output=output, default_tab="services"))

    @app.route("/api-keys/reddit/refresh-token", methods=["POST"])
    def api_keys_reddit_refresh_token():
        ok, msg = api_keys.apply_reddit_refresh_token((request.form.get("refresh_token") or "").strip())
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(message=msg, message_error=not ok, default_tab="services"))

    # ---------------------------------------------------------------- bring windows to front

    @app.route("/focus/hydrus", methods=["POST"])
    def focus_hydrus():
        ok = services.show_process_window("hydrus_client")
        return render_template("partials/message.html", message="Hydrus window not found - is it running?" if not ok else "Brought Hydrus to the front.", error=not ok)

    @app.route("/focus/systray", methods=["POST"])
    def focus_systray():
        ok = services.show_process_window("hydownloader-systray")
        return render_template("partials/message.html", message="Systray window not found - is it running?" if not ok else "Brought the systray to the front.", error=not ok)

    # ---------------------------------------------------------------- console UI fallback / shutdown

    @app.route("/launch-tui", methods=["POST"])
    def launch_tui():
        """Opens the classic console TUI in a fresh, visible console window - the fallback/
        menu option this dashboard's docstring promises. Runs `python -m hydrus_pipeline.tui`
        (see tui/__main__.py) rather than menu.main(), since services/watchdog are already
        running under this process - the TUI just needs to connect to them, not start them
        again. cwd is pinned to the project root so `-m hydrus_pipeline.tui` resolves
        regardless of whatever directory this request happened to be handled from."""
        try:
            subprocess.Popen(
                [sys.executable, "-m", "hydrus_pipeline.tui"],
                cwd=str(_PROJECT_ROOT),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return render_template("partials/message.html", message="Launching the console UI in a new window...", error=False)
        except OSError as e:
            return render_template("partials/message.html", message=f"Couldn't launch the console UI: {e}", error=True)

    @app.route("/shutdown-confirm")
    def shutdown_confirm():
        return render_template("partials/shutdown_confirm.html")

    @app.route("/shutdown-app", methods=["POST"])
    def shutdown_app():
        """Stops whatever's idle (same check as quitting the TUI - a busy daemon is left
        running) and then actually exits this process. This is the only way to cleanly stop
        the pipeline now that its console window is hidden by default - there's no window to
        close or Ctrl+C. Runs the real shutdown on a short delay in a background thread so the
        HTTP response below has time to reach the browser before the process disappears out
        from under it; os._exit skips atexit/cleanup entirely, which is fine here since
        stop_idle_components() already ran synchronously first."""
        def _do_shutdown():
            time.sleep(0.4)
            try:
                services.stop_idle_components()
            except Exception:
                pass
            sys.stdout.flush()  # os._exit() skips normal interpreter cleanup, flush included
            os._exit(0)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        return render_template(
            "partials/message.html",
            message="Shutting down - this dashboard will stop responding in a moment.", error=False,
        )


_webui_state: dict = {"thread": None, "port": None}


def is_running() -> bool:
    thread = _webui_state.get("thread")
    return thread is not None and thread.is_alive()


def run_webui(port: int = 8765, open_browser: bool = True) -> int | None:
    """Starts the Flask app in a background daemon thread (so it dies with the console
    process, same lifetime as everything else this pipeline starts) and returns the port it's
    listening on, or None if Flask isn't installed. Calling this again while already running
    just reopens the browser tab instead of spawning a second server. threaded=True matters
    here - some actions (running gallery-dl's OAuth flow) block for up to two minutes waiting
    on a browser redirect, and the dashboard's own status/subscription polling needs to keep
    working in other tabs/requests while that's in flight."""
    if not HAVE_FLASK:
        return None

    if is_running():
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{_webui_state['port']}")
        return _webui_state["port"]

    def _serve():
        # The dev server's request logger (werkzeug) writes a line per poll to stdout by
        # default - since this runs in a background thread of the same console process as the
        # menu, that's every poll from the page's 2s auto-refresh interleaving with menu
        # prompts. Silence it; actual errors still raise/propagate normally.
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)

    thread = threading.Thread(target=_serve, daemon=True, name="hydrus-pipeline-webui")
    thread.start()
    _webui_state["thread"] = thread
    _webui_state["port"] = port

    if open_browser:
        import time
        time.sleep(0.6)
        webbrowser.open(f"http://127.0.0.1:{port}")
    return port
