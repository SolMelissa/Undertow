"""
TagRank hidden-tags marker file logic: importing the marker image into Hydrus, tagging/noting
it, and syncing its tags against config/TAG_FILTERS. The marker file is a dummy image imported
into Hydrus purely to carry tags, so TagRank can read config/TAG_FILTERS' hidden-tag list back
out of Hydrus itself instead of re-reading local config on every run.

Used by scripts/tagrank_setup.py (the thin scripts/ entry point - scripts_runner.list_scripts()
only globs scripts/*.py and can't discover a script living in this subfolder directly, same
reasoning as tag_cleanup/ and performer_gazetteer.py).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tagrank_connect import TAGRANK_PATH, connect  # noqa: E402

try:
    import hydrus_api
    from config import get_tag_filters, key, set_and_persist_key
except ImportError as e:
    print(f"Error: Could not import required modules: {e}")
    print("Make sure TagRank and its dependencies are installed.")
    sys.exit(1)

logger = logging.getLogger(__name__)

# Explanatory note written onto the marker file itself, so anyone browsing Hydrus who stumbles
# on it understands why it exists and doesn't delete it as clutter.
MARKER_NOTE_TEXT = (
    "TagRank hidden-tags marker. Tags on this file are hidden from TagRank. "
    "Do not delete this file."
)


def get_marker_image_path() -> Path:
    """Returns the path to the pre-generated marker PNG shipped inside the tagrank/ repo."""
    return TAGRANK_PATH / "tagrank" / "assets" / "tagrank_hidden_tags_marker.png"


def get_marker_hash() -> str:
    """Returns the marker file's Hydrus hash as persisted in tagrank's config/KEYS, or ""
    if the marker hasn't been imported yet (key unset or still the placeholder "FILL_ME_IN")."""
    marker_hash = key("TAGRANK_HIDDEN_TAGS_FILE_HASH", "").strip()
    if marker_hash and marker_hash != "FILL_ME_IN":
        return marker_hash
    return ""


def import_marker_file(client) -> str:
    """One-time setup step: imports the marker PNG into Hydrus, tags it with
    service:tagrank/service:undertow (if TAG_SERVICE_KEY is configured), attaches
    MARKER_NOTE_TEXT as a note, and persists the resulting file hash to config/KEYS so future
    runs can find the same file again via get_marker_hash().

    Args:
        client: a connected tagrank.hydrus_client instance, e.g. from tagrank_connect.connect().

    Returns:
        The imported file's Hydrus hash (also already saved to config/KEYS).
    """
    asset_path = get_marker_image_path()
    if not asset_path.exists():
        logger.error(f"Marker image not found at {asset_path}")
        sys.exit(1)

    logger.info(f"Importing marker image from {asset_path}...")
    try:
        result = client.add_file(str(asset_path), delete_after_successful_import=False)
        logger.info(f"Import call returned: {result}")
    except Exception as e:
        logger.error(f"Import failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)

    file_hash = result.get("hash")
    if not file_hash:
        logger.error("Import call did not return a file hash")
        sys.exit(1)
    logger.info(f"Found marker file hash from import: {file_hash}")

    logger.info("Tagging marker file...")
    tag_service_key = key("TAG_SERVICE_KEY", "").strip()
    if tag_service_key and tag_service_key != "FILL_ME_IN":
        try:
            client.add_tags(
                hashes=[file_hash],
                service_keys_to_tags={tag_service_key: ["service:tagrank", "service:undertow"]}
            )
            logger.info("✓ Tagged with service:tagrank and service:undertow")
        except Exception as e:
            logger.warning(f"Could not tag marker file: {e}")
    else:
        logger.info("Skipping tagging (TAG_SERVICE_KEY not configured in config/KEYS)")

    try:
        client.set_notes(notes={"TagRank": MARKER_NOTE_TEXT}, hash_=file_hash)
        logger.info("Set explanatory note on marker file.")
    except Exception as e:
        logger.warning(f"Could not set note: {e}")

    logger.info("Saving file hash to config/KEYS...")
    try:
        set_and_persist_key("TAGRANK_HIDDEN_TAGS_FILE_HASH", file_hash)
    except Exception as e:
        logger.error(f"Could not save hash to config: {e}")
        sys.exit(1)

    return file_hash


def sync_hidden_tags(client, marker_hash: str) -> None:
    """Diffs config/TAG_FILTERS against the marker file's current tags in Hydrus and applies
    whatever add/remove calls are needed to make them match. Safe to call repeatedly/on a
    schedule - a no-op run just logs "already in sync" and returns.

    Args:
        client: a connected tagrank.hydrus_client instance, e.g. from tagrank_connect.connect().
        marker_hash: the marker file's Hydrus hash, e.g. from get_marker_hash().
    """
    hidden_tags = get_tag_filters()
    logger.info(f"Loaded {len(hidden_tags)} hidden tag(s) from config/TAG_FILTERS")

    logger.info(f"Fetching marker file ({marker_hash})...")
    try:
        resp = client.get_file_metadata(hashes=[marker_hash], only_return_basic_information=False)
        if not resp or not resp.get("metadata"):
            logger.error(f"Marker file not found (hash={marker_hash})")
            sys.exit(1)

        file_info = resp["metadata"][0]
        tag_service_key = key("TAG_SERVICE_KEY", "").strip()
        if not tag_service_key or tag_service_key == "FILL_ME_IN":
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
    except Exception as e:
        logger.error(f"Could not sync tags: {e}")
        sys.exit(1)


def setup_and_sync() -> None:
    """Full idempotent flow: connect to Hydrus, import+tag the marker file if it hasn't been
    set up yet, then sync its tags against config/TAG_FILTERS. This is what
    scripts/tagrank_setup.py runs - safe to run on every launch, not just the first time."""
    client = connect()

    marker_hash = get_marker_hash()
    if marker_hash:
        logger.info(f"Marker file already set up (hash={marker_hash}); skipping import.")
    else:
        logger.info("No marker file on record - running first-time import...")
        marker_hash = import_marker_file(client)

    sync_hidden_tags(client, marker_hash)
