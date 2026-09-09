"""
Tag-cleanup-specific preview rendering for tag_cleanup_x: the colored exploded-tag-view
breakdown and the tag-centered preview table. Generic console I/O (Renderer, ProgressReporter)
now lives in the reusable `console` module - this file only ever imports Renderer from there,
it doesn't define console primitives itself.
"""

import sys
from pathlib import Path
from typing import List, Set, Tuple

from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent / "modules"))
from console import Renderer

from tag_cleanup_x_engine import FilePreview, ParsedTag


# Above this many tags, the full exploded-view breakdown is written to a log file
# instead of the terminal, and only the first 10 tags plus the summary line are
# printed - scrolling tens of thousands of lines is neither readable nor useful.
PREVIEW_INLINE_LIMIT = 40

# kind -> rich style, for the colored terminal exploded view. Every element of
# ParsedTag.exploded is rendered as one bracketed "[text]" chip in this style,
# so the sequence reads as an exploded view of exactly what happened to the
# tag, word by word: [dir:][12-][DROP:watches][tag][tag]...
KIND_STYLES_RICH = {
    "namespace": "bold cyan",
    "number": "dim white",
    "dropped_glue": "strike dim",
    "dropped_short": "strike dim",
    "dropped_resolution": "strike dim",
    "dropped_truncation": "strike bold red",
    "reserved": "bold yellow",
    "attribute": "cyan",
    "content": "green",
    "name": "bold magenta",
    "skipped": "italic dim",
}

# Short plain-text label used inside the bracket for non-obvious kinds, for the
# log-file fallback (no color available there).
KIND_PLAIN_LABEL = {
    "namespace": "NS",
    "number": "NUM",
    "dropped_glue": "DROP",
    "dropped_short": "DROP",
    "dropped_resolution": "DROP",
    "dropped_truncation": "DROP",
    "reserved": "RES",
    "attribute": "ATTR",
    "name": "NAME",
    "skipped": "SKIP",
}


def render_exploded_rich(exploded: List[Tuple[str, str]]) -> Text:
    t = Text()
    first = True
    for text, kind in exploded:
        if not first:
            t.append(" ")
        first = False
        if kind == "structure":
            t.append(text, style="dim")
            continue
        t.append(f"[{text}]", style=KIND_STYLES_RICH.get(kind, ""))
    return t


def render_exploded_plain(exploded: List[Tuple[str, str]]) -> str:
    parts: List[str] = []
    for text, kind in exploded:
        if kind == "structure":
            parts.append(text)
            continue
        label = KIND_PLAIN_LABEL.get(kind)
        parts.append(f"[{label}:{text}]" if label else f"[{text}]")
    return " ".join(parts)


def _detected_names(entry: ParsedTag) -> List[str]:
    """Unique gazetteer name matches found in this tag, in first-seen order -
    the "name" kind entries in `exploded` (excludes single-word non-matches;
    every "name" entry is already a validated multi-word phrase/pair - see
    _extract_name_spans)."""
    seen: Set[str] = set()
    names: List[str] = []
    for text, kind in entry.exploded:
        if kind == "name" and text not in seen:
            seen.add(text)
            names.append(text)
    return names


def _render_tag_section(renderer: Renderer, idx: int, total: int, label: str, entry: ParsedTag) -> None:
    """Prints one tag-centered section: the full original tag, the name(s) the
    gazetteer detected (if any), then its exploded view (colored/struck/bold to
    show what the parser did to it), then the OUT/DROPPED summary lines."""
    renderer.out(f"===== Tag {idx}/{total} - {label} =====", style="bold")
    if entry.skipped:
        renderer.out(f"  TAG: {entry.original}")
        renderer.out("  (skipped - single word or shorter than the min-process-length threshold)",
                     style="italic dim")
        renderer.out()
        return
    names = _detected_names(entry)
    renderer.out(f"  NAME(S) DETECTED: {', '.join(names) if names else '(none)'}", style="bold magenta")
    renderer.out(f"  TAG: {entry.original}")
    renderer.out_rich(render_exploded_rich(entry.exploded),
                     f"  {render_exploded_plain(entry.exploded)}")
    out_str = ", ".join(entry.tags) if entry.tags else "(nothing kept)"
    dropped_str = ", ".join(entry.dropped) if entry.dropped else "-"
    renderer.out(f"  OUT:     {out_str}")
    renderer.out(f"  DROPPED: {dropped_str}")
    renderer.out()


def _render_tag_section_plain(idx: int, total: int, label: str, entry: ParsedTag) -> List[str]:
    lines = [f"===== Tag {idx}/{total} - {label} ====="]
    if entry.skipped:
        lines.append(f"  TAG: {entry.original}")
        lines.append("  (skipped - single word or shorter than the min-process-length threshold)")
        lines.append("")
        return lines
    names = _detected_names(entry)
    lines.append(f"  NAME(S) DETECTED: {', '.join(names) if names else '(none)'}")
    lines.append(f"  TAG: {entry.original}")
    lines.append(f"  {render_exploded_plain(entry.exploded)}")
    out_str = ", ".join(entry.tags) if entry.tags else "(nothing kept)"
    dropped_str = ", ".join(entry.dropped) if entry.dropped else "-"
    lines.append(f"  OUT:     {out_str}")
    lines.append(f"  DROPPED: {dropped_str}")
    lines.append("")
    return lines


def print_preview_table(previews: List[FilePreview], renderer: Renderer) -> None:
    """Tag-centered preview: every raw namespaced tag gets its own section
    (rather than grouping sections by file), headed by the full tag and an
    exploded, color-coded breakdown of exactly how the parser split it. Skipped
    tags (single-word/too-short - see Config.skip_single_word_tags) are left
    out of the report entirely: they were never parsed, so there's nothing to
    show about them, only a count in the summary line."""
    all_flat: List[Tuple[str, ParsedTag]] = [(fp.label, e) for fp in previews for e in fp.entries]
    flat = [(label, e) for label, e in all_flat if not e.skipped]
    total_skipped = len(all_flat) - len(flat)
    if not flat:
        renderer.out("(nothing to preview)" if not total_skipped else
                    f"(nothing to preview - all {total_skipped} matched tag(s) were skipped as single-word/short)")
        return

    inline = len(flat) <= PREVIEW_INLINE_LIMIT
    shown = flat if inline else flat[:10]

    for idx, (label, entry) in enumerate(shown, start=1):
        _render_tag_section(renderer, idx, len(flat), label, entry)

    if not inline:
        renderer.out(f"... {len(flat) - len(shown)} more tag(s) not shown here ...")

    if not inline:
        for idx, (label, entry) in enumerate(flat, start=1):
            for line in _render_tag_section_plain(idx, len(flat), label, entry):
                renderer.log_only(line)

    total_kept = sum(len(e.tags) for _, e in flat)
    total_dropped = sum(len(e.dropped) for _, e in flat)
    renderer.out(f"Summary: {len(previews)} file(s), {len(flat)} tag(s) processed "
                f"({total_skipped} more skipped as single-word/short, not shown above), "
                f"{total_kept} tag(s) kept, {total_dropped} token(s) dropped.")
