"""
Reports hydownloader's current URL queue - how many URLs are waiting, grouped by status, and
which ones have been sitting there the longest - so a stalled queue is visible without paging
through the daemon's own admin UI.
"""

from __future__ import annotations

from collections import Counter

from _common import api_client, section


def main() -> int:
    resp = api_client.get_queued_urls()
    if not resp.success:
        print(f"ERROR: {resp.error}")
        return 1
    urls = resp.data if isinstance(resp.data, list) else []

    section("Queue summary")
    print(f"Total queued URLs: {len(urls):,}")
    if not urls:
        return 0

    by_status = Counter(u.get("status_text", u.get("status", "unknown")) for u in urls)
    for status, count in by_status.most_common():
        print(f"  {status}: {count:,}")

    section("Oldest queued entries")
    with_time = [u for u in urls if u.get("time_added")]
    with_time.sort(key=lambda u: u.get("time_added"))
    for u in with_time[:15]:
        print(f"  {u.get('url', '?')}  (added {u.get('time_added')})")
    if not with_time:
        print("  (no timestamp data available)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
