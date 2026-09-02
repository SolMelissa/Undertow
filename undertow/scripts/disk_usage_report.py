"""
Walks the HydrusPipeline install's key folders (Hydrus's own directory, hydownloader-data,
logs) and reports their sizes plus free space on the install drive - a quick answer to "what's
eating my disk" without opening Explorer's slow folder-size properties dialog.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from _common import config, hr_size, section


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def main() -> int:
    section("Folder sizes")
    targets = [
        ("Hydrus install", config.HYDRUS_DIR),
        ("hydownloader-data", config.DATA_DIR),
        ("Logs", config.LOGS_DIR),
    ]
    for label, path in targets:
        if not path.exists():
            print(f"  {label:<20}: (not found: {path})")
            continue
        print(f"  {label:<20}: {hr_size(dir_size(path))}   ({path})")

    section("Biggest subfolders under hydownloader-data")
    if config.DATA_DIR.exists():
        subdirs = [p for p in config.DATA_DIR.iterdir() if p.is_dir()]
        sized = sorted(((p, dir_size(p)) for p in subdirs), key=lambda t: -t[1])
        for p, size in sized[:10]:
            print(f"  {hr_size(size):>10}  {p.name}")

    section("Free disk space")
    for drive in {config.INSTALL_ROOT.drive, config.HYDRUS_VOLUME_DRIVE}:
        if not drive:
            continue
        try:
            usage = shutil.disk_usage(drive + "\\")
            print(f"  {drive}  {hr_size(usage.free)} free of {hr_size(usage.total)}")
        except OSError as e:
            print(f"  {drive}  unavailable - {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
