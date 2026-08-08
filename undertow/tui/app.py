"""
Main Textual application - "mission control" for the pipeline. Everything here is
presentation: actual work (subscribing, pausing, deleting, health checks, ...) is delegated to
the existing backend modules (api_client, services, subscriptions, logtail, api_keys, webui).

Layout: a HUD status strip, a prominent always-visible command legend, then a cockpit split -
subscriptions (with an inline quick-subscribe bar and instant pause/delete keys, no modal
round-trip needed for the common case) plus the colorized live daemon.txt tail on the left,
and a column of instrument panels (fleet counts, a per-site sector scan, an activity
sparkline) on the right. The instrument panels are mostly flavor - there just isn't that much
real telemetry a download queue can offer - but they're driven by real numbers where the data
exists, and the whole thing keeps moving between polls via a fast cosmetic tick so it never
looks frozen. Polling and any blocking I/O run via asyncio.to_thread so the UI never stalls.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from functools import partial

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.reactive import reactive
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .. import api_client, api_keys, hydrus_client, logtail, services, subscriptions, tags, version, webui
from ..subscriptions import add_single_subscription
from .modals import (
    AddDownloadModal,
    AddSubscriptionModal,
    ConfirmModal,
    HealthCheckModal,
    HelpModal,
    RowActionsModal,
)
from .widgets import ClipboardInput as Input

SPACESHIP_THEME = Theme(
    name="hydrus-spaceship",
    primary="#39d3ff",
    secondary="#7CFC7C",
    accent="#ff9d3c",
    warning="#ffd166",
    error="#ff4f6d",
    success="#39ff88",
    foreground="#d8f6ff",
    background="#04070a",
    surface="#0a0f16",
    panel="#0d1b22",
    dark=True,
)

_ACTIVE_SUB_RE = re.compile(r"checking subscription:\s*(\d+)")
_LOG_ERROR_RE = re.compile(r"error|traceback|failed|exception", re.I)
_LOG_WARN_RE = re.compile(r"warning", re.I)
_LOG_DOWNLOAD_RE = re.compile(r"new file|downloading|downloaded", re.I)
_LOG_CHECK_RE = re.compile(r"checking subscription|starting", re.I)

_SPARK_CHARS = "▁▂▃▄▅▆▇█"
_SCAN_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_COMMAND_DECK_TEXT = (
    "[#ff9d3c]▸[/] [b #4dfff0]A[/] SUBSCRIBE   "
    "[b #4dfff0]U[/] ONE-OFF   "
    "[b #4dfff0]/[/] FILTER   "
    "[b #4dfff0]ENTER[/] ROW ACTIONS   "
    "[b #4dfff0]P[/] PAUSE/RESUME   "
    "[b #4dfff0]F[/] FORCE CHECK   "
    "[b #ff4f6d]X[/] DELETE\n"
    "[#ff9d3c]▸[/] [b #4dfff0]H[/] HYDRUS   "
    "[b #4dfff0]Y[/] SYSTRAY   "
    "[b #4dfff0]W[/] WEB UI   "
    "[b #4dfff0]C[/] DIAGNOSTICS   "
    "[b #4dfff0]K[/] API KEYS   "
    "[b #4dfff0]R[/] REFRESH   "
    "[b #4dfff0]?[/] HELP   "
    "[b #ff4f6d]Q[/] QUIT"
)


class StatusBar(Static):
    """Top strip: Hydrus/daemon/systray up-down badges, plus the daemon's own worker status
    and queue counts - the same numbers the old console printed once per menu redraw, kept
    continuously live here instead."""

    hydrus_up = reactive(False)
    daemon_up = reactive(False)
    systray_up = reactive(False)
    api_ok = reactive(True)
    sub_status = reactive("")
    url_status = reactive("")
    urls_queued = reactive(0)
    subs_due = reactive(0)

    def render(self) -> str:
        def badge(name: str, up: bool) -> str:
            color = "#39ff88" if up else "#ff4f6d"
            state = "ONLINE" if up else "OFFLINE"
            return f"[{color}]● {name} {state}[/]"

        top = "    ".join([badge("HYDRUS", self.hydrus_up), badge("DAEMON", self.daemon_up), badge("SYSTRAY", self.systray_up)])

        if not self.api_ok:
            bottom = "[#ff4f6d]daemon API unreachable - press [b]c[/b] for diagnostics[/]"
        else:
            bottom = (
                f"[#4dfff0]sub[/] {escape(self.sub_status) or '-'}    "
                f"[#4dfff0]url[/] {escape(self.url_status) or '-'}    "
                f"[#4dfff0]queued[/] {self.urls_queued}    "
                f"[#4dfff0]due[/] {self.subs_due}"
            )
        return f"{top}\n{bottom}"


class PipelineCommands(Provider):
    """Custom command-palette entries for this app's own actions (ctrl+p), alongside
    Textual's built-in system commands (theme switcher etc.)."""

    _COMMANDS = [
        ("Subscribe to a URL / gallery / artist", "add_subscription", "Add a recurring subscription"),
        ("Queue a one-off URL download", "add_download", "Download once, no subscription"),
        ("Pause/resume selected subscription", "toggle_pause_selected", "Toggles whichever row is highlighted"),
        ("Force check selected subscription", "force_check_selected", "Marks it due now, no need to open Row Actions"),
        ("Delete selected subscription", "delete_selected", "Prompts to confirm first"),
        ("Run system diagnostics", "health_check", "Service status, daemon API, gallery-dl, file-count caps"),
        ("Configure API keys", "api_keys", "Reddit OAuth / Hydrus Client API key"),
        ("Open web dashboard", "open_web", "Opens in your browser"),
        ("Bring Hydrus to the front", "focus_hydrus", ""),
        ("Bring the systray to the front", "focus_systray", ""),
        ("Refresh now", "refresh_now", "Force an immediate status/log refresh"),
        ("Show keybindings help", "show_help", ""),
        ("Quit", "quit_app", "Shuts down anything idle, leaves the rest running"),
    ]

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for title, action, help_text in self._COMMANDS:
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), partial(self.app.run_action, action), help=help_text)


class PipelineApp(App):
    TITLE = "HYDRUS PIPELINE"
    SUB_TITLE = "◈ TARGETING UPLINK ONLINE ◈"

    COMMANDS = App.COMMANDS | {PipelineCommands}

    CSS = """
    Screen {
        background: #04070a;
    }

    Header {
        background: #071018;
        color: #4dfff0;
    }

    #status-bar {
        height: 4;
        padding: 0 2;
        background: #0a0f16;
        border-bottom: heavy #123038;
        color: #d8f6ff;
    }

    #command-deck {
        height: 3;
        padding: 0 2;
        background: #0d1017;
        border-bottom: heavy #ff9d3c;
        color: #d8f6ff;
    }

    #body {
        height: 1fr;
        padding: 1 1 0 1;
    }

    #main-col {
        width: 3fr;
        height: 100%;
    }

    #side-col {
        width: 1fr;
        min-width: 34;
        height: 100%;
        margin-left: 1;
    }

    #filter-input {
        margin-bottom: 1;
        border: round #ff9d3c;
    }

    #subs-panel {
        height: 3fr;
        border: round #17323a;
        border-title-color: #4dfff0;
        border-title-style: bold;
    }

    #quick-add-input {
        margin: 0 1;
        border: round #39ff88;
    }

    #subs-table {
        height: 1fr;
    }

    DataTable > .datatable--header {
        background: #0d1b22;
        color: #4dfff0;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #123038;
        color: #baffe0;
        text-style: bold;
    }

    #log-panel {
        height: 2fr;
        margin-top: 1;
        border: round #17323a;
        border-title-color: #4dfff0;
        border-title-style: bold;
        background: #020304;
        color: #7fffb0;
    }

    #fleet-instruments, #hydrus-stats, #sector-scan, #sparkline {
        border: round #17323a;
        border-title-color: #ff9d3c;
        border-title-style: bold;
        background: #0a0f16;
        color: #d8f6ff;
        padding: 0 1;
    }

    #fleet-instruments {
        height: 10;
    }

    #hydrus-stats {
        height: 5;
        margin-top: 1;
    }

    #sector-scan {
        height: 1fr;
        margin-top: 1;
    }

    #sparkline {
        height: 6;
        margin-top: 1;
    }

    Footer {
        background: #071018;
    }
    """

    BINDINGS = [
        Binding("a", "add_subscription", "Subscribe"),
        Binding("u", "add_download", "One-off URL"),
        Binding("p", "toggle_pause_selected", "Pause/Resume"),
        Binding("f", "force_check_selected", "Force check"),
        Binding("x", "delete_selected", "Delete"),
        Binding("/", "filter_table", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("h", "focus_hydrus", "Hydrus"),
        Binding("y", "focus_systray", "Systray"),
        Binding("w", "open_web", "Web UI"),
        Binding("c", "health_check", "Diagnostics"),
        Binding("k", "api_keys", "API keys"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("[", "prev_page", "Prev page", show=False),
        Binding("]", "next_page", "Next page", show=False),
        Binding("?", "show_help", "Help"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._subs_cache: list[dict] = []
        self._subs_by_id: dict[str, dict] = {}
        self._last_checks: dict[int, dict] = {}
        self._failure_status: dict[int, dict] = {}
        self._log_offset: int | None = None
        self._filter_text: str = ""
        # Sort/page state for the subs table - column headers toggle sort_by/reverse (see
        # _on_header_selected), [ and ] page through the (already-fetched) subs cache. Fixed
        # page size for now rather than reading Settings, per the Phase 2 note that the TUI
        # doesn't get its own settings form for v1.
        self._sort_by: str = "id"
        self._sort_reverse: bool = False
        self._page: int = 1
        self._page_size: int = 25
        self._page_meta: dict = {"page": 1, "page_size": self._page_size, "total": 0, "total_pages": 1}
        self._activity_history: deque[int] = deque(maxlen=48)
        self._start_time = time.monotonic()
        self._frame = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield StatusBar(id="status-bar")
        yield Static(_COMMAND_DECK_TEXT, id="command-deck")
        with Horizontal(id="body"):
            with Vertical(id="main-col"):
                yield Input(placeholder="filter subscriptions by keywords/downloader/id...", id="filter-input")
                with Vertical(id="subs-panel"):
                    yield Input(placeholder="+ quick-subscribe: paste a URL, hit Enter (24h interval)...", id="quick-add-input")
                    yield DataTable(id="subs-table", cursor_type="row", zebra_stripes=True)
                yield RichLog(id="log-panel", markup=True, wrap=True, max_lines=1000, auto_scroll=True)
            with Vertical(id="side-col"):
                yield Static(id="fleet-instruments")
                yield Static(id="hydrus-stats")
                yield Static(id="sector-scan")
                yield Static(id="sparkline")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(SPACESHIP_THEME)
        self.theme = "hydrus-spaceship"

        v = version.get_version_info()
        status_flag = {"current": "◈ UP TO DATE ◈", "stale": "⚠ UPDATE AVAILABLE ⚠", "unknown": "◈ TARGETING UPLINK ONLINE ◈"}[v["status"]]
        self.sub_title = f"v{v['version']} ({v['commit']}{'+' if v['dirty'] else ''}) — {status_flag}"

        table = self.query_one("#subs-table", DataTable)
        # Explicit keys (matching subscriptions.SORT_KEYS names where a sort exists) rather
        # than relying on Textual's label-derived default keys - "Last result"/"New Files"
        # deliberately get no matching SORT_KEYS entry, so clicking those headers is a no-op
        # in _on_header_selected rather than sorting by something that doesn't exist.
        table.add_column("ID", key="id")
        table.add_column("Downloader", key="downloader")
        table.add_column("Keywords", key="keywords")
        table.add_column("Paused", key="paused")
        table.add_column("Due", key="due")
        table.add_column("Last result", key="last_result")
        table.add_column("New Files", key="new_files")
        table.focus()

        self.query_one("#subs-panel", Vertical).border_title = "SUBSCRIPTIONS"
        self.query_one("#log-panel", RichLog).border_title = "LIVE ACTIVITY — daemon.txt"
        self.query_one("#log-panel", RichLog).write("[#4dfff0]waiting for the first status poll...[/]")
        self.query_one("#fleet-instruments", Static).border_title = "FLEET STATUS"
        self.query_one("#hydrus-stats", Static).border_title = "HYDRUS"
        self.query_one("#sector-scan", Static).border_title = "SECTOR SCAN"
        self.query_one("#sparkline", Static).border_title = "ACTIVITY"

        self.query_one("#filter-input", Input).display = False

        self._render_fleet_instruments()
        self.query_one("#hydrus-stats", Static).update("[dim]gathering telemetry...[/]")
        self._render_sector_scan()
        self._render_sparkline()

        self.set_interval(1.5, self._tick)
        self.set_interval(0.4, self._cosmetic_tick)
        # New-files-per-sub needs a full /get_subscription_checks fetch (there's no "latest
        # only" filter upstream - see subscriptions.get_latest_checks), which is too heavy to
        # do on the fast 1.5s tick above for installs with a lot of check history. A much
        # slower dedicated interval keeps the "New Files" column live without hammering the
        # daemon on every poll.
        self.set_interval(12.0, self._check_tick)
        self.set_interval(10.0, self._hydrus_tick)
        self.call_later(self._tick)
        self.call_later(self._check_tick)
        self.call_later(self._hydrus_tick)

    # ------------------------------------------------------------------ polling

    @staticmethod
    def _fetch_backend(log_offset: int | None):
        svc_status = services.get_service_status()
        subs_resp = api_client.get_subscriptions()
        status_resp = api_client.get_status_info()
        new_lines, new_offset = logtail.read_since(log_offset)
        return svc_status, subs_resp, status_resp, new_lines, new_offset

    async def _tick(self) -> None:
        try:
            svc_status, subs_resp, status_resp, new_lines, new_offset = await asyncio.to_thread(
                self._fetch_backend, self._log_offset
            )
        except Exception as e:  # never let a bad poll kill the timer loop
            self.query_one("#log-panel", RichLog).write(f"[#ff4f6d]poll error: {escape(str(e))}[/]")
            return
        self._log_offset = new_offset
        self._apply_service_status(svc_status)
        self._apply_worker_status(status_resp)
        self._apply_subs(subs_resp)
        self._apply_log(new_lines)

    def _cosmetic_tick(self) -> None:
        """Advances the scanner spinner / pulse indicator independent of the (slower) network
        poll, purely so the cockpit never looks frozen between real updates."""
        self._frame += 1
        self._render_fleet_instruments()

    async def _check_tick(self) -> None:
        """Refreshes the "New Files" column's data on its own slow interval (see on_mount) -
        deliberately decoupled from _tick() since it fetches full check history per visible
        subscription (subscriptions.get_latest_checks), not just the cheap /get_subscriptions
        summary. Runs before the first _subs_cache is populated on app startup (both are
        call_later'd together) - that's harmless, it just no-ops with an empty ID list and
        catches up 12s later."""
        ids = [s.get("id") for s in self._subs_cache if s.get("id") is not None]
        if not ids:
            return
        try:
            self._last_checks = await asyncio.to_thread(subscriptions.get_latest_checks, ids)
            self._failure_status = await asyncio.to_thread(subscriptions.get_failure_status, self._subs_cache)
        except Exception:
            return  # supplementary display data - a failed poll here shouldn't disrupt anything
        self._render_table()

    async def _hydrus_tick(self) -> None:
        """Hydrus's own file/inbox counts - decoupled from _check_tick (which no-ops with no
        subscriptions yet) since this has nothing to do with subscriptions at all, and from a
        separate 10s interval rather than the fast _tick since it's two full search_files round
        trips to Hydrus, not a cheap status poll."""
        try:
            stats = await asyncio.to_thread(hydrus_client.get_hydrus_stats)
        except Exception:
            return  # supplementary display data - a failed poll here shouldn't disrupt anything
        widget = self.query_one("#hydrus-stats", Static)
        if not stats.reachable:
            widget.update(f"[dim]unavailable - {escape(stats.error or 'unknown')}[/]")
            return
        widget.update(
            f"[#39ff88]TOTAL FILES[/]  {stats.total_files}\n"
            f"[#39d3ff]INBOX[/]        {stats.inbox_count}"
        )

    def _apply_service_status(self, s) -> None:
        bar = self.query_one("#status-bar", StatusBar)
        bar.hydrus_up = s.hydrus_running
        bar.daemon_up = s.daemon_running
        bar.systray_up = s.systray_running

    def _apply_worker_status(self, status_resp: api_client.ApiResult) -> None:
        bar = self.query_one("#status-bar", StatusBar)
        if status_resp.success and status_resp.data:
            d = status_resp.data
            bar.api_ok = True
            bar.sub_status = str(d.get("subscription_worker_status") or "")
            bar.url_status = str(d.get("url_worker_status") or "")
            bar.urls_queued = d.get("urls_queued") or 0
            bar.subs_due = d.get("subscriptions_due") or 0
            self._activity_history.append(bar.urls_queued + bar.subs_due)
        else:
            bar.api_ok = False
            self._activity_history.append(0)
        self._render_sparkline()

    def _active_sub_id(self) -> str | None:
        bar = self.query_one("#status-bar", StatusBar)
        m = _ACTIVE_SUB_RE.search(bar.sub_status or "")
        return m.group(1) if m else None

    def _apply_subs(self, subs_resp: api_client.ApiResult) -> None:
        if not subs_resp.success:
            return  # keep showing the last known table; the status bar already flags the outage
        self._subs_cache = sorted(subs_resp.data or [], key=lambda s: s.get("id", 0))
        self._subs_by_id = {str(s.get("id")): s for s in self._subs_cache}
        self._render_table()
        self._render_fleet_instruments()
        self._render_sector_scan()

    def _apply_log(self, new_lines: list[str]) -> None:
        if not new_lines:
            return
        log_panel = self.query_one("#log-panel", RichLog)
        for line in new_lines:
            log_panel.write(self._colorize_log_line(line))

    @staticmethod
    def _colorize_log_line(line: str) -> str:
        if _LOG_ERROR_RE.search(line):
            color = "#ff4f6d"
        elif _LOG_WARN_RE.search(line):
            color = "#ffd166"
        elif _LOG_DOWNLOAD_RE.search(line):
            color = "#39ff88"
        elif _LOG_CHECK_RE.search(line):
            color = "#39d3ff"
        else:
            color = "#8aa0a8"
        return f"[{color}]{escape(line)}[/]"

    # ------------------------------------------------------------------ instrument panels
    # Mostly flavor - a download queue doesn't have a ton of genuinely useful telemetry - but
    # grounded in real numbers where the data exists, and kept moving via the cosmetic tick so
    # the cockpit doesn't look like a frozen screenshot between polls.

    def _render_fleet_instruments(self) -> None:
        counts = subscriptions.fleet_counts(self._subs_cache)
        total, active, paused, due = counts["total"], counts["active"], counts["paused"], counts["due"]

        uptime = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)

        spinner = _SCAN_FRAMES[self._frame % len(_SCAN_FRAMES)]
        pulse = "#39ff88" if self._frame % 2 == 0 else "#0d3a24"

        pm = self._page_meta
        sort_arrow = "▼" if self._sort_reverse else "▲"
        text = (
            f"[#4dfff0]TOTAL[/]    {total}\n"
            f"[#39ff88]ACTIVE[/]   {active}\n"
            f"[#ffd166]PAUSED[/]   {paused}\n"
            f"[#39d3ff]DUE NOW[/]  {due}\n"
            f"\n"
            f"[#4dfff0]SORT[/]     {self._sort_by} {sort_arrow}\n"
            f"[#4dfff0]PAGE[/]     {pm['page']}/{pm['total_pages']}  ([ / ])\n"
            f"\n"
            f"[#ff9d3c]{spinner}[/] SCANNING...\n"
            f"[{pulse}]●[/] UPLINK  {h:02}:{m:02}:{s:02}"
        )
        self.query_one("#fleet-instruments", Static).update(text)

    def _render_sector_scan(self) -> None:
        widget = self.query_one("#sector-scan", Static)
        if not self._subs_cache:
            widget.update("[dim]no contacts on scope[/]")
            return
        top = subscriptions.top_downloaders(self._subs_cache)
        max_count = max((c for _, c in top), default=1) or 1
        lines = []
        for name, count in top:
            bar_len = max(1, round(count / max_count * 12))
            bar = "▓" * bar_len + "░" * (12 - bar_len)
            label = name if len(name) <= 11 else name[:10] + "…"
            lines.append(f"[#4dfff0]{label:<11}[/] [#39ff88]{bar}[/] {count}")
        widget.update("\n".join(lines))

    def _render_sparkline(self) -> None:
        widget = self.query_one("#sparkline", Static)
        hist = list(self._activity_history)
        if not hist:
            widget.update("[dim]gathering telemetry...[/]")
            return
        lo, hi = min(hist), max(hist)
        span = max(hi - lo, 1)
        spark = "".join(_SPARK_CHARS[int((v - lo) / span * (len(_SPARK_CHARS) - 1))] for v in hist)
        widget.update(f"[#39d3ff]{spark}[/]\npeak {hi}   now {hist[-1]}")

    # ------------------------------------------------------------------ table rendering

    def _current_row_key(self, table: DataTable) -> str | None:
        if not table.row_count:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def _restore_row_key(self, table: DataTable, key: str | None) -> None:
        if key is None or not table.row_count:
            return
        try:
            idx = table.get_row_index(key)
            table.cursor_coordinate = Coordinate(idx, table.cursor_coordinate.column)
        except Exception:
            pass

    def _get_selected_sub(self) -> dict | None:
        table = self.query_one("#subs-table", DataTable)
        key = self._current_row_key(table)
        if key is None:
            return None
        return self._subs_by_id.get(key)

    def _render_table(self) -> None:
        table = self.query_one("#subs-table", DataTable)
        selected_key = self._current_row_key(table)
        active_id = self._active_sub_id()
        filt = self._filter_text.lower()

        # Filtered before sorting/paginating (unlike the web dashboard's client-side substring
        # filter, which only searches the currently-fetched page) - the TUI already holds the
        # full subs list locally, so there's no reason to let a filter miss matches sitting on
        # another page.
        candidates = self._subs_cache
        tags_by_id = tags.load_tags()
        if filt.startswith("tag:"):
            tag_query = filt[4:].strip()
            candidates = [
                s for s in candidates
                if any(tag_query in t.lower() for t in tags_by_id.get(s.get("id"), []))
            ] if tag_query else candidates
        elif filt:
            candidates = [
                s for s in candidates
                if filt in str(s.get("keywords") or "").lower()
                or filt in str(s.get("downloader") or "").lower()
                or filt in str(s.get("id"))
            ]
        ordered = subscriptions.sort_subscriptions(candidates, self._sort_by, "desc" if self._sort_reverse else "asc")
        page_items, page_meta = subscriptions.paginate(ordered, self._page, self._page_size)
        self._page = page_meta["page"]  # clamped back into range if the list shrank underneath it
        self._page_meta = page_meta

        table.clear()
        for s in page_items:
            sid = str(s.get("id"))
            keywords = str(s.get("keywords") or "")
            downloader = str(s.get("downloader") or "")

            is_active = sid == active_id
            flagged = bool((self._failure_status.get(s.get("id")) or {}).get("flagged"))
            warn_prefix = "[#ff9d3c]![/] " if flagged else ""
            sub_tags = tags_by_id.get(s.get("id"), [])
            tag_suffix = "  " + " ".join(f"[#ff9d3c]#{escape(t)}[/]" for t in sub_tags) if sub_tags else ""
            if is_active:
                kw_cell: object = Text.from_markup(f"{warn_prefix}[#ffe08a]▶ {escape(keywords)}[/]{tag_suffix}")
            elif flagged or sub_tags:
                kw_cell = Text.from_markup(f"{warn_prefix}{escape(keywords)}{tag_suffix}")
            else:
                kw_cell = keywords
            paused_cell = Text.from_markup("[#ffd166]yes[/]") if s.get("paused") else "no"
            due_cell = Text.from_markup("[#39d3ff]yes[/]") if s.get("is_due") else "no"
            last_result = str(s.get("last_result_status") or "")
            if last_result.lower() == "ok":
                lr_cell: object = Text.from_markup(f"[#39ff88]{escape(last_result)}[/]")
            elif last_result:
                lr_cell = Text.from_markup(f"[#ff9d3c]{escape(last_result)}[/]")
            else:
                lr_cell = "-"

            last_check = self._last_checks.get(s.get("id")) or {}
            new_files = last_check.get("new_files")
            if new_files is None:
                nf_cell: object = "-"
            elif new_files > 0:
                nf_cell = Text.from_markup(f"[#39ff88]{new_files}[/]")
            else:
                nf_cell = str(new_files)

            table.add_row(sid, downloader, kw_cell, paused_cell, due_cell, lr_cell, nf_cell, key=sid)

        self._restore_row_key(table, selected_key)

    # ------------------------------------------------------------------ events

    @on(Input.Changed, "#filter-input")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter_text = event.value.strip()
        self._render_table()

    @on(Input.Submitted, "#quick-add-input")
    def _on_quick_add(self, event: Input.Submitted) -> None:
        url = event.value.strip()
        if not url:
            return
        self.run_worker(self._quick_add_flow(url), exclusive=True)

    async def _quick_add_flow(self, url: str) -> None:
        quick_input = self.query_one("#quick-add-input", Input)
        quick_input.disabled = True
        result = await asyncio.to_thread(add_single_subscription, url, 24.0)
        quick_input.disabled = False
        if result.status == "Added":
            self.notify(f"UPLINK ESTABLISHED — tracking {result.detail}")
            if result.restarted_daemon:
                self.notify("New site - daemon restarted automatically to start checking it.")
            elif result.restart_error:
                self.notify(
                    f"Added, but the automatic restart to activate it failed: {result.restart_error} "
                    f"- check diagnostics (key c).",
                    severity="warning",
                )
            quick_input.value = ""
            await self._tick()
        elif result.status == "Skipped":
            self.notify(f"Already tracked: {result.detail}", severity="warning")
        else:
            self.notify(f"Failed: {result.detail}", severity="error")
        quick_input.focus()

    @on(DataTable.HeaderSelected, "#subs-table")
    def _on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        col = event.column_key.value if event.column_key else None
        if col not in subscriptions.SORT_KEYS:
            return  # "Last result"/"New Files" have no matching sort key - no-op, not an error
        self._sort_reverse = (col == self._sort_by) and not self._sort_reverse
        self._sort_by = col
        self._page = 1
        self._render_table()
        self._render_fleet_instruments()

    def action_prev_page(self) -> None:
        if self._page > 1:
            self._page -= 1
            self._render_table()
            self._render_fleet_instruments()

    def action_next_page(self) -> None:
        if self._page < self._page_meta.get("total_pages", 1):
            self._page += 1
            self._render_table()
            self._render_fleet_instruments()

    @on(DataTable.RowSelected, "#subs-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key.value if event.row_key else None
        if row_key is None:
            return
        sub = self._subs_by_id.get(row_key)
        if not sub:
            return
        # push_screen_wait() must run inside an actual Textual worker (it raises
        # NoActiveWorker otherwise) - run_worker() is what creates that context. Every modal
        # launch in this app goes through a *_flow() coroutine wrapped in run_worker() for
        # exactly this reason.
        self.run_worker(self._row_actions_flow(sub), exclusive=True)

    async def _row_actions_flow(self, sub: dict) -> None:
        changed = await self.push_screen_wait(RowActionsModal(sub))
        if changed:
            await self._tick()

    # ------------------------------------------------------------------ actions

    def action_add_subscription(self) -> None:
        self.run_worker(self._add_subscription_flow(), exclusive=True)

    async def _add_subscription_flow(self) -> None:
        changed = await self.push_screen_wait(AddSubscriptionModal())
        if changed:
            await self._tick()

    def action_add_download(self) -> None:
        self.run_worker(self.push_screen_wait(AddDownloadModal()), exclusive=True)

    def _selected_sub_for_hotkey(self) -> dict | None:
        """Shared guard for the single-key row actions (p/f/x): lets the key type normally into
        whichever input box is focused instead of hijacking it as a hotkey, then notifies and
        bails if nothing's selected. Returns None in either case - callers just check for that."""
        if self.query_one("#quick-add-input", Input).has_focus or self.query_one("#filter-input", Input).has_focus:
            return None
        sub = self._get_selected_sub()
        if not sub:
            self.notify("No subscription selected.", severity="warning")
            return None
        return sub

    def action_toggle_pause_selected(self) -> None:
        sub = self._selected_sub_for_hotkey()
        if sub is None:
            return
        self.run_worker(self._toggle_pause_flow(sub), exclusive=True)

    async def _toggle_pause_flow(self, sub: dict) -> None:
        sub_id = sub.get("id")
        new_paused = not bool(sub.get("paused"))
        resp = await asyncio.to_thread(api_client.add_or_update_subscriptions, [{"id": sub_id, "paused": new_paused}])
        if resp.accepted:
            verb = "STANDBY — target paused" if new_paused else "WEAPONS HOT — target resumed"
            self.notify(f"{verb}: #{sub_id}")
            await self._tick()
        else:
            self.notify(f"Failed: {resp.error or 'daemon rejected the request'}", severity="error")

    def action_force_check_selected(self) -> None:
        sub = self._selected_sub_for_hotkey()
        if sub is None:
            return
        self.run_worker(self._force_check_flow(sub), exclusive=True)

    async def _force_check_flow(self, sub: dict) -> None:
        sub_id = sub.get("id")
        ok, error = await asyncio.to_thread(subscriptions.force_recheck, sub_id)
        if ok:
            self.notify(f"Subscription #{sub_id} marked due - its worker thread will pick it up within a few seconds.")
            await self._tick()
        else:
            self.notify(f"Failed: {error}", severity="error")

    def action_delete_selected(self) -> None:
        sub = self._selected_sub_for_hotkey()
        if sub is None:
            return
        self.run_worker(self._delete_selected_flow(sub), exclusive=True)

    async def _delete_selected_flow(self, sub: dict) -> None:
        sub_id = sub.get("id")
        confirmed = await self.push_screen_wait(
            ConfirmModal(
                f"Delete subscription #{sub_id}? This only removes it from hydownloader - "
                f"already-downloaded files are unaffected.",
                confirm_label="Delete",
            )
        )
        if not confirmed:
            return
        ok, error = await asyncio.to_thread(subscriptions.bulk_delete, [sub_id])
        if ok:
            self.notify(f"TARGET NEUTRALIZED — subscription #{sub_id} purged")
            await self._tick()
        else:
            self.notify(f"Failed: {error}", severity="error")

    def action_filter_table(self) -> None:
        filter_input = self.query_one("#filter-input", Input)
        filter_input.display = True
        filter_input.focus()

    def action_clear_filter(self) -> None:
        filter_input = self.query_one("#filter-input", Input)
        if filter_input.display:
            filter_input.value = ""
            filter_input.display = False
            self._filter_text = ""
            self._render_table()
            self.query_one("#subs-table", DataTable).focus()

    async def action_focus_hydrus(self) -> None:
        ok = await asyncio.to_thread(services.show_process_window, "hydrus_client")
        if not ok:
            self.notify("Hydrus window not found - is it running?", severity="warning")

    async def action_focus_systray(self) -> None:
        ok = await asyncio.to_thread(services.show_process_window, "hydownloader-systray")
        if not ok:
            self.notify("Systray window not found - is it running?", severity="warning")

    def action_open_web(self) -> None:
        port = webui.run_webui()
        if port is None:
            self.notify("Web dashboard needs the 'flask' package - pip install -r requirements.txt", severity="error")
        else:
            self.notify(f"Web dashboard open at http://127.0.0.1:{port}")

    def action_health_check(self) -> None:
        self.run_worker(self.push_screen_wait(HealthCheckModal()), exclusive=True)

    def action_api_keys(self) -> None:
        # Reddit OAuth needs a real interactive console (browser popup + pasting tokens back)
        # which doesn't map cleanly onto a modal form - suspend the TUI and hand the terminal
        # to the existing console flow instead of reinventing it as Textual widgets.
        with self.suspend():
            print()
            api_keys.run()

    async def action_refresh_now(self) -> None:
        await self._tick()
        self.notify("Refreshed.")

    def action_show_help(self) -> None:
        self.run_worker(self.push_screen_wait(HelpModal()), exclusive=True)

    def action_quit_app(self) -> None:
        self.exit()


def run() -> None:
    PipelineApp().run()
