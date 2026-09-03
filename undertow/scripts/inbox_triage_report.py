"""
Buckets Hydrus's inbox (system:inbox) by how long each file has sat there - today, this week,
this month, older - so a backlog of unsorted imports is visible at a glance instead of scrolling
the inbox search inside the Hydrus client itself.
"""

from __future__ import annotations

import time

from _common import hr_age, hydrus_client, section

BUCKETS = [
    ("last 24h", 24 * 3600),
    ("last 7d", 7 * 24 * 3600),
    ("last 30d", 30 * 24 * 3600),
]
CHUNK = 256


def main() -> int:
    resp = hydrus_client.search_files(["system:inbox"])
    if not resp.success:
        print(f"ERROR: couldn't search Hydrus - {resp.error}")
        return 1
    file_ids = (resp.data or {}).get("file_ids", [])

    section("Inbox triage")
    print(f"Total inbox files: {len(file_ids):,}")
    if not file_ids:
        return 0

    now = time.time()
    counts = {label: 0 for label, _ in BUCKETS}
    counts["older"] = 0
    oldest: float | None = None

    for start in range(0, len(file_ids), CHUNK):
        chunk = file_ids[start:start + CHUNK]
        meta = hydrus_client.get_file_metadata(chunk, include_tags=False)
        if not meta.success:
            continue
        for m in (meta.data or {}).get("metadata", []):
            current = (m.get("file_services") or {}).get("current") or {}
            imported_times = [v.get("time_imported") for v in current.values() if v.get("time_imported")]
            if not imported_times:
                continue
            imported = min(imported_times)
            age = now - imported
            if oldest is None or imported < oldest:
                oldest = imported
            for label, window in BUCKETS:
                if age <= window:
                    counts[label] += 1
                    break
            else:
                counts["older"] += 1
        print(f"  ...scanned {min(start + CHUNK, len(file_ids)):,}/{len(file_ids):,}", end="\r")
    print()

    for label, _ in BUCKETS:
        print(f"  {label:<10}: {counts[label]:,}")
    print(f"  {'older':<10}: {counts['older']:,}")
    if oldest is not None:
        print(f"\nOldest inbox file: {hr_age(now - oldest)} ago")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
