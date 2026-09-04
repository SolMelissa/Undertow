#!/usr/bin/env python3
"""One-time setup: import the pre-generated hidden-tags marker image into Hydrus."""

import logging
import sys
from pathlib import Path

tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
if tagrank_path.exists():
    sys.path.insert(0, str(tagrank_path))

try:
    import hydrus_api
    from config import key, set_and_persist_key
    from tagrank.hydrus_client import create_client
    from tagrank.settings import load_settings
except ImportError as e:
    print(f"Error: Could not import required modules: {e}")
    print("Make sure TagRank and its dependencies are installed.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def get_marker_image_path():
    tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
    asset_path = tagrank_path / "tagrank" / "assets" / "tagrank_hidden_tags_marker.png"
    return asset_path


def main():
    try:
        settings = load_settings()
        client = create_client(settings)
    except Exception as e:
        logger.error(f"Could not connect to Hydrus: {e}")
        sys.exit(1)

    asset_path = get_marker_image_path()
    if not asset_path.exists():
        logger.error(f"Marker image not found at {asset_path}")
        sys.exit(1)

    logger.info(f"Importing marker image from {asset_path}...")
    try:
        client.add_file(str(asset_path), delete_after_successful_import=False)
    except Exception as e:
        logger.error(f"Import failed: {e}")
        sys.exit(1)

    logger.info("Fetching newly-imported file...")
    try:
        resp = client.search_files(tags=["system:inbox"], return_file_ids=True)
        file_ids = resp.get("file_ids") or []
        if not file_ids:
            logger.error("Could not find imported file")
            sys.exit(1)

        metadata_resp = client.get_file_metadata(file_ids=file_ids[-10:])
        marker_file = None
        for info in (metadata_resp.get("metadata") or []):
            if info.get("width") == 500 and info.get("height") == 350:
                if marker_file is None or info.get("file_id", 0) > marker_file.get("file_id", 0):
                    marker_file = info

        if not marker_file:
            logger.error("Could not identify marker image in inbox")
            sys.exit(1)

        file_hash = marker_file.get("file_hash")
        if not file_hash:
            logger.error("Imported file has no hash")
            sys.exit(1)

        logger.info(f"Found marker file: {file_hash}")
    except Exception as e:
        logger.error(f"Could not find imported file: {e}")
        sys.exit(1)

    logger.info("Tagging marker file...")
    try:
        tag_service_key = key("TAG_SERVICE_KEY", "").strip()
        if not tag_service_key or tag_service_key == "FILL_ME_IN":
            tag_service_key = None

        if tag_service_key:
            client.add_tags(
                hashes=[file_hash],
                service_keys_to_tags={tag_service_key: ["service:tagrank", "service:undertow"]}
            )
        else:
            client.add_tags(
                hashes=[file_hash],
                service_keys_to_tags={"": ["service:tagrank", "service:undertow"]}
            )
        logger.info("✓ Tagged with service:tagrank and service:undertow")
    except Exception as e:
        logger.warning(f"Could not tag marker file: {e}")

    try:
        note_text = (
            "TagRank hidden-tags marker. Tags on this file are hidden from TagRank. "
            "Do not delete this file."
        )
        client.set_notes(notes={"TagRank": note_text}, hash_=file_hash)
        logger.info("Set explanatory note on marker file.")
    except Exception as e:
        logger.warning(f"Could not set note: {e}")

    logger.info("Saving file hash to config/KEYS...")
    try:
        set_and_persist_key("TAGRANK_HIDDEN_TAGS_FILE_HASH", file_hash)
        logger.info("✓ Setup complete!")
        logger.info(f"  Marker file hash: {file_hash}")
    except Exception as e:
        logger.error(f"Could not save hash to config: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
