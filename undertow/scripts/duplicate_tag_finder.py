"""
Scans the local tag service for probable near-duplicate tags - same text but different casing
or stray whitespace - that should probably be merged as siblings but otherwise sit unnoticed as
two separate tags splitting a file's count. Read-only: reports the groups and their individual
file counts, never touches anything.
"""

from __future__ import annotations

from collections import defaultdict

from _common import hydrus_client, section


def normalize(tag: str) -> str:
    return " ".join(tag.strip().lower().split())


def main() -> int:
    key, reason = hydrus_client.get_local_tag_service_key()
    if not key:
        print(f"ERROR: {reason}")
        return 1

    resp = hydrus_client.search_tags("*", tag_service_key=key)
    if not resp.success:
        print(f"ERROR: couldn't fetch tags - {resp.error}")
        return 1
    entries = resp.data.get("tags", []) if isinstance(resp.data, dict) else []

    groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for e in entries:
        tag = e.get("tag") if isinstance(e, dict) else None
        count = e.get("count", 0) if isinstance(e, dict) else 0
        if not tag:
            continue
        groups[normalize(tag)].append((tag, count))

    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    section("Probable duplicate tags")
    print(f"Scanned {len(entries):,} tag(s), found {len(dupes)} group(s) with variants.\n")
    ranked = sorted(dupes.items(), key=lambda kv: -sum(c for _, c in kv[1]))
    for _norm, variants in ranked[:50]:
        variants_sorted = sorted(variants, key=lambda v: -v[1])
        parts = ", ".join(f"'{t}' ({c})" for t, c in variants_sorted)
        print(f"  {parts}")

    if len(dupes) > 50:
        print(f"\n... {len(dupes) - 50} more group(s) not shown.")
    if not dupes:
        print("No near-duplicate tags found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
