"""
Generic connection helper for TagRank-integration scripts: loads TagRank's own settings.json
and hands back a connected tagrank.hydrus_client instance. Any script under scripts/tagrank/
that needs to talk to Hydrus the way TagRank itself does (as opposed to the plain-requests
modules/hydrus_client.py used by the rest of scripts/, which doesn't know about TagRank's
config/settings modules) should import connect() from here rather than duplicating this setup.

Requires the sibling `tagrank/` checkout to exist next to this repo (see TAGRANK_PATH below) -
this module inserts it onto sys.path itself so callers don't each have to.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

TAGRANK_PATH = Path(__file__).resolve().parent.parent.parent.parent.parent / "tagrank"
if TAGRANK_PATH.exists():
    sys.path.insert(0, str(TAGRANK_PATH))

try:
    from tagrank.hydrus_client import create_client
    from tagrank.settings import load_settings
except ImportError as e:
    print(f"Error: Could not import required modules: {e}")
    print("Make sure TagRank and its dependencies are installed.")
    sys.exit(1)

logger = logging.getLogger(__name__)


def connect():
    """Loads TagRank's own settings.json and returns a connected tagrank.hydrus_client instance.

    Exits the process (via sys.exit) with a logged error if settings can't be loaded or Hydrus
    can't be reached - callers don't need their own try/except around this.
    """
    try:
        settings = load_settings()
        return create_client(settings)
    except Exception as e:
        logger.error(f"Could not connect to Hydrus: {e}")
        sys.exit(1)
