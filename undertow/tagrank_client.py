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

# Tracks the most recently launched comparison-GUI subprocess (separate from _state["proc"],
# which is the headless API server). launch_gui() used to be pure fire-and-forget - no handle
# kept anywhere - so every tag-pill click spawned another PySide6 process on top of whatever
# was already running, each eagerly rebuilding its own Hydrus tag/similarity index. Closing the
# window normally lets the process exit on its own; this is only for the case where a new
# launch supersedes an old, still-running one, and for reaping on Undertow's own shutdown.
_gui_state: dict = {"proc": None}


def is_available() -> bool:
    """Whether TagRank is even checked out on this machine - the tab degrades to a friendly
    'not installed' message instead of erroring when this is False."""
    return config.find_tagrank_main() is not None


def is_server_running() -> bool:
    """Probes /health, not a data route - GET /tags does real work scaled to the rated-tag
    history (measured 1.0-1.7s live on this library), so it was racing this check's old 1.5s
    timeout and intermittently reading a live server as down, which made Undertow spawn a
    second subprocess on top of the first (doomed to fail its own port bind) - the exact
    "TagRank didn't respond" failure this was supposed to detect, not avoid."""
    try:
        requests.get(f"{config.TAGRANK_API_URL}/health", timeout=5)
        return True
    except requests.RequestException:
        return False


def _start_process() -> tuple[subprocess.Popen | None, str | None]:
    """Spawns the TagRank API subprocess if one isn't already starting/running under our own
    tracking, and returns it (or the existing one) without waiting for it to answer. Caller
    must hold _lock."""
    proc = _state.get("proc")
    if proc is not None and proc.poll() is None:
        return proc, None  # already starting from a previous call

    main_py = config.find_tagrank_main()
    if main_py is None:
        return None, "TagRank isn't checked out at the configured path."
    python_exe = config.find_tagrank_python() or "python"
    try:
        config.TAGRANK_SERVER_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        out = open(config.TAGRANK_SERVER_STDOUT_LOG, "w", encoding="utf-8")
        err = open(config.TAGRANK_SERVER_STDERR_LOG, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [str(python_exe), str(main_py), "--serve", "--port", str(config.TAGRANK_PORT)],
                cwd=str(config.TAGRANK_DIR),
                stdout=out,
                stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        finally:
            out.close()
            err.close()
    except OSError as e:
        return None, f"Couldn't launch TagRank: {e}"
    _state["proc"] = proc
    return proc, None


def read_server_log(max_lines: int = 40) -> str:
    """Tails the captured stdout+stderr of the most recent API-server launch - shown alongside
    a "didn't come up in time" error so a real startup failure (missing dependency, port
    already bound, an unhandled exception) is visible instead of indistinguishable from a slow
    but healthy startup. Best-effort - returns "" if nothing's been captured yet."""
    lines: list[str] = []
    for path in (config.TAGRANK_SERVER_STDOUT_LOG, config.TAGRANK_SERVER_STDERR_LOG):
        try:
            lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    return "\n".join(lines[-max_lines:])


# Measured live: a modest rating history alone can take ~15-20s just to answer its first
# request, and startup time only grows with more history to load - so this needs real
# headroom, not just enough for the happy path. The original 20s deadline here was flush
# against *normal* startup, not just slow ones - that's what surfaced to the user as
# "Did not respond within 20 seconds" on every tab open.
#
# TagRank's server now builds its whole tag/file index (tagrank/tag_index.py) eagerly on
# startup, before it answers even /health - the per-request Hydrus round trips that used to be
# spread across every /search-options call (which themselves needed up to 180s on a real
# library, see tagrank_client.get_search_options's docstring) now all happen once, here, up
# front. 75s was already generous for the old per-request cost alone; this needs more.
STARTUP_DEADLINE_SECONDS = 240


def start_server_async() -> str | None:
    """Kicks off the subprocess (if one isn't already starting/running) without blocking for
    it to answer - used by the web UI so a tab open can show a polling "starting..." screen
    instead of freezing the request thread for up to STARTUP_DEADLINE_SECONDS. Returns an
    error string only if the subprocess itself failed to spawn."""
    if is_server_running():
        return None
    with _lock:
        if is_server_running():
            return None
        _proc, err = _start_process()
        return err


def is_server_starting() -> bool:
    """True if we've spawned a subprocess that hasn't answered yet and hasn't exited - lets
    the web UI's poll loop distinguish "still booting" from "never started / crashed"."""
    proc = _state.get("proc")
    return proc is not None and proc.poll() is None and not is_server_running()


def ensure_server_running() -> tuple[bool, str | None]:
    """Starts the TagRank API subprocess if it isn't already answering, and blocks (up to
    STARTUP_DEADLINE_SECONDS) until it does. Safe to call on every tab open - a live server
    short-circuits via is_server_running() with no extra process spawned. Prefer
    start_server_async() + is_server_running() polling for anything driven by a web request,
    so a slow startup doesn't tie up the request thread."""
    if is_server_running():
        return True, None

    with _lock:
        if is_server_running():
            return True, None

        proc, err = _start_process()
        if proc is None:
            return False, err

        # Poll for the server to come up rather than assuming a fixed sleep.
        deadline = time.monotonic() + STARTUP_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            if is_server_running():
                return True, None
            if proc.poll() is not None:
                return False, "TagRank process exited before its API came up."
            time.sleep(0.3)
        return False, f"TagRank didn't respond within {STARTUP_DEADLINE_SECONDS}s of starting."


def launch_gui(tag: str | None = None, use_similarity: bool = False) -> tuple[bool, str | None]:
    """Opens TagRank's real PySide6 comparison window (`python main.py [--tag <tag>]
    [--no-similarity]`). This is the *actual* judging UI - the in-browser pill picker only
    drives the read-only /search-options and /history/graphs endpoints, not a live comparison
    session. Passing --tag skips TagRank's own interactive numbered search-picker prompt (a
    small addition to tagrank/main.py + tagrank/tagrank/app.py made specifically for this) so
    the window comes up already searching the tag whose pill was clicked, instead of TagRank's
    generic start screen.

    use_similarity defaults to False (--no-similarity) because TagRank's visual-similarity pool
    expansion (a Hydrus distance-search per seed hash) is the slow part of a launch - the
    filter bar's Similarity toggle lets the user opt back into it when they specifically want
    visually-similar neighbors in the pool rather than a plain tag search.

    Runs hidden (CREATE_NO_WINDOW) with stdout/stderr captured to
    config.TAGRANK_LAUNCH_STDOUT_LOG/STDERR_LOG rather than the previous CREATE_NEW_CONSOLE,
    which flashed a separate, untitled console window on every launch. The webui's loading
    screen (tagrank_starting.html) tails that log instead via read_launch_log() so the same
    output is still visible, just inline in Undertow rather than as its own window."""
    main_py = config.find_tagrank_main()
    if main_py is None:
        return False, "TagRank isn't checked out at the configured path."
    python_exe = config.find_tagrank_python() or "python"
    args = [str(python_exe), str(main_py)]
    if tag:
        args += ["--tag", tag]
    if not use_similarity:
        args.append("--no-similarity")

    # A new launch supersedes any previous comparison window still running rather than piling
    # on top of it - without this, repeated tag-pill clicks (or a retry after a slow load) each
    # left the prior PySide6 process (and its already-built Hydrus index) running untracked.
    _kill_gui_proc()

    try:
        config.TAGRANK_LAUNCH_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        out = open(config.TAGRANK_LAUNCH_STDOUT_LOG, "w", encoding="utf-8")
        err = open(config.TAGRANK_LAUNCH_STDERR_LOG, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(config.TAGRANK_DIR),
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=out,
                stderr=err,
            )
        finally:
            out.close()
            err.close()
    except OSError as e:
        return False, str(e)
    _gui_state["proc"] = proc
    return True, None


def _kill_gui_proc() -> None:
    """Best-effort termination of a previously launched comparison-GUI subprocess, if it's
    still alive. Tries a graceful terminate() first (PySide6/Qt handles WM_CLOSE-equivalent
    signals reasonably), then kill() if it hasn't exited shortly after."""
    proc = _gui_state.get("proc")
    if proc is None or proc.poll() is not None:
        _gui_state["proc"] = None
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    _gui_state["proc"] = None


def read_launch_log(max_lines: int = 40) -> str:
    """Tails the captured stdout+stderr of the most recent launch_gui() subprocess, for the
    inline "console" shown on the webui's loading screen while TagRank's comparison window is
    still coming up. Best-effort - returns "" if nothing's been captured yet."""
    lines: list[str] = []
    for path in (config.TAGRANK_LAUNCH_STDOUT_LOG, config.TAGRANK_LAUNCH_STDERR_LOG):
        try:
            lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    return "\n".join(lines[-max_lines:])


# Set right when Window (tagrank/ui/window.py) is constructed, after pool-building has already
# finished - so "does a window with this title prefix exist" is a reliable "pool is ready and
# the comparison window is up" signal.
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
atexit.register(_kill_gui_proc)


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


def _patch(path: str, json_body: dict | None = None, **kwargs) -> tuple[dict | None, str | None]:
    try:
        resp = requests.patch(f"{config.TAGRANK_API_URL}{path}", json=json_body, timeout=kwargs.pop("timeout", 10), **kwargs)
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
    Computes a live per-tag Hydrus file count for every candidate tag server-side - on a large
    library with a large CANDIDATE_SEED_COUNT this is a lot more than "several seconds"
    (confirmed live: 45s wasn't enough and produced a read-timeout error in the TagRank tab),
    so this gets real headroom rather than a number tuned for a small library."""
    return _get("/search-options", timeout=180)


def get_tags() -> tuple[list | None, str | None]:
    return _get("/tags")


def search_options_filtered(filters: dict) -> tuple[dict | None, str | None]:
    """Same {"top": [...], "random": [...], "bottom": [...]} shape as get_search_options(), but
    narrowed by every filter axis in `filters` (score/resolution/rating-count/date-added bands,
    namespace/archive toggles, file/tag service selection - see tagrank_picker.html's
    tagrankGatherDbFilters()). Backed by TagRank's in-memory tag_index (built once at server
    startup, see tagrank/tag_index.py), so this is a fast in-process computation, not a fresh
    Hydrus round trip per candidate tag - the 180s timeout here is just headroom, not the
    expected case."""
    return _post("/search-options/filtered", json_body=filters, timeout=180)


def get_graphs() -> tuple[list | None, str | None]:
    """[{"title", "png_base64"}, ...] - the four summary charts. Rendering matplotlib figures
    to PNG server-side is slower than a plain JSON route, hence the longer timeout."""
    return _get("/history/graphs", timeout=30)


def start_session(query: list[str], pool_size: int | None = None, use_similarity: bool = False) -> tuple[dict | None, str | None]:
    """use_similarity defaults to False - TagRank's visual-similarity pool expansion (a Hydrus
    distance-search per seed hash) is the slow part of starting a session, so Undertow's filter
    bar's Similarity toggle (off by default) lets the user opt back into it."""
    body: dict = {"query": query, "use_similarity": use_similarity}
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


def get_settings() -> tuple[dict | None, str | None]:
    """{"hydrus": {"tag_service_key", "badge_tag_service_key"}, "pool": {..., "file_service_key"},
    ...} - only hydrus.tag_service_key/badge_tag_service_key are exposed from the hydrus
    section (see tagrank/server.py); the real secrets (api_key, rating/mmr service keys) never
    leave TagRank's own process."""
    return _get("/settings")


def patch_settings(changes: dict) -> tuple[dict | None, str | None]:
    """changes is {"section.field": value, ...}, e.g. {"pool.file_service_key": "...",
    "hydrus.tag_service_key": "...", "hydrus.badge_tag_service_key": "..."}."""
    return _patch("/settings", {"changes": changes})


def get_file_rating_details(file_id: int, file_hash: str, tags: list[str]) -> tuple[dict | None, str | None]:
    """{"photo_score", "photo_confidence", "picture_badge", "tags": [{"tag","score",
    "confidence","badge_count"}, ...]} for one comparer side - powers the in-tab comparer's
    score/badge/win-probability display (see tagrank_comparer.html and webui.py's
    _tagrank_compare_side_ctx). Implemented server-side by TagRank's GET
    /files/{file_id}/rating-details (see the contract at
    F:\\0DocsF\\0Docs\\AI\\Claude\\tagrank\\plans\\undertow-comparer-rating-details.md and its
    implementation in tagrank/server.py + tagrank/service.py:get_rating_details). Callers should
    still treat a non-None error here as "feature not available" rather than a hard failure -
    e.g. an older TagRank build without this route, or Hydrus briefly unreachable - since the
    comparer itself (image + tags + judging) works fine without this extra."""
    params = [("hash", file_hash)] + [("tag", t) for t in tags]
    return _get(f"/files/{file_id}/rating-details", params=params)


def end_session(session_id: str) -> tuple[dict | None, str | None]:
    """Persists the session's judgments to disk - always call this on every exit path
    (finishing, cancelling, or erroring out of a session), never just abandon a session id."""
    return _delete(f"/sessions/{session_id}")
