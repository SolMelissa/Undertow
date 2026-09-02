# Undertow (Hydrus Pipeline)

A Python web-dashboard app that manages **Hydrus** (media manager) + **hydownloader** (its
subscription downloader daemon) as one cockpit, so daily use is "double-click a shortcut" instead of
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

Daily-use logic used to be PowerShell (`Launch-HydrusPipeline.ps1` and friends), now ported to
the `undertow/` Python package (entry point `python -m undertow` via `run.bat`). The legacy
`.ps1` daily-use scripts were fully superseded by the port and have been deleted outright
(not kept as a fallback). Exception: `Setup-HydrusPipeline.ps1` is still the active first-time
install/provisioning tool and was intentionally left out of the port — don't route setup
questions to the Python package.

The Textual console TUI and the dashboard's old hacker/terminal theme have also been removed
as unneeded overhead — `undertow/webui.py`'s dashboard (girly/kawaii theme only) is the sole
interface now.

The setup script and the docs live at the repo root (no `legacy/`/`scripts/`/`docs/`
subfolders — except a `legacy/` folder holding old guide docs, not code).

For the full legacy→active file mapping, the per-module breakdown of `undertow/` (menu.py,
webui.py, services.py, subscriptions.py, watchdog.py, alerts.py, settings.py, tags.py,
api_client.py, api_keys.py, config.py), and where the setup script / user guide / dev
docs / dependency pins live, see `.claude/docs/ARCHITECTURE.md`. Load that file before
editing inside `undertow/` if you're not already sure which module owns the behavior in
question.

No automated test suite — a pytest suite was deliberately removed as not worth its cost.
Verify changes by running the app directly.

## Gotchas

- Don't write config/JSON files with a BOM — Windows PowerShell's `Set-Content -Encoding
  UTF8` silently adds one and breaks Python's `json` module reading it back. Python's own
  `open(path, "w", encoding="utf-8")` is already BOM-free; this is exactly why the port
  happened, so don't reintroduce PS1-style file writes.
- New subscriptions default to a 100-file cap on both the first check and every check after
  (not hydownloader's own defaults of 10,000 initial / unlimited). This is intentional —
  don't "fix" it as a bug. Per-subscription overrides exist in the Add Subscription dialog;
  retroactively capping older subscriptions lives behind the dashboard's Diagnostics modal.
- Every subscription always runs on a per-downloader worker thread — there is no
  single-threaded mode and no manual toggle for it. `add_single_subscription` restarts the
  daemon itself when a new site's worker thread isn't live yet, and
  `subscriptions.ensure_all_subscriptions_parallel()` self-heals any stragglers once at every
  app launch. Don't reintroduce a manual "activate parallel downloads" control.

<!-- code-setup-project:token-management v1 -->
## Token & Cost Management

- Context: run `/clear` between unrelated tasks. Use `/compact <instructions>`
  with a focus (e.g. "keep decisions and file paths, drop raw tool output")
  instead of a bare `/compact`. Check `/context` and `/usage` if a session
  starts to feel heavy.
- Model & effort: default to Sonnet. Reserve Opus for hard architecture or
  design work. Lower `/effort` or turn off extended thinking for small,
  mechanical tasks — thinking tokens bill as output tokens.
- Delegate verbose operations — test runs, log parsing, doc/API fetches, bulk
  file scans — to a subagent instead of running them in the main thread. Only
  the subagent's conclusion should land in the main context.
- Keep this file lean; it loads on every turn. Put workflow-specific or
  occasional-use instructions in a skill (`.claude/skills/<name>/SKILL.md`)
  instead of adding them here.
