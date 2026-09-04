#!/usr/bin/env python3
"""Sync hidden tags from TagRank config to the marker file in Hydrus.

This script reads your excluded tags from TagRank config/TAG_FILTERS and applies them as tags
to the TagRank hidden-tags marker file in Hydrus, so the marker file always reflects
your current hidden-tags configuration.

Usage:
    python scripts/sync_hidden_tags_to_marker.py

The script will:
1. Read hidden tags from TagRank config/TAG_FILTERS
2. Find the marker file using TAGRANK_HIDDEN_TAGS_FILE_HASH
3. Apply/update tags on the marker file to match your config
4. Report what was changed
"""

import logging
import sys
from pathlib import Path

import hydrus_api  # type: ignore

# Add TagRank directory to path so we can import tagrank config
tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
if tagrank_path.exists():
    sys.path.insert(0, str(tagrank_path))

from config import get_tag_filters, key
from tagrank.hydrus_client import create_client
from tagrank.settings import load_settings

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main() -> None:
    """Sync TAG_FILTERS to the marker file's tags in Hydrus."""
    # Load config
    marker_hash = key("TAGRANK_HIDDEN_TAGS_FILE_HASH", "").strip()
    if not marker_hash or marker_hash == "FILL_ME_IN":
        logger.error("TAGRANK_HIDDEN_TAGS_FILE_HASH not set in config/KEYS")
        logger.error("Run: python -m tagrank.setup_hidden_tags_marker")
        sys.exit(1)

    try:
        settings = load_settings()
        client = create_client(settings)
    except Exception as e:
        logger.error(f"Could not connect to Hydrus: {e}")
        sys.exit(1)

    # Get hidden tags from config/TAG_FILTERS
    hidden_tags = get_tag_filters()
    logger.info(f"Loaded {len(hidden_tags)} hidden tag(s) from config/TAG_FILTERS")

    # Fetch the marker file's current tags
    logger.info(f"Fetching marker file ({marker_hash})...")
    try:
        resp = client.get_file_metadata(hashes=[marker_hash], only_return_basic_information=False)
        if not resp or not resp.get("metadata"):
            logger.error(f"Marker file not found (hash={marker_hash})")
            logger.error("Make sure the file still exists in Hydrus, or re-run setup:")
            logger.error("  python -m tagrank.setup_hidden_tags_marker")
            sys.exit(1)

        file_info = resp["metadata"][0]
        tag_service_key = key("TAG_SERVICE_KEY", "").strip()
        if not tag_service_key or tag_service_key == "FILL_ME_IN":
            # Determine which service has the most tags (likely the one to use)
            tag_service_key = list(file_info.get("tags", {}).keys())[0] if file_info.get("tags") else None

        if not tag_service_key:
            logger.error("Could not determine tag service to use")
            sys.exit(1)

        service_data = file_info["tags"].get(tag_service_key, {})
        current_tags = set()
        for status in ("0", "1"):
            if status in service_data.get("display_tags", {}):
                current_tags.update(service_data["display_tags"][status])

        logger.info(f"Marker file currently has {len(current_tags)} tag(s)")
    except Exception as e:
        logger.error(f"Could not fetch marker file: {e}")
        sys.exit(1)

    # Determine what to add/remove
    hidden_set = set(hidden_tags)
    to_add = hidden_set - current_tags
    to_remove = current_tags - hidden_set
    in_sync = hidden_set & current_tags

    logger.info(f"  {len(in_sync)} tag(s) already in sync")
    if to_add:
        logger.info(f"  {len(to_add)} tag(s) to add: {', '.join(sorted(to_add))}")
    if to_remove:
        logger.info(f"  {len(to_remove)} tag(s) to remove: {', '.join(sorted(to_remove))}")

    if not to_add and not to_remove:
        logger.info("✓ Marker file is already in sync!")
        return

    # Apply changes
    try:
        if to_add:
            logger.info(f"Adding {len(to_add)} tag(s) to marker file...")
            client.add_tags(
                hashes=[marker_hash],
                service_keys_to_tags={tag_service_key: list(to_add)}
            )

        if to_remove:
            logger.info(f"Removing {len(to_remove)} tag(s) from marker file...")
            client.add_tags(
                hashes=[marker_hash],
                service_keys_to_actions_to_tags={
                    tag_service_key: {hydrus_api.TagAction.DELETE: list(to_remove)}
                }
            )

        logger.info("✓ Synced! Marker file now matches config/TAG_FILTERS")
        logger.info(f"  Hidden tags on marker file: {len(hidden_set)}")
    except Exception as e:
        logger.error(f"Could not sync tags: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
