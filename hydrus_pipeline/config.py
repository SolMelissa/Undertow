"""
Paths and install-layout constants - the Python equivalent of the top of
Launch-HydrusPipeline.ps1 ($InstallRoot, $HydrusDir, etc).
"""

from __future__ import annotations

import os
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

DAEMON_LAUNCH_STDOUT_LOG = LOGS_DIR / "daemon-launch-stdout.log"
DAEMON_LAUNCH_STDERR_LOG = LOGS_DIR / "daemon-launch-stderr.log"

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


def find_systray_exe() -> Path | None:
    """The systray exe lands inside a commit-hash-named subfolder, so it has to be searched
    for rather than referenced by a fixed path - same as $SystrayExeFound in the PS1."""
    if not SYSTRAY_DIR.exists():
        return None
    matches = list(SYSTRAY_DIR.rglob("hydownloader-systray.exe"))
    return matches[0] if matches else None
