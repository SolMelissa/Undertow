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
    """Suggestion pool for the search input: Hydrus tag autocomplete, excluding tags already in
    the active predicate list. Counts are whole-tag-domain, not narrowed by the active search -
    confirmed live that Hydrus's Client API has no way to scope /add_tags/search_tags counts to
    an arbitrary in-progress search (see hydrus_client.search_tags's docstring); an earlier pass
    assumed otherwise and was wrong. Returns ([(tag, count), ...], None) or ([], reason)."""
    resp = hydrus_client.search_tags(query)
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


def get_tag_relationships(tag: str) -> tuple[dict, str | None]:
    """Read-only siblings/parents/children lookup for a single tag - returns
    ({"ideal_tag", "siblings", "parents", "children"}, None) or ({}, reason). Flattens
    Hydrus's per-tag-service response (see hydrus_client.get_siblings_and_parents) into one
    merged view, since a single-user setup mostly cares about "what are all of this tag's
    relationships", not which specific tag service each one happens to live on."""
    resp = hydrus_client.get_siblings_and_parents([tag])
    if not resp.success:
        return {}, resp.error
    per_service = ((resp.data or {}).get("tags") or {}).get(tag) or {}
    siblings: set[str] = set()
    children: set[str] = set()
    parents: set[str] = set()
    ideal_tag = tag
    for svc_data in per_service.values():
        siblings.update(svc_data.get("siblings") or [])
        children.update(svc_data.get("descendants") or [])
        parents.update(svc_data.get("ancestors") or [])
        svc_ideal = svc_data.get("ideal_tag")
        if svc_ideal and svc_ideal != tag:
            ideal_tag = svc_ideal
    siblings.discard(tag)
    return {
        "ideal_tag": ideal_tag,
        "siblings": sorted(siblings),
        "parents": sorted(parents),
        "children": sorted(children),
    }, None


def flatten_tags(file_metadata_entry: dict) -> set[str]:
    """Every stored tag on a single /get_files/file_metadata entry, across all tag services -
    shared by the detail view and get_similar_files below so they can't drift out of sync on
    what counts as "this file's tags"."""
    all_tags: set[str] = set()
    for svc_data in (file_metadata_entry.get("tags") or {}).values():
        for tag_list in (svc_data.get("storage_tags") or {}).values():
            all_tags.update(tag_list)
    return all_tags


# ---------------------------------------------------------------------------- "files like this"
# The Hydrus-native counterpart to MyPornApp's SuggestPorns.cs: that app scored every other file
# in memory by Levenshtein title distance plus a fixed "-20/genre match, -50/star match" bonus
# (i.e. treating "star" overlap as a much stronger similarity signal than "genre" overlap).
# There's no in-memory collection to scan here, and no fixed genre/star schema to hang a fixed
# weight off of, so this generalizes that idea instead of copying the constants: a shared tag's
# weight is 1/(how many candidate files carry it) - a tag only a handful of candidates share is
# a stronger signal than one dozens of them share, the same underlying theory (rare traits in
# common matter more than common ones) without hardcoding which namespace is "the rare one".

def get_similar_files(file_id: int, limit: int = 12, max_seed_tags: int = 8, max_candidates: int = 300) -> tuple[list[dict], str | None]:
    """Returns ([{"file_id", "score", "shared_tags"}, ...], None) ranked best-first, or
    ([], reason) on failure. A file with no tags at all returns ([], None) - not an error, just
    nothing to compare against."""
    meta_resp = hydrus_client.get_file_metadata([file_id])
    if not meta_resp.success:
        return [], meta_resp.error
    entries = (meta_resp.data or {}).get("metadata") or []
    if not entries:
        return [], "file not found"

    seed_tags = flatten_tags(entries[0])
    if not seed_tags:
        return [], None

    # Capped, not "all tags" - a heavily-tagged file shouldn't trigger dozens of HTTP round
    # trips just to open its detail modal. Sorted only for determinism across runs, not
    # significance.
    seed_list = sorted(seed_tags)[:max_seed_tags]

    per_tag_candidates: dict[str, set[int]] = {}
    for tag in seed_list:
        resp = hydrus_client.search_files([tag])
        if not resp.success:
            continue
        ids = set((resp.data or {}).get("file_ids") or [])
        ids.discard(file_id)
        if ids:
            per_tag_candidates[tag] = ids

    if not per_tag_candidates:
        return [], None

    all_candidate_ids = set().union(*per_tag_candidates.values())
    # Cap the candidate pool itself too (an extremely common seed tag could otherwise pull in
    # thousands of ids) - which ones get dropped here doesn't matter much, since anything
    # actually similar will keep showing up via its other shared tags too.
    if len(all_candidate_ids) > max_candidates:
        all_candidate_ids = set(list(all_candidate_ids)[:max_candidates])

    tag_weight = {tag: 1.0 / max(len(ids), 1) for tag, ids in per_tag_candidates.items()}
    scores: dict[int, float] = {}
    shared: dict[int, list[str]] = {}
    for tag, ids in per_tag_candidates.items():
        for cid in ids & all_candidate_ids:
            scores[cid] = scores.get(cid, 0.0) + tag_weight[tag]
            shared.setdefault(cid, []).append(tag)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [
        {"file_id": fid, "score": round(score, 3), "shared_tags": sorted(shared.get(fid, []))}
        for fid, score in ranked
    ], None
