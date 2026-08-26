"""
hydownloader daemon HTTP API client - the Python equivalent of Get-DaemonApiInfo and
Invoke-DaemonApi in the PS1.

Full endpoint reference: https://gitgud.io/thatfuckingbird/hydownloader/-/raw/master/docs/API.md

Why this is worth having in Python instead of PowerShell: `requests.post(url, json=body)`
always serializes a Python list correctly, including a one-item list - there's no equivalent
of PowerShell's ConvertTo-Json collapsing a single-element array into a bare object (the bug
that caused every subscription add to 500 for a while). And on any HTTP error, the response
body is always available at `response.text` - no PowerShell-version-dependent stream-reading
gotchas to work around to see *why* the daemon rejected something.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import urllib3

from . import config

# Rolling log of daemon API calls that actually reached the network (the "api unreachable"
# early-out below doesn't count - that's a config problem, not a request) - feeds the web
# dashboard's API_TRAFFIC widget with real request-count/latency/error numbers instead of
# invented ones. Module-level and unlocked for the same reason _activity_history is in
# webui.py: worst case is one torn read on a decorative stats readout, not worth a lock.
_call_log: deque[dict] = deque(maxlen=200)


def get_call_stats() -> dict:
    """Aggregates _call_log for display - total calls tracked, calls in the last 60s, average
    latency, error count, and the most-called routes. Returns zeros/empties, not an error, when
    no calls have happened yet (a quiet daemon isn't a failure state for this readout)."""
    calls = list(_call_log)
    total = len(calls)
    if not total:
        return {"total": 0, "calls_per_min": 0, "avg_ms": 0.0, "errors": 0, "top_routes": []}
    now = time.monotonic()
    errors = sum(1 for c in calls if not c["success"])
    avg_ms = round(sum(c["duration_ms"] for c in calls) / total, 1)
    calls_per_min = sum(1 for c in calls if now - c["t"] < 60)
    per_route: dict[str, int] = {}
    for c in calls:
        per_route[c["route"]] = per_route.get(c["route"], 0) + 1
    top_routes = sorted(per_route.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return {"total": total, "calls_per_min": calls_per_min, "avg_ms": avg_ms, "errors": errors, "top_routes": top_routes}

# The daemon's self-signed cert (when SSL is on) isn't meant to be publicly trusted - same
# suppression the PS1 did with -SkipCertificateCheck.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# One shared Session for the process's lifetime instead of a bare requests.post() per call -
# every route above is polled every 2-10s by the web dashboard, and a fresh call previously
# paid a new TCP connect + (for https) TLS handshake every single time instead of reusing a
# keep-alive connection to the same loopback daemon.
_session = requests.Session()


@dataclass
class ApiResult:
    success: bool
    data: Any = None
    error: str | None = None

    def __bool__(self) -> bool:
        return self.success

    @property
    def accepted(self) -> bool:
        """True when the daemon didn't just respond, but actually reported {"status": ...} -
        the "request succeeded AND was processed" check every add/update/delete call site needs
        before treating its own action as having taken effect."""
        return self.success and bool(self.data) and bool(self.data.get("status"))


@dataclass
class DaemonApiInfo:
    base_url: str
    access_key: str
    scheme: str


# get_subscriptions() (called every 1.5s tick, uncached) and every other invoke_daemon_api()
# call route through here first - without caching, that's a disk read + full JSON parse of
# hydownloader-config.json on every single API call forever, for a file that only changes when
# the user reconfigures hydownloader itself. mtime-keyed, same pattern as tags.load_tags().
_config_cache: tuple[float, DaemonApiInfo | None, str | None] | None = None


def get_daemon_api_info() -> tuple[DaemonApiInfo | None, str | None]:
    """Returns (info, None) on success, or (None, reason) on failure - the reason is shown
    directly to the user, so it needs to say exactly what was checked and what was found
    instead of a generic "not reachable" guess."""
    global _config_cache
    cfg_path = config.HYDOWNLOADER_CONFIG_FILE
    try:
        mtime = cfg_path.stat().st_mtime
    except OSError:
        _config_cache = None
        return None, f"config file not found at {cfg_path} - has hydownloader been set up yet?"
    if _config_cache is not None and _config_cache[0] == mtime:
        return _config_cache[1], _config_cache[2]

    try:
        # utf-8-sig strips a BOM if present (harmless no-op if not) - hydownloader-config.json
        # gets written by Setup-HydrusPipeline.ps1's Set-Content -Encoding UTF8, which prepends
        # a BOM that plain "utf-8" decoding chokes on. Same footgun api_keys.py already calls
        # out for gallery-dl's config file.
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        info, reason = None, f"couldn't read {cfg_path}: {e}"
        _config_cache = (mtime, info, reason)
        return info, reason
    except json.JSONDecodeError as e:
        info, reason = None, f"{cfg_path} exists but isn't valid JSON: {e}"
        _config_cache = (mtime, info, reason)
        return info, reason

    port = cfg.get("daemon.port") or 53211
    host = cfg.get("daemon.host") or "127.0.0.1"
    ssl_enabled = cfg.get("daemon.ssl")
    access_key = cfg.get("daemon.access-key") or ""
    # hydownloader supports running with no access key at all via
    # "daemon.do-not-check-access-key": true (common for local-only, single-user setups - the
    # daemon just skips the header check). Only treat a missing key as "not set up yet" when
    # that escape hatch isn't in play; otherwise every API call would wrongly report the
    # daemon as unreachable even though it's up and would answer fine with no key header.
    skip_key_check = bool(cfg.get("daemon.do-not-check-access-key"))
    server_pem_exists = (config.DATA_DIR / "server.pem").exists()
    scheme = "https" if (ssl_enabled and server_pem_exists) else "http"
    if not access_key and not skip_key_check:
        info, reason = None, (
            f"{cfg_path} has no 'daemon.access-key' set, and 'daemon.do-not-check-access-key' "
            f"isn't true either - has an access key actually been generated for this database?"
        )
        _config_cache = (mtime, info, reason)
        return info, reason
    info = DaemonApiInfo(base_url=f"{scheme}://{host}:{port}", access_key=access_key, scheme=scheme)
    _config_cache = (mtime, info, None)
    return info, None


def invoke_daemon_api(route: str, body: Any = None, timeout: float = 8) -> ApiResult:
    """Wraps every call to the hydownloader daemon API. Returns an ApiResult instead of
    raising, so callers can show a friendly message instead of crashing - same contract as
    the PS1's Invoke-DaemonApi."""
    api, reason = get_daemon_api_info()
    if not api:
        return ApiResult(False, None, f"hydownloader daemon API isn't reachable - {reason}")

    # Only send the header when there's an actual key - some setups run with
    # "daemon.do-not-check-access-key": true and no key configured at all, and the daemon
    # doesn't expect (or need) the header in that case.
    headers = {"HyDownloader-Access-Key": api.access_key} if api.access_key else {}
    url = f"{api.base_url}{route}"
    verify = False if api.scheme == "https" else True
    # This is always a loopback call, but requests trusts env/Windows-registry proxy settings
    # by default - a VPN client or corporate proxy tool configured system-wide will otherwise
    # intercept the request and break the connection instead of letting it go direct.
    no_proxy = {"http": None, "https": None}

    start = time.monotonic()
    success = False
    try:
        if body is not None:
            resp = _session.post(url, json=body, headers=headers, timeout=timeout, verify=verify, proxies=no_proxy)
        else:
            resp = _session.post(url, headers=headers, timeout=timeout, verify=verify, proxies=no_proxy)
        resp.raise_for_status()
        data = resp.json() if resp.content else None
        success = True
        return ApiResult(True, data, None)
    except requests.exceptions.HTTPError as e:
        detail = str(e)
        if e.response is not None and e.response.text:
            detail = f"{detail} -- {e.response.text.strip()}"
        return ApiResult(False, None, detail)
    except requests.exceptions.RequestException as e:
        return ApiResult(False, None, str(e))
    finally:
        _call_log.append({
            "route": route, "success": success,
            "duration_ms": round((time.monotonic() - start) * 1000, 1),
            "t": start,
        })


# --- Convenience wrappers for the specific routes this app uses ---

def url_info(urls: list[str]) -> ApiResult:
    return invoke_daemon_api("/url_info", {"urls": urls})


def add_or_update_subscriptions(items: list[dict]) -> ApiResult:
    return invoke_daemon_api("/add_or_update_subscriptions", items)


def get_subscriptions() -> ApiResult:
    # hydownloader's route_get_subscriptions does `'from' in bottle.request.json` with no
    # null-check - sending an empty object (not no body at all) keeps that a dict so the
    # check evaluates to False instead of a bare 500. Same reasoning as the PS1 version.
    return invoke_daemon_api("/get_subscriptions", {})


def delete_subscriptions(ids: list[int]) -> ApiResult:
    return invoke_daemon_api("/delete_subscriptions", {"ids": ids})


def add_or_update_urls(urls: list[str], file_filter: str | None = None) -> ApiResult:
    """file_filter, when given, is hydownloader's per-URL "filter" column (its own
    gallery-dl --filter passthrough - see subscriptions.DEFAULT_FILE_FILTER) - callers pass
    that default so a one-off download gets the same video block a subscription does."""
    entries = []
    for u in urls:
        e = {"url": u}
        if file_filter:
            e["filter"] = file_filter
        entries.append(e)
    return invoke_daemon_api("/add_or_update_urls", entries)


def get_queued_urls() -> ApiResult:
    return invoke_daemon_api("/get_queued_urls")


# Short-TTL caches for the two calls that get fetched multiple times per poll cycle:
# get_subscription_checks(ids) is called independently (same ids) by get_latest_checks,
# get_total_downloads, and get_failure_status within a single /partials/new-files request, and
# get_status_info() is polled every 2s by three separate DOM elements in the girly layout
# (index.html's triplicated /partials/status widgets). Caching means only the first caller in
# a burst actually hits the network; everyone else in the same tick reuses that result. TTL is
# short enough that it never returns data staler than the poll interval it's serving.
_subscription_checks_cache: dict[tuple[int, ...], tuple[float, ApiResult]] = {}
_SUBSCRIPTION_CHECKS_TTL = 2.0
_status_info_cache: tuple[float, ApiResult] | None = None
_STATUS_INFO_TTL = 1.5


def get_subscription_checks(ids: list[int]) -> ApiResult:
    # hydownloader's subscription_checks table is the per-run history (new/skipped file counts,
    # status, start/end time) behind a subscription's single "last result" summary - there's no
    # "give me just the latest one" filter in the API, so callers get everything non-archived
    # for the given IDs and reduce client-side (see subscriptions.get_latest_checks/
    # get_check_history). Not filtering by ID at all (empty list) would return the whole
    # history table, which nothing here wants, so ids is required rather than optional.
    key = tuple(sorted(ids))
    now = time.monotonic()
    cached = _subscription_checks_cache.get(key)
    if cached is not None and now - cached[0] < _SUBSCRIPTION_CHECKS_TTL:
        return cached[1]
    result = invoke_daemon_api("/get_subscription_checks", {"ids": ids})
    _subscription_checks_cache[key] = (now, result)
    return result


def get_status_info() -> ApiResult:
    global _status_info_cache
    now = time.monotonic()
    if _status_info_cache is not None and now - _status_info_cache[0] < _STATUS_INFO_TTL:
        return _status_info_cache[1]
    result = invoke_daemon_api("/get_status_info")
    _status_info_cache = (now, result)
    return result


def shutdown() -> ApiResult:
    return invoke_daemon_api("/shutdown")
