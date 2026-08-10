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

import re
import subprocess
from pathlib import Path
from typing import Optional

from . import __version__

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_FILE = _REPO_ROOT / "CHANGELOG.md"
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


def check_for_update() -> dict:
    """On-demand, uncached counterpart to get_version_info()'s cached "stale" status - run a
    real `git fetch` first so this reflects what's actually on the remote right now, not just
    whatever origin/master happened to point at when this process started. Returns
    {update_available, status, error}; status mirrors get_version_info()'s "current"/"stale"/
    "unknown" values so the caller can reuse the same button copy either way."""
    fetch_ok = _git("fetch", "--quiet", "origin", "master") is not None
    head = _git("rev-parse", "HEAD")
    remote = _git("rev-parse", "origin/master")

    if head is None:
        return {"update_available": False, "status": "unknown", "error": "not a git checkout"}
    if not fetch_ok or remote is None:
        return {"update_available": False, "status": "unknown", "error": "couldn't reach origin"}

    update_available = head != remote
    return {
        "update_available": update_available,
        "status": "stale" if update_available else "current",
        "error": None,
    }


# ------------------------------------------------------------------------------- changelog
# CHANGELOG.md format: "## <version>" headers (newest first) followed by "- " bullet lines,
# parsed with a plain regex rather than a Markdown library - the file only ever needs to be
# read by this one function, and the format is fully within this project's control.
_VERSION_HEADER_RE = re.compile(r"^##\s+(\S+)", re.MULTILINE)


def _parse_changelog() -> list[dict]:
    """Returns [{"version": "1.1.0", "entries": ["...", ...]}, ...], newest-first as they
    appear in the file. Empty list if CHANGELOG.md is missing or unparsable - a missing
    changelog should degrade to "nothing to show", not break the dashboard."""
    try:
        text = _CHANGELOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return []

    headers = list(_VERSION_HEADER_RE.finditer(text))
    sections = []
    for i, m in enumerate(headers):
        version = m.group(1)
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]
        entries = [
            line.strip().lstrip("-").strip()
            for line in body.splitlines()
            if line.strip().startswith("-")
        ]
        sections.append({"version": version, "entries": entries})
    return sections


def get_changelog() -> list[dict]:
    """Full changelog, newest version first - backs the Changelog tab."""
    return _parse_changelog()


def get_changes_since(last_seen_version: Optional[str]) -> list[dict]:
    """Changelog sections newer than `last_seen_version` (exclusive), for the top-bar ribbon.
    If `last_seen_version` is None/unknown or already matches the current version, falls back
    to just the current version's own entries - the ribbon should always have something
    concrete to say about "what's new here", not go blank once you're caught up."""
    sections = _parse_changelog()
    if not sections:
        return []
    if last_seen_version:
        newer = []
        for section in sections:
            if section["version"] == last_seen_version:
                break
            newer.append(section)
        if newer:
            return newer
    return sections[:1]
