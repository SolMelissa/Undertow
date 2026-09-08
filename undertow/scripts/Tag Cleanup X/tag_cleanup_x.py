"""
Tag Cleanup X - next-generation filename-derived tag cleanup utility for Hydrus.

Started life as a verbatim copy of the sibling `tag_cleanup.py`, which is FROZEN/read-only:
all new cleanup features land here instead, so the original stays available unchanged as a
known-good fallback. The two are independent - editing this file never affects that one.

Shared with tag_cleanup.py: the editable word lists (`tag_cleanup_x_lists.py`, edited via the
webui's "Tag Cleanup Lists" panel) and the performer-name gazetteer cache built by
`performer_gazetteer.py`. NOT shared: this script keeps its own saved settings (API URL/key,
service picks, thresholds) in `tag-cleanup-x-config.json`, seeded once from the original
script's config if it exists, so reconfiguring X never disturbs tag_cleanup.py's own saved
run. The preview is mirrored to a timestamped log file during the run.

Original description follows.

Splits bulk-imported "filename tags" (e.g. dir:12-sunset hike - teen couple watches full
moon rise over lake after long trail up xz) into well-formed individual tags, previews the
result, and optionally writes them back to Hydrus via its Client API.

Run it with no arguments and it walks you through a wizard: enter (or reuse a saved) Hydrus
Client API URL and key, pick a file domain and tag service from the live list Hydrus
reports, then it dry-runs a small random sample of your real files first, shows the
IN/OUT/DROPPED preview for just that sample, and asks for confirmation before it touches
the rest - there's no separate preview/dry-run/apply menu to pick from first, and no
second confirmation once the sample is approved: it goes straight on to the full library.
The URL, key, and your last picks are stored locally so you don't have to retype them next
time - see `--reconfigure` to start over, or `--self-test` to preview the built-in fixture
tags offline without connecting to Hydrus.

Hard dependencies: requests and wordfreq. wordfreq drives truncated-token detection
(dictionary-membership on the trailing token of a block). Name detection is optional and
gazetteer-based - run the sibling script `performer_gazetteer.py` to build a local
performer-name cache from ThePornDB/StashDB (see that script for details); this file only
ever reads the cache it produces. A bare statistical/capitalization-based approach was tried
earlier and dropped as too heavy and unreliable for lowercase filename text with no case
signal. With no gazetteer cached, names pass through the same content/attribute pipeline as
any other word, same as before this feature existed.

Architecture: the text-processing engine (tag_cleanup_x_engine), Hydrus API client
(tag_cleanup_x_hydrus), rendering + progress + prompts (tag_cleanup_x_render), and
self-test fixtures (tag_cleanup_x_selftest) are all split out as focused modules. This
wizard entry point orchestrates them.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("This tool requires the 'requests' package: pip install requests", file=sys.stderr)
    sys.exit(1)

import tag_cleanup_x_lists as tag_cleanup_lists
from tag_cleanup_x_engine import Config, PerformerGazetteer, ParsedTag, FilePreview, parse_filename_tag_batch, load_performer_gazetteer
from tag_cleanup_x_hydrus import HydrusClient
from tag_cleanup_x_render import Renderer, ProgressReporter, print_preview_table
from tag_cleanup_x_selftest import run_self_test


# ---------------------------------------------------------------------------
# Locally-stored settings (API URL/key, last-picked services, preferences)
# ---------------------------------------------------------------------------
# Stored in plaintext JSON next to hydownloader's own data, same convention as
# the project's other locally-cached credentials (e.g. GALLERY_DL_USER_CONFIG_FILE
# in undertow/config.py) - no extra encryption layer, just kept out of the repo.

def _config_dir() -> Path:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from undertow import config as _undertow_config  # type: ignore
        return _undertow_config.DATA_DIR
    except Exception:
        root = Path(os.environ.get("USERPROFILE", str(Path.home())))
        return root / "HydrusPipeline" / "hydownloader-data"


def _default_local_config_path() -> Path:
    return _config_dir() / "tag-cleanup-x-config.json"


LOCAL_CONFIG_FILE = _default_local_config_path()
# Read-only fallback: the frozen tag_cleanup.py's own settings file. Used once, to seed this
# script's first run with the API URL/key and service picks the user already entered there,
# rather than making them retype everything. Never written to - the moment X saves anything
# it writes only to LOCAL_CONFIG_FILE above, so the two scripts' settings diverge from then on.
LEGACY_CONFIG_FILE = _config_dir() / "tag-cleanup-config.json"


def load_local_config() -> dict:
    for path in (LOCAL_CONFIG_FILE, LEGACY_CONFIG_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(stored, dict):
            return stored
    return {}


def save_local_config(updates: dict) -> None:
    stored = load_local_config()
    stored.update(updates)
    LOCAL_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)


# ---------------------------------------------------------------------------
# Preview / apply orchestration
# ---------------------------------------------------------------------------

DRY_RUN_SAMPLE_SIZE = 25


def _build_plan(metadata: Dict[int, List[str]], cfg: Config, renderer: Renderer, show_progress: bool = True
                ) -> Tuple[Dict[int, Tuple[List[str], List[str]]], List[FilePreview]]:
    """Turns raw {file_id: [current tags]} metadata into a write plan (fid -> (tags_to_add,
    tags_to_delete)) plus one FilePreview per file, for previewing or applying. Parses every
    matched raw tag in the batch as a single parse_filename_tag_batch call, so the corpus-global
    name-inference pass actually sees the whole batch instead of one file at a time."""
    pairs: List[Tuple[int, str]] = [
        (fid, raw_tag)
        for fid, current_tags in metadata.items()
        for raw_tag in current_tags
        if raw_tag.startswith(f"{cfg.source_namespace}:")
    ]
    plan: Dict[int, Tuple[List[str], List[str]]] = {}
    entries_by_fid: Dict[int, List[ParsedTag]] = {}
    if pairs:
        progress_reporter = None
        if show_progress:
            progress_reporter = ProgressReporter(renderer, "Parsing tags", len(pairs))
            on_progress = lambda done, total: progress_reporter.update(done)
        else:
            on_progress = None

        parsed_list = parse_filename_tag_batch([raw_tag for _, raw_tag in pairs], cfg, on_progress)

        if progress_reporter:
            progress_reporter.done()

        for (fid, raw_tag), parsed in zip(pairs, parsed_list):
            entries_by_fid.setdefault(fid, []).append(parsed)
            to_add, to_delete = plan.setdefault(fid, ([], []))
            to_add.extend(t for t in parsed.tags if t not in to_add)
            to_delete.append(raw_tag)
    previews = [FilePreview(label=f"file_id {fid}", entries=entries) for fid, entries in entries_by_fid.items()]
    return plan, previews


def _chunked(seq: List[int], size: int) -> List[List[int]]:
    return [seq[start:start + size] for start in range(0, len(seq), size)]


def run_dry_run_then_apply(client: HydrusClient, cfg: Config, services: "ServiceSelection", renderer: Renderer) -> int:
    same_service = services.source_tag_service_key == services.dest_tag_service_key

    renderer.out(f"\nSearching {services.file_service_name!r} for {cfg.target_tag_wildcards}...")
    try:
        file_ids = client.search_files(cfg.target_tag_wildcards, services.file_service_key)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        renderer.error(f"Could not reach Hydrus: {exc}")
        return 1
    renderer.out(f"Found {len(file_ids):,} file(s).")
    if not file_ids:
        return 0

    # Dry run: preview a small random sample first rather than fetching/parsing the whole
    # (possibly 100k+ file) library up front, so a bad config choice is caught in seconds
    # instead of after minutes of fetching.
    sample_size = min(DRY_RUN_SAMPLE_SIZE, len(file_ids))
    sample_ids = random.sample(file_ids, sample_size)
    renderer.out(f"\nDry run: fetching a random sample of {sample_size} file(s) to preview before touching "
          f"the full library...")
    try:
        sample_metadata = client.fetch_metadata(sample_ids, services.source_tag_service_key,
                                                  chunk_size=cfg.batch_size)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        renderer.error(f"Could not reach Hydrus while fetching sample tags: {exc}")
        return 1

    _, sample_previews = _build_plan(sample_metadata, cfg, renderer, show_progress=False)
    if not sample_previews:
        renderer.out(f"None of the {sample_size} sampled file(s) had a {cfg.source_namespace!r}-namespaced tag. "
              "Nothing to preview.")
        return 0

    print_preview_table(sample_previews, renderer)

    if not renderer.yes_no(
            f"\nAbove is a preview of {sample_size} randomly-sampled file(s) out of {len(file_ids):,} "
            f"found. Does this look right? Proceed to run on the full {len(file_ids):,} file(s)?",
            default=False):
        renderer.out("Aborted, no changes written.")
        return 0

    renderer.out(f"\nFetching tag data from {services.source_tag_service_name!r} for all {len(file_ids):,} "
          f"file(s) (in batches of {cfg.batch_size}, this is the slow part on a large library)...")
    fetch_progress = ProgressReporter(renderer, "Fetching tags", len(file_ids))
    try:
        metadata = client.fetch_metadata(
            file_ids, services.source_tag_service_key, chunk_size=cfg.batch_size,
            on_progress=lambda done, total: fetch_progress.update(done),
        )
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        fetch_progress.done()
        renderer.error(f"Could not reach Hydrus while fetching tags: {exc}")
        return 1
    fetch_progress.done()

    plan, previews = _build_plan(metadata, cfg, renderer, show_progress=True)
    if not previews:
        renderer.out(f"No tags with namespace {cfg.source_namespace!r} found in "
              f"{services.source_tag_service_name!r} on the matched files. Nothing to do.")
        return 0

    print_preview_table(previews, renderer)
    files_affected = len(plan)
    dest_note = (f"in place in {services.source_tag_service_name!r}" if same_service else
                 f"into {services.dest_tag_service_name!r}, deleting the raw tag from "
                 f"{services.source_tag_service_name!r}")

    # Group files by their exact (tags_to_add, tags_to_delete) outcome: every file bulk-
    # imported from the same source directory shares the same raw dir: tag, so this
    # typically collapses a 100k+ file library into a small number of distinct groups, each
    # written in batches of cfg.batch_size file_ids per API call instead of one call per file.
    groups: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[int]] = {}
    for fid, (tags_to_add, tags_to_delete) in plan.items():
        key = (tuple(tags_to_add), tuple(tags_to_delete))
        groups.setdefault(key, []).append(fid)
    batches = [(key, batch) for key, fids in groups.items() for batch in _chunked(fids, cfg.batch_size)]

    total_raw_tags = sum(len(fp.entries) for fp in previews)
    renderer.out(f"\nWriting cleaned-up tags for {total_raw_tags} raw tag(s) across {files_affected:,} file(s) "
          f"{dest_note} ({len(groups):,} distinct tag change(s), sent as {len(batches):,} batched "
          f"API call(s))...")

    def apply_batch(fids: List[int], tags_to_add: List[str], tags_to_delete: List[str]) -> None:
        if same_service:
            client.add_tags(
                file_ids=fids,
                tag_service_key=services.dest_tag_service_key,
                tags_to_add=tags_to_add,
                tags_to_delete=tags_to_delete,
            )
        else:
            client.add_tags_multi(
                file_ids=fids,
                service_actions={
                    services.dest_tag_service_key: (tags_to_add, []),
                    services.source_tag_service_key: ([], tags_to_delete),
                },
            )

    apply_progress = ProgressReporter(renderer, "Applying", files_affected)
    errors: List[str] = []
    processed_count = 0
    succeeded_count = 0
    with ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        futures = {
            pool.submit(apply_batch, batch, list(tags_to_add), list(tags_to_delete)): batch
            for (tags_to_add, tags_to_delete), batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                future.result()
            except (requests.RequestException, RuntimeError) as exc:
                errors.append(f"{len(batch)} file(s) starting at file {batch[0]}: {exc}")
            else:
                succeeded_count += len(batch)
            processed_count += len(batch)
            apply_progress.update(processed_count)
    apply_progress.done()

    if errors:
        failed_files = files_affected - succeeded_count
        renderer.error(f"{len(errors)} batch(es) ({failed_files:,} file(s)) failed to update (first 10 shown):")
        for line in errors[:10]:
            renderer.error(f"  - {line}")

    renderer.out(f"Submitted changes for {succeeded_count:,}/{files_affected:,} file(s) to Hydrus's add_tags "
          f"queue (applies in the background).")
    return 1 if errors else 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Tag Cleanup X: interactive wizard to clean up filename-derived Hydrus tags "
                    "via the Client API.",
        epilog="Just run `python tag_cleanup_x.py` with no arguments - it walks you through the rest.",
    )
    p.add_argument("--reconfigure", action="store_true",
                    help="Ignore saved settings and re-enter the API URL/key and service picks from scratch")
    p.add_argument("--self-test", action="store_true",
                    help="Preview the built-in fixture tags offline (no Hydrus connection) and exit")
    return p


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

def wizard_connect(renderer: Renderer, saved: dict, reconfigure: bool) -> Tuple[HydrusClient, str, str]:
    """Uses the saved API URL/key silently if both are present and they pass a connection
    test; only prompts when reconfiguring, when either is missing, or when the saved
    connection fails. Returns the connected client plus the url/key used."""
    saved_url = saved.get("api_url")
    saved_key = saved.get("api_key")

    if not reconfigure and saved_url and saved_key:
        client = HydrusClient(saved_url, saved_key)
        try:
            client.get_services()
        except (requests.RequestException, RuntimeError):
            renderer.out(f"Saved Hydrus connection ({saved_url}) did not respond - reconfigure it below.")
        else:
            return client, saved_url, saved_key

    default_url = saved_url or "http://127.0.0.1:45869"
    saved_key = None if reconfigure else saved_key

    while True:
        api_url = renderer.text("Hydrus Client API URL", default=default_url)
        if saved_key:
            masked = f"...{saved_key[-4:]}" if len(saved_key) >= 4 else "(saved)"
            if renderer.yes_no(f"Use saved API key ({masked})?", default=True):
                api_key = saved_key
            else:
                api_key = renderer.secret("Hydrus Client API access key", has_saved=False) or ""
        else:
            renderer.out("Find/create an access key in Hydrus under services > review services > the 'client api' tab.")
            api_key = renderer.secret("Hydrus Client API access key", has_saved=False) or ""

        if not api_key:
            renderer.out("  An API key is required.")
            continue

        client = HydrusClient(api_url, api_key)
        try:
            client.get_services()
        except (requests.RequestException, RuntimeError) as exc:
            renderer.out(f"  Could not connect: {exc}")
            if not renderer.yes_no("Try again?", default=True):
                raise SystemExit(1)
            saved_key = None  # force a fresh key prompt on retry
            continue

        save_local_config({"api_url": api_url, "api_key": api_key})
        return client, api_url, api_key


@dataclass
class ServiceSelection:
    file_service_name: str
    file_service_key: str
    source_tag_service_name: str
    source_tag_service_key: str
    dest_tag_service_name: str
    dest_tag_service_key: str


def wizard_pick_services(renderer: Renderer, client: HydrusClient, saved: dict) -> ServiceSelection:
    file_services = client.list_file_services()
    tag_services = client.list_tag_services()
    if not file_services:
        raise SystemExit("Hydrus reported no file services - is the Client API enabled with file access?")
    if not tag_services:
        raise SystemExit("Hydrus reported no tag services - is the Client API enabled with tag access?")

    file_options = [(f"{name}  ({type_pretty})", key) for name, key, type_pretty in file_services]
    file_key = renderer.choice("Which file domain should be searched?", file_options,
                              default_key=saved.get("file_service_key"))
    file_name = next(name for name, key, _ in file_services if key == file_key)

    tag_options = [(f"{name}  ({type_pretty})", key) for name, key, type_pretty in tag_services]
    source_key = renderer.choice("Which tag service holds the raw filename tags to read and clean up?",
                                tag_options, default_key=saved.get("source_tag_service_key"))
    source_name = next(name for name, key, _ in tag_services if key == source_key)

    renderer.out(f"\nBy default the cleaned-up tags are written back to the same service ({source_name!r}), "
          "removing the raw filename tag from there. Choose a different service if you'd rather the "
          "split tags land somewhere else (e.g. keep raw filename tags in one service, put clean "
          "tags in your main tagging service).")
    dest_default = saved.get("dest_tag_service_key", source_key)
    dest_key = renderer.choice("Which tag service should the cleaned-up tags be written to?",
                              tag_options, default_key=dest_default)
    dest_name = next(name for name, key, _ in tag_services if key == dest_key)

    save_local_config({
        "file_service_name": file_name, "file_service_key": file_key,
        "source_tag_service_name": source_name, "source_tag_service_key": source_key,
        "dest_tag_service_name": dest_name, "dest_tag_service_key": dest_key,
    })
    return ServiceSelection(file_name, file_key, source_name, source_key, dest_name, dest_key)


def wizard_build_config(renderer: Renderer, saved: dict) -> Config:
    renderer.out()
    source_namespace = renderer.text("Namespace holding the raw filename tags", default=saved.get("source_namespace", "dir"))
    drop_truncation = renderer.yes_no("Drop suspected truncated trailing tokens (short consonant-only remnants)?",
                                     default=saved.get("drop_suspected_truncation", True))
    default_cfg = Config()
    min_process_tag_length = renderer.integer(
        "Minimum full raw-tag length to bother parsing at all (shorter tags are left unchanged)",
        default=saved.get("min_process_tag_length", default_cfg.min_process_tag_length), min_value=1)
    min_token_len = renderer.integer(
        "Minimum length for an individual word to survive as its own tag (shorter words are dropped)",
        default=saved.get("min_token_len", default_cfg.min_token_len), min_value=1)

    save_local_config({
        "source_namespace": source_namespace,
        "drop_suspected_truncation": drop_truncation,
        "min_process_tag_length": min_process_tag_length,
        "min_token_len": min_token_len,
    })

    cfg = Config(source_namespace=source_namespace, drop_suspected_truncation=drop_truncation,
                 min_process_tag_length=min_process_tag_length, min_token_len=min_token_len)
    cfg.target_tag_wildcards = [f"{source_namespace}:*"]
    return cfg


def load_performer_gazetteer_for_run(renderer: Renderer) -> Optional[PerformerGazetteer]:
    """Silently uses whatever performer_gazetteer.py has cached, if anything. Building/
    refreshing that cache is entirely performer_gazetteer.py's job - this never fetches or
    prompts for API keys itself."""
    gaz = load_performer_gazetteer()
    if gaz:
        renderer.out(f"Using performer gazetteer ({len(gaz.full_name_phrases):,} name(s)/alias(es)) "
              f"for name detection - run performer_gazetteer.py to refresh it.")
    else:
        renderer.out("No performer gazetteer cached - parsing without name detection. Run "
              "performer_gazetteer.py first to enable it.")
    return gaz


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    log_path = None
    if not args.self_test:
        # Set up log file path for normal runs
        log_path = _config_dir() / "tag-cleanup-x-logs" / f"tag-cleanup-x-{time.strftime('%Y%m%d-%H%M%S')}.log"

    renderer = Renderer(log_path)
    try:
        renderer.out("=== Hydrus filename-tag cleanup X ===")

        if args.self_test:
            renderer.out()
            run_self_test(Config(), renderer)
            return 0

        saved = {} if args.reconfigure else load_local_config()
        client, _, _ = wizard_connect(renderer, saved, args.reconfigure)
        saved = load_local_config()  # picks up the freshly-saved url/key
        services = wizard_pick_services(renderer, client, saved)
        saved = load_local_config()
        cfg = wizard_build_config(renderer, saved)
        cfg.performer_gazetteer = load_performer_gazetteer_for_run(renderer)

        # run_dry_run_then_apply previews a small random sample first and asks for
        # confirmation before ever touching the full library, so there's no separate
        # dry-run-only path to choose up front.
        return run_dry_run_then_apply(client, cfg, services, renderer)
    finally:
        renderer.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted, no further changes made.", file=sys.stderr)
        sys.exit(130)
