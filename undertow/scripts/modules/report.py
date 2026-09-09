"""
Generic report-formatting helpers - no tag-cleanup-specific logic. Any script in this folder
that prints human-readable sizes, ages, or section headers should use these rather than
rolling its own formatting.
"""

from __future__ import annotations

__all__ = ["hr_size", "hr_age", "section"]


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
