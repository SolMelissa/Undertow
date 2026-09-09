"""
Sets up (first run) and syncs (every run after) TagRank's hidden-tags marker file in Hydrus.
Replaces the old separate "Setup TagRank Hidden Tags Marker" and "Sync TagRank Hidden Tags"
scripts, which were really two halves of one flow - importing the marker file was pointless
without then keeping its tags in sync, and syncing required the marker to already be imported.
All the actual logic lives in tagrank/tagrank_marker.py - this is just the thin scripts/ entry
point, since scripts_runner.list_scripts() only globs scripts/*.py and can't discover a script
living in a subfolder directly.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tagrank"))

from tagrank_marker import setup_and_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

if __name__ == "__main__":
    setup_and_sync()
