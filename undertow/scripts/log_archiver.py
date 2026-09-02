"""
Housekeeping: zips log files in hydownloader-data/logs older than 14 days into a dated archive
under logs/archive/, then deletes archives older than 90 days. Recent logs are left completely
untouched - this is purely about the flat pile of daily logs that otherwise accumulates forever.
"""

from __future__ import annotations

import time
import zipfile
from datetime import datetime

from _common import config, section

KEEP_RAW_DAYS = 14
KEEP_ARCHIVE_DAYS = 90


def main() -> int:
    logs_dir = config.LOGS_DIR
    if not logs_dir.exists():
        print(f"Logs directory not found: {logs_dir}")
        return 1

    archive_dir = logs_dir / "archive"
    archive_dir.mkdir(exist_ok=True)

    now = time.time()
    cutoff = now - KEEP_RAW_DAYS * 86400
    old_files = [p for p in logs_dir.iterdir()
                 if p.is_file() and p.suffix != ".zip" and p.stat().st_mtime < cutoff]

    section("Archiving old logs")
    if not old_files:
        print(f"  No logs older than {KEEP_RAW_DAYS} days - nothing to archive.")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = archive_dir / f"logs-{stamp}.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in old_files:
                zf.write(p, arcname=p.name)
        for p in old_files:
            p.unlink()
        print(f"  Archived {len(old_files)} file(s) -> {archive_path}")

    section("Pruning old archives")
    archive_cutoff = now - KEEP_ARCHIVE_DAYS * 86400
    old_archives = [p for p in archive_dir.glob("*.zip") if p.stat().st_mtime < archive_cutoff]
    for p in old_archives:
        p.unlink()
        print(f"  Deleted {p.name}")
    if not old_archives:
        print(f"  No archives older than {KEEP_ARCHIVE_DAYS} days.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
