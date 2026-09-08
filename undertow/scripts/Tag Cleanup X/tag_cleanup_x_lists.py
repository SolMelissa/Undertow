"""
Editable word/filter lists for tag_cleanup.py's wizard, split out into their own
dependency-free module (json + pathlib only) so webui.py can read/write them for its
"Tag Cleanup Lists" editor without importing tag_cleanup.py itself, which hard-requires
requests/wordfreq/rich at import time (and sys.exit(1)s if they're missing) - not something
the main dashboard process should risk pulling in just to render a settings form.

tag_cleanup.py's Config dataclass sources these same fields from load_lists() at
construction time, so this file is the single source of truth for both the wizard's
actual parsing behavior and the webui's editor.

The on-disk file only ever stores what the user has customized (see save_lists), but
load_lists() always seeds it with DEFAULT_LISTS on first read so the editor immediately
shows something editable instead of an empty form. compound_noun_pairs is stored as a list
of two-item [a, b] lists (JSON has no tuple type); everything else is a list of strings.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List


def _default_lists_config_path() -> Path:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from undertow import config as _undertow_config  # type: ignore
        return _undertow_config.DATA_DIR / "tag-cleanup-lists.json"
    except Exception:
        root = Path(os.environ.get("USERPROFILE", str(Path.home())))
        return root / "HydrusPipeline" / "hydownloader-data" / "tag-cleanup-lists.json"


LISTS_CONFIG_FILE = _default_lists_config_path()

# Mirrors the hardcoded defaults tag_cleanup.Config used before this file existed - kept
# here (not re-derived from Config) since Config now reads its defaults from this module.
DEFAULT_LISTS: Dict[str, List] = {
    "function_words": sorted([
        "a", "an", "the", "of", "and", "with", "in", "on", "for", "to", "at", "by", "from",
        "after", "before", "up", "down", "into", "onto", "over", "under", "through",
        "she", "he", "it", "they", "we", "her", "him", "them", "my", "your", "their",
        "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does",
        "did", "can", "could", "will", "would", "shall", "should", "may", "might",
    ]),
    "corpus_glue_words": sorted([
        "watches", "records", "picks", "pulls", "drifts", "rolls", "rains", "soaks",
        "reads", "spots", "parts", "gives", "takes", "gets", "after", "then",
        "rise", "delivers", "pours", "taps", "blows", "off", "them", "near", "during",
        "while", "across", "as",
        "tries", "poses", "shows", "strips", "rocks", "flaunt", "flaunts", "spreads",
        "spreading", "plays", "joins", "enjoys", "rides", "teases", "wears", "grabs",
        "loves", "wants", "rubs", "kisses", "strolls", "walks", "lingers", "sprints",
        "climbs", "sits", "stands", "rests", "relax", "relaxes", "leans",
    ]),
    "attribute_lexicon": sorted([
        "red", "green", "blue", "gray", "grey", "brown", "white", "black", "pale", "tan",
        "small", "big", "large", "massive", "tall", "short", "long", "thin", "slim", "thick",
        "wide", "young", "old", "quiet", "rustic", "fresh", "steep", "cool", "warm",
        "soft", "rare", "full", "wet", "dry", "clean", "bright", "dark",
        "petite", "ebony", "american", "latina", "asian", "sexy", "hot", "chick",
        "cougar", "milf", "curvy", "busty", "naughty",
        "amateur", "real", "wild", "kinky", "angelic", "babe", "babes",
    ]),
    "no_merge_target_nouns": sorted(["couple", "group", "pair", "family"]),
    "always_split": sorted(["teen", "brunette", "redhead", "blonde", "ginger"]),
    "compound_noun_pairs": [["first", "timer"]],
}

# (key, title, help) for every editable list, in the order the editor should show them.
LIST_FIELDS = [
    ("function_words", "Function words",
     "Small grammatical words dropped as glue (a, the, of, with, ...)."),
    ("corpus_glue_words", "Scene-description glue words",
     "Verbs/connectors from bulk-import filenames dropped as glue (watches, poses, rides, ...)."),
    ("attribute_lexicon", "Attribute words",
     "Neutral descriptive adjectives (colors, sizes, ages, qualities) that merge into the following noun."),
    ("no_merge_target_nouns", "Group nouns (never merge target)",
     "Nouns that stay split from a preceding attribute instead of absorbing it (couple, group, ...)."),
    ("always_split", "Always-standalone words",
     "Words that always emit as their own tag and never merge into a neighboring phrase (teen, blonde, ...)."),
]

# compound_noun_pairs is edited separately (each entry is a pair, not a single word).
COMPOUND_PAIRS_FIELD = ("compound_noun_pairs", "Compound noun pairs",
                         "Two-word phrases kept together instead of splitting into separate tags (first timer, ...).")


def load_lists() -> Dict[str, List]:
    """Returns every editable list, with DEFAULT_LISTS values for anything the on-disk file
    doesn't override. Seeds the file with the full defaults on first read, if missing."""
    try:
        with open(LISTS_CONFIG_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
        save_lists(dict(DEFAULT_LISTS))

    out: Dict[str, List] = {}
    for key, default_value in DEFAULT_LISTS.items():
        value = stored.get(key)
        out[key] = value if isinstance(value, list) else list(default_value)
    return out


def save_lists(lists: Dict[str, List]) -> None:
    LISTS_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: lists[key] for key in DEFAULT_LISTS if key in lists}
    with open(LISTS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
