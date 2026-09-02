"""
Shared helpers for the standalone scripts in this folder. Not itself a runnable script -
scripts_runner.list_scripts() skips any filename starting with "_", same convention as a
Python-private module.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Scripts run as `python -u undertow/scripts/<name>.py` (see scripts_runner.start), which puts
# this directory at sys.path[0] - not the repo root - so `from undertow import ...` fails
# unless the repo root is added explicitly first, same fix tag_cleanup.py already needed.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from undertow import api_client, config, hydrus_client  # noqa: E402

__all__ = ["api_client", "config", "hydrus_client", "hr_size", "hr_age", "section"]


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
