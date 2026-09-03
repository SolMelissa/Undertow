"""
Flags hydownloader subscriptions that look unhealthy - paused, failing their most recent check,
or never having actually pulled a file - instead of scrolling every subscription's history one
by one in the dashboard.
"""

from __future__ import annotations

from _common import api_client, section


def main() -> int:
    subs_resp = api_client.get_subscriptions()
    if not subs_resp.success:
        print(f"ERROR: {subs_resp.error}")
        return 1
    subs = subs_resp.data if isinstance(subs_resp.data, list) else []
    if not subs:
        print("No subscriptions configured.")
        return 0

    ids = [s.get("id") for s in subs if s.get("id") is not None]
    checks_resp = api_client.get_subscription_checks(ids)
    checks_by_id: dict[int, list[dict]] = {}
    if checks_resp.success:
        for c in (checks_resp.data or []):
            checks_by_id.setdefault(c.get("subscription_id"), []).append(c)

    section("Paused subscriptions")
    paused = [s for s in subs if s.get("paused")]
    for s in paused:
        print(f"  {s.get('keywords', '?')}")
    if not paused:
        print("  (none)")

    section("Subscriptions with zero downloads ever")
    zero = []
    for s in subs:
        history = checks_by_id.get(s.get("id"), [])
        total_new = sum(c.get("new_count", 0) for c in history)
        if history and total_new == 0:
            zero.append(s.get("keywords", "?"))
    for name in zero:
        print(f"  {name}")
    if not zero:
        print("  (none)")

    section("Subscriptions failing their most recent check")
    failing = []
    for s in subs:
        history = sorted(checks_by_id.get(s.get("id"), []), key=lambda c: c.get("time_seconds", 0))
        if not history:
            continue
        last = history[-1]
        status = str(last.get("status", "")).lower()
        if "error" in status or "fail" in status:
            failing.append((s.get("keywords", "?"), last.get("status")))
    for name, status in failing:
        print(f"  {name}: {status}")
    if not failing:
        print("  (none)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
