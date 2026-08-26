"""
Local, hydownloader-independent tags/groups for subscriptions - a small JSON sidecar file
keyed by subscription id, never touching hydownloader's own subscriptions table (there's no
"tags" column there, and adding one would mean forking/patching hydownloader's schema, which
is out of scope for a cockpit app that's supposed to sit on top of it unmodified). Mirrors
settings.py's shape: plain module-level load/save functions, no class, since there's exactly
one tags file.

JSON object keys are always strings, so subscription ids (ints on the hydownloader side) get
stringified going in and parsed back going out - every function here takes/returns the int id
callers actually work with; only load_tags()/save_tags() ever see the raw string-keyed form.
"""

from __future__ import annotations

import json

from . import config

TAGS_FILE = config.DATA_DIR / "hydrus-pipeline-tags.json"

# load_tags() gets called on every TUI table render (every 1.5s poll tick); the file only
# changes when save_tags() writes it (from this process or another), so cache on mtime rather
# than re-reading/re-parsing JSON off disk dozens of times a minute for no reason.
_cache: dict[int, list[str]] | None = None
_cache_mtime: float | None = None


def load_tags() -> dict[int, list[str]]:
    """Returns {subscription_id: [tags]} for every subscription that has at least one tag -
    ids with no tags simply aren't keys, not an empty-list entry. Falls back to {} on any
    read/parse failure (missing file, corrupt JSON), same "degrade rather than crash" contract
    as settings.load_settings()."""
    global _cache, _cache_mtime
    try:
        mtime = TAGS_FILE.stat().st_mtime
    except OSError:
        _cache, _cache_mtime = {}, None
        return {}
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    try:
        with open(TAGS_FILE, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        _cache, _cache_mtime = {}, None
        return {}
    if not isinstance(stored, dict):
        _cache, _cache_mtime = {}, None
        return {}
    result: dict[int, list[str]] = {}
    for k, v in stored.items():
        try:
            sub_id = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, list):
            result[sub_id] = [str(t) for t in v if str(t).strip()]
    _cache, _cache_mtime = result, mtime
    return result


def save_tags(tags_by_id: dict[int, list[str]]) -> None:
    """Overwrites the whole file with `tags_by_id` (not a merge - callers that only want to
    change one subscription's tags should read via load_tags(), mutate, then call this with
    the full dict, same pattern set_tags_for below follows). Never writes a BOM -
    open(..., "w", encoding="utf-8") only, per the project's own gotcha about PowerShell-style
    writes breaking json.load elsewhere. Subscriptions with an empty tag list are dropped
    entirely rather than stored as [], so the file doesn't accumulate empty entries forever."""
    stored = {str(sub_id): tags for sub_id, tags in tags_by_id.items() if tags}
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TAGS_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)


def get_tags_for(sub_id: int) -> list[str]:
    return load_tags().get(sub_id, [])


def set_tags_for(sub_id: int, tags: list[str]) -> None:
    """Sets (replacing, not appending to) `sub_id`'s tag list. Pass an empty list to clear a
    subscription's tags entirely - same as remove(sub_id), just without needing a second
    function name for it."""
    cleaned = sorted({t.strip() for t in tags if t.strip()})
    all_tags = load_tags()
    if cleaned:
        all_tags[sub_id] = cleaned
    else:
        all_tags.pop(sub_id, None)
    save_tags(all_tags)


def remove(sub_id: int) -> None:
    """Drops `sub_id`'s sidecar entry entirely - called on subscription delete (single and
    bulk) so the tags file doesn't accumulate orphaned entries for subscriptions that no
    longer exist. A no-op if the id had no tags to begin with."""
    all_tags = load_tags()
    if sub_id in all_tags:
        del all_tags[sub_id]
        save_tags(all_tags)


def all_tags() -> list[str]:
    """Every distinct tag currently in use, alphabetical - backs the tag-filter dropdown in
    both UIs."""
    seen: set[str] = set()
    for tags in load_tags().values():
        seen.update(tags)
    return sorted(seen)
