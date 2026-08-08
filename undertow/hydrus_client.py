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


def invoke_hydrus_api_post(route: str, body: dict | None = None, timeout: float = 8) -> ApiResult:
    """POST counterpart to invoke_hydrus_api - every Hydrus route that actually changes state
    (adding/deleting tags, tag siblings/parents) is POST with a JSON body, same split as
    api_client.invoke_daemon_api vs. a hypothetical GET-only daemon client."""
    api, reason = get_hydrus_api_info()
    if not api:
        return ApiResult(False, None, f"Hydrus Client API isn't reachable - {reason}")

    headers = {"Hydrus-Client-API-Access-Key": api.access_key}
    url = f"{api.base_url}{route}"
    try:
        resp = requests.post(url, json=body or {}, headers=headers, timeout=timeout, verify=False)
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
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False, stream=True)
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
# Route names/payload shapes for the write endpoints (add_tags, tag siblings/parents) follow
# Hydrus's documented Client API naming convention (mirroring the already-confirmed
# /get_files/search_files and /verify_access_key above), but haven't been re-confirmed against a
# live instance the way this module's docstring says the existing routes were - do that first
# against a running Hydrus (see the plan's "spike" note) if anything here 404s.

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


def search_tags(query: str, context_predicates: list[str] | None = None, tag_service_key: str | None = None) -> ApiResult:
    """Tag autocomplete with counts - the mechanism behind the Media tab's suggestion pool and
    live in-context counts (see media.get_suggested_tags). `context_predicates`, when given, is
    the current active search - Hydrus's own client search box narrows autocomplete counts to
    the current search the same way; this needs confirming against the live API (see module
    docstring above) for the exact param name it expects for that context."""
    params: dict = {"search": query}
    if tag_service_key:
        params["tag_service_key"] = tag_service_key
    if context_predicates:
        params["tags"] = str(context_predicates).replace("'", '"')
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
    {tag: action} where 1=add, 2=delete (petition actions for non-local services aren't
    exposed here since this app only ever writes to the local tag service)."""
    return invoke_hydrus_api_post(
        "/add_tags/add_tags",
        {
            "file_ids": file_ids,
            "service_keys_to_actions_to_tags": {tag_service_key: {"2": tags}},
        },
    )


def get_tag_siblings(tags: list[str]) -> ApiResult:
    return invoke_hydrus_api("/add_tag_siblings/get_tag_siblings", params={"tags": str(tags).replace("'", '"')})


def set_tag_siblings(pairs: list[tuple[str, str]], tag_service_key: str, remove: bool = False) -> ApiResult:
    """`pairs` is [(bad_tag, ideal_tag), ...] - searching for bad_tag then behaves as if the
    file were tagged ideal_tag instead, same relationship Hydrus's own sibling manager edits."""
    action = "delete" if remove else "add"
    return invoke_hydrus_api_post(
        "/add_tag_siblings/set_tag_siblings",
        {
            "pairs": [
                {"bad_tag": bad, "ideal_tag": ideal, "action": action, "tag_service_key": tag_service_key}
                for bad, ideal in pairs
            ]
        },
    )


def get_tag_parents(tags: list[str]) -> ApiResult:
    return invoke_hydrus_api("/add_tag_parents/get_tag_parents", params={"tags": str(tags).replace("'", '"')})


def set_tag_parents(pairs: list[tuple[str, str]], tag_service_key: str, remove: bool = False) -> ApiResult:
    """`pairs` is [(child_tag, parent_tag), ...] - a file tagged child_tag automatically gains
    parent_tag too, same relationship Hydrus's own parent manager edits."""
    action = "delete" if remove else "add"
    return invoke_hydrus_api_post(
        "/add_tag_parents/set_tag_parents",
        {
            "pairs": [
                {"child_tag": child, "parent_tag": parent, "action": action, "tag_service_key": tag_service_key}
                for child, parent in pairs
            ]
        },
    )


def thumbnail_response(file_id: int) -> tuple[object | None, str | None]:
    return invoke_hydrus_api_raw("/get_files/thumbnail", params={"file_id": file_id})


def file_response(file_id: int) -> tuple[object | None, str | None]:
    return invoke_hydrus_api_raw("/get_files/file", params={"file_id": file_id})
