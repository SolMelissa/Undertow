"""
Removes empty directories left behind under hydownloader-data (gallery-dl and hydownloader both
create per-job/per-subscription staging folders that are supposed to empty out after import, but
occasionally leave a bare empty folder sitting around). Only ever deletes a directory that's
completely empty - never touches anything that still has files in it.
"""

from __future__ import annotations

from _common import config, section


def main() -> int:
    root = config.DATA_DIR
    if not root.exists():
        print(f"Directory not found: {root}")
        return 1

    section("Sweeping empty folders")
    removed = 0
    # Bottom-up (deepest paths first) so a folder that becomes empty after its own
    # (already-empty) children are removed is picked up in the same pass, not a second run.
    for dirpath in sorted((p for p in root.rglob("*") if p.is_dir()),
                           key=lambda p: len(p.parts), reverse=True):
        if dirpath == root:
            continue
        try:
            next(dirpath.iterdir())
        except StopIteration:
            try:
                dirpath.rmdir()
                print(f"  Removed: {dirpath}")
                removed += 1
            except OSError as e:
                print(f"  Couldn't remove {dirpath}: {e}")
        except OSError:
            continue

    print(f"\nDone - removed {removed} empty folder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
