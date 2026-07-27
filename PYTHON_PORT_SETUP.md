# Hydrus Pipeline - Python port setup

This replaces `Launch-HydrusPipeline.ps1`, `Configure-ApiKeys.ps1`, `Stop-HydrusPipelineServices.ps1`,
and `Create-DesktopShortcut.ps1` with a Python package (`hydrus_pipeline/`), without PowerShell's
JSON-serialization and error-detail footguns that kept biting the subscription-add flow.

The interface itself has since moved past a straight 1:1 port: instead of a numbered
print()/input() menu, daily use is a full-screen [Textual](https://textual.textualize.io)
console UI - see "Daily use" below.

**Not replaced:** `Setup-HydrusPipeline.ps1` (first-time install/provisioning - installs
Hydrus, clones hydownloader, sets up the Python venv for the *daemon* itself, etc.) stays as
PowerShell. It's a run-once script, not part of daily use, and wasn't where any of the bugs
we hit actually lived.

## One-time setup

Open a terminal (PowerShell or Command Prompt) in this folder (`C:\0Docs\AI\Claude\The Pipeline`)
and run:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then recreate the Desktop shortcut so it points at the new launcher instead of PowerShell:

```
.venv\Scripts\python -m hydrus_pipeline.shortcut
```

This overwrites the existing "Hydrus Pipeline" Desktop shortcut in place - same name, same
icon, same double-click habit, just pointing at `run.bat` now instead of
`powershell.exe -File Launch-HydrusPipeline.ps1`.

## Daily use

Nothing changes from your side - double-click the "Hydrus Pipeline" Desktop shortcut like
always. It runs `run.bat`, which uses the venv if it finds one next to it, falls back to
system Python otherwise.

**This changed from the TUI-first design described further down in this doc's history:**
once services are checked/started, `menu.main()` now launches the **web dashboard**
(`hydrus_pipeline/webui.py`, http://127.0.0.1:8765) as the primary interface and hides its own
console window - there's nothing left to interact with in the console day to day. The
dashboard has a **Shutdown** button (stops the daemon/systray if idle, then exits the process)
since there's no window left to close or Ctrl+C once it's hidden.

The Textual "cockpit" TUI described below is still fully implemented and still the automatic
fallback if `flask` isn't installed - and it's one click away from the dashboard's **Console
UI** button, which opens it in a fresh console window (running `python -m
hydrus_pipeline.tui`) without re-running the startup sequence, since services are already up
by the time the dashboard exists. Both interfaces call the exact same backend functions, so
using one doesn't desync the other.

When the TUI is what's on screen (fallback, or launched via **Console UI**): a status strip
(Hydrus/daemon/systray badges + what the daemon is doing right now), a command deck banner
listing every key, a subscriptions table you can subscribe/unsubscribe from directly (no
dialog round-trip for the common case), a colorized live tail of hydownloader's own log, and a
column of instrument panels (fleet counts, a per-site sector scan, an activity sparkline).

There's a quick-subscribe bar right above the subscriptions table - paste a URL, hit Enter,
it's added at the 24h default interval with no dialog. Pressing `p`/`x` with a row selected
instantly pauses/resumes or deletes it (delete confirms first) - `enter` still opens the full
pause/resume/delete dialog if you want it.

Keybindings (also shown in the footer/command deck, and searchable via `ctrl+p`'s command
palette):

| Key | Does |
| --- | --- |
| `a` | Subscribe to a URL / gallery / artist (comma-separate for several at once) |
| `u` | Queue a one-off URL download (not recurring) |
| (quick-add bar) | Paste a URL directly into the subscriptions panel + Enter |
| `enter` | Open pause/resume/delete for the selected subscription row |
| `p` | Pause/resume the selected subscription instantly, no dialog |
| `f` | Force-check the selected subscription instantly |
| `x` | Delete the selected subscription instantly (confirms first) |
| `/` | Filter the subscriptions table live as you type |
| `escape` | Clear filter / close whatever dialog is open |
| `h` | Bring Hydrus to the front |
| `y` | Bring the systray to the front |
| `w` | Open the web dashboard in your browser |
| `c` | Run diagnostics (service status, daemon API reachability, gallery-dl on PATH, watchdog) |
| `k` | Configure API keys (suspends the TUI, runs the same Reddit-OAuth/Hydrus-key flow as before) |
| `r` | Refresh immediately |
| `?` | Keybinding help |
| `q` | Quit - shuts down anything idle, leaves anything busy running (same as before) |

The web dashboard now has considerably more than the TUI ever exposed directly in a browser -
the same subscription table/actions/diagnostics/API-key setup, plus host CPU/RAM/disk/GPU
stats, a daemon API call-traffic widget, and per-process network connections - see the
dashboard's own UI or [`README.md`](README.md) rather than this doc for what's current there.

Needs `rich`, `flask`, and `textual` (all in requirements.txt). If you set up your `.venv`
before these were added, re-run `.venv\Scripts\pip install -r requirements.txt`.

### Download speed / throttling

New subscriptions cap at **100 files on their first check and 100 per check after that**,
instead of hydownloader's own defaults (10,000 initial / unlimited regular) - that gap is why
a brand-new subscription could look like it was pulling someone's entire backstock. You can
override this per-subscription in the Add Subscription dialog if you actually want a bigger
pull once.

Every new subscription also goes straight onto a worker thread named after its downloader
(site), so different sites check **concurrently by default** - no toggle needed. Subscriptions
to the *same* site still run one at a time on their shared thread (hydownloader's own
recommendation - splitting one site across threads risks rate-limit/ban problems). The one
catch: hydownloader only spins up worker threads when the daemon *starts* - a subscription to
a site you haven't threaded yet won't actually get checked until the daemon restarts once.
The app tells you right away when this applies ("new site - needs a restart") instead of
letting it fail silently.

Press `m` for Throttle Control to handle both cases:

- **Cap existing subs too** - retroactively applies the 100/100 limits to every subscription
  already in hydownloader (from before that was the default). Takes effect on each one's next
  check; no restart needed.
- **Group + restart to activate** - retroactively assigns worker threads to subscriptions
  added before parallel-by-default existed, *and* restarts the daemon so any pending new
  worker threads (including ones auto-assigned to a subscription you just added) actually go
  live. The restart shuts the daemon down gracefully first and waits to confirm the API comes
  back up before calling it done - if it doesn't, it says so instead of pretending it worked.

## If something's off

- `python -m hydrus_pipeline.stop_services` - stops just the daemon + systray (leaves Hydrus
  running), for when `hydownloader-config.json` changed and the running daemon needs to pick
  up fresh values. Same as the old Stop-HydrusPipelineServices.ps1.
- The old `.ps1` scripts are untouched and still work if you need to fall back - nothing was
  deleted.

