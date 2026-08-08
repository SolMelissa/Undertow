# Undertow (Hydrus Pipeline)

A Python TUI app that manages **Hydrus** (media manager) + **hydownloader** (its subscription
downloader daemon) as one cockpit, so daily use is "double-click a shortcut" instead of
juggling two separate programs. This folder is the app itself, not the Hydrus/hydownloader
installs — those live under `%USERPROFILE%\HydrusPipeline\` (see `undertow/config.py`
for exact paths).

This project is a git repo (`origin` = `github.com/SolMelissa/Undertow`). When a task changes
code, commit and push to `master` when done — don't leave finished work sitting only on a
worktree branch or unpushed local commit. `run.bat` (the Desktop shortcut entry point) does a
best-effort `git pull --ff-only` on every launch specifically so pushed changes show up next
time the user runs the app; if you can't push (diverged history, conflicts, offline), say so
explicitly in your final report rather than reporting the task as done.

## Architecture: Python port superseded the PowerShell version

Daily-use logic used to be PowerShell (`Launch-HydrusPipeline.ps1` and friends). It was ported
to the `undertow/` Python package to fix JSON-serialization and error-handling issues
that kept recurring in PS1. **The `.ps1` scripts for daily use are legacy** — untouched,
kept only as a fallback, not where active work happens. See `docs/PYTHON_PORT_SETUP.md` for the
full history/rationale.

| Old (legacy, don't edit for feature work) | New (active) |
|---|---|
| `legacy/Launch-HydrusPipeline.ps1` | `undertow/menu.py` (startup wiring) + `undertow/webui.py` (primary interface, a web dashboard) |
| `legacy/Configure-ApiKeys.ps1` | `undertow/api_keys.py` |
| `legacy/Stop-HydrusPipelineServices.ps1` | `undertow/stop_services.py` |
| `legacy/Create-DesktopShortcut.ps1` | `undertow/shortcut.py` |

**Exception: `scripts/Setup-HydrusPipeline.ps1` is still the active, correct tool.** It's a run-once
first-time install/provisioning script (installs Hydrus, clones hydownloader, creates the
venv for the daemon itself) and was intentionally left out of the Python port. Don't try to
port it or route setup questions to the Python package.

## What to use for what

- **Daily-use bugs/features (subscriptions, TUI, watchdog, web dashboard, service
  start/stop/health checks, API key config)** → edit inside `undertow/`. Entry point is
  `python -m undertow` (via `run.bat`, which prefers `.venv\Scripts\python.exe` if
  present). Key modules:
  - `menu.py` — startup wiring: starts services/watchdog, then launches the web dashboard and
    hides this process's own console window (falls back to the TUI if `flask` isn't installed)
  - `webui.py` — Flask dashboard at `http://127.0.0.1:8765`, now the **primary daily-use
    interface** (subscriptions, diagnostics, API keys, host/GPU/network stats, a Shutdown
    button, and a "Console UI" button that launches the TUI fallback in a new window). Reuses
    the same backend calls the TUI does — no duplicated logic. `templates/` holds its Jinja2
    templates (htmx + Alpine.js + Tailwind/daisyUI from CDNs, no build step).
  - `tui/app.py`, `tui/modals.py` — the Textual console UI. Still fully functional as the
    fallback interface (automatic if `flask` is missing, or on-demand via the dashboard's
    "Console UI" button, which runs `tui/__main__.py` directly and skips the startup sequence
    since services are already up by then)
  - `services.py` — start/stop/status/health-check for Hydrus, the hydownloader daemon,
    systray; host RAM/disk stats and GPU stats (via `nvidia-smi`, best-effort); hiding/
    showing this process's own console window
  - `subscriptions.py` — add/pause/resume/delete subscriptions, batch import, force-checking a
    subscription now (`force_recheck`) and reading its per-run history (`get_check_history`,
    `get_latest_checks` - the latter also backs the subs table's "New Files" column)
  - `watchdog.py` — background thread that restarts the daemon/systray if they crash (Hydrus
    itself is never auto-restarted — closing it is assumed deliberate); on any restart/alert
    action it also calls `alerts.notify()`
  - `alerts.py` — native Windows toast (balloon-tip) notifications for watchdog-detected
    events, gated by `settings.json`'s `windows_toast_enabled`
  - `settings.py` — load/save `settings.json` (user-configurable overrides layered on top of
    `config.py`'s hardcoded defaults — resource alert thresholds, toast toggle, etc.)
  - `tags.py` — free-form tags on subscriptions (separate from Hydrus's own tagging), used by
    the subscriptions table's tag column/filter and `tag:` search prefix
  - `api_client.py` — talks to the Hydrus/hydownloader daemon APIs
  - `api_keys.py` — Reddit OAuth + Hydrus Client API key setup
  - `config.py` — all install paths/constants (equivalent of the top of the old PS1)
- **First-time install / provisioning on a new machine** → `scripts/Setup-HydrusPipeline.ps1` only.
- **The end-user usage guide** (how to search/tag/organize in Hydrus, folder-to-tag workflows,
  troubleshooting) → `docs/Hydrus_Pipeline_Guide.docx` / `.html`, generated by
  `scripts/build_guide.js`. This is user-facing documentation, separate from
  `docs/PYTHON_PORT_SETUP.md` (which is dev-facing: what changed in the port, TUI keybindings,
  default file caps).
- **Setup/dev-facing docs for the Python port itself** → `docs/PYTHON_PORT_SETUP.md`.
- **Dependencies**: `requirements.txt`/`pyproject.toml`, both pinned to exact versions
  (`requests`, `psutil`, `pywin32`, `rich`, `flask`, `textual`) since there's no CI to catch a
  breaking upstream release. Install into `.venv` per `docs/PYTHON_PORT_SETUP.md`, not system
  Python.
- **No automated test suite.** There used to be a pytest suite in `tests/` (plus `pytest.ini`,
  `run_tests.bat`, `requirements-dev.txt`) - it was deliberately removed since it wasn't
  pulling its weight. Verify changes by running the app directly.

## Gotchas

- Don't write config/JSON files with a BOM — Windows PowerShell's `Set-Content -Encoding
  UTF8` silently adds one and breaks Python's `json` module reading it back. Python's own
  `open(path, "w", encoding="utf-8")` is already BOM-free; this is exactly why the port
  happened, so don't reintroduce PS1-style file writes.
- New subscriptions default to a 100-file cap on both the first check and every check after
  (not hydownloader's own defaults of 10,000 initial / unlimited). This is intentional —
  don't "fix" it as a bug. Per-subscription overrides exist in the Add Subscription dialog;
  retroactively capping older subscriptions lives behind the `c` (Diagnostics) key in the TUI.
- Every subscription always runs on a per-downloader worker thread — there is no
  single-threaded mode and no manual toggle for it. `add_single_subscription` restarts the
  daemon itself when a new site's worker thread isn't live yet, and
  `subscriptions.ensure_all_subscriptions_parallel()` self-heals any stragglers once at every
  app launch. Don't reintroduce a manual "activate parallel downloads" control.
