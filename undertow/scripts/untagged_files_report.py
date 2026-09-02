"""
Finds files in Hydrus with zero tags on the local tag service - easy to lose track of after a
big bulk import, and otherwise only discoverable by manually building a "system:number of tags
= 0" search inside the Hydrus client itself.
"""

from __future__ import annotations

from _common import hydrus_client, section


def main() -> int:
    key, reason = hydrus_client.get_local_tag_service_key()
    if not key:
        print(f"ERROR: {reason}")
        return 1

    resp = hydrus_client.search_files(["system:number of tags = 0"])
    if not resp.success:
        print(f"ERROR: couldn't search Hydrus - {resp.error}")
        return 1
    file_ids = (resp.data or {}).get("file_ids", [])

    section("Untagged files")
    print(f"Files with zero tags: {len(file_ids):,}")
    if not file_ids:
        print("Nothing to do - every file has at least one tag.")
        return 0

    shown = file_ids[:25]
    print(f"\nFirst {len(shown)} file_id(s):")
    for fid in shown:
        print(f"  {fid}")
    if len(file_ids) > len(shown):
        print(f"  ... and {len(file_ids) - len(shown):,} more")
    print("\nOpen these in Hydrus with the search: system:number of tags = 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
