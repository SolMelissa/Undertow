"""
Subscription management core logic: add a subscription (single URL, or several comma-
separated - the TUI splits those before calling in). This is presentation-free by design - the
TUI (hydrus_pipeline/tui/) is the only caller now, and it drives these directly rather than
through an interactive console loop (the old Add-Subscription/Import-SubscriptionsBatch/
Manage-Subscriptions/Show-QueueStatus/Watch-WorkerStatus PS1 equivalents, all of which were
input()/print() driven and got removed along with the console menu they belonged to).
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import api_client, config, services

# hydownloader's own schema (constants.py: CREATE_SUBS_STATEMENT) defaults
# "max_files_initial" to 10000 and leaves "max_files_regular" NULL (unlimited) for any
# subscription added without explicit values - meaning a brand new subscription's first check
# will try to pull up to 10,000 files by default. That's the "downloading the whole backstock"
# behavior - not a bug in this package, just a very generous upstream default that's rarely
# what anyone actually wants. These get applied to every new subscription unless overridden.
DEFAULT_MAX_FILES_INITIAL = 100
DEFAULT_MAX_FILES_REGULAR = 100

# The standard recheck interval is randomized within this range (hours) rather than fixed at
# one value - a bulk add (batch import, or several single adds in a row) would otherwise put
# every one of those subscriptions' next checks at the exact same moment, which then keeps
# recurring in lockstep on every check_interval multiple after that. Each add draws its own
# random.uniform() call, so a batch spreads itself out on its own. Only applies when the
# caller doesn't pass an explicit hours value - a user-typed interval is never overridden.
DEFAULT_CHECK_INTERVAL_HOURS_MIN = 12.0
DEFAULT_CHECK_INTERVAL_HOURS_MAX = 24.0

# gallery-dl's own video container extensions (see its "video" postprocessor / --filter
# "extension" metadata field) - blocked by default so subscriptions and one-off downloads only
# ever pull images. Explicitly NOT blocking "gif": an animated gif is still just an image file
# as far as anyone downloading one cares, so it stays allowed even though gallery-dl can treat
# some animated formats as video-adjacent.
BLOCKED_VIDEO_EXTENSIONS = (
    "mp4", "webm", "mov", "avi", "mkv", "flv", "wmv", "m4v", "mpg", "mpeg", "3gp", "ts", "m2ts",
)
# hydownloader passes this straight through to gallery-dl's "--filter" option - a Python
# expression evaluated against each candidate file's metadata dict, where "extension" is the
# file's extension without a leading dot (gallery-dl's own standard field, present for every
# extractor). Stored on the subscriptions/urls table's "filter" column (constants.py:
# CREATE_SUBS_STATEMENT / CREATE_URLS_STATEMENT) - NULL by default upstream, meaning no
# filtering at all unless something sets it, which is what happens below.
DEFAULT_FILE_FILTER = f"extension not in {BLOCKED_VIDEO_EXTENSIONS!r}"


def default_check_interval_hours() -> float:
    return random.uniform(DEFAULT_CHECK_INTERVAL_HOURS_MIN, DEFAULT_CHECK_INTERVAL_HOURS_MAX)


@dataclass
class SubResult:
    status: str  # "Added" | "Skipped" | "Failed"
    detail: str
    id: Optional[int] = None
    restarted_daemon: bool = False
    restart_error: Optional[str] = None


def add_single_subscription(
    url: str,
    hours: float | None,
    additional_data: str | None = None,
    allow_duplicate: bool = False,
    max_files_initial: int | None = DEFAULT_MAX_FILES_INITIAL,
    max_files_regular: int | None = DEFAULT_MAX_FILES_REGULAR,
    file_filter: str | None = DEFAULT_FILE_FILTER,
) -> SubResult:
    """Shared by every entry point that adds subscriptions (a single URL, or looped once per
    URL for a comma-separated batch typed into the subscribe box) so they all detect/add/
    verify exactly the same way instead of drifting apart. Pure with one deliberate exception
    (see below) - no print()/input() - callers (the TUI) decide how to surface an "already
    subscribed" result; this used to prompt interactively here, which doesn't make sense once
    the caller is a UI event handler rather than a blocking console loop. Pass
    allow_duplicate=True to add anyway instead of skipping when a matching subscription
    already exists. Pass max_files_initial/max_files_regular=None to fall back to
    hydownloader's own (much larger) defaults instead. Pass hours=None to use the fuzzed
    standard interval (see default_check_interval_hours) instead of a user-specified one - each
    call draws its own random value, which is what keeps a bulk add from stacking every check
    at the same instant. Pass file_filter=None to allow every file type through unfiltered
    instead of the default video block (see DEFAULT_FILE_FILTER) - gifs are never blocked by
    that default either way, since they're treated as images, not video.

    Every new subscription goes straight onto a worker thread named after its downloader, so
    different sites always check concurrently - there is no single-threaded mode, and nothing
    to toggle. The one thing this can't do is spin the thread up on an already-running daemon
    (hydownloader only creates subscription-worker threads at startup), so when the assigned
    worker_id isn't live yet, this function restarts the daemon itself right here rather than
    asking the caller to nag the user about it later - the whole point of "always parallel" is
    that nobody should ever have to remember a manual step. SubResult.restarted_daemon reports
    whether that happened; restart_error carries the reason if the restart itself failed (the
    subscription was still added successfully - it just won't check until the daemon comes up,
    same as any other reason the daemon might be down). If get_active_worker_ids() can't tell
    whether the thread is live (API hiccup right after a call that just succeeded), this does
    NOT force a restart on a guess - ensure_all_subscriptions_parallel() catches anything that
    slips through here on the next app startup."""
    if hours is None:
        hours = default_check_interval_hours()

    info_resp = api_client.url_info([url])
    if not info_resp.success:
        return SubResult("Failed", f"couldn't reach the daemon: {info_resp.error}")
    info = (info_resp.data or [{}])[0]

    if info.get("sub_downloader"):
        existing = info.get("existing_subscriptions") or []
        if existing and not allow_duplicate:
            return SubResult("Skipped", f"already subscribed (id={existing[0].get('id')})")
        downloader = info["sub_downloader"]
        keywords = info.get("sub_keywords")
    else:
        downloader = "raw"
        keywords = url

    # hydownloader's actual subscriptions table column is "check_interval" (seconds, despite
    # the bare name). Ported straight from the PS1 fix - a prior version of this used
    # "check_interval_seconds", which doesn't exist as a column and made the daemon 500 on
    # every single add.
    sub_entry = {
        "downloader": downloader,
        "keywords": keywords,
        "check_interval": int(hours * 3600),
        "worker_id": downloader,
    }
    if additional_data:
        sub_entry["additional_data"] = additional_data
    if max_files_initial is not None:
        sub_entry["max_files_initial"] = max_files_initial
    if max_files_regular is not None:
        sub_entry["max_files_regular"] = max_files_regular
    if file_filter:
        sub_entry["filter"] = file_filter

    add_resp = api_client.add_or_update_subscriptions([sub_entry])
    if not add_resp.accepted:
        err = add_resp.error or "daemon rejected the request"
        return SubResult("Failed", err)

    # The daemon reporting {"status":"ok"} only means it accepted and processed the request -
    # not that a new row actually landed in its subscriptions table. Re-fetch the live list
    # and confirm this exact downloader/keywords pair is actually in it before calling it added.
    time.sleep(0.2)
    verify_resp = api_client.get_subscriptions()
    match = None
    if verify_resp.success and verify_resp.data:
        matches = [s for s in verify_resp.data if s.get("downloader") == downloader and s.get("keywords") == keywords]
        match = matches[-1] if matches else None

    if not match:
        return SubResult("Failed", "daemon said ok but it's not showing up in /get_subscriptions afterward")

    restarted_daemon = False
    restart_error = None
    active = services.get_active_worker_ids()
    if active is not None and downloader not in active:
        restart = services.restart_daemon()
        restarted_daemon = restart.success
        if not restart.success:
            restart_error = restart.error
    return SubResult(
        "Added",
        f"{downloader} / {keywords}",
        id=match.get("id"),
        restarted_daemon=restarted_daemon,
        restart_error=restart_error,
    )


def get_subscription_by_id(sub_id: int) -> dict | None:
    resp = api_client.get_subscriptions()
    if not (resp.success and resp.data):
        return None
    for s in resp.data:
        if s.get("id") == sub_id:
            return s
    return None


_UNCHANGED = object()


def update_subscription(
    sub_id: int,
    *,
    keywords: str | None = _UNCHANGED,
    check_interval_hours: float | None = _UNCHANGED,
    max_files_initial: int | None = _UNCHANGED,
    max_files_regular: int | None = _UNCHANGED,
    file_filter: str | None = _UNCHANGED,
) -> tuple[bool, str | None]:
    """Edits an existing subscription's keywords/interval/file caps/filter in place, instead of
    the delete-and-re-add dance those previously required (which also throws away its check
    history, since a re-add is a brand new row with a new id). "downloader"/"worker_id" are
    deliberately not editable here - changing those needs the same daemon-restart dance
    add_single_subscription does for a new worker thread, which doesn't belong in a quick edit;
    delete + re-add is still the right move if a subscription is on the wrong site entirely.

    Only keyword arguments actually passed get sent to the daemon - each one defaults to a
    private sentinel (not None) specifically so a caller can pass max_files_initial=None or
    file_filter=None to deliberately CLEAR that field back to hydownloader's own default
    (uncapped / unfiltered) without it being indistinguishable from "leave this field alone".
    Returns (ok, error)."""
    update: dict = {"id": sub_id}
    if keywords is not _UNCHANGED:
        update["keywords"] = keywords
    if check_interval_hours is not _UNCHANGED:
        update["check_interval"] = int(check_interval_hours * 3600)
    if max_files_initial is not _UNCHANGED:
        update["max_files_initial"] = max_files_initial
    if max_files_regular is not _UNCHANGED:
        update["max_files_regular"] = max_files_regular
    if file_filter is not _UNCHANGED:
        update["filter"] = file_filter or None

    if len(update) == 1:
        return True, None  # nothing actually changed - not an error, just a no-op

    resp = api_client.add_or_update_subscriptions([update])
    if not resp.accepted:
        return False, resp.error or "daemon rejected the request"
    return True, None


def _bulk_update(compute_update: Callable[[dict], dict | None]) -> tuple[int, int, str | None]:
    """Shared by every "retroactively fix up existing subscriptions" helper below: fetch every
    subscription, ask `compute_update` what (if anything) each one needs changed, and push
    whatever came back in one batched /add_or_update_subscriptions call. `compute_update`
    returns None for a subscription that's already correct (skipped), or an update dict
    (always including "id") otherwise. Returns (updated_count, total_count, error)."""
    resp = api_client.get_subscriptions()
    if not resp.success:
        return 0, 0, resp.error
    subs = resp.data or []

    updates = [u for s in subs if (u := compute_update(s)) is not None]
    if not updates:
        return 0, len(subs), None

    add_resp = api_client.add_or_update_subscriptions(updates)
    if not add_resp.accepted:
        return 0, len(subs), add_resp.error or "daemon rejected the request"
    return len(updates), len(subs), None


def cap_existing_subscription_file_limits(
    max_files_initial: int = DEFAULT_MAX_FILES_INITIAL,
    max_files_regular: int = DEFAULT_MAX_FILES_REGULAR,
) -> tuple[int, int, str | None]:
    """Retroactively applies max_files_initial/max_files_regular to every existing
    subscription that doesn't already have a limit at or below the target (subscriptions
    added before this default existed are sitting on hydownloader's own defaults - 10000
    initial, unlimited regular). Takes effect on each subscription's *next* check - no daemon
    restart needed, unlike worker_id reassignment. Returns (updated_count, total_count, error)."""
    def compute(s: dict) -> dict | None:
        current_initial = s.get("max_files_initial")
        current_regular = s.get("max_files_regular")
        needs_initial = current_initial is None or current_initial > max_files_initial
        needs_regular = current_regular is None or current_regular > max_files_regular
        if needs_initial or needs_regular:
            return {"id": s["id"], "max_files_initial": max_files_initial, "max_files_regular": max_files_regular}
        return None

    return _bulk_update(compute)


def block_video_on_existing_subscriptions(file_filter: str = DEFAULT_FILE_FILTER) -> tuple[int, int, str | None]:
    """Retroactively applies the video-blocking filter (see DEFAULT_FILE_FILTER) to every
    existing subscription that doesn't already have it - subscriptions added before this
    default existed have no "filter" set at all, meaning every file type (including video)
    still gets through. Only touches subscriptions with no filter of their own; one that
    already has a custom "filter" value set is presumed deliberate and left alone. Takes
    effect on each subscription's *next* check - no daemon restart needed. Returns
    (updated_count, total_count, error)."""
    def compute(s: dict) -> dict | None:
        if not s.get("filter"):
            return {"id": s["id"], "filter": file_filter}
        return None

    return _bulk_update(compute)


def fuzz_existing_intervals(force_all: bool = False) -> tuple[int, int, str | None]:
    """Re-randomizes check_interval for existing subscriptions still sitting on the standard
    band (12-24h, same range as default_check_interval_hours()) - a batch add from before the
    interval fuzzing existed left every one of those subscriptions on the exact same fixed
    interval, which stacks their next checks right back together even though each is
    individually "random". Draws a fresh random.uniform() per subscription, same as a new add
    would, so a bulk run here spreads them out exactly like fuzzing at add-time does.

    Only touches subscriptions whose current interval already falls within that band by
    default - one a user deliberately set outside it (e.g. checking hourly, or weekly) is
    presumed intentional and left alone unless force_all=True re-randomizes every subscription
    regardless of its current interval. Takes effect on each subscription's *next* check - no
    daemon restart needed. Returns (updated_count, total_count, error)."""
    lo = int(DEFAULT_CHECK_INTERVAL_HOURS_MIN * 3600)
    hi = int(DEFAULT_CHECK_INTERVAL_HOURS_MAX * 3600)

    def compute(s: dict) -> dict | None:
        current = s.get("check_interval")
        in_default_band = current is not None and lo <= current <= hi
        if not force_all and not in_default_band:
            return None
        return {"id": s["id"], "check_interval": int(default_check_interval_hours() * 3600)}

    return _bulk_update(compute)


def sync_priority_by_last_success() -> tuple[int, int, str | None]:
    """Keeps each subscription's `priority` column set so that whenever several subscriptions
    on the same worker thread are due at once, hydownloader checks the least-recently
    *successfully* updated one first.

    hydownloader's own due-subscription query (db.py::get_due_subscription_ids) sorts
    `order by priority desc, ifnull(last_check, 0) asc` - so priority is the dominant sort key,
    and its own fallback tiebreaker is last_check (any attempt, success OR failure), not
    last_successful_check. That matters: a subscription that's been failing repeatedly still
    gets its last_check bumped on every failed attempt, so hydownloader's own ordering alone
    would keep pushing it to the back of the queue even though it hasn't actually downloaded
    anything in a long time - exactly backwards from "prioritize what's gone longest without a
    real update." Setting priority = -last_successful_check (and 0, the max, for subscriptions
    that have never once succeeded) overrides that: sorted priority desc, the oldest
    last_successful_check - or no success at all - always sorts first, regardless of how many
    failed attempts happened in between.

    This has to be re-synced periodically rather than set once, since last_successful_check
    keeps advancing every time a subscription actually succeeds (see watchdog.py, which calls
    this once per its own cycle). Only sends updates for subscriptions whose stored priority is
    actually out of sync, so a no-op cycle costs nothing beyond the initial fetch. Returns
    (updated_count, total_count, error)."""
    def compute(s: dict) -> dict | None:
        last_success = s.get("last_successful_check")
        desired_priority = 0 if last_success is None else -int(last_success)
        if s.get("priority") != desired_priority:
            return {"id": s["id"], "priority": desired_priority}
        return None

    return _bulk_update(compute)


def assign_worker_ids_by_downloader() -> tuple[int, int, str | None]:
    """Retroactively groups every existing subscription onto a worker thread named after its
    downloader (e.g. all "gelbooru" subs on a "gelbooru" thread), so different sites check in
    parallel instead of hydownloader's single-threaded default. Grouping by downloader is what
    hydownloader's own docs recommend, and it inherently avoids the one real danger they call
    out (never split a single site across multiple threads - a shared thread per site can't).

    IMPORTANT: subscription worker threads are only spawned when hydownloader-daemon starts -
    this reassignment has no effect until the daemon is restarted afterward (see
    services.restart_daemon()). Returns (updated_count, total_count, error)."""
    def compute(s: dict) -> dict | None:
        downloader = str(s.get("downloader") or "")
        if downloader and s.get("worker_id") != downloader:
            return {"id": s["id"], "worker_id": downloader}
        return None

    return _bulk_update(compute)


def ensure_all_subscriptions_parallel() -> tuple[int, int, bool, str | None]:
    """Runs once at every app startup (menu.main(), right after
    services.start_required_services()) so no subscription can ever be left running
    single-threaded without anyone needing to remember a manual button - this is the
    "always on, permanently" version of what used to be the Throttle panel's "Group + restart"
    action. Retroactively reassigns every subscription's worker_id to match its downloader
    (assign_worker_ids_by_downloader) - in practice this only ever touches subscriptions added
    before auto-parallel was the default, since add_single_subscription always sets worker_id
    correctly on the way in now. Then restarts the daemon if that reassignment changed
    anything, or if any subscription's worker_id isn't already backed by a live thread (which
    covers a subscription added in a previous session that ended - crash, force-quit - before
    its own post-add restart in add_single_subscription could complete).

    Returns (updated_count, total_count, restarted, error). `error` covers both a failure to
    read/reassign subscriptions in the first place AND a restart that was attempted but didn't
    come back up (restart_daemon()'s own error message, surfaced here rather than swallowed) -
    either way `restarted` comes back False and the daemon is left running under its prior
    thread grouping. Never raises, since this is routine startup housekeeping that must never
    block the app from launching."""
    updated, total, error = assign_worker_ids_by_downloader()
    if error or total == 0:
        return updated, total, False, error

    needs_restart = updated > 0
    if not needs_restart:
        active = services.get_active_worker_ids()
        if active is not None:
            resp = api_client.get_subscriptions()
            if resp.success and resp.data:
                needs_restart = any(
                    str(s.get("downloader") or "") not in active
                    for s in resp.data
                    if s.get("downloader")
                )

    if not needs_restart:
        return updated, total, False, None

    restart = services.restart_daemon()
    return updated, total, restart.success, (None if restart.success else restart.error)


# ------------------------------------------------------------------------- per-run check history
# hydownloader logs one row per subscription check to its "subscription_checks" table (new/
# skipped file counts, status, start/end time) - a subscription's `last_result_status` column is
# just a denormalized copy of the most recent row's status, with no file counts attached. These
# helpers are what let the TUI show more than that one-word summary: a live "files downloaded
# last run" column on the main table, a force-check action, and a per-subscription history view.

def force_recheck(sub_id: int) -> tuple[bool, str | None]:
    """Marks a subscription as immediately due for its next check, without resetting it to a
    from-scratch "initial" check (that's setting last_check to None instead of 0, which also
    makes the next check eligible for max_files_initial's larger cap and logs "this is the
    first check for this subscription" - a more disruptive action this doesn't expose). Since
    hydownloader computes "due" as last_check + check_interval <= now, last_check=0 satisfies
    that for any positive check_interval while staying non-None, so the check queues as a
    regular (not initial) run. The subscription's already-running worker thread (see
    ensure_all_subscriptions_parallel() - every subscription always has one) picks it up on its
    own poll loop, typically within a few seconds - no daemon restart needed, since this
    doesn't touch worker_id."""
    resp = api_client.add_or_update_subscriptions([{"id": sub_id, "last_check": 0}])
    if not resp.accepted:
        return False, resp.error or "daemon rejected the request"
    return True, None


def recheck_stale_subscriptions(older_than_hours: float = 1.0) -> tuple[int, int, str | None]:
    """Force-rechecks (see force_recheck) every subscription whose last check was more than
    older_than_hours ago - run once at app startup (menu.py's main(), right after Hydrus/the
    daemon come up) so reopening the pipeline after being away also catches subscriptions up,
    instead of waiting for each one's own check_interval to naturally roll around. Subscriptions
    that have never been checked (last_check is None - still awaiting their first, "initial"
    check) are left alone; that's a separate, already-queued state this shouldn't disturb.
    Returns (rechecked_count, total_count, error)."""
    resp = api_client.get_subscriptions()
    if not resp.success:
        return 0, 0, resp.error
    subs = resp.data or []

    cutoff = time.time() - older_than_hours * 3600
    stale_ids = [
        s["id"] for s in subs
        if s.get("last_check") is not None and s["last_check"] < cutoff
    ]
    if not stale_ids:
        return 0, len(subs), None

    # last_check=0 (not None) - see force_recheck's docstring for why that's the "due now, but
    # not a from-scratch initial check" state, batched here into one request instead of one
    # add_or_update_subscriptions call per subscription.
    updates = [{"id": sid, "last_check": 0} for sid in stale_ids]
    add_resp = api_client.add_or_update_subscriptions(updates)
    if not add_resp.accepted:
        return 0, len(subs), add_resp.error or "daemon rejected the request"
    return len(stale_ids), len(subs), None


def fleet_counts(subs: list[dict]) -> dict[str, int]:
    """total/active/paused/due counts - the same handful of numbers both the TUI's FLEET
    STATUS panel and the web dashboard's fleet widget show, computed once here so a change to
    what "due" means (or any future fleet stat) only has to happen in one place."""
    total = len(subs)
    paused = sum(1 for s in subs if s.get("paused"))
    due = sum(1 for s in subs if s.get("is_due"))
    return {"total": total, "active": total - paused, "paused": paused, "due": due}


def top_downloaders(subs: list[dict], limit: int = 10) -> list[tuple[str, int]]:
    """Subscription count per downloader (site), most-subscribed first, capped at `limit` -
    backs the TUI's SECTOR SCAN panel and the web dashboard's sector widget."""
    counts: dict[str, int] = {}
    for s in subs:
        name = str(s.get("downloader") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def get_latest_checks(ids: list[int]) -> dict[int, dict]:
    """Returns {subscription_id: latest subscription_checks row} for the given IDs - the "files
    downloaded last run" data the main subscriptions table and Row Actions summary line need.
    There's no "latest only" filter on /get_subscription_checks, so this fetches every
    non-archived check for the given IDs and reduces client-side; fine at normal history sizes,
    but this is exactly why callers should poll it on a slower dedicated interval rather than
    the fast per-second UI tick (see tui/app.py's separate _check_tick). Silently returns {} on
    any failure or empty input - this is supplementary display data, not worth surfacing as an
    error state of its own."""
    if not ids:
        return {}
    resp = api_client.get_subscription_checks(ids)
    if not resp.success or not resp.data:
        return {}
    latest: dict[int, dict] = {}
    for check in resp.data:
        sub_id = check.get("subscription_id")
        if sub_id is None:
            continue
        current = latest.get(sub_id)
        if current is None or (check.get("time_started") or 0) > (current.get("time_started") or 0):
            latest[sub_id] = check
    return latest


def get_total_downloads(ids: list[int]) -> dict[int, int]:
    """Returns {subscription_id: sum of new_files across every recorded check} - hydownloader
    has no running-total column on the subscriptions table itself (see CREATE_SUBS_STATEMENT),
    so this is the only way to get a "lifetime files downloaded" figure: fetch every check row
    and sum client-side, same /get_subscription_checks call and same "supplementary display
    data, not worth its own error state" reasoning as get_latest_checks above. IDs with no
    recorded checks simply don't appear in the result (treat missing as 0, not an error)."""
    if not ids:
        return {}
    resp = api_client.get_subscription_checks(ids)
    if not resp.success or not resp.data:
        return {}
    totals: dict[int, int] = {}
    for check in resp.data:
        sub_id = check.get("subscription_id")
        if sub_id is None:
            continue
        totals[sub_id] = totals.get(sub_id, 0) + (check.get("new_files") or 0)
    return totals


# A subscription that fails once or twice in a row is normal noise (a site rate-limiting, a
# transient network blip) - only *sustained* trouble should visibly alarm anyone. These are the
# two independent signals that count as "sustained": either its most recent checks are on an
# unbroken failure streak, or it simply hasn't succeeded in a long time (which also catches a
# subscription stuck failing every single check since it was added, not just a recent streak).
FAILURE_ALERT_CONSECUTIVE = 3
FAILURE_ALERT_STALE_DAYS = 14.0


def get_failure_status(subs: list[dict]) -> dict[int, dict]:
    """Returns {subscription_id: {"consecutive_failures", "last_success_days_ago", "flagged"}}
    for the given subscriptions - the data behind the main table's failure badge and the
    watchdog's alerting. Takes full subscription dicts (as already fetched by the caller via
    api_client.get_subscriptions()) rather than bare ids, since "days since last success" comes
    straight off each subscription's own last_successful_check field - no need to re-fetch
    subscriptions just to get that. consecutive_failures still needs one
    /get_subscription_checks call (there's no "how many failures in a row" field anywhere
    upstream), same supplementary-data contract as get_latest_checks/get_total_downloads:
    silently omits a subscription rather than erroring if that call fails.

    "flagged" is the actual UI-facing signal - True once consecutive_failures reaches
    FAILURE_ALERT_CONSECUTIVE, OR last_success_days_ago reaches FAILURE_ALERT_STALE_DAYS
    (covers a subscription that alternates single failures with single successes forever,
    which never builds a failure streak but also never makes real progress)."""
    ids = [s.get("id") for s in subs if s.get("id") is not None]
    if not ids:
        return {}
    resp = api_client.get_subscription_checks(ids)
    checks_by_sub: dict[int, list[dict]] = {}
    if resp.success and resp.data:
        for check in resp.data:
            sub_id = check.get("subscription_id")
            if sub_id is not None:
                checks_by_sub.setdefault(sub_id, []).append(check)

    now = time.time()
    result: dict[int, dict] = {}
    for s in subs:
        sub_id = s.get("id")
        if sub_id is None:
            continue
        checks = sorted(checks_by_sub.get(sub_id, []), key=lambda c: c.get("time_started") or 0, reverse=True)
        consecutive = 0
        for c in checks:
            status = str(c.get("status") or "").lower()
            if status == "ok" or not status:
                break
            consecutive += 1

        last_success = s.get("last_successful_check")
        last_success_days_ago = (now - last_success) / 86400 if last_success else None

        result[sub_id] = {
            "consecutive_failures": consecutive,
            "last_success_days_ago": last_success_days_ago,
            "flagged": consecutive >= FAILURE_ALERT_CONSECUTIVE
            or (last_success_days_ago is not None and last_success_days_ago >= FAILURE_ALERT_STALE_DAYS),
        }
    return result


def get_flagged_subscription_count() -> tuple[int, str | None]:
    """Convenience wrapper for Diagnostics: how many subscriptions are currently flagged (see
    get_failure_status), without the caller needing to fetch subscriptions and reduce failure
    status itself just to show one summary number. Returns (count, error)."""
    resp = api_client.get_subscriptions()
    if not resp.success:
        return 0, resp.error
    subs = resp.data or []
    status = get_failure_status(subs)
    return sum(1 for v in status.values() if v["flagged"]), None


def get_check_history(sub_id: int, limit: int = 10) -> tuple[list[dict], str | None]:
    """Returns up to `limit` most recent subscription_checks rows for one subscription, newest
    first - the data behind Row Actions' "History" button. Returns (rows, error)."""
    resp = api_client.get_subscription_checks([sub_id])
    if not resp.success:
        return [], resp.error
    rows = resp.data or []
    rows.sort(key=lambda c: c.get("time_started") or 0, reverse=True)
    return rows[:limit], None


# gallery-dl's own log lines this looks for, in the order checked. hydownloader's
# subscription_checks.status only ever records the generic "http error" (or similar
# one/two-word codes) it gets back from gallery-dl's exit status - the actual HTTP status
# code, response body, and any rate-limit/auth explanation only exist in the per-subscription
# gallery-dl log file, not in anything the API exposes. This re-derives the useful part of
# that from the log so the UI can show more than "http error".
_HTTP_STATUS_RE = re.compile(r'"[A-Z]+ [^"]*" (\d{3}) ')
_RATE_LIMIT_RE = re.compile(
    r"^\[.*?\]\[(?:info|error)\](?:\[[^\]]*\])?\s*(.*(?:[Rr]ate limit|[Ll]imit [Ee]xceeded).*)$", re.MULTILINE
)
_ABORT_RE = re.compile(r"^\[.*?\]\[error\](?:\[[^\]]*\])?\s*(.+)$", re.MULTILINE)


def explain_check_error(sub_id: int) -> Optional[str]:
    """Best-effort human-readable explanation for a subscription's last failed check, pulled
    from its per-subscription gallery-dl log (LOGS_DIR/subscription-<id>-gallery-dl-latest.txt).
    Returns None if the log is missing or has nothing more specific to add - callers should
    keep showing the raw status ("http error" etc.) from get_check_history/get_latest_checks
    either way, this is meant to be appended alongside it, not replace it."""
    log_path = config.LOGS_DIR / f"subscription-{sub_id}-gallery-dl-latest.txt"
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    status_match = None
    for status_match in _HTTP_STATUS_RE.finditer(text):
        pass  # keep the last match - the one that actually aborted the run

    detail_lines = [m.strip() for m in _RATE_LIMIT_RE.findall(text)]
    if not detail_lines:
        detail_lines = [m.strip() for m in _ABORT_RE.findall(text)]

    parts = []
    if status_match:
        parts.append(f"HTTP {status_match.group(1)}")
    if detail_lines:
        # last [error]/[info] line is the actual failure reason (rate limit reset time,
        # auth failure, etc.) - earlier ones are usually just gallery-dl restating context
        parts.append(detail_lines[-1])

    if not parts:
        return None
    return " — ".join(parts)
