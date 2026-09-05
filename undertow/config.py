"""
Paths and install-layout constants - the Python equivalent of the top of
Launch-HydrusPipeline.ps1 ($InstallRoot, $HydrusDir, etc).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

INSTALL_ROOT = Path(os.environ["USERPROFILE"]) / "HydrusPipeline"
HYDRUS_DIR = INSTALL_ROOT / "hydrus"
HYDOWNLOADER_REPO_DIR = INSTALL_ROOT / "hydownloader"
DATA_DIR = INSTALL_ROOT / "hydownloader-data"
LOGS_DIR = DATA_DIR / "logs"
HYDRUS_EXE = HYDRUS_DIR / "hydrus_client.exe"
HYDOWNLOADER_CONFIG_FILE = DATA_DIR / "hydownloader-config.json"
SYSTRAY_DIR = INSTALL_ROOT / "hydownloader-systray"

WATCHDOG_INTERVAL_SECONDS = 90
WATCHDOG_STATUS_FILE = LOGS_DIR / "watchdog-status.json"
WATCHDOG_HISTORY_FILE = LOGS_DIR / "watchdog-history.jsonl"

DAEMON_LAUNCH_STDOUT_LOG = LOGS_DIR / "daemon-launch-stdout.log"
DAEMON_LAUNCH_STDERR_LOG = LOGS_DIR / "daemon-launch-stderr.log"

# TagRank's comparison-window subprocess used to run under CREATE_NEW_CONSOLE, which flashed a
# separate, untitled console window every time a pill was clicked. It's now launched hidden
# (CREATE_NO_WINDOW) with output captured here instead, so the webui's loading screen can show
# it inline (see tagrank_client.read_launch_log / templates/partials/girly/tagrank_starting.html).
TAGRANK_LAUNCH_STDOUT_LOG = LOGS_DIR / "tagrank-launch-stdout.log"
TAGRANK_LAUNCH_STDERR_LOG = LOGS_DIR / "tagrank-launch-stderr.log"

# Same idea for TagRank's headless API server subprocess (`main.py --serve`) - it used to
# discard stdout/stderr to DEVNULL entirely, so a startup failure (missing dependency, port
# already bound, a stack trace) left no trace anywhere and the tab just showed a generic
# "didn't come up in time" message with nothing to diagnose it from.
TAGRANK_SERVER_STDOUT_LOG = LOGS_DIR / "tagrank-server-stdout.log"
TAGRANK_SERVER_STDERR_LOG = LOGS_DIR / "tagrank-server-stderr.log"

# hydownloader's own main log - records subscription checks, downloads, and errors as they
# happen. Used by logtail.py for the "what's it actually doing right now" live feed.
DAEMON_LOG_FILE = LOGS_DIR / "daemon.txt"

# Used by Configure-ApiKeys port (api_keys.py)
GALLERY_DL_CONFIG_FILE = Path(os.environ["USERPROFILE"]) / "gallery-dl" / "config.json"
# The overlay hydownloader's own gallery-dl invocations actually load (see the daemon's
# per-subscription log "Configuration Files [...]" line) - GALLERY_DL_CONFIG_FILE above is a
# separate, unrelated config only used for the standalone `gallery-dl` CLI calls this module
# shells out to (oauth:reddit, --simulate test). Per-service keys (tumblr, imgur, etc.) need to
# land here instead, or the subscription daemon will never see them.
GALLERY_DL_USER_CONFIG_FILE = DATA_DIR / "gallery-dl-user-config.json"
IMPORT_JOBS_FILE = DATA_DIR / "hydownloader-import-jobs.py"
HYDRUS_API_URL = "http://localhost:45869"

# Hydrus's media library lives on a VeraCrypt volume mounted as A: - see
# services.ensure_veracrypt_drive_mounted(). VeraCrypt isn't always installed to the same
# place, so a couple of common locations are tried before giving up.
HYDRUS_VOLUME_DRIVE = "A:"
VERACRYPT_EXE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VeraCrypt" / "VeraCrypt.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "VeraCrypt" / "VeraCrypt.exe",
]


def find_veracrypt_exe() -> Path | None:
    for candidate in VERACRYPT_EXE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def find_systray_exe() -> Path | None:
    """The systray exe lands inside a commit-hash-named subfolder, so it has to be searched
    for rather than referenced by a fixed path - same as $SystrayExeFound in the PS1."""
    if not SYSTRAY_DIR.exists():
        return None
    matches = list(SYSTRAY_DIR.rglob("hydownloader-systray.exe"))
    return matches[0] if matches else None


# TagRank (github.com's own separate project, not part of the HydrusPipeline install tree) -
# a standalone tool that rates Hydrus tags/files via pairwise comparison. Undertow drives it
# headlessly through its local HTTP API (see undertow/tagrank_client.py and
# tagrank/docs/api.md) rather than importing it as a library, so only a filesystem path and a
# port are needed here, not a dependency. This is a fixed dev-machine path, not something the
# installer provisions - if TagRank isn't checked out there, the TagRank tab just reports it's
# unavailable instead of erroring the rest of the app.
TAGRANK_DIR = Path(r"F:\0DocsF\0Docs\AI\Claude\tagrank")
TAGRANK_PORT = 8420
TAGRANK_API_URL = f"http://127.0.0.1:{TAGRANK_PORT}"


def find_tagrank_python() -> Path | None:
    """TagRank runs out of its own venv (created via its own requirements.txt, not Undertow's
    interpreter) - prefer that venv's python.exe so its FastAPI/torch/etc deps resolve, falling
    back to a bare 'python' on PATH if the venv hasn't been set up."""
    venv_python = TAGRANK_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    return None


TAGRANK_RENAMED_PYTHON_NAME = "Undertow - TagRank.exe"


def find_tagrank_renamed_python() -> Path | None:
    """A friendlier-named copy of the TagRank venv's python.exe, so Task Manager shows
    "Undertow - TagRank.exe" for the subprocess instead of a bare "python.exe" indistinguishable
    from every other Python process on the machine (including Undertow's own, and any other
    venv's). Copied into the *same* .venv/Scripts folder as the original rather than out to some
    other directory: CPython's venv resolution walks up from the running executable's own
    directory looking for pyvenv.cfg (one level up from Scripts) to find the venv's real
    site-packages - copying it anywhere else would silently lose that and fall back to a bare/
    global Python with none of TagRank's installed dependencies.

    Recreated whenever missing or a different size than the source (venv recreated/upgraded),
    otherwise reused as-is. Returns None if there's no venv python.exe to copy from."""
    venv_python = find_tagrank_python()
    if venv_python is None:
        return None
    renamed = venv_python.parent / TAGRANK_RENAMED_PYTHON_NAME
    try:
        if not renamed.exists() or renamed.stat().st_size != venv_python.stat().st_size:
            shutil.copy2(venv_python, renamed)
    except OSError:
        return venv_python  # fall back to the original name if the copy failed
    return renamed


def find_tagrank_main() -> Path | None:
    main_py = TAGRANK_DIR / "main.py"
    return main_py if main_py.exists() else None


# ---------------------------------------------------------------------- settings-aware getters
# The constants above are the ultimate hardcoded fallbacks; these getters check settings.json
# (via undertow.settings) for a user override first. Each imports `settings` inside the
# function body rather than at module top - config.py is imported by nearly every other module
# in the package, and settings.py itself imports config.py for path/default construction, so a
# top-level `config -> settings -> config` cycle would break imports everywhere. Deferring the
# import to call time avoids that entirely.

def get_watchdog_interval_seconds() -> int:
    from . import settings
    return int(settings.load_settings().get("watchdog_interval_seconds", WATCHDOG_INTERVAL_SECONDS))


def get_hydrus_api_url() -> str:
    from . import settings
    return str(settings.load_settings().get("hydrus_api_url", HYDRUS_API_URL))
