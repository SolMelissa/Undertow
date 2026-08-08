"""
Tails hydownloader's main log (daemon.txt) for real-time visibility into what the daemon is
actually doing right now - which subscription/URL it's checking, what gallery-dl is finding
and downloading, and any errors - instead of just the aggregate counts /get_status_info gives
(queued/due totals, a short worker-status string). This is what the web/TUI dashboards were
missing: they showed the same numbers everywhere with nothing showing actual activity.

Read directly off disk instead of through the daemon's HTTP API: this package always runs on
the same machine as the daemon (config.py assumes a local install), so there's no network
round trip, no auth, and no risk of the API being unreachable independently affecting this.
"""

from __future__ import annotations

from . import config

# How many lines to seed a fresh dashboard/web session with on first load, so it isn't a blank
# pane until the next line gets written - and how far back to look for those lines, so a
# multi-GB daemon.txt doesn't get read into memory just to grab the tail.
_INITIAL_LINES = 200
_INITIAL_TAIL_BYTES = 262_144  # 256 KiB


def read_since(offset: int | None) -> tuple[list[str], int]:
    """offset=None means "first call": seeds with the last _INITIAL_LINES lines and returns a
    cursor positioned at end-of-file, so the *next* call only returns genuinely new lines.
    Otherwise returns whatever's been appended since the given byte offset, plus the new
    cursor to pass next time. Handles log rotation/truncation (offset now beyond the file's
    current size) by resetting to the start instead of raising.

    Binary mode throughout so the offset is an unambiguous byte count, comparable directly to
    os.stat().st_size - text-mode file.tell() cookies aren't guaranteed to be plain byte
    offsets and aren't safe to compare against stat() results.
    """
    if not config.DAEMON_LOG_FILE.exists():
        return [], 0

    if offset is None:
        with open(config.DAEMON_LOG_FILE, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            start = max(0, end - _INITIAL_TAIL_BYTES)
            f.seek(start)
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return [ln for ln in lines[-_INITIAL_LINES:] if ln.strip()], end

    with open(config.DAEMON_LOG_FILE, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if offset > size:
            offset = 0  # file was rotated/truncated since last read - start over
        f.seek(offset)
        data = f.read()
        new_offset = f.tell()

    lines = [ln for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    return lines, new_offset
