# Undertow - Python port setup

This replaces `Launch-HydrusPipeline.ps1`, `Configure-ApiKeys.ps1`, `Stop-HydrusPipelineServices.ps1`,
and `Create-DesktopShortcut.ps1` with a Python package (`undertow/`), without PowerShell's
JSON-serialization and error-detail footguns that kept biting the subscription-add flow.

The interface itself has since moved past a straight 1:1 port: instead of a numbered
print()/input() menu, daily use is the web dashboard - see "Daily use" below. (An earlier
iteration used a full-screen Textual console UI; that's since been removed - see "Daily use"
for what replaced it.)

**Not replaced:** `Setup-HydrusPipeline.ps1` (first-time install/provisioning - installs
Hydrus, clones hydownloader, sets up the Python venv for the *daemon* itself, etc.) stays as
PowerShell. It's a run-once script, not part of daily use, and wasn't where any of the bugs
we hit actually lived.

## One-time setup

This folder lives at `F:\Apple\iCloudDrive\0Docs\AI\Claude\The Pipeline` (it used to be
`C:\0Docs\AI\Claude\The Pipeline`). Only this app moved - the Hydrus install, the
hydownloader clone, and all databases and media stay at `%USERPROFILE%\Undertow`,
which is what `undertow/config.py` still points at.

Because the folder is now cloud-synced, the venv lives **outside** it. Open a terminal
(PowerShell or Command Prompt) in this folder and run:

```
python -m venv "%LOCALAPPDATA%\Undertow\venv"
"%LOCALAPPDATA%\Undertow\venv\Scripts\pip" install -r requirements.txt
```

A venv is thousands of small files plus native binaries - kept inside an iCloud folder it
causes constant sync churn, and the sync client can evict or half-write `python.exe`/`*.pyd`
and break the launcher in confusing ways. `run.bat` still checks for an in-folder `.venv`
first, so the old layout keeps working if you'd rather go back to it.

Then recreate the Desktop shortcut so it points at the new launcher instead of PowerShell:

```
"%LOCALAPPDATA%\Undertow\venv\Scripts\python" -m undertow.shortcut
```

This overwrites the existing "Undertow" Desktop shortcut in place - same name, same
icon, same double-click habit, just pointing at `run.bat` now instead of
`powershell.exe -File Launch-HydrusPipeline.ps1`.

## Daily use

Nothing changes from your side - double-click the "Undertow" Desktop shortcut like
always. It runs `run.bat`, which uses the venv if it finds one next to it, falls back to
system Python otherwise.

**This changed from the TUI-first design described further down in this doc's history:**
once services are checked/started, `menu.main()` now launches the **web dashboard**
(`undertow/webui.py`, http://127.0.0.1:8765) as the primary and only interface and hides its
own console window - there's nothing left to interact with in the console day to day. The
dashboard has a **Shutdown** button (stops the daemon/systray if idle, then exits the process)
since there's no window left to close or Ctrl+C once it's hidden.

The Textual "cockpit" TUI this doc originally described, and the dashboard's old
hacker/terminal theme, have both since been removed outright as unneeded overhead - girly/
kawaii is the dashboard's only look, and there's no TUI fallback left if `flask` isn't
installed (`menu.main()` just reports the missing dependency and exits). See
[`README.md`](README.md) rather than this doc for what's current in the dashboard - the
subscription table/actions/diagnostics/API-key setup, host RAM/disk/GPU stats, a daemon API
call-traffic widget, and per-process network connections.

Needs `rich` and `flask` (both in requirements.txt). If you set up your `.venv` before these
were added, re-run `.venv\Scripts\pip install -r requirements.txt`.

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

The dashboard's **Diag**nostics modal has a Throttle Control section to handle both cases:

- **Cap existing subs too** - retroactively applies the 100/100 limits to every subscription
  already in hydownloader (from before that was the default). Takes effect on each one's next
  check; no restart needed.
- **Group + restart to activate** - retroactively assigns worker threads to subscriptions
  added before parallel-by-default existed, *and* restarts the daemon so any pending new
  worker threads (including ones auto-assigned to a subscription you just added) actually go
  live. The restart shuts the daemon down gracefully first and waits to confirm the API comes
  back up before calling it done - if it doesn't, it says so instead of pretending it worked.

## If something's off

Use the dashboard's **Shutdown** button and relaunch if `hydownloader-config.json` changed and
the running daemon needs to pick up fresh values. The old `.ps1` daily-use scripts, the
one-off `stop_services.py` fallback, and the console TUI have all been removed as fully
superseded by the web dashboard - only `Setup-HydrusPipeline.ps1` (first-time provisioning)
remains.

