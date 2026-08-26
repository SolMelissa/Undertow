# Undertow

A cockpit that manages [Hydrus Network](https://hydrusnetwork.github.io/hydrus/)
(a local media manager/tagger) together with
[hydownloader](https://gitgud.io/thatfuckingbird/hydownloader) (its subscription/downloader
daemon) as a single app, so daily use is "double-click a shortcut" instead of juggling two
separate programs and their processes by hand.

This repo is the cockpit app itself — **not** the Hydrus or hydownloader installs. Those live
under `%USERPROFILE%\HydrusPipeline\` once set up (see [Configuration](#configuration) below).

## What it actually does

- Starts Hydrus, the hydownloader daemon, and the hydownloader systray if they aren't already
  running (and leaves them alone if they are).
- Runs a background watchdog that restarts the daemon/systray if either crashes.
  Hydrus itself is never auto-restarted — closing it is assumed to be deliberate.
- Opens a local **web dashboard** (`http://127.0.0.1:8765`) as the primary interface — add/
  pause/resume/delete subscriptions, queue a one-off URL download, watch what the daemon is
  doing right now, run health diagnostics, and configure API keys, all from a browser tab. The
  launcher's own console window hides itself once the dashboard is up, since there's nothing
  left to interact with there day to day.
- The original full-screen console UI (built with [Textual](https://textual.textualize.io)) is
  still fully there as a fallback — one click away via the dashboard's **Console UI** button
  (opens it in a fresh console window without redoing any startup steps), and it's what launches
  automatically instead if `flask` isn't installed.

## Quick start

**Already set up?** Just double-click the "Undertow" Desktop shortcut (or run
`run.bat`). It starts whatever services aren't already running, opens the web dashboard in your
browser, and hides its own console window. Use the dashboard's **Shutdown** button to stop the
whole pipeline cleanly — there's no window left to close or Ctrl+C once it's hidden.

**First time on this machine?** Run `Setup-HydrusPipeline.ps1` from a *non-admin* PowerShell
window. It clones our Hydrus fork ([SolMelissa/hydrus](https://github.com/SolMelissa/hydrus),
`undertow` branch) and builds its venv, installs Python, Git, FFmpeg, gallery-dl, and Poetry via
`winget`/`pip`, clones and configures hydownloader, and walks you through the two steps that can't be scripted
(Reddit app registration, Hydrus Client API key generation). See that script's header comment
for prerequisites (`winget` must be available). This is a one-time, idempotent script — safe
to re-run if it's interrupted.

**Setting up this app's own Python environment** (only needed once, separately from the above):

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m undertow.shortcut
```

The last command (re)creates the "Undertow" Desktop shortcut pointing at `run.bat`.

## Daily use: the web dashboard

`run.bat` (via `python -m undertow`) checks/starts services, then opens
`http://127.0.0.1:8765` in your browser: real-time Hydrus/daemon/systray status, the
subscriptions table (click a row to pause/resume/delete/force-check/view history), a
quick-subscribe bar, a live-colorized tail of hydownloader's log, and a column of instrument
panels (fleet counts, per-site activity, host RAM/disk/GPU, daemon API call stats).

Command bar buttons (top of the page): **Subscribe**, **One-off** URL download,
**Diag**nostics, API **Keys**, bring **Hydrus**/**Tray** to the front, **Sync** (refresh now),
**Console UI** (opens the classic TUI fallback in a new window), and **Shutdown** (stops the
daemon/systray if idle and closes the dashboard process — the only clean way to stop the
pipeline now that the launcher's own console window hides itself).

### Fallback: the console TUI

Click **Console UI** in the dashboard (or run `python -m undertow.tui` directly) to open
the original full-screen Textual cockpit in its own console window — it connects to the same
already-running services rather than starting them again, so both interfaces can be used side
by side. It's also what launches automatically instead of the web dashboard if `flask` isn't
installed.

| Key | Does |
| --- | --- |
| `a` | Subscribe to a URL / gallery / artist (comma-separate for several at once) |
| `u` | Queue a one-off URL download (not recurring) |
| *(quick-add bar)* | Paste a URL directly into the subscriptions panel + Enter |
| `enter` | Open pause/resume/delete/force-check for the selected subscription |
| `p` | Pause/resume the selected subscription instantly |
| `f` | Force-check the selected subscription instantly |
| `x` | Delete the selected subscription (confirms first) |
| `/` | Filter the subscriptions table live as you type |
| `h` / `y` | Bring Hydrus / the systray to the front |
| `w` | Open the web dashboard in your browser |
| `c` | Run diagnostics (service status, daemon API reachability, gallery-dl on PATH, watchdog) |
| `k` | Configure API keys (Reddit OAuth / Hydrus Client API key) |
| `q` | Quit — stops anything idle, leaves anything busy running |

Full rationale for these (why the 100-file default cap exists, why subscriptions are always
per-site parallel, etc.) is in [`PYTHON_PORT_SETUP.md`](PYTHON_PORT_SETUP.md) — note that doc
still describes the TUI as the primary interface and is due for an update to match the above.

### If something's off

- `python -m undertow.stop_services` — stops just the daemon + systray (leaves Hydrus
  running). Use this after editing `hydownloader-config.json` by hand, so the daemon picks up
  the new values on its next start.
- Diagnostics (the dashboard's **Diag** button, or `c` in the TUI) checks service status,
  whether the daemon's API is actually reachable (not just "is the process running"), whether
  `gallery-dl` resolves on `PATH`, and the watchdog's last action.

## Project layout

```
undertow/
  __main__.py       entry point (python -m undertow)
  menu.py           startup/shutdown wiring: start services, run the watchdog, launch the web
                     dashboard and hide the console (falls back to the TUI if flask is missing)
  config.py         all install paths/constants (equivalent of the old PS1's path variables)
  services.py       start/stop/status/health-check for Hydrus, the daemon, the systray;
                     host RAM/disk/GPU + top-process CPU stats; hide/show this process's console
  subscriptions.py  add/pause/resume/delete subscriptions, batch import, force-recheck, history
  api_client.py     thin HTTP client for the hydownloader daemon's own API (tracks call stats)
  watchdog.py       background thread that restarts the daemon/systray if either crashes
  webui.py          Flask dashboard at http://127.0.0.1:8765 - the primary interface (reuses the
                     same backend calls as the TUI; also launches the TUI as a fallback and
                     handles the Shutdown button)
  api_keys.py       Reddit OAuth + Hydrus Client API key setup flow
  logtail.py        tails hydownloader's daemon.txt log for the dashboard/TUI
  shortcut.py       (re)creates the Desktop shortcut
  stop_services.py  stops the daemon + systray on demand
  templates/        Jinja2 templates for the web dashboard (htmx + Alpine.js + Tailwind/daisyUI,
                     all from CDNs - no build step)
  tui/
    __main__.py     lets `python -m undertow.tui` launch just the TUI, used by the
                     dashboard's "Console UI" button (skips the service-start sequence)
    app.py          the Textual application (the fallback interface)
    modals.py       dialogs (add subscription, add download, confirm, health check, help, row actions)
    widgets.py      small reusable widgets
```

Everything above is the active codebase. `Launch-HydrusPipeline.ps1`,
`Configure-ApiKeys.ps1`, `Stop-HydrusPipelineServices.ps1`, and
`Create-DesktopShortcut.ps1` are the **legacy PowerShell originals** this package
replaced — left in place only as a fallback, not where feature work happens.
`Setup-HydrusPipeline.ps1` is the one PowerShell script still in active use (see
Quick start above).

## Configuration

Everything Hydrus/hydownloader-related lives under `%USERPROFILE%\HydrusPipeline\`:

| Path | What |
| --- | --- |
| `HydrusPipeline\hydrus\` | Our Hydrus fork, run from source (git clone + venv, not a winget/release build) |
| `HydrusPipeline\hydownloader\` | The cloned hydownloader repo (its own Python/Poetry env, run as the daemon) |
| `HydrusPipeline\hydownloader-data\` | hydownloader's database, config (`hydownloader-config.json`), and logs |
| `HydrusPipeline\hydownloader-data\logs\daemon.txt` | The live log the TUI/web UI tail |
| `HydrusPipeline\hydownloader-systray\` | The systray GUI (exe path varies by commit hash, so it's searched for at runtime) |

See [`undertow/config.py`](undertow/config.py) for the exact constants.

## Requirements

- Windows (uses `pywin32` for window-focusing, console-hiding, and console-close handling).
- Python 3.10+ with `requests`, `psutil`, `pywin32`, `rich`, `flask`, `textual`
  (`requirements.txt` — install into `.venv`, not system Python).
- `nvidia-smi` on `PATH` is optional — the dashboard's GPU widget uses it if present and shows
  "not detected" otherwise. No equivalent exists for AMD/Intel GPUs today.
- No automated test suite by design — verify changes by running the app directly
  (`python -m undertow`).

## Further reading

- [`PYTHON_PORT_SETUP.md`](PYTHON_PORT_SETUP.md) — full daily-use walkthrough,
  keybinding rationale, throttling/parallel-download details, and the history of why this was
  ported from PowerShell.
- [`CLAUDE.md`](CLAUDE.md) — contributor-facing guide to what to edit for what kind of change,
  plus known gotchas (BOM-free file writes, subscription file-count defaults, always-parallel
  workers).
- [`Hydrus_Pipeline_Guide.docx`](Hydrus_Pipeline_Guide.docx) / `.html` — end-user guide
  to using *Hydrus itself* (searching, tagging, folder-to-tag workflows), generated by
  `build_guide.js`. Unrelated to this app's own code — it documents the media manager
  you're driving.
