"""
One-shot health check: pings the Hydrus Client API and the hydownloader daemon API, and prints
file/inbox counts, configured services, and daemon status - the "is everything up and talking
to itself" check that otherwise means opening two different UIs to answer.
"""

from __future__ import annotations

from _common import api_client, hydrus_client, section


def main() -> int:
    section("Hydrus Client API")
    verify = hydrus_client.verify_access_key()
    if not verify.success:
        print(f"  UNREACHABLE - {verify.error}")
    else:
        print("  OK - access key verified")
        stats = hydrus_client.get_hydrus_stats()
        if stats.reachable:
            print(f"  Total files: {stats.total_files:,}")
            print(f"  Inbox:       {stats.inbox_count:,}")
        services_resp = hydrus_client.get_services()
        if services_resp.success:
            svc_dict = (services_resp.data or {}).get("services", {})
            print(f"  Services configured: {len(svc_dict)}")
            for _key, svc in sorted(svc_dict.items(), key=lambda kv: kv[1].get("name", "")):
                print(f"    - {svc.get('name', '?'):<24} ({svc.get('type_pretty', '?')})")

    section("hydownloader daemon API")
    status = api_client.get_status_info()
    if not status.success:
        print(f"  UNREACHABLE - {status.error}")
    else:
        print("  OK - daemon responding")
        for k, v in (status.data or {}).items():
            print(f"    {k}: {v}")

    subs = api_client.get_subscriptions()
    if subs.success:
        items = subs.data if isinstance(subs.data, list) else []
        active = sum(1 for s in items if not s.get("paused"))
        print(f"  Subscriptions: {len(items)} total, {active} active")
    else:
        print(f"  Couldn't list subscriptions - {subs.error}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
