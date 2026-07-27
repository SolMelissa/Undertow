"""
Custom widget subclasses for the Textual TUI.

ClipboardInput fixes ctrl+v paste. Textual's own Input.action_paste() only reads
self.app.clipboard - Textual's *internal* in-app clipboard, which is only ever populated by
copying/cutting text from inside a Textual widget (ctrl+c/ctrl+x on a selection). It does not
read the real OS clipboard. Combined with the fact that Textual enables mouse tracking (for
click support), which Windows' console API treats as mutually exclusive with QuickEdit Mode -
so right-click-to-paste stops working too the moment this app is running - there was
effectively no way to get a URL copied from a browser into any Input box here. ClipboardInput
overrides ctrl+v to pull straight from the real Windows clipboard via pywin32 instead, with a
fallback to Textual's own clipboard if that ever fails (e.g. nothing on the clipboard yet).
"""

from __future__ import annotations

from textual.widgets import Input as _Input

try:
    import win32clipboard
    import win32con
except ImportError:  # pragma: no cover - pywin32 is a hard requirement on Windows (see
    # requirements.txt); this is just a safety net so a missing/broken install degrades to
    # Textual's own clipboard instead of crashing every Input on the screen.
    win32clipboard = None
    win32con = None


def read_system_clipboard_text() -> str | None:
    """Best-effort read of the real OS clipboard as text. Returns None if there's no text on
    the clipboard, the clipboard couldn't be opened (another app briefly holding it is normal
    and not an error), or pywin32 isn't available."""
    if win32clipboard is None:
        return None
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
            raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        return None
    except Exception:
        return None
    finally:
        win32clipboard.CloseClipboard()


class ClipboardInput(_Input):
    """Drop-in replacement for textual.widgets.Input whose ctrl+v reads the real OS clipboard
    first, falling back to Textual's in-app clipboard so it still behaves normally for text
    copied from elsewhere in the app."""

    def action_paste(self) -> None:
        text = read_system_clipboard_text() or self.app.clipboard
        if not text:
            return
        # Input is single-line - mirror Textual's own bracketed-paste handling (_on_paste),
        # which also only takes the first line rather than silently dropping the rest.
        line = text.splitlines()[0] if text else text
        start, end = self.selection
        self.replace(line, start, end)
