"""
Generic filename-derived tag cleanup utility for Hydrus.

Splits bulk-imported "filename tags" (e.g. dir:12-sunset hike - teen couple
watches full moon rise over lake after long trail up xz) into well-formed
individual tags, previews the result, and optionally writes them back to
Hydrus via its Client API.

Usage:
    python tag_cleanup.py --preview
    python tag_cleanup.py --dry-run
    python tag_cleanup.py --apply

Only hard dependency: requests. wordfreq is an optional soft dependency used
for name detection; without it, only the configured known_names list is used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
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
        "while", "across", "as", "them off"})
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
        for group in services.get("services", {}).values() if isinstance(services.get("services"), dict) else []:
            pass
        # Hydrus returns {"services": {service_key: {...}}} in newer API versions,
        # but also a flat "tag_services"/"file_services" style in older ones.
        services_dict = services.get("services", {})
        if isinstance(services_dict, dict):
            for key, svc in services_dict.items():
                if svc.get("name") == name:
                    return key
        raise ValueError(f"No Hydrus service found with name {name!r}")

    def search_files(self, tags: List[str], file_service_key: str) -> List[int]:
        params = {
            "tags": json.dumps(tags),
            "file_service_key": file_service_key,
            "return_file_ids": "true",
        }
        result = self._get("/get_files/search_files", params)
        return result.get("file_ids", [])

    def fetch_metadata(self, file_ids: List[int], tag_service_key: str) -> Dict[int, List[str]]:
        params = {"file_ids": json.dumps(file_ids)}
        result = self._get("/get_files/file_metadata", params)
        out: Dict[int, List[str]] = {}
        for meta in result.get("metadata", []):
            fid = meta.get("file_id")
            tags_block = meta.get("tags", {}).get(tag_service_key, {})
            storage = tags_block.get("storage_tags", {})
            current = storage.get("0", [])
            out[fid] = current
        return out

    def add_tags(self, file_ids: List[int], tag_service_key: str,
                 tags_to_add: List[str], tags_to_delete: List[str]) -> None:
        actions: Dict[str, List[str]] = {}
        if tags_to_add:
            actions["0"] = tags_to_add
        if tags_to_delete:
            actions["1"] = tags_to_delete
        if not actions:
            return
        payload = {
            "file_ids": file_ids,
            "service_keys_to_actions_to_tags": {tag_service_key: actions},
        }
        self._post("/add_tags/add_tags", payload)


# ---------------------------------------------------------------------------
# Preview / apply orchestration
# ---------------------------------------------------------------------------

def print_preview_table(results: List[ParsedTag]) -> None:
    for r in results:
        in_str = r.original
        out_str = ", ".join(r.tags) if r.tags else "(nothing kept)"
        dropped_str = ", ".join(r.dropped) if r.dropped else "-"
        print(f"IN:      {in_str}")
        print(f"OUT:     {out_str}")
        print(f"DROPPED: {dropped_str}")
        print("-" * 70)


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
    results = [parse_filename_tag(t, cfg) for t in FIXTURES]
    print_preview_table(results)
    expected = ["sunset", "hike", "teen", "couple", "full moon", "lake", "long trail"]
    actual = results[0].tags
    if actual == expected:
        print("Fixture 1 matches expected output. OK.")
    else:
        print(f"Fixture 1 MISMATCH.\n  expected: {expected}\n  actual:   {actual}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Clean up filename-derived Hydrus tags.")
    p.add_argument("--source-namespace", default="dir", help="Namespace holding raw filename tags (default: dir)")
    p.add_argument("--wildcard", action="append", dest="wildcards",
                    help="Tag wildcard to search for (repeatable, default: '<namespace>:*')")
    p.add_argument("--file-service", default="all local files", help="Hydrus file service name")
    p.add_argument("--tag-service", default="my tags", help="Hydrus tag service name to read/write")
    p.add_argument("--api-url", default="http://127.0.0.1:45869", help="Hydrus Client API base URL")
    p.add_argument("--api-key", help="Hydrus Client API access key")
    p.add_argument("--known-names", nargs="*", default=[], help="Extra known name tokens")
    p.add_argument("--no-truncation-drop", action="store_true", help="Disable trailing-truncation drop heuristic")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Run against built-in fixtures only, no API calls")
    mode.add_argument("--dry-run", action="store_true", help="Fetch real tags from Hydrus, show planned changes, write nothing")
    mode.add_argument("--apply", action="store_true", help="Fetch real tags, and write add/delete changes to Hydrus")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    cfg = Config(
        source_namespace=args.source_namespace,
        target_service_name=args.tag_service,
        file_service_name=args.file_service,
        drop_suspected_truncation=not args.no_truncation_drop,
    )
    if args.known_names:
        cfg.known_names |= set(k.lower() for k in args.known_names)
    if args.wildcards:
        cfg.target_tag_wildcards = args.wildcards
    else:
        cfg.target_tag_wildcards = [f"{cfg.source_namespace}:*"]

    if args.preview or not (args.dry_run or args.apply):
        print("Running preview against built-in fixtures (no Hydrus connection made).\n")
        run_self_test(cfg)
        return 0

    if not args.api_key:
        print("--api-key is required for --dry-run or --apply", file=sys.stderr)
        return 1

    client = HydrusClient(args.api_url, args.api_key)
    file_service_key = client.resolve_service_key(cfg.file_service_name)
    tag_service_key = client.resolve_service_key(cfg.target_service_name)

    file_ids = client.search_files(cfg.target_tag_wildcards, file_service_key)
    print(f"Found {len(file_ids)} file(s) matching {cfg.target_tag_wildcards}.")
    if not file_ids:
        return 0

    metadata = client.fetch_metadata(file_ids, tag_service_key)

    plan: List[Tuple[int, str, ParsedTag]] = []
    for fid, current_tags in metadata.items():
        for raw_tag in current_tags:
            if not raw_tag.startswith(f"{cfg.source_namespace}:"):
                continue
            parsed = parse_filename_tag(raw_tag, cfg)
            plan.append((fid, raw_tag, parsed))

    print_preview_table([p for _, _, p in plan])

    if args.dry_run:
        print(f"\nDRY RUN: would update {len(plan)} tag(s) across {len(file_ids)} file(s). No changes written.")
        return 0

    if args.interactive if hasattr(args, "interactive") else True:
        confirm = input(f"\nApply {len(plan)} tag change(s) to Hydrus now? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted, no changes written.")
            return 0

    batch_size = cfg.batch_size
    for i in range(0, len(plan), batch_size):
        batch = plan[i:i + batch_size]
        for fid, raw_tag, parsed in batch:
            client.add_tags(
                file_ids=[fid],
                tag_service_key=tag_service_key,
                tags_to_add=parsed.tags,
                tags_to_delete=[raw_tag],
            )
    print(f"Submitted {len(plan)} tag change(s) to Hydrus's add_tags queue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
