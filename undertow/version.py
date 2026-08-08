"""
Runtime version/freshness info for display in the app's titlebars (web dashboard tab title +
hero heading, TUI header). Answers "is the code that's actually running right now the same code
that's pushed to origin/master?" - useful because run.bat's git pull is best-effort (it silently
no-ops on diverged history, conflicts, or being offline), so a stale checkout can otherwise run
for a long time with no visible sign anything's out of date.

Git calls are best-effort and cheap but not free, so the result is computed once per process and
cached - it can't change without a restart (git pull happens only at launch, before this process's
Python even imports this module) other than someone pulling in a second terminal mid-session, which
is an edge case not worth polling for.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from . import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_cache: Optional[dict] = None


def _git(*args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_version_info() -> dict:
    """Returns {version, commit, dirty, status} where status is one of
    "current" (HEAD matches origin/master and no local changes), "stale" (behind/ahead/dirty),
    or "unknown" (not a git checkout, or git/network info unavailable)."""
    global _cache
    if _cache is not None:
        return _cache

    head = _git("rev-parse", "HEAD")
    remote = _git("rev-parse", "origin/master")
    dirty = bool(_git("status", "--porcelain"))

    if head is None:
        status = "unknown"
    elif remote is None:
        status = "unknown" if not dirty else "stale"
    elif head != remote or dirty:
        status = "stale"
    else:
        status = "current"

    _cache = {
        "version": __version__,
        "commit": head[:7] if head else "unknown",
        "dirty": dirty,
        "status": status,
    }
    return _cache
