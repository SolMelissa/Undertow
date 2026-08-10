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

# Shared, connection-pooled session instead of bare requests.get/post - the Media tab's grid can
# fire off dozens of thumbnail requests for a single page view (see webui.py's /media/thumbnail
# proxy), and each of those used to open a brand new TCP connection to Hydrus's local API before
# this. A pooled Session keeps those connections alive and reuses them, which is the difference
# between "48 fresh handshakes" and "48 requests over a handful of warm connections" on every
# Media tab page load - the single biggest win available here since the actual byte transfer is
# already loopback-fast. Thread-safe for concurrent requests (this Flask app runs threaded=True;
# requests.Session's connection pool is designed for exactly this).
_session = requests.Session()
_session.mount("http://", requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=16))


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
    # Bypass system/IE proxy config, same reasoning as api_client.invoke_daemon_api - this is
    # always a loopback call and shouldn't be routed through a VPN/corporate proxy.
    no_proxy = {"http": None, "https": None}
    try:
        resp = _session.get(url, params=params, headers=headers, timeout=timeout, verify=False, proxies=no_proxy)
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


def invoke_hydrus_api_post(route: str, body: dict | None = None, timeout: float = 8) -> ApiResult:
    """POST counterpart to invoke_hydrus_api - every Hydrus route that actually changes state
    (adding/deleting tags, tag siblings/parents) is POST with a JSON body, same split as
    api_client.invoke_daemon_api vs. a hypothetical GET-only daemon client."""
    api, reason = get_hydrus_api_info()
    if not api:
        return ApiResult(False, None, f"Hydrus Client API isn't reachable - {reason}")

    headers = {"Hydrus-Client-API-Access-Key": api.access_key}
    url = f"{api.base_url}{route}"
    # Bypass system/IE proxy config, same reasoning as invoke_hydrus_api above.
    no_proxy = {"http": None, "https": None}
    try:
        resp = _session.post(url, json=body or {}, headers=headers, timeout=timeout, verify=False, proxies=no_proxy)
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


def invoke_hydrus_api_raw(route: str, params: dict | None = None, timeout: float = 20) -> tuple[requests.Response | None, str | None]:
    """Like invoke_hydrus_api but returns the raw response (for binary bodies - thumbnails/file
    bytes) instead of trying to json-decode it. Callers stream .content straight through to the
    browser (see webui.py's /media/thumbnail and /media/file proxy routes) so the Hydrus access
    key never has to be exposed client-side."""
    api, reason = get_hydrus_api_info()
    if not api:
        return None, f"Hydrus Client API isn't reachable - {reason}"
    headers = {"Hydrus-Client-API-Access-Key": api.access_key}
    url = f"{api.base_url}{route}"
    # Bypass system/IE proxy config, same reasoning as invoke_hydrus_api above.
    no_proxy = {"http": None, "https": None}
    try:
        resp = _session.get(url, params=params, headers=headers, timeout=timeout, verify=False, stream=True, proxies=no_proxy)
        resp.raise_for_status()
        return resp, None
    except requests.exceptions.HTTPError as e:
        detail = str(e)
        if e.response is not None and e.response.text:
            detail = f"{detail} -- {e.response.text.strip()}"
        return None, detail
    except requests.exceptions.RequestException as e:
        return None, str(e)


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


# ------------------------------------------------------------------------------ media browsing
# Everything below backs the Media tab (undertow/media.py + webui.py's /media/* and
# /partials/media routes) - searching, thumbnails/file bytes, and tag/tag-relationship CRUD.
# Confirmed against a live instance (Hydrus 665 / API v88) and against the official docs at
# hydrusnetwork.github.io/hydrus/developer_api.html - two things an earlier pass guessed wrong
# and that are worth knowing if this ever needs revisiting:
#   - /add_tags/search_tags has NO way to scope counts to an arbitrary in-progress search - only
#     `file_service_key`/`tag_service_key`/`tag_display_type`. Counts are always whole-tag-domain,
#     not "how many of my current search results have this tag". Hydrus silently ignores unknown
#     query params instead of erroring, so a wrong param name here looks like it "works" until
#     you compare the numbers.
#   - There is no write endpoint for tag siblings/parents at all yet - only the read-only
#     GET /add_tags/get_siblings_and_parents. Hydrus's own docs say pair-based editing "will
#     appear in a different API request in future" - it doesn't exist today, on any route name.

def search_files(predicates: list[str], return_hashes: bool = False) -> ApiResult:
    """General-purpose search, unlike _search_file_count (which only returns a length). Callers
    pass a plain list of strings exactly as Hydrus's own search bar takes them - tags
    (`creator:foo`) and system predicates (`system:inbox`) mixed freely - with no client-side
    interpretation; Hydrus itself ANDs every entry in the list."""
    return invoke_hydrus_api(
        "/get_files/search_files",
        params={
            "tags": str(predicates).replace("'", '"'),
            "return_file_ids": "true",
            "return_hashes": str(return_hashes).lower(),
        },
    )


def get_file_metadata(file_ids: list[int]) -> ApiResult:
    """Per-file metadata for the detail view - dimensions, size, and (with include_service_keys_
    to_tags) every tag on the file grouped by tag service. Hydrus caps how many ids one call can
    take; callers doing full-library operations should chunk, but the browser's own page-sized
    grid (see media.py) never sends more than one page's worth."""
    return invoke_hydrus_api(
        "/get_files/file_metadata",
        params={
            "file_ids": str(file_ids),
            "include_service_keys_to_tags": "true",
        },
    )


def search_tags(query: str, tag_service_key: str | None = None) -> ApiResult:
    """Tag autocomplete with counts - the mechanism behind the Media tab's suggestion pool. Counts
    are whole-tag-domain (or whatever `tag_service_key` scopes to), NOT narrowed by any active
    search - confirmed live that /add_tags/search_tags has no parameter for that (see module
    docstring above). Still useful for "what tags exist / are common", just not "what tags would
    actually narrow my current results", which would need a much more expensive per-candidate
    search_files call to compute and isn't done here."""
    params: dict = {"search": query}
    if tag_service_key:
        params["tag_service_key"] = tag_service_key
    return invoke_hydrus_api("/add_tags/search_tags", params=params)


def get_services() -> ApiResult:
    """Every configured Hydrus service (tag repositories, file domains, ratings, ...) keyed by
    service key - needed to resolve which tag service key to scope add_tags/siblings/parents
    calls to (almost always the local "my tags" service for a single-user setup)."""
    return invoke_hydrus_api("/get_services")


def get_local_tag_service_key() -> tuple[str | None, str | None]:
    """Resolves the local ("my tags") tag service's key - returns (key, None) on success or
    (None, reason) otherwise. Not cached at this layer (get_services is a cheap, infrequent
    call - once per tag-edit action, not per page render) so a service added/removed in Hydrus
    is picked up on the next call rather than needing a restart."""
    resp = get_services()
    if not resp.success:
        return None, resp.error
    services_by_key = (resp.data or {}).get("services", {})
    for key, svc in services_by_key.items():
        if svc.get("type") == 5 and svc.get("name", "").lower() in ("my tags", "local tags"):
            return key, None
    # Fall back to the first local tag service (type 5) by whatever name Hydrus gave it.
    for key, svc in services_by_key.items():
        if svc.get("type") == 5:
            return key, None
    return None, "no local tag service found on this Hydrus instance"


def add_tags(file_ids: list[int], tags: list[str], tag_service_key: str) -> ApiResult:
    return invoke_hydrus_api_post(
        "/add_tags/add_tags",
        {"file_ids": file_ids, "service_keys_to_tags": {tag_service_key: tags}},
    )


def delete_tags(file_ids: list[int], tags: list[str], tag_service_key: str) -> ApiResult:
    """Hydrus's add_tags route also handles deletes - actions are passed per-tag-service as
    {action_code: [tags]}. Confirmed against the official docs: 0=add/local, 1=delete/local,
    2=pend, 3=rescind pend, 4=petition, 5=rescind petition (2-5 are repository-only actions this
    app never uses, since it only ever writes to the local tag service)."""
    return invoke_hydrus_api_post(
        "/add_tags/add_tags",
        {
            "file_ids": file_ids,
            "service_keys_to_actions_to_tags": {tag_service_key: {"1": tags}},
        },
    )


def get_siblings_and_parents(tags: list[str]) -> ApiResult:
    """Read-only - confirmed live that Hydrus's Client API has no write endpoint for tag
    siblings/parents yet (Hydrus's own docs: pair-based editing "will appear in a different API
    request in future"). Response is keyed by tag, then by tag service, each holding
    {siblings, ideal_tag, descendants (children), ancestors (parents)} - see
    media.get_tag_relationships for how the Media tab's read-only relationships modal
    consumes this."""
    return invoke_hydrus_api("/add_tags/get_siblings_and_parents", params={"tags": str(tags).replace("'", '"')})


def thumbnail_response(file_id: int) -> tuple[object | None, str | None]:
    return invoke_hydrus_api_raw("/get_files/thumbnail", params={"file_id": file_id})


def file_response(file_id: int) -> tuple[object | None, str | None]:
    return invoke_hydrus_api_raw("/get_files/file", params={"file_id": file_id})
