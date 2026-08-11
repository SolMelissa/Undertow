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
import math

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


# Shape filter: system:ratio predicates rather than a client-side hide, so switching shapes
# re-runs the actual Hydrus search and fills a whole page with that shape instead of pruning
# down whatever page of unfiltered results happened to already be loaded. Thresholds (ratio
# >1.15 landscape, <0.87 portrait, else square) match undertowClassifyMediaCard()'s client-side
# per-thumbnail classification in index.html, expressed as the nearest clean ratio fractions.
_SHAPE_PREDICATES: dict[str, list[str]] = {
    "square": ["system:ratio wider than 20:23", "system:ratio taller than 23:20"],
    "portrait": ["system:ratio taller than 20:23"],
    "landscape": ["system:ratio wider than 23:20"],
}
_ALL_SHAPE_PREDICATES: set[str] = {p for preds in _SHAPE_PREDICATES.values() for p in preds}


def set_shape_filter(session_id: str, shape: str | None) -> None:
    """Swaps out any previously-applied shape predicate(s) for the ones matching `shape` (None
    clears the filter back to showing every shape)."""
    for p in list(get_session_predicates(session_id)):
        if p in _ALL_SHAPE_PREDICATES:
            remove_predicate(session_id, p)
    for p in _SHAPE_PREDICATES.get(shape or "", []):
        add_predicate(session_id, p)


def is_shape_predicate(predicate: str) -> bool:
    """Whether `predicate` is one of set_shape_filter's system:ratio predicates - used to hide
    it from the visible predicate-pill row (it's represented by the shape toggle buttons
    instead) while still keeping it in the actual search."""
    return predicate in _ALL_SHAPE_PREDICATES


def active_shape_filter(active_predicates: list[str]) -> str | None:
    """Which shape's predicate(s) (if any) are currently active, for highlighting the right
    shape button - inverse of set_shape_filter."""
    for shape, preds in _SHAPE_PREDICATES.items():
        if all(p in active_predicates for p in preds):
            return shape
    return None


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


# ---------------------------------------------------------------------------------- bulk tagging
# Backs the Tag Relations tab's "Bulk Tagging" and "Tag Migration" sub-tabs. Both are plain
# add_tags/delete_tags calls across a batch of files matched by a search - NOT the same thing as
# a real Hydrus tag sibling/parent relationship (which would make old_tag permanently redirect
# to new_tag for files added later too). Hydrus's Client API has no write endpoint for tag
# siblings/parents at all (see hydrus_client.get_siblings_and_parents's docstring) - migrate_tag
# below is the closest legitimate substitute: it fixes every file that matches *right now*, but
# is a one-time edit, not a standing relationship. Say so in the UI rather than implying otherwise.

def _bulk_tag_edit(predicates: list[str], tag: str, action) -> tuple[int, str | None]:
    file_ids, err = get_current_results(predicates)
    if err:
        return 0, err
    if not file_ids:
        return 0, None
    service_key, key_err = hydrus_client.get_local_tag_service_key()
    if not service_key:
        return 0, key_err
    resp = action(file_ids, [tag], service_key)
    if not resp.success:
        return 0, resp.error
    return len(file_ids), None


def bulk_add_tag(predicates: list[str], tag: str) -> tuple[int, str | None]:
    """Adds `tag` to every file currently matching `predicates`. Returns (files touched, None)
    or (0, reason)."""
    return _bulk_tag_edit(predicates, tag, hydrus_client.add_tags)


def bulk_remove_tag(predicates: list[str], tag: str) -> tuple[int, str | None]:
    """Removes `tag` from every file currently matching `predicates`. Returns (files touched,
    None) or (0, reason)."""
    return _bulk_tag_edit(predicates, tag, hydrus_client.delete_tags)


def migrate_tag(old_tag: str, new_tag: str, extra_predicates: list[str] | None = None) -> tuple[int, str | None]:
    """"Renames"/merges old_tag -> new_tag across every file that has old_tag right now (plus
    any extra_predicates to narrow the scope further): adds new_tag then removes old_tag on
    each matching file. This is a one-time edit of the tags actually on files today, NOT a real
    Hydrus sibling relationship - see the module note above. Returns (files touched, None) or
    (0, reason); if the add half succeeds but the delete half fails, the file count still
    reflects files that got new_tag (they're not left worse off), with the delete failure
    reported in the error message."""
    predicates = [old_tag, *(extra_predicates or [])]
    file_ids, err = get_current_results(predicates)
    if err:
        return 0, err
    if not file_ids:
        return 0, None
    service_key, key_err = hydrus_client.get_local_tag_service_key()
    if not service_key:
        return 0, key_err
    add_resp = hydrus_client.add_tags(file_ids, [new_tag], service_key)
    if not add_resp.success:
        return 0, add_resp.error
    del_resp = hydrus_client.delete_tags(file_ids, [old_tag], service_key)
    if not del_resp.success:
        return len(file_ids), f"added {new_tag} to {len(file_ids)} file(s), but failed to remove {old_tag}: {del_resp.error}"
    return len(file_ids), None


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


def _tag_reference_count(tag: str) -> int:
    """Whole-tag-domain reference count for one exact tag, via the same autocomplete endpoint
    get_suggested_tags uses - Hydrus's search_tags matches on substring, so this hunts the
    response for the entry whose value is an exact match rather than trusting the first hit."""
    resp = hydrus_client.search_tags(tag)
    if not resp.success:
        return 0
    for entry in (resp.data or {}).get("tags", []):
        if isinstance(entry, dict) and entry.get("value") == tag:
            return entry.get("count", 0) or 0
    return 0


_CONNECTED_TAGS_MAX_CANDIDATES = 30
_CONNECTED_TAGS_PREVIEW_TOP_N = 8


def get_connected_tags(active_predicates: list[str]) -> tuple[list[dict], str | None]:
    """Union of every active tag predicate's siblings/parents/children (1 level, system:
    predicates skipped since they have no tag relationships), each annotated with its
    whole-library reference count and sorted by that count descending - so the Media tab's
    "Connected" section surfaces the most-used related tags first regardless of which of your
    several active tags they came from. Capped at _CONNECTED_TAGS_MAX_CANDIDATES tags (each
    needs its own count lookup) and, since a preview add-count needs a full search_files call
    per candidate, that preview is only computed for the top _CONNECTED_TAGS_PREVIEW_TOP_N by
    reference count. Returns ([{"tag", "kind", "via", "count", "preview_count"}, ...], None) or
    ([], reason) - a lookup failure on one predicate is skipped rather than failing the whole
    section, since partial connections are still useful."""
    tag_predicates = [p for p in active_predicates if not p.startswith("system:")]
    if not tag_predicates:
        return [], None

    active_set = set(active_predicates)
    seen: dict[str, dict] = {}
    last_err: str | None = None
    for p in tag_predicates:
        if len(seen) >= _CONNECTED_TAGS_MAX_CANDIDATES:
            break
        relationships, err = get_tag_relationships(p)
        if err:
            last_err = err
            continue
        for kind, key in (("sibling", "siblings"), ("parent", "parents"), ("child", "children")):
            for t in relationships.get(key, []):
                if t in active_set or t in seen:
                    continue
                seen[t] = {"tag": t, "kind": kind, "via": p}
                if len(seen) >= _CONNECTED_TAGS_MAX_CANDIDATES:
                    break

    if not seen:
        return [], (last_err if last_err else None)

    candidates = list(seen.values())
    for c in candidates:
        c["count"] = _tag_reference_count(c["tag"])
    candidates.sort(key=lambda c: c["count"], reverse=True)

    for c in candidates[:_CONNECTED_TAGS_PREVIEW_TOP_N]:
        resp = hydrus_client.search_files(active_predicates + [c["tag"]])
        c["preview_count"] = len((resp.data or {}).get("file_ids") or []) if resp.success else None
    for c in candidates[_CONNECTED_TAGS_PREVIEW_TOP_N:]:
        c["preview_count"] = None

    return candidates, None


_TAG_MAP_MAX_LOOKUPS = 60


def get_tag_family_map(tag: str, depth: int = 2) -> tuple[dict, str | None]:
    """Family-tree view of `tag`: its siblings inline, plus parents (ancestors, expanded upward)
    and children (descendants, expanded downward), each recursively expanded up to `depth`
    levels. Hydrus's siblings/parents API is one round trip per tag, so a wide+deep tree could
    otherwise fan out into hundreds of requests - capped at _TAG_MAP_MAX_LOOKUPS total lookups
    (shared across both directions), after which further branches are shown as leaf nodes with
    no further expansion. Returns ({"tag", "siblings", "ancestors", "descendants"}, None) or
    ({}, reason)."""
    depth = max(1, min(depth, 4))
    lookups = 0

    def expand(t: str, key: str, remaining: int, seen: set[str]) -> dict:
        nonlocal lookups
        node = {"tag": t, "siblings": [], "next": []}
        if lookups >= _TAG_MAP_MAX_LOOKUPS:
            return node
        rel, err = get_tag_relationships(t)
        lookups += 1
        if err:
            return node
        node["siblings"] = rel.get("siblings", [])
        if remaining <= 0:
            return node
        for nt in rel.get(key, []):
            if nt in seen:
                continue
            seen.add(nt)
            node["next"].append(expand(nt, key, remaining - 1, seen))
        return node

    root_rel, root_err = get_tag_relationships(tag)
    lookups += 1
    if root_err:
        return {}, root_err

    seen_up = {tag}
    ancestors = []
    for p in root_rel.get("parents", []):
        if p in seen_up:
            continue
        seen_up.add(p)
        ancestors.append(expand(p, "parents", depth - 1, seen_up))

    seen_down = {tag}
    descendants = []
    for c in root_rel.get("children", []):
        if c in seen_down:
            continue
        seen_down.add(c)
        descendants.append(expand(c, "children", depth - 1, seen_down))

    return {
        "tag": tag,
        "siblings": root_rel.get("siblings", []),
        "ancestors": ancestors,
        "descendants": descendants,
    }, None


def layout_tag_family_radial(family: dict, width: int = 900, height: int = 560, max_nodes: int = 36) -> dict:
    """Turns get_tag_family_map()'s nested ancestor/descendant trees into absolute (x, y) node
    positions and parent-child edges for a hub-and-spoke render: the searched tag sits at the
    center, ancestors fan out in concentric rings to the left (one ring per level - closer ring
    = more directly connected), descendants fan out the same way to the right, and each node's
    siblings are listed as a small satellite label right next to it rather than their own ring
    (they're "the same tag" for search purposes, not a separate hop). Capped at `max_nodes`
    total real graph nodes (siblings don't count against the cap) - a wide/deep tree beyond that
    is silently dropped rather than overlapping labels into illegibility; `truncated` in the
    return value counts how many were dropped so the caller can say so."""
    cx, cy = width / 2, height / 2
    max_radius = min(width, height) / 2 - 70
    nodes: list[dict] = []
    edges: list[dict] = []
    truncated = 0

    def walk(items: list[dict], level: int, parent_id: int, angle_center: float, angle_spread: float) -> None:
        nonlocal truncated
        if not items:
            return
        radius = min(max_radius, 90 + level * 110)
        n = len(items)
        angles = [angle_center] if n == 1 else [
            angle_center - angle_spread / 2 + angle_spread * i / (n - 1) for i in range(n)
        ]
        # Each branch gets a narrower slice of arc for its own children, so a deep chain
        # converges toward a straight spoke instead of re-fanning to the full width every level.
        child_spread = max(angle_spread / max(n, 1), 16)
        for item, angle in zip(items, angles):
            if len(nodes) >= max_nodes:
                truncated += 1
                continue
            rad = math.radians(angle)
            x = cx + radius * math.cos(rad)
            y = cy + radius * math.sin(rad)
            node_id = len(nodes)
            nodes.append({
                "id": node_id, "tag": item["tag"], "siblings": item.get("siblings", []),
                "x": round(x, 1), "y": round(y, 1), "level": level,
            })
            edges.append({"from": parent_id, "to": node_id})
            walk(item.get("next", []), level + 1, node_id, angle, child_spread)

    root_id = 0
    nodes.append({
        "id": root_id, "tag": family["tag"], "siblings": family.get("siblings", []),
        "x": cx, "y": cy, "level": 0,
    })
    walk(family.get("ancestors", []), 1, root_id, 180, 130)
    walk(family.get("descendants", []), 1, root_id, 0, 130)

    pos = {n["id"]: (n["x"], n["y"]) for n in nodes}
    for e in edges:
        e["x1"], e["y1"] = pos[e["from"]]
        e["x2"], e["y2"] = pos[e["to"]]

    return {"width": width, "height": height, "nodes": nodes, "edges": edges, "truncated": truncated}


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
