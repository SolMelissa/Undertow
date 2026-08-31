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
    result = _delete(f"/sessions/{session_id}")
    _session_tags.pop(session_id, None)
    return result


# session_id -> the tag it was started around, purely for display in tagrank_session.html
# (the API itself doesn't echo this back). Lost on process restart, same as every other
# in-memory-only bit of UI state in this app - harmless, it's just a label.
_session_tags: dict[str, str] = {}


def remember_session_tag(session_id: str, tag: str) -> None:
    _session_tags[session_id] = tag


def get_session_tag(session_id: str) -> str:
    return _session_tags.get(session_id, "?")
