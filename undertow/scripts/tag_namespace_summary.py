"""
Reports how many tags (and total file-tag associations) exist under each common namespace on
the local tag service - a quick "what's actually in my tag soup" overview instead of guessing.
"""

from __future__ import annotations

from _common import hydrus_client, section

NAMESPACES = [
    "creator", "character", "series", "meta", "studio", "performer",
    "person", "title", "site", "medium", "genre",
]


def main() -> int:
    key, reason = hydrus_client.get_local_tag_service_key()
    if not key:
        print(f"ERROR: {reason}")
        return 1

    section("Tag namespace summary")
    for ns in NAMESPACES:
        resp = hydrus_client.search_tags(f"{ns}:*", tag_service_key=key)
        if not resp.success:
            print(f"  {ns:<10}: ERROR - {resp.error}")
            continue
        entries = resp.data.get("tags", []) if isinstance(resp.data, dict) else []
        total_uses = sum(e.get("count", 0) for e in entries if isinstance(e, dict))
        print(f"  {ns:<10}: {len(entries):>6,} unique tag(s), {total_uses:>8,} use(s)")

    print("\nNamespace not listed but curious? Search e.g. 'yournamespace:*' directly in Hydrus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
