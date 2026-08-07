"""
Hydrus Client API client - the Hydrus-side counterpart to api_client.py (which only talks to
hydownloader). Until this module existed, the Hydrus API key configured in api_keys.py was
write-only: it got saved into hydownloader's import-jobs script for gallery-dl's own import
step, but nothing in this package ever actually called Hydrus's own API with it, so the
dashboards had zero visibility into Hydrus itself (file counts, inbox size) - only into
hydownloader's queue.

Full endpoint reference: the "help/client_api.html" shipped inside the Hydrus install is a
generic links page, not the real API reference - confirmed against a live local instance
instead (see get_services/search_files below), API version 88 / Hydrus 665 at the time of
writing.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests
import urllib3

from . import api_keys, config
from .api_client import ApiResult

# Hydrus's own SSL cert (when Client API SSL is enabled) has the same "not meant to be publicly
# trusted" situation api_client.py already suppresses the warning for.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class HydrusApiInfo:
    base_url: str
    access_key: str


def get_hydrus_api_info() -> tuple[HydrusApiInfo | None, str | None]:
    """Returns (info, None) on success, or (None, reason) on failure - same contract as
    api_client.get_daemon_api_info(). The key itself is read via api_keys.get_hydrus_key_status()
    rather than parsed again here - that function already owns extracting it from
    hydownloader's import-jobs script (the only place it's actually stored), so re-parsing it
    here would be a second copy of that regex to keep in sync."""
    ok, key = api_keys.get_hydrus_key_status()
    if not ok or not key:
        return None, "no Hydrus Client API key configured yet - see the API Keys panel"
    return HydrusApiInfo(base_url=config.get_hydrus_api_url(), access_key=key), None


def invoke_hydrus_api(route: str, params: dict | None = None, timeout: float = 8) -> ApiResult:
    """GET-based, unlike hydownloader's invoke_daemon_api (all POST) - the Hydrus Client API
    uses GET with query-string params for every read-only route this module calls. Returns an
    ApiResult with the same .accepted/.success contract as api_client's, so callers (and any
    future Hydrus write endpoints) can be handled identically."""
    api, reason = get_hydrus_api_info()
    if not api:
        return ApiResult(False, None, f"Hydrus Client API isn't reachable - {reason}")

    headers = {"Hydrus-Client-API-Access-Key": api.access_key}
    url = f"{api.base_url}{route}"
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
        resp.raise_for_status()
        data = resp.json() if resp.content else None
        return ApiResult(True, data, None)
    except requests.exceptions.HTTPError as e:
        detail = str(e)
        if e.response is not None and e.response.text:
            detail = f"{detail} -- {e.response.text.strip()}"
        return ApiResult(False, None, detail)
    except requests.exceptions.RequestException as e:
        return ApiResult(False, None, str(e))


def verify_access_key() -> ApiResult:
    """Confirms the configured key is actually valid and reachable, and reports what it's
    allowed to do - separate from get_hydrus_api_info(), which only checks that a key string
    exists locally, not that Hydrus will accept it."""
    return invoke_hydrus_api("/verify_access_key")


def _search_file_count(tags: list[str]) -> ApiResult:
    """Hydrus's API has no dedicated "count only" route - a file count means fetching the
    matching file_ids and taking len() of them (confirmed against a live instance: this is
    also exactly what the Hydrus client's own UI does internally for its search-result counts).
    Fine at this poll cadence (see hydrus_client_stats' caller - matches the "New Files" column's
    slow dedicated interval, not the fast per-second tick) - the response is a flat list of
    ints, so even a six-figure library stays a small payload."""
    return invoke_hydrus_api(
        "/get_files/search_files",
        params={"tags": str(tags).replace("'", '"'), "return_file_ids": "true", "return_hashes": "false"},
    )


@dataclass
class HydrusStats:
    reachable: bool
    error: str | None = None
    total_files: int | None = None
    inbox_count: int | None = None


def get_hydrus_stats() -> HydrusStats:
    """Best-effort Hydrus-side numbers for the dashboards - total files and inbox size, the
    two counts anyone actually glances at. Returns reachable=False (not an exception) on any
    failure, same "supplementary display data" contract as subscriptions.get_latest_checks:
    callers should show an unobtrusive "unavailable" state, not an error banner, since this is
    a nice-to-have widget, not something the pipeline's core function depends on."""
    total_resp = _search_file_count(["system:everything"])
    if not total_resp.success:
        return HydrusStats(reachable=False, error=total_resp.error)
    total_files = len((total_resp.data or {}).get("file_ids") or [])

    inbox_resp = _search_file_count(["system:inbox"])
    inbox_count = len((inbox_resp.data or {}).get("file_ids") or []) if inbox_resp.success else None

    return HydrusStats(reachable=True, total_files=total_files, inbox_count=inbox_count)
