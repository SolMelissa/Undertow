"""
Console + log rendering, progress reporting, and user prompts for tag_cleanup_x.

Every line printed to the terminal is also appended to a timestamped log file
with [HH:MM:SS] prefixes. All user I/O happens through Renderer methods.
"""

import getpass
import sys
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple

from rich.console import Console
from rich.text import Text

from tag_cleanup_x_engine import FilePreview, ParsedTag


class Renderer:
    """Console + log tee: every line printed to the terminal is also appended to a
    timestamped log file."""

    def __init__(self, log_path: Path | None = None) -> None:
        self._console = Console()
        self._log_file = None
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log_path, "w", encoding="utf-8")

    def out(self, text: str = "", style: str | None = None) -> None:
        """Print to console (with optional rich style) and append timestamped line to log."""
        self._console.print(text, style=style)
        self._log_line(text)

    def out_rich(self, renderable, plain: str) -> None:
        """Print styled renderable to console, plain text to log."""
        self._console.print(renderable)
        self._log_line(plain)

    def log_only(self, text: str) -> None:
        """Log file only - used for full detail the console truncates."""
        self._log_line(text)

    def error(self, text: str) -> None:
        """Print to stderr and log, prefixed 'ERROR: '."""
        self._console.print(f"ERROR: {text}", style=None, file=sys.stderr)
        self._log_line(f"ERROR: {text}")

    def _log_line(self, text: str) -> None:
        """Append timestamped line to log file."""
        if self._log_file:
            stamp = time.strftime("%H:%M:%S")
            self._log_file.write(f"[{stamp}] {text}\n")
            self._log_file.flush()

    def text(self, label: str, default: Optional[str] = None) -> str:
        """Prompt for text input."""
        suffix = f" [{default}]" if default else ""
        self._log_line(f"{label}{suffix}")
        while True:
            raw = input(f"{label}{suffix}: ").strip()
            if raw:
                self._log_line(f"  → {raw}")
                return raw
            if default is not None:
                self._log_line(f"  → {default} (default)")
                return default
            self.out("  A value is required.")

    def secret(self, label: str, has_saved: bool) -> Optional[str]:
        """Prompt for a secret (password/API key). Never logs the value."""
        hint = " (leave blank to keep the saved key)" if has_saved else ""
        self._log_line(f"{label}{hint}")
        value = getpass.getpass(f"{label}{hint}: ").strip()
        value = "".join(ch for ch in value if ch.isprintable())
        if value:
            self._log_line("  → (api key entered)")
            return value
        else:
            self._log_line("  → (kept saved key)" if has_saved else "  → (not entered)")
            return None

    def integer(self, label: str, default: int, min_value: int = 0) -> int:
        """Prompt for an integer."""
        suffix = f" [{default}]"
        self._log_line(f"{label}{suffix}")
        while True:
            raw = input(f"{label}{suffix}: ").strip()
            if not raw:
                self._log_line(f"  → {default} (default)")
                return default
            if raw.isdigit() and int(raw) >= min_value:
                self._log_line(f"  → {raw}")
                return int(raw)
            self.out(f"  Enter a whole number >= {min_value}.")

    def yes_no(self, label: str, default: bool = True) -> bool:
        """Prompt for yes/no."""
        suffix = " [Y/n]" if default else " [y/N]"
        self._log_line(f"{label}{suffix}")
        raw = input(f"{label}{suffix}: ").strip().lower()
        result = default if not raw else raw.startswith("y")
        self._log_line(f"  → {'Yes' if result else 'No'}")
        return result

    def choice(self, label: str, options: List[Tuple[str, str]], default_key: Optional[str] = None) -> str:
        """Prompt for a choice from a list of (display_text, value) tuples."""
        self.out(f"\n{label}")
        default_idx = 1
        for idx, (display, value) in enumerate(options, start=1):
            marker = " (saved default)" if value == default_key else ""
            self.out(f"  {idx}. {display}{marker}")
            if value == default_key:
                default_idx = idx
        while True:
            self._log_line(f"Choose 1-{len(options)} [{default_idx}]")
            raw = input(f"Choose 1-{len(options)} [{default_idx}]: ").strip()
            if not raw:
                chosen_value = options[default_idx - 1][1]
                self._log_line(f"  → {default_idx} (default)")
                return chosen_value
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                chosen_value = options[int(raw) - 1][1]
                self._log_line(f"  → {raw}")
                return chosen_value
            self.out("  Not a valid choice, try again.")

    def close(self) -> None:
        """Close the log file."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None


class ProgressReporter:
    """Prints a single overwritten status line, throttled to at most once every
    5 seconds, so a long-running fetch/apply loop never looks hung. Also logs
    progress to the renderer with a 5-second cadence."""

    def __init__(self, renderer: Renderer, label: str, total: int, min_interval: float = 5.0):
        self.renderer = renderer
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
        status = (
            f"{self.label}: {done:,}/{self.total:,} ({pct:4.1f}%)  "
            f"{rate:,.0f}/s  elapsed {elapsed:6.0f}s  eta {eta:6.0f}s"
        )
        sys.stdout.write(f"\r{status}   ")
        sys.stdout.flush()
        self.renderer.log_only(status)

    def done(self) -> None:
        self.update(self.total, force=True)
        sys.stdout.write("\n")
        sys.stdout.flush()


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
