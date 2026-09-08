"""
Tag Cleanup X text engine: content-agnostic parsing of filename-derived tags.

This module handles text processing only - no network, no terminal I/O, no prompts.
Its only file reads are the editable word lists (via tag_cleanup_x_lists) and the
optional performer-name gazetteer cache that performer_gazetteer.py builds.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from wordfreq import zipf_frequency
except ImportError:
    print("This tool requires the 'wordfreq' package: pip install wordfreq", file=sys.stderr)
    sys.exit(1)

import tag_cleanup_x_lists as tag_cleanup_lists


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
    # These five sets, plus compound_noun_pairs below, are user-editable - their actual
    # working values live in tag_cleanup_lists.json (see tag_cleanup_x_lists.py), edited via
    # the webui's "Tag Cleanup Lists" panel. The default_factory below always reflects
    # whatever's on disk (falling back to tag_cleanup_lists.DEFAULT_LISTS on first run), so
    # a fresh Config() picks up the user's current customizations automatically.
    function_words: set = field(default_factory=lambda: set(tag_cleanup_lists.load_lists()["function_words"]))
    corpus_glue_words: set = field(default_factory=lambda: set(tag_cleanup_lists.load_lists()["corpus_glue_words"]))
    attribute_lexicon: set = field(default_factory=lambda: set(tag_cleanup_lists.load_lists()["attribute_lexicon"]))
    # Multi-person/group nouns that stay split from a preceding attribute
    # (e.g. "teen couple" -> "teen", "couple") since the demographic word is
    # itself a useful standalone tag when it describes a group, not a single
    # object or individual.
    no_merge_target_nouns: set = field(default_factory=lambda: set(tag_cleanup_lists.load_lists()["no_merge_target_nouns"]))
    # Tokens that always emit as their own tag: never absorbed into an attribute
    # phrase (leading or trailing), and act as a hard phrase-assembly boundary
    # so e.g. "teen first timer" splits at "teen" instead of gluing across it.
    # Hair-color words live here rather than in attribute_lexicon: unlike an
    # object descriptor (e.g. "massive black boulder", where stacking onto the
    # following noun is correct), a hair color describes the performer and
    # shouldn't drag in whatever unrelated word happens to follow it (e.g.
    # "redhead deepthroat" merging into one tag).
    always_split: set = field(default_factory=lambda: set(tag_cleanup_lists.load_lists()["always_split"]))
    # Explicit two-word compounds where the second word is a "strong" noun that
    # should keep the pair together (e.g. "first timer") rather than falling
    # through to two standalone tags. Kept as an explicit allowlist rather than
    # generic noun-pair NLP, to stay high-precision.
    compound_noun_pairs: set = field(default_factory=lambda: {
        tuple(pair) for pair in tag_cleanup_lists.load_lists()["compound_noun_pairs"]})
    # \d+ year(s) old -> a single grouped token, with the following adjective
    # (if any) continuing as its own tag rather than being absorbed.
    age_pattern_enabled: bool = True
    # Accumulate a run of consecutive attribute_lexicon tokens before the
    # following noun into one phrase (e.g. "massive black boulder") instead of
    # only ever merging a single leading adjective.
    attribute_stacking_enabled: bool = True
    drop_suspected_truncation: bool = True
    # When True, a block's trailing token is dropped as truncated if wordfreq
    # doesn't recognize it as a real English word (catches clipped fragments
    # like "librar" or "sto" that a length/vowel heuristic can't). When False,
    # falls back to the legacy length/no-vowel heuristic.
    dictionary_truncation_enabled: bool = True
    min_token_len: int = 2
    drop_resolution_like: bool = True
    # Tags that are a single word, or whose full raw text is shorter than this,
    # are never parsed at all - splitting a single word is meaningless, and a
    # short tag is almost always already atomic.
    skip_single_word_tags: bool = True
    min_process_tag_length: int = 35
    batch_size: int = 512
    max_workers: int = 8
    interactive: bool = True
    request_retries: int = 3
    # Optional performer-name gazetteer (see "Performer-name gazetteer" section below).
    # None means name detection is off - parsing behaves exactly as it did before this
    # feature existed.
    performer_gazetteer: Optional["PerformerGazetteer"] = None


NUMBER_PREFIX_RE = re.compile(r"^\d+-")
RESOLUTION_RE = re.compile(r"^\d{2,4}x\d{2,4}$")
AGE_UNIT_TOKENS = {"year", "years"}


def split_camel_case(text: str) -> str:
    for pattern in CASE_BOUNDARIES:
        text = pattern.sub(" ", text)
    return text


def strip_number_prefix(block: str) -> str:
    return NUMBER_PREFIX_RE.sub("", block, count=1)


def looks_truncated_legacy(tok: str, min_token_len: int) -> bool:
    # Heuristic: very short (<=2 char) alpha token with no vowel, at the very
    # end of a block, is very likely a truncated filename remnant (xz, qp, bk).
    # Can't distinguish a real clipped word (e.g. "librar") from a genuine
    # short token, which is why dictionary_truncation_enabled is preferred.
    if len(tok) > 2:
        return False
    if len(tok) < 1:
        return False
    if not tok.isalpha():
        return False
    return not any(c in "aeiou" for c in tok)


def looks_truncated_dictionary(tok: str) -> bool:
    # A single character is never a real trailing tag on its own, regardless of
    # what wordfreq reports for it (single letters score high as pronouns/
    # abbreviations, e.g. "i"). For everything else, the "small" wordlist is a
    # much tighter membership check than "best" (which folds in noisy sources
    # where short strings coincide with real abbreviations) - it correctly
    # zeroes out clipped fragments like "librar" or "sto" that "best" does not.
    if len(tok) <= 1:
        return True
    if not tok.isalpha():
        return False
    return zipf_frequency(tok, "en", wordlist="small") <= 0.0


# ---------------------------------------------------------------------------
# Performer-name gazetteer (read-only here; built by performer_gazetteer.py)
# ---------------------------------------------------------------------------
# Entirely optional/additive: with no cache on disk, cfg.performer_gazetteer
# stays None and parsing behaves exactly as it did before this existed. When
# present, it fixes two problems the plain attribute/content classifier can't:
# a performer surname/given-name that is also an ordinary English word (e.g. a
# common color or virtue used as a stage name) was getting silently absorbed
# into an attribute-adjective merge or dropped by the wordfreq truncation
# check, since neither has any concept of "this is a name". A lone gazetteer
# hit on a single token is deliberately NOT enough to accept it as a name by
# itself - that's exactly what causes false positives on common-word names.
# Only a full multi-word name/alias phrase match, or two ADJACENT tokens that
# actually co-occurred as a first+last name pair in some real scraped
# name/alias (or an initial standing in for the first-name half of one), are
# accepted; everything else falls through to the normal attribute/content
# classifier untouched.
#
# Fetching and building the gazetteer (from ThePornDB/StashDB) lives entirely
# in the sibling script `performer_gazetteer.py` - run it directly to build or
# refresh the cache. This module only ever reads the cache file it writes.

JSON_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "json"
PERFORMER_GAZETTEER_CACHE_FILE = JSON_OUTPUT_DIR / "performer-gazetteer.json"


@dataclass
class PerformerGazetteer:
    full_name_phrases: Set[str]
    # (first_token, last_token) pairs pulled from the endpoints of every real
    # multi-word name/alias performer_gazetteer.py fetched - NOT the cross
    # product of independent first-name/last-name sets. That distinction is
    # the whole point: "grace" and "cruz" can each be a real first/last name
    # without "grace cruz" ever being an actual performer, so the adjacency
    # check below only accepts pairs that were seen together.
    name_pairs: Set[Tuple[str, str]]
    # Single-word stage names/aliases - can't corroborate themselves as a real name the way a
    # full phrase or a name_pairs co-occurrence can, so precision here is deliberately not the
    # goal: many are foreign/invented words that would otherwise fail the dictionary-truncation
    # check and get silently dropped rather than kept, which defeats the point. A hit gets the
    # same full "name" protection as a full-phrase/pair match (never merged into a neighboring
    # attribute/content phrase, never dropped as truncated) - being wrong about which single
    # dictionary word is a name costs nothing here as long as it stays a separate tag.
    single_names: Set[str] = field(default_factory=set)
    # Derived at load time: last_token -> set of first-letters of every first
    # name actually paired with it, for the "j smith" initial-form match.
    initials_by_last: Dict[str, Set[str]] = field(default_factory=dict)
    max_phrase_len: int = 2


def load_performer_gazetteer() -> Optional[PerformerGazetteer]:
    try:
        with open(PERFORMER_GAZETTEER_CACHE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        raw_pairs = payload.get("name_pairs", [])
        name_pairs = {(p[0], p[1]) for p in raw_pairs if isinstance(p, (list, tuple)) and len(p) == 2}
        initials_by_last: Dict[str, Set[str]] = {}
        for first, last in name_pairs:
            if first:
                initials_by_last.setdefault(last, set()).add(first[0])
        return PerformerGazetteer(
            full_name_phrases=set(payload.get("full_name_phrases", [])),
            name_pairs=name_pairs,
            single_names=set(payload.get("single_names", [])),
            initials_by_last=initials_by_last,
            max_phrase_len=payload.get("max_phrase_len", 2),
        )
    except (OSError, ValueError, KeyError):
        return None


def _extract_name_spans(tokens: List[str], gaz: Optional[PerformerGazetteer],
                         cfg: "Config") -> List[Tuple[str, bool]]:
    """Scans left-to-right for gazetteer matches: longest full-name/alias phrase match first
    (2+ words), else an adjacent pair that actually co-occurred as a first+last name in some
    real scraped name/alias - either the literal pair (either order), or an initial standing in
    for the first-name half (e.g. "j smith", either order) - else a lone token that's a known
    single-word stage name/mononym (PerformerGazetteer.single_names). Two tokens that are each
    independently a known first/last name are never enough on their own unless they were
    actually seen together - see PerformerGazetteer.name_pairs; the single_names check is the
    one deliberate exception to "a lone hit is never enough", made because mononym stage names
    are common in this domain and getting the merge-avoidance right matters far more here than
    being right about which specific word is a name (see single_names' own docstring). Falls
    through untouched with no gazetteer loaded.

    Real scraped performer/alias data is noisy - a scene-descriptor alias like "petite teen"
    puts ordinary descriptive words into name_pairs, which would otherwise happily pair up with
    an unrelated neighbor (e.g. "angelic teen" in real data, even though neither is a real name
    here) and defeat cfg.always_split/glue-word handling entirely. Any token that's a reserved
    always_split word, function word, or corpus glue word is never allowed to participate in a
    match, in either role - full-phrase or adjacent-pair/initial-form.

    Attribute-lexicon words (colors, sizes, demographic/scene-descriptor adjectives - see
    Config.attribute_lexicon, e.g. "brunette", "hunk") get that same protection, but ONLY for the
    adjacent-pair/initial-form fallback below: that's the loose, heuristic match (any independently
    known first name next to any independently known last name) that was tearing a
    should-stand-alone descriptor apart by pairing it with an unrelated neighbor. An exact
    full-name-phrase match is a different animal - a literal, specific alias the gazetteer already
    has on file - so a real performer name that happens to contain a color/attribute word (e.g.
    "Keira Blue") must NOT be blocked from matching there; excluding attribute words only from the
    loose fallback keeps both guarantees at once."""
    if not gaz or not tokens:
        return [(t, False) for t in tokens]
    core_protected = cfg.always_split | cfg.function_words | cfg.corpus_glue_words
    protected = core_protected | cfg.attribute_lexicon
    n = len(tokens)
    out: List[Tuple[str, bool]] = []
    i = 0
    while i < n:
        matched = False
        max_span = min(gaz.max_phrase_len, n - i)
        for span in range(max_span, 1, -1):
            span_tokens = tokens[i:i + span]
            if any(t in core_protected for t in span_tokens):
                continue
            phrase = " ".join(span_tokens)
            if phrase in gaz.full_name_phrases:
                out.append((phrase, True))
                i += span
                matched = True
                break
        if matched:
            continue
        if i + 1 < n and tokens[i] not in protected and tokens[i + 1] not in protected:
            a, b = tokens[i], tokens[i + 1]
            is_known_pair = (a, b) in gaz.name_pairs or (b, a) in gaz.name_pairs
            is_initial_form = (
                (len(a) == 1 and a in gaz.initials_by_last.get(b, ())) or
                (len(b) == 1 and b in gaz.initials_by_last.get(a, ())))
            if is_known_pair or is_initial_form:
                out.append((f"{a} {b}", True))
                i += 2
                continue
        if tokens[i] not in protected and tokens[i] in gaz.single_names:
            out.append((tokens[i], True))
            i += 1
            continue
        out.append((tokens[i], False))
        i += 1
    return out


@dataclass
class ParsedTag:
    original: str
    namespace_stripped: str
    tags: List[str]
    dropped: List[str]
    # Ordered (display_text, kind) pairs spanning the whole original tag -
    # namespace, number prefix, block separators, and every individual token
    # tagged with its fate ("attribute", "reserved", "content",
    # "dropped_glue", "dropped_short", "dropped_resolution",
    # "dropped_truncation") - this is the "exploded view" the preview renders.
    exploded: List[Tuple[str, str]] = field(default_factory=list)
    # True when this tag was never parsed at all because it's a single word or
    # shorter than cfg.min_process_tag_length - `tags` is just [namespace_stripped]
    # unchanged, `exploded` is a single "skipped" entry.
    skipped: bool = False


@dataclass
class FilePreview:
    """One real file (or one offline fixture), with every namespaced tag it had
    parsed. Almost always a single entry, but a file can carry more than one
    dir: tag, so entries stays a list rather than collapsing to one ParsedTag."""
    label: str
    entries: List[ParsedTag]


TRAILING_MARKER_RE = re.compile(r"^([A-Za-z][A-Za-z'\-]*)[(\-]\d+\)?$")


def strip_trailing_marker(tok: str) -> str:
    """Strips a filename-artifact suffix like "(1)" or "-1" off a word, e.g.
    "fox(1)" -> "fox". Must run before normalize_token, which would otherwise
    fold the digits into the word itself (producing a bogus "fox1")."""
    m = TRAILING_MARKER_RE.match(tok)
    return m.group(1) if m else tok


def _tokenize_block(block: str, cfg: Config) -> List[str]:
    """Camel-case split, erase configured delimiters (plus "&", not always
    present in a user's delimiter list) to spaces, then per-word cleanup:
    strip a trailing filename artifact, then normalize."""
    block = split_camel_case(block)
    for delim in cfg.delimiters:
        block = block.replace(delim, " ")
    block = block.replace("&", " ")

    tokens: List[str] = []
    for raw in block.split(" "):
        raw = raw.strip()
        if not raw:
            continue
        norm = normalize_token(strip_trailing_marker(raw))
        if norm:
            tokens.append(norm)
    return tokens


def _tokenize_raw_tag(raw_tag: str, cfg: Config) -> Tuple[str, List[List[str]], str, str]:
    """Namespace-strip, block-split, and tokenize a raw tag. Also returns the
    stripped namespace prefix and number prefix verbatim (e.g. "dir:", "12-")
    purely so the exploded-view preview can show them as their own leading
    elements."""
    value = raw_tag
    prefix = f"{cfg.source_namespace}:"
    namespace_text = ""
    if value.startswith(prefix):
        namespace_text = prefix
        value = value[len(prefix):]
    original_value = value

    blocks = value.split(cfg.primary_delimiter)
    number_prefix_text = ""
    if cfg.strip_leading_number_prefix and blocks:
        m = NUMBER_PREFIX_RE.match(blocks[0])
        if m:
            number_prefix_text = m.group(0)
        blocks[0] = strip_number_prefix(blocks[0])

    return original_value, [_tokenize_block(block, cfg) for block in blocks], namespace_text, number_prefix_text


def _classify_token(tok: str, cfg: Config) -> str:
    """One of "reserved", "glue", "attribute", or "content". Checked in this
    order so a token can't be in both `always_split` and `attribute_lexicon`
    (the always_split entries are meant to be removed from attribute_lexicon,
    but this keeps the classifier correct even if a caller forgets to)."""
    if tok in cfg.always_split:
        return "reserved"
    if tok in cfg.function_words or tok in cfg.corpus_glue_words:
        return "glue"
    if tok in cfg.attribute_lexicon:
        return "attribute"
    return "content"


def _process_block(tokens: List[str], categories: List[str], is_last_block: bool,
                    cfg: Config) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """Drops glue/junk, then assembles phrases (reserved-standalone, age
    pattern, compound-noun pairs, attribute stacking + noun merge). Also
    returns a per-token trace, in original order, of (token, kind) for every
    input token - the "kind" is exactly the drop-reason or category decided
    below, used to render the exploded view. This intentionally reflects each
    token's own fate, not the merged phrase it ends up part of, matching the
    one-bracket-per-word exploded layout."""
    n = len(tokens)
    dropped: List[str] = []
    trace: List[Tuple[str, str]] = []

    candidates: List[str] = []
    candidate_idx: List[int] = []
    for i, tok in enumerate(tokens):
        if categories[i] == "name":
            # Already validated against the performer gazetteer - skip the length/glue/
            # resolution/truncation checks below entirely. Names routinely fail the
            # wordfreq truncation check (they're not English dictionary words), which is
            # exactly the bug this category exists to route around.
            candidates.append(tok)
            candidate_idx.append(i)
            trace.append((tok, "name"))
            continue
        if len(tok) < cfg.min_token_len:
            dropped.append(tok)
            trace.append((tok, "dropped_short"))
            continue
        if categories[i] == "glue":
            dropped.append(tok)
            trace.append((tok, "dropped_glue"))
            continue
        if cfg.drop_resolution_like and RESOLUTION_RE.match(tok):
            dropped.append(tok)
            trace.append((tok, "dropped_resolution"))
            continue
        is_last_token_overall = is_last_block and i == n - 1
        if cfg.drop_suspected_truncation and is_last_token_overall:
            # A final token of length <=2 is dropped outright, regardless of
            # wordfreq - short abbreviation-shaped fragments ("st", "po", "wa")
            # often coincide with real short words/abbreviations that a
            # dictionary-membership check alone won't catch. Longer fragments
            # still go through the dictionary/legacy check below. A name's own
            # trailing initial never reaches this branch - it was already
            # absorbed into the protected "name" token above.
            if len(tok) <= 2:
                dropped.append(tok)
                trace.append((tok, "dropped_truncation"))
                continue
            is_trunc = (looks_truncated_dictionary(tok) if cfg.dictionary_truncation_enabled
                        else looks_truncated_legacy(tok, cfg.min_token_len))
            if is_trunc:
                dropped.append(tok)
                trace.append((tok, "dropped_truncation"))
                continue
        candidates.append(tok)
        candidate_idx.append(i)
        trace.append((tok, categories[i]))

    kept: List[str] = []
    i = 0
    while i < len(candidates):
        tok = candidates[i]
        orig_idx = candidate_idx[i]
        cat = categories[orig_idx]

        if cat in ("reserved", "name"):
            kept.append(tok)
            i += 1
            continue

        if (cfg.age_pattern_enabled and tok.isdigit() and i + 2 < len(candidates)
                and candidates[i + 1] in AGE_UNIT_TOKENS and candidates[i + 2] == "old"):
            kept.append(f"{tok} {candidates[i + 1]} old")
            i += 3
            continue

        if (i + 1 < len(candidates) and categories[candidate_idx[i + 1]] not in ("reserved", "name")
                and (tok, candidates[i + 1]) in cfg.compound_noun_pairs):
            kept.append(f"{tok} {candidates[i + 1]}")
            i += 2
            continue

        if cat == "attribute":
            run = [tok]
            j = i + 1
            if cfg.attribute_stacking_enabled:
                while j < len(candidates) and categories[candidate_idx[j]] == "attribute":
                    run.append(candidates[j])
                    j += 1
            if (j < len(candidates) and candidates[j] not in cfg.no_merge_target_nouns
                    and categories[candidate_idx[j]] not in ("reserved", "name")):
                run.append(candidates[j])
                kept.append(" ".join(run))
                i = j + 1
                continue
            # No noun to attach to (e.g. followed by a name, a no_merge_target_noun,
            # a reserved word, or nothing) - keep the accumulated attributes standalone.
            kept.extend(run)
            i = j
            continue

        kept.append(tok)
        i += 1

    return kept, dropped, trace


def _should_skip_processing(raw_tag: str, cfg: Config) -> bool:
    """A tag is left completely unparsed when it's a single word, or its full
    raw text is shorter than cfg.min_process_tag_length - splitting either is
    pointless work (see Config.skip_single_word_tags)."""
    if not cfg.skip_single_word_tags:
        return False
    value = raw_tag
    prefix = f"{cfg.source_namespace}:"
    if value.startswith(prefix):
        value = value[len(prefix):]
    if cfg.strip_leading_number_prefix:
        block0, _, rest = value.partition(cfg.primary_delimiter)
        value = strip_number_prefix(block0) + (cfg.primary_delimiter + rest if rest else "")
    word_count = len(value.split())
    return word_count <= 1 or len(raw_tag) < cfg.min_process_tag_length


def parse_filename_tag_batch(raw_tags: List[str], cfg: Config,
                             on_progress=None) -> List[ParsedTag]:
    """Parse a batch of raw namespaced tags. Tags matching _should_skip_processing
    are passed through unchanged, in place, so batch order is preserved."""
    results: List[ParsedTag] = []
    for raw_tag in raw_tags:
        if _should_skip_processing(raw_tag, cfg):
            value = raw_tag
            prefix = f"{cfg.source_namespace}:"
            if value.startswith(prefix):
                value = value[len(prefix):]
            if cfg.strip_leading_number_prefix:
                block0, _, rest = value.partition(cfg.primary_delimiter)
                value = strip_number_prefix(block0) + (cfg.primary_delimiter + rest if rest else "")
            results.append(ParsedTag(
                original=raw_tag, namespace_stripped=value,
                tags=[value] if value else [], dropped=[],
                exploded=[(raw_tag, "skipped")], skipped=True,
            ))
            continue

        original_value, blocks_tokens, namespace_text, number_prefix_text = _tokenize_raw_tag(raw_tag, cfg)
        dropped: List[str] = []
        kept_tags: List[str] = []
        exploded: List[Tuple[str, str]] = []
        if namespace_text:
            exploded.append((namespace_text, "namespace"))
        if number_prefix_text:
            exploded.append((number_prefix_text, "number"))
        for block_idx, tokens in enumerate(blocks_tokens):
            is_last_block = block_idx == len(blocks_tokens) - 1
            if block_idx > 0:
                exploded.append((cfg.primary_delimiter.strip(), "structure"))
            units = _extract_name_spans(tokens, cfg.performer_gazetteer, cfg)
            unit_tokens = [u for u, _ in units]
            categories = ["name" if is_name else _classify_token(u, cfg) for u, is_name in units]
            kept, blk_dropped, blk_trace = _process_block(unit_tokens, categories, is_last_block, cfg)
            kept_tags.extend(kept)
            dropped.extend(blk_dropped)
            exploded.extend(blk_trace)

        seen: Set[str] = set()
        deduped: List[str] = []
        for t in kept_tags:
            if t not in seen:
                seen.add(t)
                deduped.append(t)

        results.append(ParsedTag(original=raw_tag, namespace_stripped=original_value,
                                  tags=deduped, dropped=dropped, exploded=exploded))
        if on_progress and len(results) % 512 == 0:
            on_progress(len(results), len(raw_tags))

    if on_progress:
        on_progress(len(results), len(raw_tags))
    return results


def parse_filename_tag(raw_tag: str, cfg: Config) -> ParsedTag:
    """Parse a single raw namespaced tag (e.g. 'dir:12-sunset hike - ...'). Thin
    wrapper around parse_filename_tag_batch for single-item callers."""
    return parse_filename_tag_batch([raw_tag], cfg)[0]
