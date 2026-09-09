"""
Launches the performer-name gazetteer builder (ThePornDB / StashDB) that tag_cleanup_x.py's
optional name-detection pass reads its cache from. All the actual logic lives in
tag_cleanup/tag_cleanup_x_gazetteer.py - this is just the thin scripts/ entry point, since
scripts_runner.list_scripts() only globs scripts/*.py and can't discover a script living in
a subfolder directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "modules"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tag_cleanup"))

from tag_cleanup_x_gazetteer import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted, cache left unchanged.", file=sys.stderr)
        sys.exit(130)
