"""
Subprocess lifecycle + HTTP client for TagRank's headless API (see
F:\\0DocsF\\0Docs\\AI\\Claude\\tagrank\\docs\\api.md). Undertow launches TagRank's own
`python main.py --serve` as a localhost-only subprocess the first time the TagRank tab is
opened, and stops it when Undertow shuts down - it's never run as a persistent daemon of its
own. Every request/response shape here mirrors the API doc's JSON exactly; nothing is
duplicated from TagRank's own source beyond what that doc already promises as stable.
"""

from __future__ import annotations

import atexit
import subprocess
import threading
import time

import requests

from . import config

try:
    import win32gui
    HAVE_WIN32 = True
except ImportError:
    HAVE_WIN32 = False

_state: dict = {"proc": None}
_lock = threading.Lock()


def is_available() -> bool:
    """Whether TagRank is even checked out on this machine - the tab degrades to a friendly
    'not installed' message instead of erroring when this is False."""
    return config.find_tagrank_main() is not None


def is_server_running() -> bool:
    try:
        requests.get(f"{config.TAGRANK_API_URL}/tags", timeout=1.5)
        return True
    except requests.RequestException:
        return False


def ensure_server_running() -> tuple[bool, str | None]:
    """Starts the TagRank API subprocess if it isn't already answering. Safe to call on every
    tab open - a live server short-circuits via is_server_running() with no extra process
    spawned."""
    if is_server_running():
        return True, None

    with _lock:
        if is_server_running():
            return True, None

        proc = _state.get("proc")
        if proc is not None and proc.poll() is None:
            # Already starting from a previous call - just wait for it below.
            pass
        else:
            main_py = config.find_tagrank_main()
            if main_py is None:
                return False, "TagRank isn't checked out at the configured path."
            python_exe = config.find_tagrank_python() or "python"
            try:
                proc = subprocess.Popen(
                    [str(python_exe), str(main_py), "--serve", "--port", str(config.TAGRANK_PORT)],
                    cwd=str(config.TAGRANK_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except OSError as e:
                return False, f"Couldn't launch TagRank: {e}"
            _state["proc"] = proc

        # Poll for the server to come up rather than assuming a fixed sleep - startup time
        # varies with how much rating history it has to load.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if is_server_running():
                return True, None
            if proc.poll() is not None:
                return False, "TagRank process exited before its API came up."
            time.sleep(0.3)
        return False, "TagRank didn't respond within 20s of starting."


def launch_gui(tag: str | None = None) -> tuple[bool, str | None]:
    """Opens TagRank's real PySide6 comparison window (`python main.py [--tag <tag>]`) in its
    own console, same fresh-window pattern as webui.py's launch_tui(). This is the *actual*
    judging UI - the in-browser pill picker only drives the read-only /search-options and
    /history/graphs endpoints, not a live comparison session. Passing --tag skips TagRank's own
    interactive numbered search-picker prompt (a small addition to tagrank/main.py +
    tagrank/tagrank/app.py made specifically for this) so the window comes up already searching
    the tag whose pill was clicked, instead of TagRank's generic start screen."""
    main_py = config.find_tagrank_main()
    if main_py is None:
        return False, "TagRank isn't checked out at the configured path."
    python_exe = config.find_tagrank_python() or "python"
    args = [str(python_exe), str(main_py)]
    if tag:
        args += ["--tag", tag]
    try:
        subprocess.Popen(
            args,
            cwd=str(config.TAGRANK_DIR),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except OSError as e:
        return False, str(e)
    return True, None


# Set right when Window (tagrank/ui/window.py) is constructed, after pool-building has already
# finished - so "does a window with this title prefix exist" is a reliable "pool is ready and
# the comparison window is up" signal, distinct from the console window that appears the
# instant the subprocess starts (same pid, but Popen's CREATE_NEW_CONSOLE window title is the
# command line, never this).
_READY_WINDOW_TITLE_PREFIX = "TagRank - Comparisons"


def is_gui_ready() -> bool:
    """Whether TagRank's comparison window has appeared yet - polled by the webui's loading
    screen between launch_gui() and the window actually being up, since pool-building (a live
    Hydrus similarity search) can take anywhere from under a second to tens of seconds."""
    if not HAVE_WIN32:
        return True  # can't detect readiness - don't block the UI on a check that can't work
    found = []

    def _enum(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if win32gui.IsWindowVisible(hwnd) and title.startswith(_READY_WINDOW_TITLE_PREFIX):
            found.append(hwnd)
            return False
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception:
        # EnumWindows raises when the callback returns False to stop early (that's how a match
        # short-circuits the scan) - the match itself already landed in `found` before that, so
        # this is the expected path on success, not a real failure. See services.py's
        # show_process_window() for the same catch-and-ignore-then-check-the-list shape.
        pass
    return bool(found)


def stop_server() -> None:
    """Prefer POST /shutdown so an active session gets a chance to be ended first (per the API
    doc) - only kill the process outright if that request itself fails to land."""
    with _lock:
        proc = _state.get("proc")
        if proc is None or proc.poll() is not None:
            _state["proc"] = None
            return
        try:
            requests.post(f"{config.TAGRANK_API_URL}/shutdown", timeout=3)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _state["proc"] = None


atexit.register(stop_server)


# --------------------------------------------------------------------------------------
# Thin HTTP wrappers - every call returns (data, error) so webui.py routes can render a
# friendly message instead of a stack trace on the (rare, since this is all localhost)
# failure case.
# --------------------------------------------------------------------------------------

def _get(path: str, **kwargs) -> tuple[dict | None, str | None]:
    try:
        resp = requests.get(f"{config.TAGRANK_API_URL}{path}", timeout=kwargs.pop("timeout", 10), **kwargs)
    except requests.RequestException as e:
        return None, str(e)
    return _unwrap(resp)


def _post(path: str, json_body: dict | None = None, **kwargs) -> tuple[dict | None, str | None]:
    try:
        resp = requests.post(f"{config.TAGRANK_API_URL}{path}", json=json_body, timeout=kwargs.pop("timeout", 10), **kwargs)
    except requests.RequestException as e:
        return None, str(e)
    return _unwrap(resp)


def _delete(path: str, **kwargs) -> tuple[dict | None, str | None]:
    try:
        resp = requests.delete(f"{config.TAGRANK_API_URL}{path}", timeout=kwargs.pop("timeout", 10), **kwargs)
    except requests.RequestException as e:
        return None, str(e)
    return _unwrap(resp)


def _unwrap(resp: requests.Response) -> tuple[dict | None, str | None]:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", {})
            return None, detail.get("message") or str(detail)
        except Exception:
            return None, f"HTTP {resp.status_code}"
    if not resp.content:
        return {}, None
    return resp.json(), None


def get_search_options() -> tuple[dict | None, str | None]:
    """{"top": [...], "random": [...], "bottom": [...]} of {index, tag, score, file_count}.
    Computes a live per-tag Hydrus file count for every candidate tag server-side, so this can
    take several seconds on a large tag set - well past the default 10s wrapper timeout."""
    return _get("/search-options", timeout=45)


def get_tags() -> tuple[list | None, str | None]:
    return _get("/tags")


def get_graphs() -> tuple[list | None, str | None]:
    """[{"title", "png_base64"}, ...] - the four summary charts. Rendering matplotlib figures
    to PNG server-side is slower than a plain JSON route, hence the longer timeout."""
    return _get("/history/graphs", timeout=30)


def start_session(query: list[str], pool_size: int | None = None) -> tuple[dict | None, str | None]:
    body: dict = {"query": query}
    if pool_size is not None:
        body["pool_size"] = pool_size
    return _post("/sessions", json_body=body)


def get_job(job_id: str) -> tuple[dict | None, str | None]:
    return _get(f"/sessions/{job_id}")


def next_pair(session_id: str) -> tuple[dict | None, str | None]:
    return _get(f"/sessions/{session_id}/next-pair")


def submit_result(session_id: str, choice: str) -> tuple[dict | None, str | None]:
    return _post(f"/sessions/{session_id}/result", json_body={"choice": choice})


def undo(session_id: str) -> tuple[dict | None, str | None]:
    return _post(f"/sessions/{session_id}/undo")


def end_session(session_id: str) -> tuple[dict | None, str | None]:
    """Persists the session's judgments to disk - always call this on every exit path
    (finishing, cancelling, or erroring out of a session), never just abandon a session id."""
    return _delete(f"/sessions/{session_id}")
