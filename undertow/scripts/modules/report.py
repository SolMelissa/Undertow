"""
Generic report-formatting helpers - no tag-cleanup-specific logic. Any script in this folder
that prints human-readable sizes, ages, or section headers should use these rather than
rolling its own formatting.
"""

from __future__ import annotations

__all__ = ["hr_size", "hr_age", "section", "unwrap_or_error", "require_local_tag_service_key"]


def hr_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:3.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}PB"


def hr_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def unwrap_or_error(resp):
    """Returns resp.data if the ApiResponse succeeded, else prints 'ERROR: {resp.error}' and
    returns None. Scripts should treat a None return as "print already handled, bail with 1"."""
    if not resp.success:
        print(f"ERROR: {resp.error}")
        return None
    return resp.data


def require_local_tag_service_key(hydrus_client_module):
    """Resolves the local tag service key via hydrus_client_module.get_local_tag_service_key(),
    or prints 'ERROR: {reason}' and returns None if it isn't available."""
    key, reason = hydrus_client_module.get_local_tag_service_key()
    if not key:
        print(f"ERROR: {reason}")
        return None
    return key
