"""
Media browsing/search backend for the webui's Media tab - the Hydrus-native counterpart to
MyPornApp's Search.cs, but without that app's fixed Genre/Star/Source facet schema: search
predicates here are plain strings exactly as Hydrus's own search bar takes them (tags and
system predicates mixed freely), and Hydrus does the ANDing/sibling-resolution server-side, so
there's no client-side filter logic to maintain. What *is* ported from MyPornApp is purely the
display/interaction shape - a removable "current search" pill list plus a live-count suggestion
pool - now driven by Hydrus's own tag-autocomplete endpoint instead of an in-memory recompute.

Active search state lives per-browser-session (see get_session_predicates/set_session_predicates)
rather than in a module-level global - MyPornApp's static PornCollection.CurrentPornsList was
fine for a single-user desktop app with one window, but this is a web dashboard that can have
multiple tabs/devices open against it at once, and they must not share one search.
"""

from __future__ import annotations

import hashlib

from . import hydrus_client

# Namespace -> pill color, so *any* namespace (not just a fixed Genre/Star/Source set) gets a
# consistent color. A handful of common namespaces get a fixed, deliberately-chosen color;
# anything else falls back to a deterministic hash-based color so it's still stable across
# renders/sessions without needing to be registered anywhere.
_NAMESPACE_COLORS: dict[str, str] = {
    "creator": "#ff6fa5",
    "character": "#39d3ff",
    "series": "#ffd166",
    "": "#8aa0a8",  # unnamespaced tags
}
_FALLBACK_PALETTE = ["#c792ea", "#82e0aa", "#f5b7b1", "#85c1e9", "#f7dc6f", "#a9dfbf"]


def namespace_of(tag: str) -> str:
    return tag.split(":", 1)[0] if ":" in tag else ""


def namespace_color(tag: str) -> str:
    ns = namespace_of(tag)
    if ns in _NAMESPACE_COLORS:
        return _NAMESPACE_COLORS[ns]
    idx = int(hashlib.sha1(ns.encode("utf-8")).hexdigest(), 16) % len(_FALLBACK_PALETTE)
    return _FALLBACK_PALETTE[idx]


# ------------------------------------------------------------------------- per-session search state
# Keyed by Flask session id (see webui.py's /media routes, which pass request.cookies/session).
# A plain module-level dict, same "worst case is a stale read on a decorative widget" tradeoff
# other in-memory caches in this codebase (api_client's _call_log) already accept - a lost
# search on process restart just means the user's search bar is empty again, not corrupted data.
_session_predicates: dict[str, list[str]] = {}


def get_session_predicates(session_id: str) -> list[str]:
    return list(_session_predicates.get(session_id, []))


def set_session_predicates(session_id: str, predicates: list[str]) -> None:
    _session_predicates[session_id] = list(predicates)


def add_predicate(session_id: str, predicate: str) -> list[str]:
    current = _session_predicates.setdefault(session_id, [])
    if predicate not in current:
        current.append(predicate)
    return list(current)


def remove_predicate(session_id: str, predicate: str) -> list[str]:
    current = _session_predicates.setdefault(session_id, [])
    if predicate in current:
        current.remove(predicate)
    return list(current)


def clear_predicates(session_id: str) -> None:
    _session_predicates.pop(session_id, None)


# --------------------------------------------------------------------------------------- search

def get_current_results(active_predicates: list[str]) -> tuple[list[int], str | None]:
    """Returns (file_ids, None) on success or ([], reason) on failure - thin wrapper over
    hydrus_client.search_files. An empty predicate list is a deliberate no-op (returns []
    immediately) rather than "system:everything" by default, so opening the Media tab with no
    search active doesn't try to render the whole library as a grid."""
    if not active_predicates:
        return [], None
    resp = hydrus_client.search_files(active_predicates)
    if not resp.success:
        return [], resp.error
    return list((resp.data or {}).get("file_ids") or []), None


def get_suggested_tags(active_predicates: list[str], query: str = "", limit: int = 50) -> tuple[list[tuple[str, int]], str | None]:
    """Suggestion pool for the search input: Hydrus tag autocomplete scoped to the current
    search context, already excluding tags redundant with active predicates. Returns
    ([(tag, count), ...], None) or ([], reason)."""
    resp = hydrus_client.search_tags(query, context_predicates=active_predicates or None)
    if not resp.success:
        return [], resp.error
    raw = (resp.data or {}).get("tags", [])
    active_set = set(active_predicates)
    results: list[tuple[str, int]] = []
    for entry in raw:
        # Hydrus's search_tags response shape has varied across API versions - handle both a
        # flat string list and a list of {"value": ..., "count": ...} objects.
        if isinstance(entry, dict):
            tag, count = entry.get("value", ""), entry.get("count", 0)
        else:
            tag, count = str(entry), 0
        if tag and tag not in active_set:
            results.append((tag, count))
    results.sort(key=lambda t: t[1], reverse=True)
    return results[:limit], None
