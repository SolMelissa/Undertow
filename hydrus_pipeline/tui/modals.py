"""
Modal dialogs for the Textual TUI - add subscription, one-off download, row actions (pause/
resume/delete/force-check/history), health check, confirm, and help. Each one drives the
existing backend functions (api_client, subscriptions, services) directly via
asyncio.to_thread, since those are synchronous/blocking (requests calls, file I/O) and must
not run on Textual's event loop thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from rich.markup import escape
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static

from .. import api_client, services, subscriptions, watchdog
from ..subscriptions import add_single_subscription
from .widgets import ClipboardInput as Input

_RESULT_COLORS = {"Added": "#39ff88", "Skipped": "#ffd166", "Failed": "#ff4f6d"}


class ConfirmModal(ModalScreen[bool]):
    """Generic yes/no confirmation. Returns True if confirmed, False otherwise (Cancel or
    Escape)."""

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal > #dialog {
        width: 62; height: auto; padding: 1 2;
        border: heavy $error; background: $surface;
    }
    ConfirmModal #question { margin-bottom: 1; }
    ConfirmModal #buttons { height: auto; align: right middle; }
    ConfirmModal Button { margin-left: 1; }
    """

    def __init__(self, question: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.question = question
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(escape(self.question), id="question")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant="error")

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(False)


class RowActionsModal(ModalScreen[bool]):
    """Opened by pressing Enter (or clicking) on a subscription row - one place for pause/
    resume/delete/force-check/history instead of memorizing separate keybindings or typing an
    ID by hand, which is what the old console menu forced. Returns True if anything actually
    changed, so the caller knows to refresh immediately instead of waiting for the next poll
    tick."""

    DEFAULT_CSS = """
    RowActionsModal { align: center middle; }
    RowActionsModal > #dialog {
        width: 70; height: auto; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    RowActionsModal #keywords { color: $text-muted; margin-bottom: 1; }
    RowActionsModal #meta { margin-bottom: 1; }
    RowActionsModal #last-run { color: $text-muted; margin-bottom: 1; }
    RowActionsModal #buttons { height: auto; align: right middle; }
    RowActionsModal Button { margin-left: 1; margin-bottom: 1; }
    """

    def __init__(self, sub: dict) -> None:
        super().__init__()
        self.sub = sub

    def compose(self) -> ComposeResult:
        sub_id = self.sub.get("id")
        downloader = self.sub.get("downloader")
        keywords = self.sub.get("keywords")
        paused = bool(self.sub.get("paused"))
        last_result = self.sub.get("last_result_status") or "-"
        with Vertical(id="dialog"):
            yield Static(f"[b]#{sub_id}[/b]  {escape(str(downloader))}", id="title")
            yield Static(escape(str(keywords)), id="keywords")
            status_markup = "[#ffd166]paused[/]" if paused else "[#39ff88]active[/]"
            yield Static(f"Status: {status_markup}    Last result: {escape(str(last_result))}", id="meta")
            yield Static("Last run: checking...", id="last-run")
            with Horizontal(id="buttons"):
                yield Button("Resume" if paused else "Pause", id="toggle-pause", variant="success" if paused else "warning")
                yield Button("Force check", id="force-check")
                yield Button("Edit", id="edit")
                yield Button("History", id="history")
                yield Button("Delete", id="delete", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.run_worker(self._load_last_run(), exclusive=True)

    async def _load_last_run(self) -> None:
        sub_id = self.sub.get("id")
        latest = await asyncio.to_thread(subscriptions.get_latest_checks, [sub_id])
        check = latest.get(sub_id)
        last_run = self.query_one("#last-run", Static)
        if not check:
            last_run.update("Last run: no recorded checks yet")
            return
        new_files = check.get("new_files")
        skipped = check.get("already_seen_files")
        finished = check.get("time_finished")
        when = datetime.fromtimestamp(finished).strftime("%Y-%m-%d %H:%M") if finished else "?"
        last_run.update(
            f"Last run ({when}): [#39ff88]{new_files if new_files is not None else '-'} new[/], "
            f"{skipped if skipped is not None else '-'} already seen"
        )

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#toggle-pause")
    async def _toggle_pause(self) -> None:
        sub_id = self.sub.get("id")
        new_paused = not bool(self.sub.get("paused"))
        resp = await asyncio.to_thread(api_client.add_or_update_subscriptions, [{"id": sub_id, "paused": new_paused}])
        if resp.accepted:
            self.app.notify(f"Subscription #{sub_id} {'paused' if new_paused else 'resumed'}.")
            self.dismiss(True)
        else:
            self.app.notify(f"Failed: {resp.error or 'daemon rejected the request'}", severity="error")

    @on(Button.Pressed, "#force-check")
    async def _force_check(self) -> None:
        sub_id = self.sub.get("id")
        ok, error = await asyncio.to_thread(subscriptions.force_recheck, sub_id)
        if ok:
            self.app.notify(
                f"Subscription #{sub_id} marked due - its worker thread will pick it up within "
                f"a few seconds, no restart needed."
            )
            self.dismiss(True)
        else:
            self.app.notify(f"Failed: {error}", severity="error")

    @on(Button.Pressed, "#edit")
    def _edit(self) -> None:
        # push_screen_wait() must run inside an actual Textual worker (raises NoActiveWorker
        # otherwise) - run_worker() provides that context.
        self.run_worker(self._edit_flow(), exclusive=True)

    async def _edit_flow(self) -> None:
        changed = await self.app.push_screen_wait(EditSubscriptionModal(self.sub))
        if changed:
            self.dismiss(True)

    @on(Button.Pressed, "#history")
    def _history(self) -> None:
        # push_screen_wait() must run inside an actual Textual worker (raises NoActiveWorker
        # otherwise) - run_worker() provides that context.
        self.run_worker(self._history_flow(), exclusive=True)

    async def _history_flow(self) -> None:
        await self.app.push_screen_wait(CheckHistoryModal(self.sub))

    @on(Button.Pressed, "#delete")
    def _delete(self) -> None:
        # push_screen_wait() must run inside an actual Textual worker (raises NoActiveWorker
        # otherwise) - run_worker() provides that context.
        self.run_worker(self._delete_flow(), exclusive=True)

    async def _delete_flow(self) -> None:
        sub_id = self.sub.get("id")
        confirmed = await self.app.push_screen_wait(
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
            self.app.notify(f"Subscription #{sub_id} deleted.")
            self.dismiss(True)
        else:
            self.app.notify(f"Failed: {error}", severity="error")

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(False)


class EditSubscriptionModal(ModalScreen[bool]):
    """Edits keywords/check interval/file caps/filter on an existing subscription in place -
    the alternative to deleting and re-adding, which loses check history since a re-add is a
    brand new row. Downloader/site isn't editable here (see subscriptions.update_subscription's
    docstring for why). Returns True if the edit actually saved, so RowActionsModal knows to
    refresh."""

    DEFAULT_CSS = """
    EditSubscriptionModal { align: center middle; }
    EditSubscriptionModal > #dialog {
        width: 78; height: auto; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    EditSubscriptionModal #hint { color: $text-muted; margin-bottom: 1; }
    EditSubscriptionModal Input { margin-bottom: 1; }
    EditSubscriptionModal #buttons { height: auto; align: right middle; }
    EditSubscriptionModal Button { margin-left: 1; }
    """

    def __init__(self, sub: dict) -> None:
        super().__init__()
        self.sub = sub

    def compose(self) -> ComposeResult:
        sub_id = self.sub.get("id")
        downloader = self.sub.get("downloader")
        interval = self.sub.get("check_interval")
        hours = round(interval / 3600, 2) if interval else ""
        with Vertical(id="dialog"):
            yield Static(f"[b]EDIT #{sub_id}[/b]  {escape(str(downloader))}", id="title")
            yield Static(
                "Downloader/site can't be changed here - delete and re-add if this is on the "
                "wrong site entirely. Leave a file-limit blank to remove that cap; leave the "
                "filter blank to allow every file type through.",
                id="hint",
            )
            yield Input(value=str(self.sub.get("keywords") or ""), placeholder="keywords", id="keywords-input")
            yield Input(value=str(hours), placeholder="check interval in hours", id="hours-input")
            yield Input(value=str(self.sub.get("max_files_initial") or ""), placeholder="max files (initial check, blank = unlimited)", id="max-initial-input")
            yield Input(value=str(self.sub.get("max_files_regular") or ""), placeholder="max files (regular check, blank = unlimited)", id="max-regular-input")
            yield Input(value=str(self.sub.get("filter") or ""), placeholder="file filter (blank = allow everything)", id="filter-input")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#keywords-input", Input).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#save")
    async def _save(self) -> None:
        sub_id = self.sub.get("id")
        keywords = self.query_one("#keywords-input", Input).value.strip()
        if not keywords:
            self.app.notify("Keywords can't be empty.", severity="warning")
            return

        try:
            hours = float(self.query_one("#hours-input", Input).value.strip())
            if hours <= 0:
                raise ValueError
        except ValueError:
            self.app.notify("Check interval must be a positive number of hours.", severity="warning")
            return

        def parse_cap(widget_id: str) -> int | None:
            raw = self.query_one(widget_id, Input).value.strip()
            return int(raw) if raw else None

        try:
            max_initial = parse_cap("#max-initial-input")
            max_regular = parse_cap("#max-regular-input")
        except ValueError:
            self.app.notify("File limits must be whole numbers.", severity="warning")
            return

        file_filter = self.query_one("#filter-input", Input).value.strip()

        ok, error = await asyncio.to_thread(
            subscriptions.update_subscription, sub_id,
            keywords=keywords, check_interval_hours=hours,
            max_files_initial=max_initial, max_files_regular=max_regular, file_filter=file_filter,
        )
        if ok:
            self.app.notify(f"Subscription #{sub_id} updated.")
            self.dismiss(True)
        else:
            self.app.notify(f"Failed: {error}", severity="error")

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(False)


class CheckHistoryModal(ModalScreen[None]):
    """Detailed per-run history for one subscription (Row Actions -> "History") - the daemon's
    subscription_checks table, which is more than the single "last result" word the main table
    and row summary can show. Loaded on mount via asyncio.to_thread since it's a network call."""

    DEFAULT_CSS = """
    CheckHistoryModal { align: center middle; }
    CheckHistoryModal > #dialog {
        width: 96; height: auto; max-height: 90%; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    CheckHistoryModal #hint { color: $text-muted; margin-bottom: 1; }
    CheckHistoryModal #rows { height: auto; max-height: 20; border: round $panel; }
    CheckHistoryModal #buttons { height: auto; align: right middle; margin-top: 1; }
    CheckHistoryModal Button { margin-left: 1; }
    """

    def __init__(self, sub: dict) -> None:
        super().__init__()
        self.sub = sub

    def compose(self) -> ComposeResult:
        sub_id = self.sub.get("id")
        downloader = self.sub.get("downloader")
        keywords = self.sub.get("keywords")
        with Vertical(id="dialog"):
            yield Static(f"[b]CHECK HISTORY[/b] — #{sub_id} {escape(str(downloader))}", id="title")
            yield Static(escape(str(keywords)), id="hint")
            yield RichLog(id="rows", markup=True, wrap=True, max_lines=200)
            with Horizontal(id="buttons"):
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        rows_widget = self.query_one("#rows", RichLog)
        rows_widget.write("[#4dfff0]loading...[/]")
        sub_id = self.sub.get("id")
        rows, error = await asyncio.to_thread(subscriptions.get_check_history, sub_id)
        rows_widget.clear()
        if error:
            rows_widget.write(f"[#ff4f6d]Failed to load history: {escape(error)}[/]")
            return
        if not rows:
            rows_widget.write("[dim]no recorded checks yet[/]")
            return
        for i, check in enumerate(rows):
            started = check.get("time_started")
            finished = check.get("time_finished")
            status = str(check.get("status") or "")
            new_files = check.get("new_files")
            skipped = check.get("already_seen_files")
            when = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S") if started else "?"
            duration = f"{finished - started:.0f}s" if started and finished else "?"
            ok = status.lower() == "ok" or not status
            color = "#39ff88" if ok else "#ff4f6d"
            rows_widget.write(
                f"[{color}]{when}[/]  ({duration})  new: {new_files if new_files is not None else '-'}  "
                f"already seen: {skipped if skipped is not None else '-'}  "
                f"status: {escape(status) if status else 'ok'}"
            )
            # only the newest row's log file is still around to explain (see
            # subscriptions.explain_check_error), so only bother checking it
            if i == 0 and not ok:
                detail = await asyncio.to_thread(subscriptions.explain_check_error, sub_id)
                if detail:
                    rows_widget.write(f"  [dim]↳ {escape(detail)}[/]")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class AddSubscriptionModal(ModalScreen[bool]):
    """Subscribe to one URL, or several comma-separated at once. Returns True if at least one
    subscription was actually added, so the app knows to refresh immediately."""

    DEFAULT_CSS = """
    AddSubscriptionModal { align: center middle; }
    AddSubscriptionModal > #dialog {
        width: 78; height: auto; max-height: 90%; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    AddSubscriptionModal #hint { color: $text-muted; margin-bottom: 1; }
    AddSubscriptionModal Input { margin-bottom: 1; }
    AddSubscriptionModal #buttons { height: auto; align: right middle; margin-bottom: 1; }
    AddSubscriptionModal Button { margin-left: 1; }
    AddSubscriptionModal #results { height: auto; max-height: 12; border: round $panel; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._added_any = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]SUBSCRIBE[/b]", id="title")
            yield Static(
                "Paste one URL, or several separated by commas. hydownloader auto-detects the "
                "site; unrecognized sites still work by re-checking the exact URL on an interval.",
                id="hint",
            )
            yield Input(placeholder="https://example.com/artist/...", id="url-input")
            yield Input(placeholder="check interval in hours (default: random 12-24h)", id="hours-input")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Subscribe", id="submit", variant="primary")
            yield RichLog(id="results", markup=True, wrap=True, max_lines=200)

    def on_mount(self) -> None:
        self.query_one("#url-input", Input).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(self._added_any)

    @on(Input.Submitted, "#url-input")
    @on(Input.Submitted, "#hours-input")
    async def _submit_via_enter(self) -> None:
        await self._do_submit()

    @on(Button.Pressed, "#submit")
    async def _submit_via_button(self) -> None:
        await self._do_submit()

    async def _do_submit(self) -> None:
        url_input = self.query_one("#url-input", Input)
        raw = url_input.value.strip()
        if not raw:
            self.app.notify("Enter at least one URL.", severity="warning")
            return
        hours_raw = self.query_one("#hours-input", Input).value.strip()
        hours: float | None
        try:
            hours = float(hours_raw) if hours_raw else None
            if hours is not None and hours <= 0:
                hours = None
        except ValueError:
            hours = None

        urls = [u.strip() for u in raw.split(",") if u.strip()]
        submit_btn = self.query_one("#submit", Button)
        submit_btn.disabled = True
        results = self.query_one("#results", RichLog)

        for u in urls:
            # hours=None -> each URL draws its own fuzzed standard interval (see
            # subscriptions.add_single_subscription), so a comma-separated batch doesn't stack
            # every check at the same instant.
            result = await asyncio.to_thread(add_single_subscription, u, hours)
            color = _RESULT_COLORS.get(result.status, "#d8f6ff")
            results.write(f"[{color}]{result.status:<8}[/] {escape(u)}")
            results.write(f"         {escape(result.detail)}")
            if result.status == "Added":
                self._added_any = True
                self.app.notify(f"Subscribed: {result.detail}")
                if result.restarted_daemon:
                    results.write(
                        "         [#39ff88]new site - daemon restarted automatically to activate its "
                        "worker thread[/]"
                    )
                elif result.restart_error:
                    results.write(
                        f"         [#ffd166]added, but the automatic restart to activate its worker "
                        f"thread failed: {escape(result.restart_error)} - check diagnostics (key c)[/]"
                    )

        submit_btn.disabled = False
        url_input.value = ""
        url_input.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(self._added_any)


class AddDownloadModal(ModalScreen[None]):
    """One-off URL download (not a recurring subscription)."""

    DEFAULT_CSS = """
    AddDownloadModal { align: center middle; }
    AddDownloadModal > #dialog {
        width: 78; height: auto; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    AddDownloadModal #hint { color: $text-muted; margin-bottom: 1; }
    AddDownloadModal Input { margin-bottom: 1; }
    AddDownloadModal #buttons { height: auto; align: right middle; }
    AddDownloadModal Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]ONE-OFF DOWNLOAD[/b]", id="title")
            yield Static("Paste a URL to download once. Several, comma-separated, is fine.", id="hint")
            yield Input(placeholder="https://example.com/gallery/...", id="url-input")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Queue", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#url-input", Input).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#url-input")
    async def _submit_via_enter(self) -> None:
        await self._do_submit()

    @on(Button.Pressed, "#submit")
    async def _submit_via_button(self) -> None:
        await self._do_submit()

    async def _do_submit(self) -> None:
        raw = self.query_one("#url-input", Input).value.strip()
        if not raw:
            self.app.notify("Enter at least one URL.", severity="warning")
            return
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        resp = await asyncio.to_thread(api_client.add_or_update_urls, urls, subscriptions.DEFAULT_FILE_FILTER)
        if resp.accepted:
            self.app.notify(f"Queued {len(urls)} URL(s) for download.")
        else:
            self.app.notify(f"Failed to queue: {resp.error or 'daemon rejected the request'}", severity="error")
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class HealthCheckModal(ModalScreen[None]):
    """System diagnostics - process status, daemon API reachability, gallery-dl PATH check,
    and the background watchdog's last action, all in one place instead of scattered across
    separate menu options like the old console version had."""

    DEFAULT_CSS = """
    HealthCheckModal { align: center middle; }
    HealthCheckModal > #dialog {
        width: 88; height: auto; max-height: 90%; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    HealthCheckModal #report { height: auto; margin-bottom: 1; }
    HealthCheckModal #cap-result { height: auto; margin-bottom: 1; }
    HealthCheckModal #buttons { height: auto; align: right middle; }
    HealthCheckModal Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]SYSTEM DIAGNOSTICS[/b]", id="title")
            yield Static("Running checks...", id="report")
            yield Static("", id="cap-result")
            with Horizontal(id="buttons"):
                yield Button("Restart down services", id="restart", disabled=True)
                yield Button("Cap existing subs' file limits", id="cap-existing")
                yield Button("Block video on existing subs", id="block-video")
                yield Button("Fuzz existing intervals", id="fuzz-intervals")
                yield Button("Refresh", id="refresh")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self.query_one("#report", Static).update("Running checks...")
        self.run_worker(self._load_report(), exclusive=True)

    async def _load_report(self) -> None:
        report = await asyncio.to_thread(services.get_health_report)
        flagged_count, flagged_error = await asyncio.to_thread(subscriptions.get_flagged_subscription_count)
        s = report.status

        def badge(up: bool, pid: int | None) -> str:
            return f"[#39ff88]running (PID {pid})[/]" if up else "[#ff4f6d]NOT running[/]"

        lines = [
            f"Hydrus:               {badge(s.hydrus_running, s.hydrus_pid)}",
            f"hydownloader daemon:  {badge(s.daemon_running, s.daemon_pid)}",
            f"hydownloader systray: {badge(s.systray_running, s.systray_pid)}",
            "",
        ]
        if report.api_reachable:
            lines.append(f"Daemon API:           [#39ff88]reachable[/] at {escape(report.api_base_url or '')}")
        else:
            lines.append(f"Daemon API:           [#ff4f6d]NOT reachable[/] - {escape(report.api_reason or 'unknown')}")
        if report.hydrus_api_reachable:
            lines.append("Hydrus API:           [#39ff88]reachable[/]")
        else:
            lines.append(f"Hydrus API:           [#ff4f6d]NOT reachable[/] - {escape(report.hydrus_api_reason or 'unknown')}")
        if flagged_error:
            lines.append(f"Flagged subs:         [#ff4f6d]couldn't check[/] - {escape(flagged_error)}")
        elif flagged_count:
            lines.append(f"Flagged subs:         [#ffd166]{flagged_count}[/] failing repeatedly or stale - see the main table")
        else:
            lines.append("Flagged subs:         [#39ff88]none[/]")
        lines.append("")

        gdl = report.gallery_dl
        if gdl.on_path:
            lines.append(f"gallery-dl:           [#39ff88]found[/] ({escape(gdl.resolved_path or '')})")
        elif gdl.user_install_path:
            lines.append("gallery-dl:           [#ffd166]installed but NOT on PATH[/]")
            lines.append(f"  {escape(gdl.hint or '')}")
        else:
            lines.append("gallery-dl:           [#ff4f6d]NOT found[/] (not on PATH, 'pip show' found nothing either)")

        if report.multiple_python_paths:
            lines.append(f"\n[dim]Note: multiple Python installs on PATH: {escape(', '.join(report.multiple_python_paths))}[/]")

        if report.watchdog_last_check:
            lines.append(f"\nBackground watchdog last ran at {escape(report.watchdog_last_check)}.")
            if report.watchdog_actions:
                lines.append("Its last check took action:")
                for a in report.watchdog_actions:
                    lines.append(f"  - {escape(a)}")

        incidents = await asyncio.to_thread(watchdog.get_incident_history)
        lines.append("\nRecent incidents (watchdog):")
        if not incidents:
            lines.append("  none recorded yet this install")
        else:
            for inc in incidents:
                lines.append(f"  [dim]{escape(inc.get('ts', ''))}[/]")
                for a in inc.get("actions", []):
                    lines.append(f"    - {escape(a)}")

        self.query_one("#report", Static).update("\n".join(lines))
        needs_restart = not (s.hydrus_running and s.daemon_running and s.systray_running)
        self.query_one("#restart", Button).disabled = not needs_restart

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#refresh")
    def _on_refresh(self) -> None:
        self._refresh()

    @on(Button.Pressed, "#restart")
    async def _restart(self) -> None:
        self.query_one("#restart", Button).disabled = True
        await asyncio.to_thread(services.start_required_services)
        self.app.notify("Restart attempted - see the status above.")
        self._refresh()

    @on(Button.Pressed, "#cap-existing")
    async def _cap_existing(self) -> None:
        """Retrofits subscriptions.DEFAULT_MAX_FILES_INITIAL/REGULAR onto subscriptions added
        before that default existed (hydownloader's own default is 10000 initial / unlimited
        regular per check - the "downloading someone's whole backstock" behavior on an old
        subscription). Unrelated to worker-thread grouping, which is now fully automatic and
        has no manual control anywhere - this is the one piece of the old Throttle panel that
        was a real, ongoing user choice rather than a one-time migration nag, so it lives here
        instead of disappearing with the rest of that panel."""
        result = self.query_one("#cap-result", Static)
        result.update("Applying file-count caps to existing subscriptions...")
        updated, total, error = await asyncio.to_thread(subscriptions.cap_existing_subscription_file_limits)
        if error:
            result.update(f"[#ff4f6d]Failed: {escape(error)}[/]")
        else:
            result.update(
                f"[#39ff88]Capped {updated} of {total} subscription(s).[/] Takes effect on each one's "
                f"next check - no daemon restart needed."
            )

    @on(Button.Pressed, "#block-video")
    async def _block_video(self) -> None:
        """Retrofits subscriptions.DEFAULT_FILE_FILTER onto subscriptions added before the
        video block existed - those have no "filter" of their own set at all, so every file
        type (including video) still comes through unfiltered. Only touches subscriptions with
        no filter already set; a custom filter is presumed deliberate and left alone."""
        result = self.query_one("#cap-result", Static)
        result.update("Applying video block to existing subscriptions...")
        updated, total, error = await asyncio.to_thread(subscriptions.block_video_on_existing_subscriptions)
        if error:
            result.update(f"[#ff4f6d]Failed: {escape(error)}[/]")
        else:
            result.update(
                f"[#39ff88]Applied the video block to {updated} of {total} subscription(s).[/] Takes effect "
                f"on each one's next check - no daemon restart needed."
            )

    @on(Button.Pressed, "#fuzz-intervals")
    def _fuzz_intervals(self) -> None:
        # push_screen_wait() must run inside an actual Textual worker (raises NoActiveWorker
        # otherwise) - run_worker() provides that context.
        self.run_worker(self._fuzz_intervals_flow(), exclusive=True)

    async def _fuzz_intervals_flow(self) -> None:
        """Re-randomizes check_interval (12-24h spread, see subscriptions.
        default_check_interval_hours) for subscriptions still sitting on that standard band -
        mainly for subs added before add-time fuzzing existed, which all landed on the exact
        same fixed interval and so still stack their next checks together despite each one
        individually being "on the standard interval". Only touches subscriptions in that band
        by default; a confirm prompt offers the force-all variant for subs with a deliberately
        custom interval too."""
        force_all = await self.app.push_screen_wait(
            ConfirmModal(
                "Fuzz intervals for subscriptions on the standard 12-24h band. Also re-fuzz "
                "subscriptions with a custom (non-standard) interval?",
                confirm_label="Also fuzz custom intervals",
            )
        )
        result = self.query_one("#cap-result", Static)
        result.update("Re-randomizing check intervals...")
        updated, total, error = await asyncio.to_thread(subscriptions.fuzz_existing_intervals, force_all)
        if error:
            result.update(f"[#ff4f6d]Failed: {escape(error)}[/]")
        else:
            scope = "every" if force_all else "the standard-band"
            result.update(
                f"[#39ff88]Re-fuzzed {updated} of {total} subscription(s)[/] ({scope} interval(s) touched, "
                f"12-24h spread). Takes effect on each one's next check - no daemon restart needed."
            )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class HelpModal(ModalScreen[None]):
    """Keybinding cheatsheet - bound to '?'."""

    DEFAULT_CSS = """
    HelpModal { align: center middle; }
    HelpModal > #dialog {
        width: 78; height: auto; padding: 1 2;
        border: heavy $primary; background: $surface;
    }
    HelpModal #buttons { height: auto; align: right middle; margin-top: 1; }
    """

    _ROWS = [
        ("a", "Subscribe to a URL / gallery / artist"),
        ("u", "Queue a one-off URL download"),
        ("(quick-add bar)", "Paste a URL directly into the subscriptions panel + Enter"),
        ("enter", "Open actions for the selected subscription"),
        ("p", "Pause/resume the selected subscription instantly"),
        ("f", "Force-check the selected subscription instantly (marks it due now)"),
        ("x", "Delete the selected subscription (confirms first)"),
        ("/", "Filter the subscriptions table"),
        ("escape", "Clear filter / close dialog"),
        ("h", "Bring Hydrus to the front"),
        ("y", "Bring the systray to the front"),
        ("w", "Open the web dashboard in your browser"),
        ("c", "Run system diagnostics / health check (also caps existing subs' file limits)"),
        ("k", "Configure API keys"),
        ("r", "Refresh now"),
        ("ctrl+p", "Command palette"),
        ("q", "Quit (shuts down anything idle)"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[b]KEYBINDINGS[/b]", id="title")
            body = "\n".join(f"[#4dfff0]{key:<8}[/] {escape(desc)}" for key, desc in self._ROWS)
            yield Static(body)
            with Horizontal(id="buttons"):
                yield Button("Close", id="close")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)
