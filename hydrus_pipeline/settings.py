"""
User-editable overrides for values that used to be hardcoded Python constants (watchdog
interval, Hydrus API URL, default file caps, interval fuzz range, resource-alert thresholds).
Persisted as a small JSON file next to the rest of hydownloader's data so changing one of these
no longer requires editing source and redeploying - see the Settings page in webui.py.

Deliberately module-level functions (load/save), not a class - there's exactly one settings
file and no need for instances. Every read re-loads from disk rather than caching in memory,
since the watchdog thread and the web request thread are both readers and neither should have
to coordinate invalidating a shared cache; the file is small and read infrequently enough
(once per watchdog cycle, once per settings-page render) that this costs nothing that matters.
"""

from __future__ import annotations

import json

from . import config

SETTINGS_FILE = config.DATA_DIR / "hydrus-pipeline-settings.json"

# max_files_initial/regular and check_interval_hours_min/max deliberately duplicate the literal
# values of subscriptions.py's DEFAULT_MAX_FILES_INITIAL/REGULAR and
# DEFAULT_CHECK_INTERVAL_HOURS_MIN/MAX rather than importing them - subscriptions.py already
# imports this module, so importing subscriptions.py back here would be a circular import. Keep
# these two literals in sync with subscriptions.py's constants by hand.
DEFAULTS: dict = {
    "watchdog_interval_seconds": config.WATCHDOG_INTERVAL_SECONDS,
    "hydrus_api_url": config.HYDRUS_API_URL,
    "max_files_initial": 100,
    "max_files_regular": 100,
    "check_interval_hours_min": 12.0,
    "check_interval_hours_max": 24.0,
    "resource_alert_thresholds": {"disk_pct": 90.0, "ram_pct": 90.0},
    "windows_toast_enabled": True,
}


def load_settings() -> dict:
    """Returns the effective settings dict: DEFAULTS shallow-merged with whatever's actually
    in the settings file, so a file predating a newer default key still works. Falls back to
    DEFAULTS.copy() on any read/parse failure (missing file, corrupt JSON) rather than raising -
    a broken settings file should degrade to "nothing's been customized yet", not crash the
    watchdog thread or the dashboard."""
    merged = DEFAULTS.copy()
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return merged
    if isinstance(stored, dict):
        merged.update(stored)
    return merged


def save_settings(updates: dict) -> None:
    """Read-modify-write: merges `updates` into whatever's currently stored (not into DEFAULTS,
    so a value already customized by a prior save that isn't part of this update stays as-is)
    and writes the result back. Never writes a BOM - open(..., "w", encoding="utf-8") only,
    per the project's own gotcha about PowerShell-style writes breaking json.load elsewhere."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}

    stored.update(updates)
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)
