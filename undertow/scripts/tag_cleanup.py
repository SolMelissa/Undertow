"""
Generic filename-derived tag cleanup utility for Hydrus.

Splits bulk-imported "filename tags" (e.g. dir:12-sunset hike - teen couple
watches full moon rise over lake after long trail up xz) into well-formed
individual tags, previews the result, and optionally writes them back to
Hydrus via its Client API.

Run it with no arguments and it walks you through a wizard: enter (or reuse a
saved) Hydrus Client API URL and key, pick a file domain and tag service from
the live list Hydrus reports, then it dry-runs a small random sample of your
real files first, shows the IN/OUT/DROPPED preview for just that sample, and
asks for confirmation before it touches the rest - there's no separate
preview/dry-run/apply menu to pick from first, and no second confirmation once
the sample is approved: it goes straight on to the full library. The URL,
key, and your last picks are stored locally so you don't have to retype them
next time - see `--reconfigure` to start over, or `--self-test` to preview the
built-in fixture tags offline without connecting to Hydrus.

Only hard dependency: requests. wordfreq is an optional soft dependency used
for name detection; without it, only the configured known_names list is used.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import html
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import requests
except ImportError:
    print("This tool requires the 'requests' package: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from wordfreq import zipf_frequency
    HAVE_WORDFREQ = True
except ImportError:
    HAVE_WORDFREQ = False


# ---------------------------------------------------------------------------
# Locally-stored settings (API URL/key, last-picked services, preferences)
# ---------------------------------------------------------------------------
# Stored in plaintext JSON next to hydownloader's own data, same convention as
# the project's other locally-cached credentials (e.g. GALLERY_DL_USER_CONFIG_FILE
# in undertow/config.py) - no extra encryption layer, just kept out of the repo.

def _default_local_config_path() -> Path:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from undertow import config as _undertow_config  # type: ignore
        return _undertow_config.DATA_DIR / "tag-cleanup-config.json"
    except Exception:
        root = Path(os.environ.get("USERPROFILE", str(Path.home())))
        return root / "HydrusPipeline" / "hydownloader-data" / "tag-cleanup-config.json"


LOCAL_CONFIG_FILE = _default_local_config_path()


def load_local_config() -> dict:
    try:
        with open(LOCAL_CONFIG_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        return stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        return {}


def save_local_config(updates: dict) -> None:
    stored = load_local_config()
    stored.update(updates)
    LOCAL_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)


# ---------------------------------------------------------------------------
# Core text-processing engine (content-agnostic)
# ---------------------------------------------------------------------------

def normalize_token(tok: str) -> str:
    tok = tok.strip().lower()
    tok = re.sub(r"[^\w\s\-'&]", "", tok)
    tok = re.sub(r"\s+", " ", tok)
    return tok.strip().strip("-")


CASE_BOUNDARIES = [
    re.compile(r"(?<=[a-z0-9])(?=[A-Z])"),
    re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])"),
]


@dataclass
class Config:
    source_namespace: str = "dir"
    target_service_name: str = "my tags"
    file_service_name: str = "all local files"
    target_tag_wildcards: list = field(default_factory=lambda: ["dir:*"])
    primary_delimiter: str = " - "
    delimiters: list = field(default_factory=lambda: ["_", ",", "|", ";"])
    strip_leading_number_prefix: bool = True
    function_words: set = field(default_factory=lambda: {
        "a", "an", "the", "of", "and", "with", "in", "on", "for", "to", "at", "by", "from",
        "after", "before", "up", "down", "into", "onto", "over", "under", "through",
        "she", "he", "it", "they", "we", "her", "him", "them", "my", "your", "their",
        "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does",
        "did", "can", "could", "will", "would", "shall", "should", "may", "might"})
    corpus_glue_words: set = field(default_factory=lambda: {
        "watches", "records", "picks", "pulls", "drifts", "rolls", "rains", "soaks",
        "reads", "spots", "parts", "gives", "takes", "gets", "after", "then",
        "rise", "delivers", "pours", "taps", "blows", "off", "them", "near", "during",
        "while", "across", "as"})
    attribute_lexicon: set = field(default_factory=lambda: {
        # neutral generics ONLY: colors, sizes, ages, materials, qualities
        "red", "green", "blue", "gray", "grey", "brown", "white", "black", "pale", "tan",
        "small", "big", "large", "tall", "short", "long", "thin", "slim", "thick", "wide",
        "young", "old", "teen", "quiet", "rustic", "fresh", "steep", "cool", "warm",
        "soft", "rare", "full", "wet", "dry", "clean", "bright", "dark"})
    # Multi-person/group nouns that stay split from a preceding attribute
    # (e.g. "teen couple" -> "teen", "couple") since the demographic word is
    # itself a useful standalone tag when it describes a group, not a single
    # object or individual.
    no_merge_target_nouns: set = field(default_factory=lambda: {"couple", "group", "pair", "family"})
    proper_noun_min_words: int = 2
    wordfreq_min_zipf_for_tag: float = 2.0
    known_names: set = field(default_factory=set)
    name_namespace: str = "character"
    drop_suspected_truncation: bool = True
    min_token_len: int = 2
    drop_resolution_like: bool = True
    batch_size: int = 512
    max_workers: int = 8
    interactive: bool = True
    request_retries: int = 3


NUMBER_PREFIX_RE = re.compile(r"^\d+-")
RESOLUTION_RE = re.compile(r"^\d{2,4}x\d{2,4}$")


def split_camel_case(text: str) -> str:
    for pattern in CASE_BOUNDARIES:
        text = pattern.sub(" ", text)
    return text


def strip_number_prefix(block: str) -> str:
    return NUMBER_PREFIX_RE.sub("", block, count=1)


def looks_truncated(tok: str, min_token_len: int) -> bool:
    # Heuristic: very short (<=2 char) alpha token with no vowel, at the very
    # end of a block, is very likely a truncated filename remnant (xz, qp, bk).
    if len(tok) > 2:
        return False
    if len(tok) < 1:
        return False
    if not tok.isalpha():
        return False
    return not any(c in "aeiou" for c in tok)


def is_name_candidate(tok: str, cfg: Config) -> bool:
    if tok in cfg.known_names:
        return True
    if tok in cfg.function_words or tok in cfg.corpus_glue_words:
        return False
    if tok in cfg.attribute_lexicon:
        return False
    if not HAVE_WORDFREQ:
        return False
    zipf = zipf_frequency(tok, "en")
    return zipf < cfg.wordfreq_min_zipf_for_tag and zipf > 0.0


@dataclass
class ParsedTag:
    original: str
    namespace_stripped: str
    tags: List[str]
    dropped: List[str]


@dataclass
class FilePreview:
    """One real file (or one offline fixture), with every namespaced tag it had
    parsed. Almost always a single entry, but a file can carry more than one
    dir: tag, so entries stays a list rather than collapsing to one ParsedTag."""
    label: str
    entries: List[ParsedTag]


def parse_filename_tag(raw_tag: str, cfg: Config) -> ParsedTag:
    """Parse a single raw namespaced tag (e.g. 'dir:12-sunset hike - ...')."""
    value = raw_tag
    prefix = f"{cfg.source_namespace}:"
    if value.startswith(prefix):
        value = value[len(prefix):]
    original_value = value

    # Split into blocks on primary delimiter (' - '), then on secondary
    # delimiters within each block.
    blocks = value.split(cfg.primary_delimiter)
    if cfg.strip_leading_number_prefix and blocks:
        blocks[0] = strip_number_prefix(blocks[0])

    dropped: List[str] = []
    kept_tags: List[str] = []

    for block_idx, block in enumerate(blocks):
        block = split_camel_case(block)
        for delim in cfg.delimiters:
            block = block.replace(delim, " ")

        raw_tokens = [normalize_token(t) for t in block.split(" ")]
        raw_tokens = [t for t in raw_tokens if t]

        # Pass 1: drop function/glue words and junk, keep everything else as
        # a candidate list preserving order.
        candidates: List[str] = []
        n = len(raw_tokens)
        for i, tok in enumerate(raw_tokens):
            if len(tok) < cfg.min_token_len:
                dropped.append(tok)
                continue
            if tok in cfg.function_words or tok in cfg.corpus_glue_words:
                dropped.append(tok)
                continue
            if cfg.drop_resolution_like and RESOLUTION_RE.match(tok):
                dropped.append(tok)
                continue
            is_last_token_overall = (block_idx == len(blocks) - 1) and (i == n - 1)
            if cfg.drop_suspected_truncation and is_last_token_overall and looks_truncated(tok, cfg.min_token_len):
                dropped.append(tok)
                continue
            candidates.append(tok)

        # Pass 2: detect name runs among remaining candidates.
        name_flags = [is_name_candidate(t, cfg) for t in candidates]

        # Pass 3: merge attribute-adjective + following-noun phrases, and
        # group consecutive name tokens into a single character: tag.
        i = 0
        while i < len(candidates):
            tok = candidates[i]
            if name_flags[i]:
                run = [tok]
                j = i + 1
                while j < len(candidates) and name_flags[j]:
                    run.append(candidates[j])
                    j += 1
                kept_tags.append(f"{cfg.name_namespace}:{' '.join(run)}")
                i = j
                continue

            if (tok in cfg.attribute_lexicon and i + 1 < len(candidates) and not name_flags[i + 1]
                    and candidates[i + 1] not in cfg.no_merge_target_nouns):
                phrase = f"{tok} {candidates[i + 1]}"
                kept_tags.append(phrase)
                i += 2
                continue

            kept_tags.append(tok)
            i += 1

    # De-duplicate while preserving order.
    seen: Set[str] = set()
    deduped: List[str] = []
    for t in kept_tags:
        if t not in seen:
            seen.add(t)
            deduped.append(t)

    return ParsedTag(original=raw_tag, namespace_stripped=original_value, tags=deduped, dropped=dropped)


# ---------------------------------------------------------------------------
# Hydrus Client API layer
# ---------------------------------------------------------------------------

class HydrusClient:
    def __init__(self, base_url: str, access_key: str, retries: int = 3, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Hydrus-Client-API-Access-Key"] = access_key
        self.retries = retries
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        return self._request("GET", path, params=params)

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json=payload)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except Exception as exc:  # noqa: BLE001 - want to retry on any transient failure
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"Request to {url} failed after {self.retries} attempts: {last_exc}")

    def get_services(self) -> dict:
        return self._get("/get_services", {})

    def resolve_service_key(self, name: str) -> str:
        services = self.get_services()
        services_dict = services.get("services", {})
        if isinstance(services_dict, dict):
            for key, svc in services_dict.items():
                if svc.get("name") == name:
                    return key
        available = sorted(svc.get("name", "?") for svc in services_dict.values()) if isinstance(services_dict, dict) else []
        raise ValueError(
            f"No Hydrus service found named {name!r}. Available services: {', '.join(available) or '(none returned)'}"
        )

    def list_services(self) -> List[Tuple[str, str, str]]:
        """Returns (name, service_key, type_pretty) tuples for every service Hydrus knows about."""
        services = self.get_services()
        services_dict = services.get("services", {})
        if not isinstance(services_dict, dict):
            return []
        return sorted(
            (svc.get("name", "?"), key, svc.get("type_pretty", "?"))
            for key, svc in services_dict.items()
        )

    def list_file_services(self) -> List[Tuple[str, str, str]]:
        return [s for s in self.list_services() if "file" in s[2].lower()]

    def list_tag_services(self) -> List[Tuple[str, str, str]]:
        return [s for s in self.list_services() if "tag" in s[2].lower()]

    def search_files(self, tags: List[str], file_service_key: str) -> List[int]:
        params = {
            "tags": json.dumps(tags),
            "file_service_key": file_service_key,
            "return_file_ids": "true",
        }
        result = self._get("/get_files/search_files", params)
        return result.get("file_ids", [])

    def fetch_metadata(self, file_ids: List[int], tag_service_key: str,
                        chunk_size: int = 256, on_progress=None) -> Dict[int, List[str]]:
        """Chunked so a large library (tens of thousands of files) doesn't build one giant
        query string and time out in a single request; on_progress(done, total), if given, is
        called after each chunk so the caller can print progress."""
        out: Dict[int, List[str]] = {}
        total = len(file_ids)
        for start in range(0, total, chunk_size):
            chunk = file_ids[start:start + chunk_size]
            params = {"file_ids": json.dumps(chunk)}
            result = self._get("/get_files/file_metadata", params)
            for meta in result.get("metadata", []):
                fid = meta.get("file_id")
                tags_block = meta.get("tags", {}).get(tag_service_key, {})
                storage = tags_block.get("storage_tags", {})
                current = storage.get("0", [])
                out[fid] = current
            if on_progress:
                on_progress(min(start + chunk_size, total), total)
        return out

    def add_tags(self, file_ids: List[int], tag_service_key: str,
                 tags_to_add: List[str], tags_to_delete: List[str]) -> None:
        self.add_tags_multi(file_ids, {tag_service_key: (tags_to_add, tags_to_delete)})

    def add_tags_multi(self, file_ids: List[int],
                        service_actions: Dict[str, Tuple[List[str], List[str]]]) -> None:
        """service_actions maps tag_service_key -> (tags_to_add, tags_to_delete), so a single
        call can add to one tag service (e.g. the cleaned-tag destination) while deleting from a
        different one (e.g. the raw-filename-tag source), when those aren't the same service."""
        service_keys_to_actions_to_tags: Dict[str, Dict[str, List[str]]] = {}
        for tag_service_key, (tags_to_add, tags_to_delete) in service_actions.items():
            actions: Dict[str, List[str]] = {}
            if tags_to_add:
                actions["0"] = tags_to_add
            if tags_to_delete:
                actions["1"] = tags_to_delete
            if actions:
                service_keys_to_actions_to_tags[tag_service_key] = actions
        if not service_keys_to_actions_to_tags:
            return
        payload = {
            "file_ids": file_ids,
            "service_keys_to_actions_to_tags": service_keys_to_actions_to_tags,
        }
        self._post("/add_tags/add_tags", payload)


# ---------------------------------------------------------------------------
# Preview / apply orchestration
# ---------------------------------------------------------------------------

class ProgressPrinter:
    """Prints a single overwritten status line, throttled to at most once every
    `min_interval` seconds, so a long-running fetch/apply loop never looks hung
    even when nothing else in the terminal is moving."""

    def __init__(self, label: str, total: int, min_interval: float = 0.5):
        self.label = label
        self.total = total
        self.min_interval = min_interval
        self.start = time.monotonic()
        self._last_print = 0.0

    def update(self, done: int, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_print < self.min_interval and done < self.total:
            return
        self._last_print = now
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        pct = (done / self.total * 100) if self.total else 100.0
        eta = (self.total - done) / rate if rate > 0 else 0.0
        sys.stdout.write(
            f"\r{self.label}: {done:,}/{self.total:,} ({pct:4.1f}%)  "
            f"{rate:,.0f}/s  elapsed {elapsed:6.0f}s  eta {eta:6.0f}s   "
        )
        sys.stdout.flush()

    def done(self) -> None:
        self.update(self.total, force=True)
        sys.stdout.write("\n")
        sys.stdout.flush()


# Above this many files, the full IN/OUT/DROPPED breakdown is written to a log file
# instead of the terminal, and only the first 10 files plus the summary line are
# printed - scrolling tens of thousands of lines is neither readable nor useful.
PREVIEW_INLINE_LIMIT = 40


def _render_file_block(idx: int, total: int, fp: FilePreview) -> List[str]:
    lines = [f"===== File {idx}/{total} - {fp.label} ====="]
    for entry in fp.entries:
        out_str = ", ".join(entry.tags) if entry.tags else "(nothing kept)"
        dropped_str = ", ".join(entry.dropped) if entry.dropped else "-"
        lines.append(f"  IN:      {entry.original}")
        lines.append(f"  OUT:     {out_str}")
        lines.append(f"  DROPPED: {dropped_str}")
    lines.append("")
    return lines


def print_preview_table(previews: List[FilePreview], log_path: Optional[Path] = None) -> None:
    if not previews:
        print("(nothing to preview)")
        return

    inline = len(previews) <= PREVIEW_INLINE_LIMIT
    shown = previews if inline else previews[:10]

    lines: List[str] = []
    for idx, fp in enumerate(shown, start=1):
        lines.extend(_render_file_block(idx, len(previews), fp))
    print("\n".join(lines).rstrip("\n"))

    if not inline:
        print(f"... {len(previews) - len(shown)} more file(s) not shown here ...")

    if log_path and not inline:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        full_lines: List[str] = []
        for idx, fp in enumerate(previews, start=1):
            full_lines.extend(_render_file_block(idx, len(previews), fp))
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(full_lines) + "\n")
        print(f"Full IN/OUT/DROPPED detail for all {len(previews)} file(s) written to: {log_path}")

    total_entries = sum(len(fp.entries) for fp in previews)
    total_kept = sum(len(e.tags) for fp in previews for e in fp.entries)
    total_dropped = sum(len(e.dropped) for fp in previews for e in fp.entries)
    print(f"Summary: {len(previews)} file(s), {total_entries} tag(s) processed, "
          f"{total_kept} tag(s) kept, {total_dropped} token(s) dropped.")


# Local, offline HTML report - never uploaded anywhere. Regenerated fresh every run
# so it always reflects the most recent preview only; old reports aren't kept around.
HTML_OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "html"

_HTML_STYLE = """
  * { box-sizing: border-box; }
  body { margin: 0; background: #f4f5f7; color: #14171c;
         font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }
  .page { max-width: 860px; margin: 0 auto; padding: 40px 24px 72px; }
  .eyebrow { font-family: Consolas, "Courier New", monospace; font-size: 12px; letter-spacing: .1em;
             text-transform: uppercase; color: #7a8190; margin: 0 0 8px; }
  h1 { font-size: 28px; margin: 0 0 10px; }
  .lede { font-size: 15px; line-height: 1.55; color: #4b515c; max-width: 70ch; margin: 0 0 22px; }
  .summary { display: flex; border: 1px solid #dde1e6; border-radius: 10px; overflow: hidden; background: #fff; }
  .summary .stat { flex: 1; padding: 14px 18px; border-right: 1px solid #dde1e6; }
  .summary .stat:last-child { border-right: none; }
  .summary .stat .num { font-family: Consolas, monospace; font-size: 22px; font-weight: 700; }
  .summary .stat .label { font-size: 12px; color: #7a8190; margin-top: 4px; }
  .summary .stat.keep .num { color: #1e7a5e; }
  .summary .stat.drop .num { color: #a6433a; }
  .files { display: flex; flex-direction: column; gap: 18px; margin-top: 32px; }
  .file-card { background: #fff; border: 1px solid #dde1e6; border-radius: 12px; padding: 18px 20px 20px; }
  .file-card__head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  .file-card__eyebrow { font-family: Consolas, monospace; font-size: 11px; letter-spacing: .08em; color: #7a8190; }
  .file-card__title { font-size: 16px; font-weight: 600; margin: 0; }
  .file-card__path { font-family: Consolas, "Courier New", monospace; font-size: 12.5px; line-height: 1.6;
                      background: #eef0f3; border: 1px solid #dde1e6; border-radius: 8px; padding: 9px 12px;
                      color: #4b515c; overflow-x: auto; white-space: pre; margin: 0 0 14px; }
  .file-card__path .ns { color: #205c4b; font-weight: 700; }
  .file-card__body { display: grid; grid-template-columns: 1.4fr 1fr; gap: 14px; margin-bottom: 14px; }
  .tagblock { border-radius: 8px; padding: 10px 12px 12px; border: 1px solid; }
  .tagblock--keep { background: #e4f4ee; border-color: #bee3d3; }
  .tagblock--drop { background: #fbeae8; border-color: #f0c7c1; }
  .tagblock__label { font-size: 11px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; margin-bottom: 8px; }
  .tagblock--keep .tagblock__label { color: #1e7a5e; }
  .tagblock--drop .tagblock__label { color: #a6433a; }
  .chips { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
  .chips li { font-family: Consolas, monospace; font-size: 12px; padding: 3px 8px; border-radius: 5px;
              background: #fff; border: 1px solid #bee3d3; color: #14171c; }
  .chips--drop li { border-color: #f0c7c1; color: #7a8190; text-decoration: line-through; text-decoration-color: #a6433a; }
  .chips li.empty, .chips--drop li.empty { text-decoration: none; font-style: italic; color: #7a8190; border-style: dashed; }
  footer.note { margin-top: 36px; font-size: 12.5px; color: #7a8190; border-top: 1px solid #dde1e6; padding-top: 14px; }
  @media (max-width: 620px) { .file-card__body { grid-template-columns: 1fr; } .summary { flex-direction: column; }
    .summary .stat { border-right: none; border-bottom: 1px solid #dde1e6; } .summary .stat:last-child { border-bottom: none; } }
"""


def write_html_report(previews: List[FilePreview], title: str, summary_note: str) -> Path:
    HTML_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = HTML_OUTPUT_DIR / f"tag-cleanup-{stamp}.html"

    total_files = len(previews)
    total_entries = sum(len(fp.entries) for fp in previews)
    total_kept = sum(len(e.tags) for fp in previews for e in fp.entries)
    total_dropped = sum(len(e.dropped) for fp in previews for e in fp.entries)

    def chips(items: List[str], empty_label: str) -> str:
        if not items:
            return f'<li class="empty">{html.escape(empty_label)}</li>'
        return "".join(f"<li>{html.escape(t)}</li>" for t in items)

    cards: List[str] = []
    for idx, fp in enumerate(previews, start=1):
        blocks: List[str] = []
        for entry in fp.entries:
            ns, _, rest = entry.original.partition(":")
            blocks.append(f"""
      <div class="file-card__path"><span class="ns">{html.escape(ns)}:</span>{html.escape(rest)}</div>
      <div class="file-card__body">
        <div class="tagblock tagblock--keep">
          <div class="tagblock__label">Kept &mdash; {len(entry.tags)} tag(s)</div>
          <ul class="chips">{chips(entry.tags, "(nothing kept)")}</ul>
        </div>
        <div class="tagblock tagblock--drop">
          <div class="tagblock__label">Dropped &mdash; {len(entry.dropped)} token(s)</div>
          <ul class="chips chips--drop">{chips(entry.dropped, "(nothing dropped)")}</ul>
        </div>
      </div>""")
        cards.append(f"""
    <section class="file-card">
      <div class="file-card__head">
        <span class="file-card__eyebrow">FILE {idx:02d} / {total_files}</span>
        <h2 class="file-card__title">{html.escape(fp.label)}</h2>
      </div>{"".join(blocks)}
    </section>""")

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<div class="page">
  <header>
    <p class="eyebrow">tag_cleanup.py</p>
    <h1>{html.escape(title)}</h1>
    <p class="lede">{html.escape(summary_note)}</p>
    <div class="summary">
      <div class="stat"><div class="num">{total_files}</div><div class="label">files</div></div>
      <div class="stat"><div class="num">{total_entries}</div><div class="label">tags processed</div></div>
      <div class="stat keep"><div class="num">{total_kept}</div><div class="label">tags kept</div></div>
      <div class="stat drop"><div class="num">{total_dropped}</div><div class="label">tokens dropped</div></div>
    </div>
  </header>
  <div class="files">{"".join(cards)}
  </div>
  <footer class="note">Generated {datetime.datetime.now().isoformat(timespec="seconds")} by tag_cleanup.py. Local file only - nothing here is uploaded anywhere.</footer>
</div>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


FIXTURES = [
    "dir:12-sunset hike - teen couple watches full moon rise over lake after long trail up xz",
    "dir:03-bird watching - adult woman records a rare woodpecker through her binoculars on quiet morning qp",
    "dir:08-kitchen prep - young chef parts her fresh herbs after trimming up fresh basil for supper bk",
    "dir:26-slow river - small boat drifts past tall reeds and old wooden pier under warm noon sun fw",
    "dir:45-autumn walk - tall man picks up red maple leaves after strong wind blows them off branch ql",
    "dir:17-morning coffee - slim barista pours hot milk into large ceramic cup on rustic wooden counter vp",
    "dir:31-library quiet - short student reads thick novel while soft rain taps glass window during study mn",
    "dir:09-garden work - kind grandma pulls up long weeds after steady rain soaks green flower bed tr",
    "dir:22-bike ride - fit courier delivers fresh bread across old stone bridge up steep city hill kx",
    "dir:38-mountain view - young hiker spots a gray fox near high ridge trail as cool fog rolls in yd",
]


def run_self_test(cfg: Config) -> None:
    previews = [FilePreview(label=f"fixture {i}", entries=[parse_filename_tag(t, cfg)])
                for i, t in enumerate(FIXTURES, start=1)]
    print_preview_table(previews)
    report_path = write_html_report(
        previews, title="Self-Test Preview",
        summary_note=f"Offline preview of the {len(FIXTURES)} built-in fixture tags in FIXTURES - "
                      "no Hydrus connection involved.")
    print(f"HTML report written to: {report_path}")

    expected = ["sunset", "hike", "teen", "couple", "full moon", "lake", "long trail"]
    actual = previews[0].entries[0].tags
    if actual == expected:
        print("Fixture 1 matches expected output. OK.")
    else:
        print(f"Fixture 1 MISMATCH.\n  expected: {expected}\n  actual:   {actual}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Interactive wizard to clean up filename-derived Hydrus tags via the Client API.",
        epilog="Just run `python tag_cleanup.py` with no arguments - it walks you through the rest.",
    )
    p.add_argument("--reconfigure", action="store_true",
                    help="Ignore saved settings and re-enter the API URL/key and service picks from scratch")
    p.add_argument("--self-test", action="store_true",
                    help="Preview the built-in fixture tags offline (no Hydrus connection) and exit")
    return p


# ---------------------------------------------------------------------------
# Small interactive-prompt helpers
# ---------------------------------------------------------------------------

def prompt_text(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        print("  A value is required.")


def prompt_secret(label: str, has_saved: bool) -> Optional[str]:
    """Returns None if the caller should keep whatever secret is already saved."""
    hint = " (leave blank to keep the saved key)" if has_saved else ""
    value = getpass.getpass(f"{label}{hint}: ").strip()
    return value or None


def prompt_yes_no(label: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    raw = input(f"{label}{suffix}: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def prompt_choice(label: str, options: List[Tuple[str, str]], default_key: Optional[str] = None) -> str:
    """options: list of (display_text, value). Returns the chosen value."""
    print(f"\n{label}")
    default_idx = 1
    for idx, (display, value) in enumerate(options, start=1):
        marker = " (saved default)" if value == default_key else ""
        print(f"  {idx}. {display}{marker}")
        if value == default_key:
            default_idx = idx
    while True:
        raw = input(f"Choose 1-{len(options)} [{default_idx}]: ").strip()
        if not raw:
            return options[default_idx - 1][1]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print("  Not a valid choice, try again.")


def prompt_name_list(label: str, default: List[str]) -> List[str]:
    default_str = ", ".join(default) if default else "(none)"
    raw = input(f"{label} (comma-separated) [{default_str}]: ").strip()
    if not raw:
        return default
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

def wizard_connect(saved: dict, reconfigure: bool) -> Tuple[HydrusClient, str, str]:
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
            print(f"Saved Hydrus connection ({saved_url}) did not respond - reconfigure it below.")
        else:
            return client, saved_url, saved_key

    default_url = saved_url or "http://127.0.0.1:45869"
    saved_key = None if reconfigure else saved_key

    while True:
        api_url = prompt_text("Hydrus Client API URL", default=default_url)
        if saved_key:
            masked = f"...{saved_key[-4:]}" if len(saved_key) >= 4 else "(saved)"
            if prompt_yes_no(f"Use saved API key ({masked})?", default=True):
                api_key = saved_key
            else:
                api_key = prompt_secret("Hydrus Client API access key", has_saved=False) or ""
        else:
            print("Find/create an access key in Hydrus under services > review services > the 'client api' tab.")
            api_key = prompt_secret("Hydrus Client API access key", has_saved=False) or ""

        if not api_key:
            print("  An API key is required.")
            continue

        client = HydrusClient(api_url, api_key)
        try:
            client.get_services()
        except (requests.RequestException, RuntimeError) as exc:
            print(f"  Could not connect: {exc}")
            if not prompt_yes_no("Try again?", default=True):
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


def wizard_pick_services(client: HydrusClient, saved: dict) -> ServiceSelection:
    file_services = client.list_file_services()
    tag_services = client.list_tag_services()
    if not file_services:
        raise SystemExit("Hydrus reported no file services - is the Client API enabled with file access?")
    if not tag_services:
        raise SystemExit("Hydrus reported no tag services - is the Client API enabled with tag access?")

    file_options = [(f"{name}  ({type_pretty})", key) for name, key, type_pretty in file_services]
    file_key = prompt_choice("Which file domain should be searched?", file_options,
                              default_key=saved.get("file_service_key"))
    file_name = next(name for name, key, _ in file_services if key == file_key)

    tag_options = [(f"{name}  ({type_pretty})", key) for name, key, type_pretty in tag_services]
    source_key = prompt_choice("Which tag service holds the raw filename tags to read and clean up?",
                                tag_options, default_key=saved.get("source_tag_service_key"))
    source_name = next(name for name, key, _ in tag_services if key == source_key)

    print(f"\nBy default the cleaned-up tags are written back to the same service ({source_name!r}), "
          "removing the raw filename tag from there. Choose a different service if you'd rather the "
          "split tags land somewhere else (e.g. keep raw filename tags in one service, put clean "
          "tags in your main tagging service).")
    dest_default = saved.get("dest_tag_service_key", source_key)
    dest_key = prompt_choice("Which tag service should the cleaned-up tags be written to?",
                              tag_options, default_key=dest_default)
    dest_name = next(name for name, key, _ in tag_services if key == dest_key)

    save_local_config({
        "file_service_name": file_name, "file_service_key": file_key,
        "source_tag_service_name": source_name, "source_tag_service_key": source_key,
        "dest_tag_service_name": dest_name, "dest_tag_service_key": dest_key,
    })
    return ServiceSelection(file_name, file_key, source_name, source_key, dest_name, dest_key)


def wizard_build_config(saved: dict) -> Config:
    print()
    source_namespace = prompt_text("Namespace holding the raw filename tags", default=saved.get("source_namespace", "dir"))
    known_names = prompt_name_list("Extra known name tokens to always tag as character:", default=saved.get("known_names", []))
    drop_truncation = prompt_yes_no("Drop suspected truncated trailing tokens (short consonant-only remnants)?",
                                     default=saved.get("drop_suspected_truncation", True))

    save_local_config({
        "source_namespace": source_namespace,
        "known_names": known_names,
        "drop_suspected_truncation": drop_truncation,
    })

    cfg = Config(source_namespace=source_namespace, drop_suspected_truncation=drop_truncation)
    cfg.known_names |= set(known_names)
    cfg.target_tag_wildcards = [f"{source_namespace}:*"]
    return cfg


DRY_RUN_SAMPLE_SIZE = 25


def _build_plan(metadata: Dict[int, List[str]], cfg: Config) -> Tuple[Dict[int, Tuple[List[str], List[str]]], List[FilePreview]]:
    """Turns raw {file_id: [current tags]} metadata into a write plan (fid -> (tags_to_add,
    tags_to_delete)) plus one FilePreview per file, for previewing or applying."""
    plan: Dict[int, Tuple[List[str], List[str]]] = {}
    previews: List[FilePreview] = []
    for fid, current_tags in metadata.items():
        entries: List[ParsedTag] = []
        for raw_tag in current_tags:
            if not raw_tag.startswith(f"{cfg.source_namespace}:"):
                continue
            parsed = parse_filename_tag(raw_tag, cfg)
            entries.append(parsed)
            to_add, to_delete = plan.setdefault(fid, ([], []))
            to_add.extend(t for t in parsed.tags if t not in to_add)
            to_delete.append(raw_tag)
        if entries:
            previews.append(FilePreview(label=f"file_id {fid}", entries=entries))
    return plan, previews


def _chunked(seq: List[int], size: int) -> List[List[int]]:
    return [seq[start:start + size] for start in range(0, len(seq), size)]


def run_dry_run_then_apply(client: HydrusClient, cfg: Config, services: ServiceSelection) -> int:
    same_service = services.source_tag_service_key == services.dest_tag_service_key

    print(f"\nSearching {services.file_service_name!r} for {cfg.target_tag_wildcards}...")
    try:
        file_ids = client.search_files(cfg.target_tag_wildcards, services.file_service_key)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"Could not reach Hydrus: {exc}", file=sys.stderr)
        return 1
    print(f"Found {len(file_ids):,} file(s).")
    if not file_ids:
        return 0

    # Dry run: preview a small random sample first rather than fetching/parsing the whole
    # (possibly 100k+ file) library up front, so a bad config choice is caught in seconds
    # instead of after minutes of fetching.
    sample_size = min(DRY_RUN_SAMPLE_SIZE, len(file_ids))
    sample_ids = random.sample(file_ids, sample_size)
    print(f"\nDry run: fetching a random sample of {sample_size} file(s) to preview before touching "
          f"the full library...")
    try:
        sample_metadata = client.fetch_metadata(sample_ids, services.source_tag_service_key,
                                                  chunk_size=cfg.batch_size)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f"Could not reach Hydrus while fetching sample tags: {exc}", file=sys.stderr)
        return 1

    _, sample_previews = _build_plan(sample_metadata, cfg)
    if not sample_previews:
        print(f"None of the {sample_size} sampled file(s) had a {cfg.source_namespace!r}-namespaced tag. "
              "Nothing to preview.")
        return 0

    print_preview_table(sample_previews)
    report_path = write_html_report(
        sample_previews, title="Dry-Run Sample Preview",
        summary_note=f"Random sample of {sample_size} file(s) out of {len(file_ids):,} matched by "
                     f"{cfg.target_tag_wildcards}, fetched from {services.source_tag_service_name!r} "
                     "before touching anything.")
    print(f"HTML report written to: {report_path}")

    if not prompt_yes_no(
            f"\nAbove is a preview of {sample_size} randomly-sampled file(s) out of {len(file_ids):,} "
            f"found. Does this look right? Proceed to run on the full {len(file_ids):,} file(s)?",
            default=False):
        print("Aborted, no changes written.")
        return 0

    print(f"\nFetching tag data from {services.source_tag_service_name!r} for all {len(file_ids):,} "
          f"file(s) (in batches of {cfg.batch_size}, this is the slow part on a large library)...")
    fetch_progress = ProgressPrinter("Fetching tags", len(file_ids))
    try:
        metadata = client.fetch_metadata(
            file_ids, services.source_tag_service_key, chunk_size=cfg.batch_size,
            on_progress=lambda done, total: fetch_progress.update(done),
        )
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        fetch_progress.done()
        print(f"Could not reach Hydrus while fetching tags: {exc}", file=sys.stderr)
        return 1
    fetch_progress.done()

    plan, previews = _build_plan(metadata, cfg)
    if not previews:
        print(f"No tags with namespace {cfg.source_namespace!r} found in "
              f"{services.source_tag_service_name!r} on the matched files. Nothing to do.")
        return 0

    log_path = LOCAL_CONFIG_FILE.parent / "tag-cleanup-last-preview.txt"
    print_preview_table(previews, log_path=log_path)
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
    print(f"\nWriting cleaned-up tags for {total_raw_tags} raw tag(s) across {files_affected:,} file(s) "
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

    apply_progress = ProgressPrinter("Applying", files_affected)
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
        print(f"{len(errors)} batch(es) ({failed_files:,} file(s)) failed to update (first 10 shown):",
              file=sys.stderr)
        for line in errors[:10]:
            print(f"  - {line}", file=sys.stderr)

    print(f"Submitted changes for {succeeded_count:,}/{files_affected:,} file(s) to Hydrus's add_tags "
          f"queue (applies in the background).")
    return 1 if errors else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    print("=== Hydrus filename-tag cleanup ===")

    if args.self_test:
        print()
        run_self_test(Config())
        return 0

    saved = {} if args.reconfigure else load_local_config()
    client, _, _ = wizard_connect(saved, args.reconfigure)
    saved = load_local_config()  # picks up the freshly-saved url/key
    services = wizard_pick_services(client, saved)
    saved = load_local_config()
    cfg = wizard_build_config(saved)

    # run_dry_run_then_apply previews a small random sample first and asks for
    # confirmation before ever touching the full library, so there's no separate
    # dry-run-only path to choose up front.
    return run_dry_run_then_apply(client, cfg, services)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted, no further changes made.", file=sys.stderr)
        sys.exit(130)
