"""
Generic console + log tee and progress reporting - no tag-cleanup-specific logic.

Every line printed to the terminal via Renderer is also appended to a timestamped log file
with [HH:MM:SS] prefixes. Any script in this folder that needs terminal output, user prompts,
or a throttled progress line should use Renderer/ProgressReporter from here rather than
printing directly, so behavior (and the log-file tee) stays consistent across scripts.
"""

import getpass
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Console


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
        """Prompt for a secret (password/API key). Never logs the value.

        On Windows, getpass.getpass() reads directly from the console (CONIN$) via msvcrt,
        bypassing stdin entirely - when this process is a piped subprocess with no real
        console attached (e.g. launched from the webui's Scripts tab), that read can never
        be satisfied and just hangs forever. Fall back to a plain (echoed) input() whenever
        stdin isn't an interactive tty, since there's no real terminal to mask input on
        anyway in that case."""
        hint = " (leave blank to keep the saved key)" if has_saved else ""
        self._log_line(f"{label}{hint}")
        if sys.stdin.isatty():
            value = getpass.getpass(f"{label}{hint}: ").strip()
        else:
            value = input(f"{label}{hint}: ").strip()
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
