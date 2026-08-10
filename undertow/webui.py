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
route here returns a Jinja2-rendered template fragment straight from undertow/templates/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from . import api_client, api_keys, config, hydrus_client, logtail, media, services, settings, subscriptions, tags, version, watchdog
from .subscriptions import add_single_subscription

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from flask import Flask, Response, jsonify, make_response, render_template, request
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

    @app.context_processor
    def _inject_version():
        return {"version_info": version.get_version_info()}

_ACTIVE_SUB_RE = re.compile(r"checking subscription:\s*(\d+)")

# Girly-mode user image gallery: ONE flat drop folder (static/images/anime/ - no per-slot
# subfolders) shared by every image spot in templates/index.html's #girly-view. Each spot just
# asks for a slot name (a target aspect ratio + on-page size, see _GALLERY_SLOTS/kawaii.css's
# .kawaii-gallery-* classes) and _anime_pick() below picks whichever image in the shared pool
# fits that ratio best - there's no manual sorting into folders, the ratio *is* the sort.
# If nothing in the pool fits a slot well enough, that spot renders explanatory placeholder text
# instead of a mis-cropped image (see partials/girly/gallery_slot.html) - "add a picture shaped
# like this" rather than silently cramming a square photo into a wide banner.
_ANIME_DIR = _PROJECT_ROOT / "undertow" / "static" / "images" / "anime"
# Only large slots (>=500px on their shortest side, see .kawaii-gallery-* in kawaii.css) - no
# icon/avatar/thumbnail-scale spots. This is a user's own photo gallery, not favicon decoration,
# so every slot is meant to actually show the picture, not shrink it into a corner sticker.
_GALLERY_SLOTS = {
    "lg":     {"ratio": 1.0,        "label": "square, ~1:1 (e.g. 700×700)"},
    "wide":   {"ratio": 800 / 260,  "label": "wide banner, ~3:1 (e.g. 1200×400)"},
    "tall":   {"ratio": 1 / 1.8,    "label": "tall portrait, ~1:1.8 (e.g. 600×1080)"},
}
_GALLERY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# How forgiving the ratio match is: log-distance beyond this means "doesn't fit this slot at
# all" (falls back to placeholder text) rather than just "less likely to be picked". log(2.6)
# lets e.g. a 4:3 photo still count for "icon" (~1:1) but keeps a true wide/tall shot out of a
# square slot and vice versa.
_GALLERY_FIT_CUTOFF = math.log(2.6)


def _image_aspect_ratio(path: Path) -> float | None:
    """Pure-stdlib width/height sniffer (no Pillow dependency) covering the formats gallery
    images realistically show up in. Returns None (treated as "unknown, assume it fits fine")
    for anything it can't parse rather than raising - a malformed/unsupported file should still
    be selectable, just without the aspect-ratio weighting."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".svg":
            text = path.read_text(encoding="utf-8", errors="ignore")[:4000]
            m = re.search(r'viewBox=["\']\s*[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)', text)
            if not m:
                m = re.search(r'width=["\']([\d.]+)', text), re.search(r'height=["\']([\d.]+)', text)
                if m[0] and m[1]:
                    w, h = float(m[0].group(1)), float(m[1].group(1))
                    return w / h if h else None
                return None
            w, h = float(m.group(1)), float(m.group(2))
            return w / h if h else None

        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
                return w / h if h else None
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w = int.from_bytes(head[6:8], "little")
                h = int.from_bytes(head[8:10], "little")
                return w / h if h else None
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                f.seek(0)
                data = f.read(64)
                if data[12:16] == b"VP8 ":
                    w = int.from_bytes(data[26:28], "little") & 0x3FFF
                    h = int.from_bytes(data[28:30], "little") & 0x3FFF
                elif data[12:16] == b"VP8L":
                    b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                    w = 1 + (((b1 & 0x3F) << 8) | b0)
                    h = 1 + (((b3 & 0xF) << 10) | (b2 << 2) | (b1 >> 6))
                else:
                    return None
                return w / h if h else None
            if head[:2] == b"\xff\xd8":  # JPEG - scan markers for the first SOFn segment
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3):
                        f.read(3)
                        h = int.from_bytes(f.read(2), "big")
                        w = int.from_bytes(f.read(2), "big")
                        return w / h if h else None
                    if marker[1] in (0xD8, 0xD9):
                        return None
                    seg_len = int.from_bytes(f.read(2), "big")
                    if seg_len < 2:
                        return None
                    f.seek(seg_len - 2, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _image_has_transparency(path: Path) -> bool:
    """Pure-stdlib alpha-channel sniffer (same "no Pillow" constraint as _image_aspect_ratio
    above) - true only when the file actually carries transparent/semi-transparent pixels, not
    just "is a format that's capable of it" (a .png without alpha is still opaque). Covers PNG
    (by far the common case for "cutout" gallery images - see static/images/anime/README.md)
    and GIF's single-color transparency; WEBP and JPEG fall back to False (undecided/opaque)
    rather than trying to fully decode a VP8L bitstream just to answer this one question -
    those slots just keep the normal card framing, which is a safe default either way."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".png":
            with open(path, "rb") as f:
                data = f.read()
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                return False
            color_type = data[25]
            if color_type in (4, 6):  # grayscale+alpha, RGBA
                return True
            if color_type == 3:  # palette - only transparent if a tRNS chunk is present
                return b"tRNS" in data
            return False
        if suffix == ".gif":
            with open(path, "rb") as f:
                data = f.read()
            # Graphic Control Extension: 0x21 0xF9 0x04 <flags> ... - bit 0 of flags is the
            # transparent-color flag. Good enough for "does this GIF use transparency at all".
            idx = data.find(b"\x21\xf9\x04")
            return idx != -1 and idx + 3 < len(data) and (data[idx + 3] & 0x01)
        if suffix == ".webp":
            with open(path, "rb") as f:
                head = f.read(32)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP" and head[12:16] == b"VP8X":
                return bool(head[20] & 0x10)
            return False
        return False
    except OSError:
        return False


_ANIME_STATS_PATH = _ANIME_DIR / ".gallery_stats.json"


def _load_gallery_stats() -> dict:
    """Show-count tracker for the shared pool, keyed by filename (dotfile, so it's never itself
    picked up as a candidate image). Missing/corrupt file just means "nothing tracked yet"."""
    try:
        return json.loads(_ANIME_STATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_gallery_stats(stats: dict) -> None:
    try:
        _ANIME_DIR.mkdir(parents=True, exist_ok=True)
        _ANIME_STATS_PATH.write_text(json.dumps(stats), encoding="utf-8")
    except OSError:
        pass  # best-effort - a failed write just means balancing resets, nothing user-visible


def _anime_pool() -> list[Path]:
    if not _ANIME_DIR.is_dir():
        return []
    return [
        p for p in _ANIME_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _GALLERY_EXTS
    ]


def _anime_pick(slot: str) -> str | None:
    """Picks a filename from the shared static/images/anime/ pool for `slot`, purely by aspect
    ratio - there's no per-slot folder to sort into. Returns None if the pool is empty or
    nothing in it fits this slot's ratio closely enough (see _GALLERY_FIT_CUTOFF); the caller
    renders placeholder text explaining what shape image would fill the spot instead.

    Among images that DO fit, selection is weighted random: better ratio match wins out over
    worse, and rarely-shown images win out over frequently-shown ones (tracked in
    .gallery_stats.json) - the same image pool feeding 14 different-shaped spots should still
    settle into every photo getting roughly even overall exposure, not just whichever one
    happens to be squarest."""
    target = _GALLERY_SLOTS.get(slot, {}).get("ratio", 1.0)
    pool = _anime_pool()
    if not pool:
        return None

    fits = []
    for p in pool:
        ratio = _image_aspect_ratio(p)
        fits.append(0.0 if (ratio is None or ratio <= 0) else abs(math.log(ratio / target)))

    if min(fits) > _GALLERY_FIT_CUTOFF:
        return None

    good = [(p, d) for p, d in zip(pool, fits) if d <= _GALLERY_FIT_CUTOFF]
    stats = _load_gallery_stats()
    weights = [(1.0 / (dist + 0.15)) * (1.0 / (stats.get(p.name, 0) + 1)) for p, dist in good]
    chosen = random.choices([p for p, _ in good], weights=weights, k=1)[0]

    # Drop stats for files no longer in the pool so the tracker doesn't grow stale entries.
    current_names = {p.name for p in pool}
    stats = {name: count for name, count in stats.items() if name in current_names}
    stats[chosen.name] = stats.get(chosen.name, 0) + 1
    _save_gallery_stats(stats)

    return chosen.name

# Activity history for the sparkline/queue-graph widgets - one (urls_queued, subscriptions_due)
# sample per /partials/status poll (the page polls that every 2s, so this is a rolling ~96s
# window). Module-level and unguarded by a lock: the dev server thread model here is
# single-threaded per request in practice at this sample rate, and a torn read/append on a
# deque of int pairs is harmless cosmetic jitter at worst, not worth a lock for decorative charts.
_activity_history: deque[tuple[int, int]] = deque(maxlen=48)
_START_TIME = time.monotonic()
# Wall-clock counterpart to _START_TIME (which is monotonic, not comparable to hydownloader's
# epoch-based check timestamps) - marks "this session" for the subs list's session-download
# pill (see subscriptions.get_session_downloads).
_SESSION_START_EPOCH = time.time()


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


def _girly_theme() -> bool:
    """Theme signal for partial routes that render differently per-theme. The girly layout in
    index.html sets `document.body`'s hx-headers to {"X-Pipeline-Theme": "kawaii"} whenever the
    user switches themes (see applyPipelineTheme() in index.html) - htmx re-reads an ancestor's
    hx-headers attribute fresh on every request, so every existing hx-get/hx-post in the page
    automatically carries the current theme with zero per-element plumbing. Terminal mode never
    sets this header (or clears it back to "night"), so its polling is completely unaffected."""
    return (request.headers.get("X-Pipeline-Theme") or "") == "kawaii"


def _themed_template(name: str) -> str:
    """`name` like "fleet.html" -> "partials/girly/fleet.html" in kawaii mode, else
    "partials/name" - the one place that picks which of the two independent partial sets (see
    templates/partials/ vs templates/partials/girly/) a themed route renders from."""
    if _girly_theme():
        return f"partials/girly/{name}"
    return f"partials/{name}"


if HAVE_FLASK:
    # Routes are only registered when Flask actually imported - `app` is None otherwise, and
    # @app.route on None would blow up at import time for every other module in this package
    # too, not just this one. See run_webui() for the friendly fallback message.

    @app.route("/")
    def index():
        info = version.get_version_info()
        st = settings.load_settings()
        last_seen = st.get("last_seen_version")
        is_first_run = last_seen != info["version"]
        if is_first_run:
            # Mark seen immediately - "first run of this version" means the first page load
            # after an update, not "every load until someone dismisses a banner". A manual
            # browser refresh a moment later should already show the caught-up state.
            settings.save_settings({"last_seen_version": info["version"]})
        return render_template("index.html", is_first_run=is_first_run)

    @app.route("/version/check", methods=["POST"])
    def version_check():
        result = version.check_for_update()
        return render_template(
            "partials/version_check.html", checked=True,
            update_available=result["update_available"], error=result["error"],
        )

    @app.route("/partials/changelog")
    def partial_changelog():
        return render_template("partials/changelog.html", sections=version.get_changelog())

    @app.route("/images/anime/slot/<slot>")
    def images_anime_slot(slot):
        """HTML fragment (not a raw image) for one girly-view gallery spot - htmx hx-gets this
        on load and swaps in either an <img> pointing at the chosen file under
        static/images/anime/ (served by Flask's normal static route, no custom bytes-serving
        route needed here) or a placeholder telling the user what shape image to drop in."""
        slot_info = _GALLERY_SLOTS.get(slot)
        if slot_info is None:
            return "", 404
        filename = _anime_pick(slot)
        resp = render_template(
            "partials/girly/gallery_slot.html",
            filename=filename, label=slot_info["label"],
            transparent=_image_has_transparency(_ANIME_DIR / filename) if filename else False,
        )
        return Response(resp, headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- polled partials

    @app.route("/partials/status")
    def partial_status():
        svc = services.get_service_status()
        drive_mounted = Path(config.HYDRUS_VOLUME_DRIVE + "\\").exists()
        status_resp = api_client.get_status_info()
        if status_resp.success and status_resp.data:
            d = status_resp.data
            urls_queued = d.get("urls_queued") or 0
            subs_due = d.get("subscriptions_due") or 0
            ctx = dict(
                status=svc, api_ok=True, drive_mounted=drive_mounted,
                sub_status=d.get("subscription_worker_status") or "",
                url_status=d.get("url_worker_status") or "",
                urls_queued=urls_queued,
                subs_due=subs_due,
            )
            _activity_history.append((urls_queued, subs_due))
        else:
            ctx = dict(status=svc, api_ok=False, drive_mounted=drive_mounted, sub_status="", url_status="", urls_queued=0, subs_due=0)
            _activity_history.append((0, 0))
        # Decorative "signature" readout for the header - a real hash of the current worker
        # state (not random), so it changes exactly when something real changes rather than
        # ticking pointlessly every poll. Pure flavor, but grounded in real state.
        sig_input = f"{ctx['sub_status']}{ctx['url_status']}{ctx['urls_queued']}{ctx['subs_due']}{svc.hydrus_pid}{svc.daemon_pid}"
        ctx["sig"] = hashlib.sha1(sig_input.encode()).hexdigest()[:8]
        return render_template(_themed_template("status.html"), **ctx)

    @app.route("/partials/fleet")
    def partial_fleet():
        subs_resp = api_client.get_subscriptions()
        subs = sorted(subs_resp.data or [], key=lambda s: s.get("id", 0)) if subs_resp.success else []
        counts = subscriptions.fleet_counts(subs)
        uptime_s = int(time.monotonic() - _START_TIME)
        h, rem = divmod(uptime_s, 3600)
        m, sec = divmod(rem, 60)

        return render_template(
            _themed_template("fleet.html"),
            total=counts["total"], active=counts["active"], paused=counts["paused"], due=counts["due"],
            uptime=f"{h:02}:{m:02}:{sec:02}",
        )

    @app.route("/partials/sector")
    def partial_sector():
        subs_resp = api_client.get_subscriptions()
        subs = subs_resp.data or [] if subs_resp.success else []
        ranked = subscriptions.top_downloaders(subs)
        max_count = max((c for _, c in ranked), default=1) or 1
        sector = [{"name": name, "count": count, "pct": round(count / max_count * 100, 1)} for name, count in ranked]
        return render_template(_themed_template("sector_scan.html"), sector=sector)

    @app.route("/partials/sparkline")
    def partial_sparkline():
        return render_template(_themed_template("sparkline.html"), spark=_build_sparkline(list(_activity_history)))

    @app.route("/partials/queue-graph")
    def partial_queue_graph():
        return render_template(_themed_template("queue_graph.html"), graph=_build_dual_line(list(_activity_history)))

    @app.route("/partials/netstat")
    def partial_netstat():
        return render_template(_themed_template("netstat.html"), stats=api_client.get_call_stats())

    @app.route("/partials/hoststats")
    def partial_hoststats():
        host = services.get_host_stats()
        thresholds = settings.load_settings().get("resource_alert_thresholds", {})
        breaches = services.check_resource_thresholds(host, thresholds)
        banner = "; ".join(breaches.values()) if breaches else None
        return render_template(_themed_template("hoststats.html"), host=host, gpu=services.get_gpu_stats(), banner=banner)

    @app.route("/partials/topprocs")
    def partial_topprocs():
        return render_template(_themed_template("topprocs.html"), procs=services.get_top_processes())

    @app.route("/partials/netconn")
    def partial_netconn():
        return render_template(_themed_template("netconn.html"), conns=services.get_network_connections())

    @app.route("/partials/hydrus")
    def partial_hydrus():
        return render_template(_themed_template("hydrus_stats.html"), stats=hydrus_client.get_hydrus_stats())

    @app.route("/partials/subscriptions")
    def partial_subscriptions():
        sort_by = request.args.get("sort_by") or "id"
        sort_dir = request.args.get("sort_dir") or "asc"
        page = request.args.get("page", 1, type=int) or 1
        page_size = request.args.get("page_size", 25, type=int) or 25
        tag_query = (request.args.get("tag") or "").strip().lower()
        grouped = request.args.get("grouped") in ("1", "true", "True")
        group_sort = request.args.get("group_sort") or "name"
        group_dir = request.args.get("group_dir") or "asc"

        subs_resp = api_client.get_subscriptions()
        status_resp = api_client.get_status_info()
        active_id = _active_sub_id(status_resp.data if status_resp.success else None)
        if subs_resp.success:
            all_subs = subs_resp.data or []
            tags_by_id = tags.load_tags()
            if tag_query:
                all_subs = [s for s in all_subs if any(tag_query in t.lower() for t in tags_by_id.get(s.get("id"), []))]
            failure_status = subscriptions.get_failure_status(all_subs) if grouped else {}
            all_ids = [s.get("id") for s in all_subs if s.get("id") is not None]
            session_totals = subscriptions.get_session_downloads(all_ids, _SESSION_START_EPOCH) if all_ids else {}
            for s in all_subs:
                s["last_check_display"] = _format_last_check_column(s)
                s["check_interval_display"] = _format_check_interval_column(s)
                s["next_check_display"] = _format_next_check(s)
                s["tags"] = tags_by_id.get(s.get("id"), [])
                s["flagged"] = bool(failure_status.get(s.get("id"), {}).get("flagged"))
                s["session_new_files"] = session_totals.get(s.get("id"), 0)
                # hydownloader's /get_queued_urls has no per-subscription linkage, so "queued"
                # here is approximated as "due to run now or currently running" rather than a
                # true queue depth.
                s["queued_count"] = 1 if (s.get("is_due") or s.get("id") == active_id) else 0

            if grouped:
                groups = subscriptions.group_by_downloader(all_subs, sort_by=group_sort, sort_dir=group_dir)
                return render_template(
                    "partials/girly/subs_grouped.html", groups=groups, active_id=active_id, error=None,
                    total=len(all_subs), group_sort=group_sort, group_dir=group_dir,
                )

            totals = None
            if sort_by == "total_dls":
                ids = [s.get("id") for s in all_subs if s.get("id") is not None]
                totals = subscriptions.get_total_downloads(ids) if ids else {}
            ordered = subscriptions.sort_subscriptions(all_subs, sort_by, sort_dir, totals=totals)
            subs, meta = subscriptions.paginate(ordered, page, page_size)
            meta["sort_by"], meta["sort_dir"] = sort_by, sort_dir
            return render_template(_themed_template("subs_table.html"), subs=subs, active_id=active_id, error=None, meta=meta)
        if grouped:
            return render_template("partials/girly/subs_grouped.html", groups=[], active_id=None, error=subs_resp.error, total=0,
                                    group_sort=group_sort, group_dir=group_dir)
        return render_template(_themed_template("subs_table.html"), subs=[], active_id=None, error=subs_resp.error, meta=None)

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

    # ---------------------------------------------------------------- subscriptions: export/import

    @app.route("/subscriptions/export")
    def export_subscriptions():
        payload = json.dumps(subscriptions.export_subscriptions(), indent=2)
        filename = f"hydrus-pipeline-subscriptions-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            payload, mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/subscriptions/import-modal")
    def import_subscriptions_modal():
        return render_template("partials/import_subscriptions_modal.html", results=None)

    @app.route("/subscriptions/import", methods=["POST"])
    def import_subscriptions_route():
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return render_template("partials/import_subscriptions_modal.html", results=None, message="Choose a file to import.", message_error=True)
        try:
            entries = json.loads(upload.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return render_template("partials/import_subscriptions_modal.html", results=None, message=f"Not a valid export file: {e}", message_error=True)
        if not isinstance(entries, list):
            return render_template("partials/import_subscriptions_modal.html", results=None, message="Not a valid export file: expected a JSON list.", message_error=True)

        allow_duplicate = (request.form.get("allow_duplicate") or "") == "true"
        results = subscriptions.import_subscriptions(entries, allow_duplicate=allow_duplicate)
        rows = [
            {
                "label": f"{e.get('downloader', '?')} / {e.get('keywords', '?')}",
                "status": r.status, "detail": r.detail,
                "restarted_daemon": r.restarted_daemon, "restart_error": r.restart_error,
            }
            for e, r in zip(entries, results)
        ]
        headers = {"HX-Trigger": "refreshSubs"} if any(r.status == "Added" for r in results) else {}
        return render_template("partials/import_subscriptions_modal.html", results=rows), 200, headers

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
        return render_template(
            "partials/edit_subscription_modal.html", sub=sub, message=None,
            tags_value=", ".join(tags.get_tags_for(sub_id)),
        )

    @app.route("/subscriptions/<int:sub_id>/edit", methods=["POST"])
    def edit_subscription(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp

        def _tags_value() -> str:
            return ", ".join(tags.get_tags_for(sub_id))

        keywords = (request.form.get("keywords") or "").strip()
        if not keywords:
            return render_template("partials/edit_subscription_modal.html", sub=sub, message="Keywords can't be empty.", message_error=True, tags_value=_tags_value())

        hours_raw = (request.form.get("hours") or "").strip()
        try:
            hours = float(hours_raw)
            if hours <= 0:
                raise ValueError
        except ValueError:
            return render_template("partials/edit_subscription_modal.html", sub=sub, message="Check interval must be a positive number of hours.", message_error=True, tags_value=_tags_value())

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
            return render_template("partials/edit_subscription_modal.html", sub=sub, message=f"Failed: {error}", message_error=True, tags_value=_tags_value())

        # Not transactional with the update_subscription() call above (one's a hydownloader API
        # write, the other's a local JSON write) - acceptable since tags are low-stakes local
        # metadata, not something hydownloader itself needs to stay in sync with.
        tags_raw = (request.form.get("tags") or "").strip()
        tags.set_tags_for(sub_id, [t.strip() for t in tags_raw.split(",") if t.strip()])

        updated_sub = subscriptions.get_subscription_by_id(sub_id) or sub
        return _render_actions_modal(
            updated_sub, sub_id, message=f"Subscription #{sub_id} updated.",
            headers={"HX-Trigger": "refreshSubs"},
        )

    @app.route("/subscriptions/<int:sub_id>/confirm-pause")
    def confirm_pause(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        verb = "Resume" if sub.get("paused") else "Pause"
        return render_template(
            "partials/confirm_action_modal.html",
            title=f"{verb} subscription #{sub_id}?",
            body=f"{sub.get('downloader')} / {sub.get('keywords')}",
            confirm_label=verb, confirm_variant="primary",
            post_url=f"/subscriptions/{sub_id}/toggle-pause",
            cancel_url=f"/subscriptions/{sub_id}/actions",
        )

    @app.route("/subscriptions/<int:sub_id>/confirm-force-check")
    def confirm_force_check(sub_id: int):
        sub, error_resp = _require_sub(sub_id)
        if error_resp:
            return error_resp
        return render_template(
            "partials/confirm_action_modal.html",
            title=f"Force-check subscription #{sub_id}?",
            body=f"{sub.get('downloader')} / {sub.get('keywords')}\nMarks it due now - its worker thread picks it up within a few seconds.",
            confirm_label="Force check", confirm_variant="primary",
            post_url=f"/subscriptions/{sub_id}/force-check",
            cancel_url=f"/subscriptions/{sub_id}/actions",
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
            tags.remove(sub_id)
            return "", 200, headers
        return render_template("partials/message.html", message=f"Failed: {resp.error or 'daemon rejected the request'}", error=True), 200, headers

    # ---------------------------------------------------------------- subscriptions: bulk actions

    # (button label, confirm-modal variant, past-tense verb for the success toast, executor)
    _BULK_ACTIONS = {
        "pause": ("Pause", "danger", "Paused", lambda ids: subscriptions.bulk_pause(ids, True)),
        "resume": ("Resume", "primary", "Resumed", lambda ids: subscriptions.bulk_pause(ids, False)),
        "force-check": ("Force-check", "primary", "Force-checked", subscriptions.bulk_force_recheck),
        "delete": ("Delete", "danger", "Deleted", subscriptions.bulk_delete),
    }

    def _parse_bulk_ids(raw: str) -> list[int]:
        ids = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    @app.route("/subscriptions/bulk/confirm")
    def bulk_confirm():
        action = request.args.get("action") or ""
        ids = _parse_bulk_ids(request.args.get("ids") or "")
        entry = _BULK_ACTIONS.get(action)
        if not entry or not ids:
            return render_template("partials/message.html", message="Nothing selected.", error=True)
        label, variant, _, _ = entry
        return render_template(
            "partials/confirm_action_modal.html",
            title=f"{label} {len(ids)} subscription(s)?",
            body=f"This will {label.lower()} the {len(ids)} currently-selected subscription(s).",
            confirm_label=label, confirm_variant=variant,
            post_url=f"/subscriptions/bulk/execute?action={action}&ids={','.join(str(i) for i in ids)}",
            cancel_url=None,
        )

    @app.route("/subscriptions/bulk/execute", methods=["POST"])
    def bulk_execute():
        action = request.args.get("action") or ""
        ids = _parse_bulk_ids(request.args.get("ids") or "")
        entry = _BULK_ACTIONS.get(action)
        # "subs-selection-cleared" (not camelCase) - Alpine's @eventname.window directive is an
        # HTML attribute *name*, which browsers lowercase on parse, so a camelCase custom event
        # dispatched by htmx would silently never match a camelCase Alpine listener.
        headers = {"HX-Trigger": "refreshSubs, closeModal, subs-selection-cleared"}
        if not entry or not ids:
            return render_template("partials/message.html", message="Nothing selected.", error=True), 200, headers
        _, _, past_tense, run = entry
        ok, error = run(ids)
        if ok:
            return render_template("partials/message.html", message=f"{past_tense} {len(ids)} subscription(s).", error=False), 200, headers
        return render_template("partials/message.html", message=f"Failed: {error}", error=True), 200, headers

    # ---------------------------------------------------------------- diagnostics

    def _diagnostics_ctx() -> dict:
        flagged_count, flagged_error = subscriptions.get_flagged_subscription_count()
        return dict(
            report=services.get_health_report(), flagged_count=flagged_count, flagged_error=flagged_error,
            incidents=watchdog.get_incident_history(),
        )

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

    # ------------------------------------------------------------- status tab (girly-mode home
    # for the same info the old Diagnostics modal showed - see partials/girly/status_panel.html)

    @app.route("/partials/status-panel")
    def status_panel():
        return render_template("partials/girly/status_panel.html", **_diagnostics_ctx(), cap_message=None, cap_error=False, restart_message=None)

    @app.route("/status/restart-services", methods=["POST"])
    def status_restart():
        services.start_required_services()
        return render_template(
            "partials/girly/status_panel.html", **_diagnostics_ctx(),
            cap_message=None, cap_error=False, restart_message="Restart attempted - see status above.",
        ), 200, {"HX-Trigger": "refreshSubs"}

    def _render_status_panel(updated: int, total: int, error: str | None, success_message: str) -> str:
        cap_message, cap_error = (f"Failed: {error}", True) if error else (success_message, False)
        return render_template(
            "partials/girly/status_panel.html", **_diagnostics_ctx(),
            cap_message=cap_message, cap_error=cap_error, restart_message=None,
        )

    @app.route("/status/cap-existing", methods=["POST"])
    def status_cap_existing():
        updated, total, error = subscriptions.cap_existing_subscription_file_limits()
        return _render_status_panel(
            updated, total, error,
            f"Capped {updated} of {total} subscription(s). Takes effect on each one's next check - no daemon restart needed.",
        )

    @app.route("/status/fuzz-intervals", methods=["POST"])
    def status_fuzz_intervals():
        force_all = (request.form.get("force_all") or "") == "true"
        updated, total, error = subscriptions.fuzz_existing_intervals(force_all=force_all)
        scope = "every" if force_all else "the standard-band"
        return _render_status_panel(
            updated, total, error,
            f"Re-fuzzed {updated} of {total} subscription(s) ({scope} interval(s) touched, 12-24h spread). "
            f"Takes effect on each one's next check - no daemon restart needed.",
        )

    @app.route("/status/block-video", methods=["POST"])
    def status_block_video():
        updated, total, error = subscriptions.block_video_on_existing_subscriptions()
        return _render_status_panel(
            updated, total, error,
            f"Applied the video block to {updated} of {total} subscription(s). Takes effect on each one's "
            f"next check - no daemon restart needed.",
        )

    # ---------------------------------------------------------------- settings

    @app.route("/settings")
    def settings_view():
        return render_template("partials/settings_modal.html", s=settings.load_settings(), message=None)

    @app.route("/settings/save", methods=["POST"])
    def settings_save():
        form = request.form

        def bad(message: str):
            return render_template("partials/settings_modal.html", s=settings.load_settings(), message=message, message_error=True)

        def parse_int_or_none(field: str) -> int | None:
            raw = (form.get(field) or "").strip()
            return int(raw) if raw else None

        try:
            watchdog_interval = int((form.get("watchdog_interval_seconds") or "").strip())
            if watchdog_interval < 10:
                raise ValueError
        except ValueError:
            return bad("Watchdog interval must be a whole number of seconds (at least 10).")

        hydrus_api_url = (form.get("hydrus_api_url") or "").strip()
        if not hydrus_api_url.startswith(("http://", "https://")):
            return bad("Hydrus API URL must start with http:// or https://.")

        try:
            max_files_initial = parse_int_or_none("max_files_initial")
            max_files_regular = parse_int_or_none("max_files_regular")
            if (max_files_initial is not None and max_files_initial < 1) or (max_files_regular is not None and max_files_regular < 1):
                raise ValueError
        except ValueError:
            return bad("Default file caps must be positive whole numbers, or blank for hydownloader's own (uncapped) default.")

        try:
            interval_min = float(form.get("check_interval_hours_min") or "")
            interval_max = float(form.get("check_interval_hours_max") or "")
            if interval_min <= 0 or interval_max <= interval_min:
                raise ValueError
        except ValueError:
            return bad("Check interval range must be positive numbers with min < max.")

        try:
            thresholds = {}
            for key in ("disk_pct", "ram_pct"):
                v = float(form.get(f"threshold_{key}") or "")
                if not (0 < v <= 100):
                    raise ValueError
                thresholds[key] = v
        except ValueError:
            return bad("Resource alert thresholds must be percentages between 0 and 100.")

        settings.save_settings({
            "watchdog_interval_seconds": watchdog_interval,
            "hydrus_api_url": hydrus_api_url,
            "max_files_initial": max_files_initial,
            "max_files_regular": max_files_regular,
            "check_interval_hours_min": interval_min,
            "check_interval_hours_max": interval_max,
            "resource_alert_thresholds": thresholds,
            "windows_toast_enabled": form.get("windows_toast_enabled") == "on",
        })
        return render_template("partials/settings_modal.html", s=settings.load_settings(), message="Settings saved.", message_error=False)

    # ---------------------------------------------------------------- Media (Hydrus browse/tag)
    # Search state lives server-side per browser session (see media.py's module docstring) -
    # identified by an unsigned cookie, fine for a single-user localhost dashboard. Every route
    # below re-renders the same media_browser.html panel after mutating that state, so the
    # panel's htmx target always ends up showing the current predicates/results/suggestions.

    _MEDIA_SID_COOKIE = "undertow_media_sid"
    _MEDIA_PAGE_SIZE = 48

    def _media_sid() -> tuple[str, bool]:
        """Returns (session_id, is_new) - is_new tells the caller to set the cookie on its
        response, since reading request.cookies can't itself set anything on the way out."""
        sid = request.cookies.get(_MEDIA_SID_COOKIE)
        if sid:
            return sid, False
        return uuid.uuid4().hex, True

    def _media_panel_ctx(sid: str) -> dict:
        api, reason = hydrus_client.get_hydrus_api_info()
        if not api:
            return {"reachable": False, "reason": reason}

        predicates = media.get_session_predicates(sid)
        file_ids, search_error = media.get_current_results(predicates)
        suggestions, suggest_error = media.get_suggested_tags(predicates, query=request.args.get("q", ""))
        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1
        total = len(file_ids)
        total_pages = max(1, math.ceil(total / _MEDIA_PAGE_SIZE))
        page = min(page, total_pages)
        start = (page - 1) * _MEDIA_PAGE_SIZE

        # Shape (square/portrait/landscape) is implemented as system:ratio predicate(s) mixed
        # into the same session predicate list the search actually runs against (see
        # media.set_shape_filter), but shown to the user via the shape toggle buttons instead of
        # as a removable tag pill - so it's excluded from the visible predicate pills here.
        visible_predicates = [p for p in predicates if not media.is_shape_predicate(p)]
        active_shape = media.active_shape_filter(predicates)

        # Connected-tags display (see media_browser.html): union of every active tag predicate's
        # siblings/parents/children, sorted by whole-library reference count, with a preview of
        # how many results adding each one would produce.
        connected, connected_err = media.get_connected_tags(predicates)

        return {
            "reachable": True,
            "predicates": [{"tag": p, "color": media.namespace_color(p)} for p in visible_predicates],
            "file_ids": file_ids[start:start + _MEDIA_PAGE_SIZE],
            "total": total, "page": page, "total_pages": total_pages,
            "suggestions": [{"tag": t, "count": c, "color": media.namespace_color(t)} for t, c in suggestions],
            "search_error": search_error or suggest_error or connected_err,
            "query": request.args.get("q", ""),
            "connected": connected,
            "active_shape": active_shape,
        }

    def _media_panel_response(sid: str, is_new: bool):
        resp = make_response(render_template("partials/media_browser.html", **_media_panel_ctx(sid)))
        if is_new:
            resp.set_cookie(_MEDIA_SID_COOKIE, sid, max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax")
        return resp

    @app.route("/partials/media")
    def partial_media():
        sid, is_new = _media_sid()
        return _media_panel_response(sid, is_new)

    @app.route("/media/search/add", methods=["POST"])
    def media_search_add():
        sid, is_new = _media_sid()
        predicate = (request.form.get("predicate") or "").strip()
        if predicate:
            media.add_predicate(sid, predicate)
        return _media_panel_response(sid, is_new)

    @app.route("/media/search/remove", methods=["POST"])
    def media_search_remove():
        sid, is_new = _media_sid()
        predicate = (request.form.get("predicate") or "").strip()
        if predicate:
            media.remove_predicate(sid, predicate)
        return _media_panel_response(sid, is_new)

    @app.route("/media/search/clear", methods=["POST"])
    def media_search_clear():
        sid, is_new = _media_sid()
        media.clear_predicates(sid)
        return _media_panel_response(sid, is_new)

    @app.route("/media/search/shape", methods=["POST"])
    def media_search_shape():
        """Shape (square/portrait/landscape) toggle - reruns the Hydrus search with a
        system:ratio predicate swapped in/out instead of just hiding already-loaded thumbnails
        client-side, so a full page of the requested shape actually loads."""
        sid, is_new = _media_sid()
        shape = (request.form.get("shape") or "").strip()
        current = media.active_shape_filter(media.get_session_predicates(sid))
        media.set_shape_filter(sid, None if shape == current else shape)
        return _media_panel_response(sid, is_new)

    @app.route("/media/search/suggest")
    def media_search_suggest():
        """Just the suggestion-pill fragment, refreshed as the user types in the search input -
        separate from /partials/media (which re-renders the whole panel including the grid) so
        keystrokes don't re-fetch thumbnails on every keystroke."""
        sid, _ = _media_sid()
        predicates = media.get_session_predicates(sid)
        suggestions, _err = media.get_suggested_tags(predicates, query=request.args.get("q", ""))
        return render_template(
            "partials/media_suggestions.html",
            suggestions=[{"tag": t, "count": c, "color": media.namespace_color(t)} for t, c in suggestions],
        )

    # Thumbnails/files are immutable for a given file_id (Hydrus doesn't re-thumbnail a file
    # under the same id), so the browser can cache them hard - this is what actually cuts
    # perceived Media tab load time on repeat visits: paging back and forth, or re-rendering
    # the panel after a search-predicate change, redraws thumbnails already on disk in the
    # browser's cache instead of round-tripping to Hydrus again for bytes it already has.
    _MEDIA_CACHE_HEADERS = {"Cache-Control": "public, max-age=604800, immutable"}

    @app.route("/media/thumbnail/<int:file_id>")
    def media_thumbnail(file_id: int):
        resp, err = hydrus_client.thumbnail_response(file_id)
        if resp is None:
            return "", 502
        return Response(resp.content, mimetype=resp.headers.get("Content-Type", "image/png"), headers=_MEDIA_CACHE_HEADERS)

    @app.route("/media/file/<int:file_id>")
    def media_file(file_id: int):
        resp, err = hydrus_client.file_response(file_id)
        if resp is None:
            return "", 502
        return Response(resp.content, mimetype=resp.headers.get("Content-Type", "application/octet-stream"), headers=_MEDIA_CACHE_HEADERS)

    @app.route("/media/<int:file_id>/detail")
    def media_detail(file_id: int):
        meta_resp = hydrus_client.get_file_metadata([file_id])
        if not meta_resp.success:
            return render_template("partials/media_error_modal.html", message=f"couldn't load file: {meta_resp.error}")
        entries = (meta_resp.data or {}).get("metadata") or []
        if not entries:
            return render_template("partials/media_error_modal.html", message="file not found")
        file_meta = entries[0]
        # This app only ever writes to the local tag service (see
        # hydrus_client.get_local_tag_service_key), but flatten_tags still shows tags from any
        # service the file happens to have (e.g. a PTR) rather than hiding them.
        tag_rows = sorted(
            ({"tag": t, "color": media.namespace_color(t)} for t in media.flatten_tags(file_meta)),
            key=lambda r: r["tag"],
        )
        return render_template(
            "partials/media_detail.html",
            file_id=file_id, meta=file_meta, tags=tag_rows,
        )

    @app.route("/media/<int:file_id>/similar")
    def media_similar(file_id: int):
        """Lazy-loaded strip inside the detail modal (see media_detail.html's hx-trigger="load")
        rather than computed inline with the rest of the modal - it costs several extra Hydrus
        API round trips (see media.get_similar_files), so a file the user just glances at and
        closes shouldn't pay for it if the strip hasn't rendered yet."""
        results, err = media.get_similar_files(file_id)
        return render_template("partials/media_similar.html", results=results, error=err)

    @app.route("/media/<int:file_id>/tags/add", methods=["POST"])
    def media_tags_add(file_id: int):
        tag = (request.form.get("tag") or "").strip()
        service_key, err = hydrus_client.get_local_tag_service_key()
        if tag and service_key:
            hydrus_client.add_tags([file_id], [tag], service_key)
        return media_detail(file_id)

    @app.route("/media/<int:file_id>/tags/remove", methods=["POST"])
    def media_tags_remove(file_id: int):
        tag = (request.form.get("tag") or "").strip()
        service_key, err = hydrus_client.get_local_tag_service_key()
        if tag and service_key:
            hydrus_client.delete_tags([file_id], [tag], service_key)
        return media_detail(file_id)

    # --------------------------------------------------------- tag siblings/parents (read-only)
    # Hydrus's Client API has no write endpoint for tag siblings/parents yet (confirmed live -
    # see hydrus_client.get_siblings_and_parents's docstring), so this is lookup-only. Editing
    # still has to happen in Hydrus's own client for now.

    def _tag_relationships_ctx(searched_tag: str = "") -> dict:
        ctx = {
            "searched_tag": searched_tag, "ideal_tag": "", "siblings": [], "parents": [], "children": [], "message": None,
            "map_family": None, "map_message": None, "map_depth": 2,
        }
        if not searched_tag:
            return ctx
        relationships, err = media.get_tag_relationships(searched_tag)
        if err:
            ctx["message"] = err
        else:
            ctx.update(relationships)
        return ctx

    @app.route("/partials/tag-relations")
    def partial_tag_relations():
        return render_template("partials/girly/tag_relations_tab.html", **_tag_relationships_ctx(request.args.get("tag", "").strip()))

    @app.route("/partials/tag-map")
    def partial_tag_map():
        tag = (request.args.get("tag") or "").strip()
        try:
            depth = int(request.args.get("depth", 2))
        except ValueError:
            depth = 2
        depth = max(1, min(depth, 4))
        ctx = {"map_family": None, "map_message": None, "map_depth": depth}
        if tag:
            family, err = media.get_tag_family_map(tag, depth)
            ctx["map_message"] = err
            ctx["map_family"] = family if not err else None
        return render_template("partials/girly/tag_map.html", **ctx)

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
        if ok:
            # Round-trips the just-saved key against Hydrus itself right now, rather than only
            # finding out it was mistyped later via the Diagnostics health report - called from
            # here (not inside api_keys.apply_hydrus_key) since hydrus_client.py already imports
            # api_keys at module scope, so importing hydrus_client back into api_keys.py would
            # be a circular import.
            verify = hydrus_client.verify_access_key()
            if verify.success:
                msg += " Verified - Hydrus accepted the key."
            else:
                msg += f" Warning: saved, but Hydrus didn't accept it when tested just now ({verify.error})."
                ok = False
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(message=msg, message_error=not ok, default_tab="hydrus"))

    @app.route("/api-keys/service/<service_id>/test", methods=["POST"])
    def api_keys_service_test(service_id: str):
        ok, output = api_keys.test_service_key(service_id)
        return render_template("partials/api_keys_modal.html", **_api_keys_ctx(
            tested_service_id=service_id, service_test_ok=ok, service_test_output=output, default_tab="services",
        ))

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

    # ---------------------------------------------------------------- service pill click actions
    # Backs the navbar's clickable Hydrus/Downloader/Tray/Drive status pills (see
    # templates/partials/girly/status.html) - clicking a pill restarts that service, or
    # mounts/dismounts the A: VeraCrypt drive. Synchronous, same blocking-is-fine convention as
    # the Diagnostics "Restart Services" button already uses.

    _SERVICE_RESTARTERS = {
        "hydrus": services.restart_hydrus_service,
        "systray": services.restart_systray_service,
    }

    @app.route("/services/<name>/restart", methods=["POST"])
    def service_restart(name: str):
        headers = {"HX-Trigger": "refreshSubs"}
        if name == "daemon":
            result = services.restart_daemon()
            error = None if result.success else result.error
        elif name in _SERVICE_RESTARTERS:
            error = _SERVICE_RESTARTERS[name]()
        else:
            return render_template("partials/message.html", message=f"unknown service: {name}", error=True), 200, headers
        if error:
            return render_template("partials/message.html", message=f"Restart failed: {error}", error=True), 200, headers
        return render_template("partials/message.html", message=f"{name} restarted.", error=False), 200, headers

    @app.route("/services/drive/toggle", methods=["POST"])
    def service_drive_toggle():
        headers = {"HX-Trigger": "refreshSubs"}
        drive_root = Path(config.HYDRUS_VOLUME_DRIVE + "\\")
        if drive_root.exists():
            ok = services.dismount_veracrypt_drive()
            message = "Drive dismounted." if ok else "Failed to dismount the drive - check VeraCrypt."
        else:
            ok = services.ensure_veracrypt_drive_mounted()
            message = "Drive mounted." if ok else "Failed to mount the drive - check VeraCrypt for a password prompt."
        return render_template("partials/message.html", message=message, error=not ok), 200, headers

    # ---------------------------------------------------------------- console UI fallback / shutdown

    @app.route("/launch-tui", methods=["POST"])
    def launch_tui():
        """Opens the classic console TUI in a fresh, visible console window - the fallback/
        menu option this dashboard's docstring promises. Runs `python -m undertow.tui`
        (see tui/__main__.py) rather than menu.main(), since services/watchdog are already
        running under this process - the TUI just needs to connect to them, not start them
        again. cwd is pinned to the project root so `-m undertow.tui` resolves
        regardless of whatever directory this request happened to be handled from."""
        try:
            subprocess.Popen(
                [sys.executable, "-m", "undertow.tui"],
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
                # Still exit even if the idle-stop failed (nothing left to shut down cleanly
                # otherwise) - log it so a failed stop isn't silently lost.
                logging.getLogger(__name__).exception("stop_idle_components failed during shutdown")
            sys.stdout.flush()  # os._exit() skips normal interpreter cleanup, flush included
            os._exit(0)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        return render_template(
            "partials/message.html",
            message="Shutting down - this dashboard will stop responding in a moment.", error=False,
        )

    @app.route("/shutdown-full", methods=["POST"])
    def shutdown_full():
        """Stronger version of /shutdown-app for the WebView2 app frame's window-close handler
        (UndertowLauncher.cs) - unconditionally stops the daemon, systray, and Hydrus
        itself, then dismounts the VeraCrypt volume, then exits this process. The frame POSTs
        here synchronously before closing so the work has a moment to start; it doesn't wait
        for the full response since dismounting can take a few seconds."""
        def _do_shutdown():
            try:
                services.stop_everything()
            except Exception:
                logging.getLogger(__name__).exception("stop_everything failed during full shutdown")
            sys.stdout.flush()
            os._exit(0)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        return render_template(
            "partials/message.html",
            message="Shutting down everything...", error=False,
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
