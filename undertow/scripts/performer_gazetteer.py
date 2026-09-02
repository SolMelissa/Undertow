"""
Standalone fetcher/builder for the performer-name gazetteer that tag_cleanup.py's optional
name-detection pass reads. Pulls performer names + known aliases from ThePornDB and/or
StashDB, normalizes each one the same way tag_cleanup.py tokenizes tags (so gazetteer
phrases match tag tokens exactly), and writes the merged result to performer-gazetteer.json
next to tag-cleanup-config.json.

Run it standalone: `python performer_gazetteer.py`. It walks you through entering (or
reusing saved) API keys for either/both sources, same local-config convention as
tag_cleanup.py's Hydrus connection. tag_cleanup.py never calls these APIs itself - it only
ever reads the cache file this script writes, so name detection stays off there until this
has been run at least once.

Hard dependency: requests. Also imports a few shared helpers (text normalization, local
JSON config storage) directly from the sibling tag_cleanup.py module.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import requests
except ImportError:
    print("This tool requires the 'requests' package: pip install requests", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tag_cleanup import (  # noqa: E402
    LOCAL_CONFIG_FILE, PERFORMER_GAZETTEER_CACHE_FILE, PerformerGazetteer,
    load_local_config, load_performer_gazetteer, normalize_token, prompt_secret,
    prompt_yes_no, save_local_config, split_camel_case,
)

TPDB_BASE_URL = "https://api.theporndb.net"
STASHDB_GRAPHQL_URL = "https://stashdb.org/graphql"


def _normalize_name_phrase(name: str) -> List[str]:
    text = split_camel_case(name)
    text = normalize_token(text)
    return [t for t in text.split(" ") if t]


def build_performer_gazetteer(raw_entries: List[Tuple[str, List[str]]]) -> PerformerGazetteer:
    """raw_entries is a list of (name, aliases) pairs pulled from one or more sources. Only
    multi-word names/aliases contribute - a single-word stage name can't corroborate itself
    for the adjacency check tag_cleanup.py relies on, so it would just become a silent
    single-token accept-list, the exact false-positive failure mode being avoided there.

    name_pairs stores the (first_token, last_token) ENDPOINTS of each real multi-word
    name/alias - not independent first-name/last-name sets - so tag_cleanup.py's adjacency
    check can require that a pair actually co-occurred, rather than accepting any first name
    next to any last name regardless of whether that specific pairing ever existed."""
    full_phrases = set()
    name_pairs = set()
    max_len = 2
    for name, aliases in raw_entries:
        for candidate in [name, *aliases]:
            if not candidate:
                continue
            tokens = _normalize_name_phrase(candidate)
            if len(tokens) < 2:
                continue
            full_phrases.add(" ".join(tokens))
            name_pairs.add((tokens[0], tokens[-1]))
            max_len = max(max_len, len(tokens))
    return PerformerGazetteer(full_name_phrases=full_phrases, name_pairs=name_pairs,
                               max_phrase_len=max_len)


def save_performer_gazetteer(gaz: PerformerGazetteer) -> None:
    payload = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "full_name_phrases": sorted(gaz.full_name_phrases),
        "name_pairs": sorted([first, last] for first, last in gaz.name_pairs),
        "max_phrase_len": gaz.max_phrase_len,
    }
    PERFORMER_GAZETTEER_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PERFORMER_GAZETTEER_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def fetch_theporndb_performer_names(api_key: str, on_progress=None) -> List[Tuple[str, List[str]]]:
    """Paginates ThePornDB's REST /performers listing. Field names for aliases vary slightly
    across their documented plugin integrations, so this checks a couple of plausible keys
    rather than assuming one - first real run is the way to confirm the exact shape."""
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"
    session.headers["Accept"] = "application/json"
    session.headers["User-Agent"] = "Undertow-tag-cleanup/1.0"

    results: List[Tuple[str, List[str]]] = []
    page = 1
    while True:
        resp = session.get(f"{TPDB_BASE_URL}/performers", params={"page": page}, timeout=30)
        if resp.status_code == 429:
            time.sleep(5)
            continue
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", [])
        if not data:
            break
        for p in data:
            name = p.get("name") or ""
            aliases = p.get("aliases")
            if aliases is None:
                extras = p.get("extras") or {}
                aliases = extras.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            results.append((name, list(aliases or [])))
        if on_progress:
            on_progress(len(results))
        meta = payload.get("meta") or {}
        last_page = meta.get("last_page")
        if last_page and page >= last_page:
            break
        if last_page is None and len(data) == 0:
            break
        page += 1
        time.sleep(0.2)
    return results


def fetch_stashdb_performer_names(api_key: str, on_progress=None) -> List[Tuple[str, List[str]]]:
    """Paginates StashDB's GraphQL queryPerformers (stash-box schema)."""
    session = requests.Session()
    session.headers["ApiKey"] = api_key
    session.headers["Content-Type"] = "application/json"
    query = """
    query QueryPerformers($page: Int!, $per_page: Int!) {
      queryPerformers(input: {page: $page, per_page: $per_page}) {
        count
        performers { name aliases }
      }
    }
    """

    results: List[Tuple[str, List[str]]] = []
    page = 1
    per_page = 100
    while True:
        resp = session.post(STASHDB_GRAPHQL_URL, json={
            "query": query, "variables": {"page": page, "per_page": per_page},
        }, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"StashDB GraphQL error: {payload['errors']}")
        block = (payload.get("data") or {}).get("queryPerformers") or {}
        performers = block.get("performers", [])
        if not performers:
            break
        for p in performers:
            results.append((p.get("name") or "", list(p.get("aliases") or [])))
        if on_progress:
            on_progress(len(results))
        if len(performers) < per_page:
            break
        page += 1
        time.sleep(0.1)
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch/refresh the performer-name gazetteer tag_cleanup.py uses for "
                     "optional name detection.",
        epilog="Just run `python performer_gazetteer.py` with no arguments - it walks you "
               "through the rest.",
    )
    p.add_argument("--reconfigure", action="store_true",
                    help="Ignore saved API keys and re-enter them from scratch")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    print("=== Performer-name gazetteer builder (ThePornDB / StashDB) ===")

    existing = load_performer_gazetteer()
    if existing:
        print(f"\nCurrently cached: {len(existing.full_name_phrases):,} full name(s)/alias(es).")

    saved = {} if args.reconfigure else load_local_config()

    tpdb_key = saved.get("theporndb_api_key")
    if prompt_yes_no("Fetch performers from ThePornDB?", default=bool(tpdb_key)):
        entered = prompt_secret("ThePornDB API key", has_saved=bool(tpdb_key))
        tpdb_key = entered or tpdb_key
        if tpdb_key:
            save_local_config({"theporndb_api_key": tpdb_key})
    else:
        tpdb_key = None

    stashdb_key = saved.get("stashdb_api_key")
    if prompt_yes_no("Fetch performers from StashDB?", default=bool(stashdb_key)):
        entered = prompt_secret("StashDB API key", has_saved=bool(stashdb_key))
        stashdb_key = entered or stashdb_key
        if stashdb_key:
            save_local_config({"stashdb_api_key": stashdb_key})
    else:
        stashdb_key = None

    if not tpdb_key and not stashdb_key:
        print("No API key provided - nothing to fetch, cache left unchanged.")
        return 0

    raw_entries: List[Tuple[str, List[str]]] = []
    had_failure = False
    if tpdb_key:
        print("\nFetching performers from ThePornDB (this can take a while on first run)...")
        try:
            raw_entries.extend(fetch_theporndb_performer_names(tpdb_key))
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"  ThePornDB fetch failed: {exc}", file=sys.stderr)
            had_failure = True
    if stashdb_key:
        print("Fetching performers from StashDB...")
        try:
            raw_entries.extend(fetch_stashdb_performer_names(stashdb_key))
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"  StashDB fetch failed: {exc}", file=sys.stderr)
            had_failure = True

    if not raw_entries:
        print("No performer data retrieved - cache left unchanged.")
        return 1

    gaz = build_performer_gazetteer(raw_entries)
    save_performer_gazetteer(gaz)
    print(f"\nBuilt performer gazetteer: {len(gaz.full_name_phrases):,} full name(s)/alias(es), "
          f"{len(gaz.name_pairs):,} known first+last name pair(s).")
    print(f"Saved to: {PERFORMER_GAZETTEER_CACHE_FILE}")
    return 1 if had_failure else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted, cache left unchanged.", file=sys.stderr)
        sys.exit(130)
