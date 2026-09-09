"""
Backs the dashboard's Tag Cleanup tab. Reuses tag_cleanup_x's Config/engine/plan-building
logic in-process (imported directly, not shelled out to as a subprocess) so the browser gets
a live regex-preview form instead of driving the interactive CLI wizard. Hydrus I/O goes
through undertow's own hydrus_client.py (the webui's stored API key) - only the
text-processing engine and a couple of small private helpers are reused from tag_cleanup_x.py
itself, which stays otherwise untouched (its own subprocess-driven CLI wizard on the Scripts
tab keeps working exactly as before).

Source namespaces are a plain typed list, not a live-pulled checklist: an earlier version
pulled every namespace in the library via a bare search_tags("*") wildcard scan (Hydrus has no
dedicated "list namespaces" endpoint) to populate a checkbox grid, but that's a full-tag-store
scan - needlessly expensive, and not something worth doing just to save typing "dir, filename"
once. Tag services and the file domain, by contrast, come from get_services() (cheap - no
search, just service metadata) and are shown as real checkboxes; the picks are persisted to
disk (see load_web_config/save_web_config) so they're remembered between launches instead of
defaulting back to "my tags" every time.

Applying is a background job (large libraries take minutes) tracked the same way
scripts_runner.py tracks a running script - one job at a time, polled by offset cursor - just
running a Python function in a thread instead of a subprocess, since there's no separate
process/stdout to capture here.
"""

from __future__ import annotations

import json
import random
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, hydrus_client

_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import tag_cleanup_x  # noqa: E402  (its own imports add scripts/modules + scripts/tag_cleanup to sys.path)
from tag_cleanup_x_engine import Config, build_split_regex, load_performer_gazetteer  # noqa: E402
from tag_cleanup_x_render import _render_tag_section_plain  # noqa: E402

DRY_RUN_SAMPLE_SIZE = tag_cleanup_x.DRY_RUN_SAMPLE_SIZE

# search_files/get_file_metadata default to invoke_hydrus_api's own 8s timeout, which a wide
# wildcard predicate (or a large file_ids chunk) over a real library can comfortably exceed -
# same reasoning as search_tags's timeout in list_services below.
_SEARCH_TIMEOUT = 30

# Hydrus service `type` codes (same constants webui.py's TagRank services panel uses, confirmed
# against a live /get_services response) worth offering as tag-service/file-domain checkboxes.
_TAG_SERVICE_TYPES = {0, 5}  # TAG_REPOSITORY, TAG_DOMAIN (local tag services)
_FILE_SERVICE_TYPES = {2, 15, 21}  # FILE_DOMAIN, ALL_LOCAL_FILES, ALL_MY_FILES


def list_services() -> tuple[list[dict], list[dict], Optional[str]]:
    """(tag_services, file_services) as [{"key", "name"}, ...] for the form's checkboxes - a
    single cheap get_services() call, not a tag-store scan."""
    resp = hydrus_client.get_services()
    if not resp.success:
        return [], [], resp.error
    services_map = (resp.data or {}).get("services", {})
    tag_services, file_services = [], []
    for key, svc in services_map.items():
        name = svc.get("name", key)
        svc_type = svc.get("type")
        if svc_type in _TAG_SERVICE_TYPES:
            tag_services.append({"key": key, "name": name})
        if svc_type in _FILE_SERVICE_TYPES:
            file_services.append({"key": key, "name": name})
    tag_services.sort(key=lambda s: s["name"])
    file_services.sort(key=lambda s: s["name"])
    return tag_services, file_services, None


# ---------------------------------------------------------------------------------- persisted prefs
# Which tag services / file domain the checkboxes had checked last time - a separate small file
# from tag_cleanup_x's own LOCAL_CONFIG_FILE (scripts/tag-cleanup-x-config.json), same convention
# that file's own docstring establishes: the web tab's picks shouldn't disturb the CLI wizard's
# saved run, and vice versa.

_WEB_CONFIG_FILE = config.DATA_DIR / "tag-cleanup-web-config.json"


def load_web_config() -> dict:
    try:
        with open(_WEB_CONFIG_FILE, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def save_web_config(updates: dict) -> None:
    stored = load_web_config()
    stored.update(updates)
    _WEB_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_WEB_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)


def build_config_from_form(form) -> Config:
    """Turns the tag-cleanup form's POST/GET data into a Config, mirroring wizard_build_config's
    fields (namespaces, unnamespaced, delimiters, split_regex, thresholds)."""
    namespaces = [ns.strip() for ns in (form.get("namespaces") or "dir").split(",") if ns.strip()]
    include_unnamespaced = form.get("unnamespaced") == "on"
    delimiters = [d.strip() for d in form.getlist("delimiter") if d.strip()]
    custom_delims = [d.strip() for d in (form.get("custom_delimiters") or "").split(",") if d.strip()]
    delimiters = delimiters + [d for d in custom_delims if d not in delimiters]
    split_regex = (form.get("split_regex") or "").strip() or None
    drop_truncation = form.get("drop_truncation") == "on"
    try:
        min_process_tag_length = max(1, int(form.get("min_process_tag_length") or 35))
    except ValueError:
        min_process_tag_length = 35
    try:
        min_token_len = max(1, int(form.get("min_token_len") or 2))
    except ValueError:
        min_token_len = 2

    cfg = Config(
        source_namespaces=namespaces or ["dir"],
        include_unnamespaced=include_unnamespaced,
        delimiters=delimiters,
        split_regex=split_regex,
        drop_suspected_truncation=drop_truncation,
        min_process_tag_length=min_process_tag_length,
        min_token_len=min_token_len,
    )
    cfg.target_tag_wildcards = tag_cleanup_x._target_tag_wildcards(cfg.source_namespaces, include_unnamespaced)
    cfg.performer_gazetteer = load_performer_gazetteer()
    return cfg


def _extract_current_tags(file_metadata: dict, tag_service_key: str) -> list[str]:
    """Current (non-pending, non-petitioned) tags on one file_metadata entry for one tag
    service - handles both the modern storage_tags shape and the older
    service_keys_to_statuses_to_tags shape, same dual-shape defensiveness as
    media.get_suggested_tags for search_tags."""
    tags_block = (file_metadata.get("tags") or {}).get(tag_service_key)
    if tags_block:
        storage = tags_block.get("storage_tags") or {}
        return list(storage.get("0", []))
    legacy = file_metadata.get("service_keys_to_statuses_to_tags") or {}
    legacy_block = legacy.get(tag_service_key) or {}
    return list(legacy_block.get("0", []))


def search_files_multi_domain(predicates: list[str], file_service_keys: list[str]
                                ) -> tuple[list[int], Optional[str]]:
    """Unions search results across every checked file domain - Hydrus's search_files only
    accepts one file_service_key per call, so multiple checked domains means one call per
    domain, deduped. An empty list searches Hydrus's default "all my files" domain (one call,
    no file_service_key param), same as before file-domain checkboxes existed."""
    keys = file_service_keys or [None]
    seen: set[int] = set()
    for key in keys:
        resp = hydrus_client.search_files(predicates, file_service_key=key, timeout=_SEARCH_TIMEOUT)
        if not resp.success:
            return [], resp.error
        seen.update((resp.data or {}).get("file_ids") or [])
    return list(seen), None


def fetch_tags_by_file(file_ids: list[int], tag_service_keys: list[str], chunk_size: int = 256
                        ) -> tuple[dict[int, list[str]], Optional[str]]:
    """Per-file current tags, merged (union, deduped) across every checked source tag service -
    parsing doesn't care which service a raw tag came from, only whether it matches the
    configured source namespace(s). Which exact service(s) each raw tag should be deleted from
    on apply is handled separately in _run_apply, not tracked here."""
    metadata: dict[int, list[str]] = {}
    for start in range(0, len(file_ids), chunk_size):
        chunk = file_ids[start:start + chunk_size]
        resp = hydrus_client.get_file_metadata(chunk, include_tags=True, timeout=_SEARCH_TIMEOUT)
        if not resp.success:
            return {}, resp.error
        for entry in (resp.data or {}).get("metadata", []):
            fid = entry.get("file_id")
            if fid is None:
                continue
            merged = metadata.setdefault(fid, [])
            for tag_service_key in tag_service_keys:
                for tag in _extract_current_tags(entry, tag_service_key):
                    if tag not in merged:
                        merged.append(tag)
    return metadata, None


def dry_run_preview(cfg: Config, tag_service_keys: list[str], file_service_keys: Optional[list[str]] = None
                     ) -> tuple[list[str], int, int, Optional[str]]:
    """Returns (preview_lines, sample_size, total_matches, error) - a random-sample dry run,
    same approach and sample size as tag_cleanup_x.run_dry_run_then_apply's own dry run, just
    rendered as plain-text lines for the browser instead of printed via a console Renderer."""
    file_ids, err = search_files_multi_domain(cfg.target_tag_wildcards, file_service_keys or [])
    if err:
        return [], 0, 0, err
    if not file_ids:
        return [], 0, 0, None

    sample_size = min(DRY_RUN_SAMPLE_SIZE, len(file_ids))
    sample_ids = random.sample(file_ids, sample_size)
    metadata, err = fetch_tags_by_file(sample_ids, tag_service_keys)
    if err:
        return [], sample_size, len(file_ids), err

    _, previews = tag_cleanup_x._build_plan(metadata, cfg, renderer=None, show_progress=False)
    flat = [(fp.label, e) for fp in previews for e in fp.entries if not e.skipped]
    total_skipped = sum(len(fp.entries) for fp in previews) - len(flat)
    if not flat:
        msg = "(nothing to preview)" if not total_skipped else (
            f"(nothing to preview - all {total_skipped} matched tag(s) in the sample were "
            "skipped as single-word/short)")
        return [msg], sample_size, len(file_ids), None

    lines: list[str] = []
    shown = flat[:40]
    for idx, (label, entry) in enumerate(shown, start=1):
        lines.extend(_render_tag_section_plain(idx, len(flat), label, entry))
    if len(flat) > len(shown):
        lines.append(f"... {len(flat) - len(shown)} more tag(s) not shown here ...")

    total_kept = sum(len(e.tags) for _, e in flat)
    total_dropped = sum(len(e.dropped) for _, e in flat)
    lines.append(f"Summary: {len(previews)} file(s) sampled, {len(flat)} tag(s) processed "
                 f"({total_skipped} more skipped as single-word/short, not shown above), "
                 f"{total_kept} tag(s) kept, {total_dropped} token(s) dropped.")
    return lines, sample_size, len(file_ids), None


# ---------------------------------------------------------------------------------- apply job
# One tracked job at a time, same convention as scripts_runner._runs - starting a new apply
# while one is already running is a no-op the frontend guards against by disabling the button.

@dataclass
class ApplyJob:
    lines: list[str] = field(default_factory=list)
    running: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_jobs: dict[str, ApplyJob] = {}
_JOB_KEY = "tag_cleanup"


def is_apply_running() -> bool:
    job = _jobs.get(_JOB_KEY)
    return bool(job and job.running)


def _run_apply(cfg: Config, source_keys: list[str], dest_keys: list[str],
                file_service_keys: Optional[list[str]], log) -> None:
    log(f"Searching for {cfg.target_tag_wildcards} ...")
    file_ids, err = search_files_multi_domain(cfg.target_tag_wildcards, file_service_keys or [])
    if err:
        log(f"Could not reach Hydrus: {err}")
        return
    log(f"Found {len(file_ids):,} file(s).")
    if not file_ids:
        return

    log("Fetching tag data (this is the slow part on a large library)...")
    metadata, err = fetch_tags_by_file(file_ids, source_keys, chunk_size=cfg.batch_size)
    if err:
        log(f"Could not fetch tags: {err}")
        return

    plan, previews = tag_cleanup_x._build_plan(metadata, cfg, renderer=None, show_progress=False)
    if not previews:
        log("No matching tags found on any matched file. Nothing to do.")
        return

    files_affected = len(plan)
    total_raw_tags = sum(len(fp.entries) for fp in previews)
    log(f"Writing cleaned-up tags for {total_raw_tags} raw tag(s) across {files_affected:,} "
        f"file(s) (adding to {len(dest_keys)} destination service(s), deleting raw tags from "
        f"{len(source_keys)} source service(s))...")

    groups: dict[tuple, list[int]] = {}
    for fid, (tags_to_add, tags_to_delete) in plan.items():
        key = (tuple(tags_to_add), tuple(tags_to_delete))
        groups.setdefault(key, []).append(fid)

    errors: list[str] = []
    done = 0
    for (tags_to_add, tags_to_delete), fids in groups.items():
        for start in range(0, len(fids), cfg.batch_size):
            batch = fids[start:start + cfg.batch_size]
            if tags_to_add:
                for dest_key in dest_keys:
                    r = hydrus_client.add_tags(batch, list(tags_to_add), dest_key)
                    if not r.success:
                        errors.append(r.error or "add_tags failed")
            if tags_to_delete:
                # A raw tag may only actually live on some of the checked source services -
                # deleting it from a service it isn't on is a harmless no-op on Hydrus's end,
                # so this doesn't need to track exactly which service each tag came from.
                for source_key in source_keys:
                    r = hydrus_client.delete_tags(batch, list(tags_to_delete), source_key)
                    if not r.success:
                        errors.append(r.error or "delete_tags failed")
            done += len(batch)
            log(f"Progress: {done:,}/{files_affected:,} file(s)...")

    if errors:
        log(f"{len(errors)} batch write(s) reported an error (first 5 shown):")
        for e in errors[:5]:
            log(f"  - {e}")
    log(f"Done. Submitted changes for {files_affected:,} file(s) to Hydrus (applies in the background).")


def start_apply(cfg: Config, source_keys: list[str], dest_keys: list[str],
                 file_service_keys: Optional[list[str]] = None) -> bool:
    if is_apply_running():
        return False
    job = ApplyJob(running=True)
    _jobs[_JOB_KEY] = job

    def log(line: str) -> None:
        with job.lock:
            job.lines.append(line)

    def worker() -> None:
        try:
            _run_apply(cfg, source_keys, dest_keys, file_service_keys, log)
        except Exception as exc:  # keep the job panel alive/reportable on an unexpected failure
            log(f"[error] {exc}")
        finally:
            with job.lock:
                job.running = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def read_apply_output(since: Optional[int]) -> tuple[list[str], int, bool]:
    job = _jobs.get(_JOB_KEY)
    if job is None:
        return [], 0, False
    with job.lock:
        start_at = 0 if since is None else since
        return job.lines[start_at:], len(job.lines), job.running
