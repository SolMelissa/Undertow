# Changelog

All notable changes to Undertow are tracked here, one section per version. Newest first.

## 1.13.3
- Fixed the Tag Cleanup Lists editor's compound-pairs validation error discarding whatever the
  user had just typed into every *other* field, not just the bad pairs line - the error path
  re-rendered from the on-disk lists instead of echoing back the submitted form values, so fixing
  one typo silently threw away unrelated edits.
- Added the `wordfreq` pin to `pyproject.toml` (it was only in `requirements.txt`), keeping the
  two dependency lists in sync.
- Removed a stray `sh.exe.stackdump` crash dump that had ended up committed at the repo root.

## 1.13.2
- Fixed `tag_cleanup_lists.py` incorrectly showing up as a runnable/killable script in the
  Scripts tab. It's a support module `webui.py` imports directly (not a subprocess script), but
  `list_scripts()` globbed every `.py` file under `scripts/` without distinguishing the two. Added
  a `NOT_RUNNABLE` set to `scripts_runner.py` to exclude it explicitly.

## 1.13.1
- Fixed the version pill never noticing code changes pushed straight into this checkout (the
  normal case for background sessions - no separate worktree). `check_for_update()` used to only
  compare disk HEAD against `origin/master`, which are already equal the instant such a push
  lands, so the pill stayed stuck on "up to date" forever even though the running process was
  still executing the old code from before the push. It now also tracks `_STARTUP_HEAD` (the
  commit this process actually loaded at launch) and flags a new `restart_needed` state whenever
  disk HEAD has moved past that, regardless of origin.
- The pill's action is now a real fix for both cases: clicking it pulls (a safe no-op if nothing
  needs pulling) then calls `version.restart_process()`, which re-execs `python -m undertow` in
  place. Previously it only sent `HX-Refresh` to reload the browser tab, which can't force Python
  to re-import already-loaded modules - a pulled code change never actually took effect without
  someone manually restarting the app.

## 1.13.0
- Added a "Tag Cleanup Lists" editor to the Scripts tab (Interactive Wizards group): the word
  lists that drive the Tag Cleanup Wizard's parser (function words, scene-description glue words,
  attribute words, group nouns, always-standalone words, and compound noun pairs like "first
  timer") are now editable from the dashboard instead of only as hardcoded Python sets. Backed by
  a new dependency-free `tag_cleanup_lists.py` module (JSON file at
  `hydownloader-data/tag-cleanup-lists.json`, seeded from the previous hardcoded defaults) that
  both `tag_cleanup.py`'s `Config` and the new `GET/POST /scripts/tag-cleanup-lists` routes read
  from, so edits apply the next time the wizard runs with no code changes needed.

## 1.12.4
- Disabled the browser's saved-history autocomplete dropdown on every text input across the
  dashboard (subscription/tag/search/API-key/script-input fields) by adding `autocomplete="off"`,
  matching what the Media tab's predicate field already did.
- Added a Kill button to the Scripts tab, next to the Send button in a running script's terminal.
  Backed by a new `scripts_runner.stop()` that force-kills the subprocess (`Popen.kill()`), so a
  hung/stuck script can be reset without waiting it out. Wired through a new
  `POST /scripts/kill/<name>` route.
- Fixed the header's TagRank status pill showing stale red/"not running" for up to 60s after the
  server actually came up (e.g. right after opening the TagRank tab, which silently starts the
  server in the background). Because the pill's own poll loop only re-checks every 60s while the
  toggle button always re-checks live state, this made clicking the still-red pill hit the
  "already running -> stop" branch instead of "start" - stopping a server the header never showed
  as up in the first place. `/partials/tagrank` and `/tagrank/server-poll` now fire the same
  `refreshSubs` header-refresh trigger other service actions already use, the moment they confirm
  the server is live.

## 1.12.2
- Fixed the version-check pill in the dashboard header permanently showing its "checking..."
  spinner stacked on top of the real status pill, making it look stuck/duplicated forever and
  never settling on an up-to-date/update-available state. The spinner's `display: none` CSS rule
  had the same specificity as the later `.kawaii-pill { display: inline-flex }` rule it needed to
  override, so the later rule always won and the spinner never actually hid. Also fixed the
  spinner not appearing at all when manually clicking the pill to re-check (the click's htmx
  request only marked the clicked span, not the wrapper the spinner CSS keys off of) by adding
  `hx-indicator` so clicks mark the wrapper too. Verified against a real update: pointed the
  version-check code at a disposable local git sandbox, advanced its "origin" a commit ahead,
  and confirmed the live dashboard correctly flagged the update, pulled it via a real click, and
  landed on "up to date" afterward.

## 1.12.1
- Added a "Similarity search" toggle to the TagRank filter panel (off by default) so clicking a
  tag pill starts a comparison session with a fast plain tag search instead of TagRank's slower
  visual-similarity pool expansion, unless you specifically want visually-similar files in the
  pool. Required matching changes in the tagrank fork (`StartSessionRequest.use_similarity`,
  threaded through `service.start_session()` into `pool.build_pool(use_similarity=...)`).

## 1.12.0
- Added 10 backend/tag utility scripts to the Scripts tab, and redesigned the tab into
  icon+description cards grouped into **Reports** (Hydrus Health Check, Inbox Triage, Untagged
  Files, Duplicate Tag Finder, Namespace Summary, Subscription Health, Queue Report, Disk Usage)
  and **Housekeeping** (Log Archiver, Empty Folder Sweep), alongside the existing Interactive
  Wizards group (Tag Cleanup, Performer Gazetteer). Each script is a plain standalone `.py` file
  under `undertow/scripts/` reusing the existing scripts_runner subprocess/terminal
  infrastructure - a new script just needs an entry in `scripts_runner.SCRIPT_META` to get a
  labeled card instead of a bare filename pill.

## 1.11.0
- Tag Relations tab redesign - it had grown into 5 sub-tabs (2 of them unbuilt stubs) with
  duplicate search boxes and no clear story for what to use it for, so it's now 3:
  - **Explore** merges the old "Siblings & Parents" lookup and "Tag Map" into one search: type
    a tag once and see its siblings/parents/children plus the family-tree map together, instead
    of two separate tabs each wanting their own tag typed in.
  - **Bulk Edit** merges "Bulk Tagging" and "Tag Migration" into one panel with an inner
    Add/Remove vs. Rename/Merge toggle, since both are the same "batch-edit files matching a
    search" operation under different presentation.
  - **Namespaces** replaces the unbuilt stub with a real read-only browser: pick a namespace
    (e.g. `character`) and see every tag under it ranked by file count, for spotting
    near-duplicate/misspelled tags worth migrating - click any tag to jump straight into
    Explore for it.

## 1.10.2
- Added a Refresh button to the TagRank tab's "not checked out" and error states, so a downed
  or not-yet-started TagRank service can be retried without switching tabs away and back.

## 1.10.1
- Fixed the version pill's update check: it now shows a spinning reload icon while checking,
  and the "update available" state turns red (it was showing the same blue as the default
  state, so a real update never looked different from anything else).

## 1.10.0
- TagRank tab filter bar rework:
  - The DB Search button now shows a spinner and "Searching..." status while a search is in
    flight, and a visible error if the request itself fails, instead of giving no feedback at
    all while waiting.
  - Removed the "live"/"DB" badges from every filter field - they described an internal
    implementation detail (which metrics have data on-screen vs. only in the DB) rather than
    anything a user needs to think about.
  - Band+center sliders (score, resolution, rating count, date added) are now grouped in a
    bordered panel, with larger slider thumbs and toggle buttons.
  - Every filter now defaults to "show everything" - band sliders used to open on a narrow
    default window (e.g. only scores within +-2 of center) that silently hid results; their
    default ranges now cover the whole metric.
  - The File/tag service pickers moved to the bottom of the filter panel, since their
    "Select..." mode expands a panel downward.
  - Removed the separate "Services" section between the tag pills and the rating-history
    graphs - redundant now that file/tag service selection lives in the filter bar itself.
  - The Top rated / Random / Bottom rated pill groups are now one flat block, deduped by tag
    and sorted purely by TrueSkill (MMR) score, instead of three separate sections.

## 1.9.0
- Added a "TagRank" status pill to the dashboard header, alongside Hydrus/Downloader/Tray/Drive
  - green when TagRank's headless API subprocess is up, red when it isn't, click to
  start or stop it. Hidden entirely on machines without a TagRank checkout. Reuses the
  same `tagrank_client` start/stop functions the TagRank tab itself already relies on.

## 1.8.3
- Fixed `Undertow.exe` failing to launch with "Couldn't start the WebView2 runtime: Unable to
  load DLL 'WebView2Loader.dll'" (0x8007007E), a regression from 1.8.2's framework-dependent
  switch: `dotnet publish` with `PublishSingleFile` doesn't bundle native dependencies into the
  exe when not self-contained, so `WebView2Loader.dll` was left as a loose file in the publish
  output - and `build_launcher.bat` was deleting that whole temp folder instead of keeping the
  dll. It now copies `WebView2Loader.dll` next to `Undertow.exe` at the repo root.

## 1.8.2
- `build_launcher.bat`/`launcher.csproj`: switched `Undertow.exe` from a self-contained .NET
  publish to framework-dependent (`SelfContained=false`) - shrinks the built exe from ~163MB
  to under 1MB with no functional change, since this machine always has the matching .NET
  runtime installed anyway. Deleted the stale 163MB self-contained build from disk (it was
  never git-tracked either way).

## 1.8.1
- Fixed the version pill's "couldn't check · retry" error: `git fetch`'s 3s timeout was too
  tight for a real network round-trip and was intermittently getting clipped, reported as
  "couldn't reach origin" even when the fetch would have succeeded given another second or two.
- The version pill now checks for updates on page load and every 60s (previously only on
  click), and clicking an "update available" pill now actually runs `git pull --ff-only` and
  reloads the page - it used to just reload without pulling anything.

## 1.8.0
- Overhauled the TagRank tab's tag-picker filter bar: two connected search modes, Live
  (instant, client-side, only over already-fetched tags) and DB Search (round-trips to
  TagRank for fresh results). Filter tag and Min files (now a logical-increment
  0/1/2/3/4/5/10/15/20/25/50/100/.../10000 stepper) work in both modes.
- Replaced the old Min/Max score number fields with a "band + centerpoint" slider pair (a
  dual-thumb spread slider plus a single centerpoint slider below it) and reused that same
  control for four new filter axes: resolution aspect ratio, resolution pixel count, rating
  count, and date added. Score band filters live; the other four are DB Search only, since
  that data isn't present on an already-fetched tag pill.
- Added DB Search toggles for namespaced/unnamespaced, archived/inbox, and file/tag service
  selection (multi-select pill panels, reusing the same service lists the Services panel
  already sources from Undertow's own Hydrus API key).
- New `tagrank_client.search_options_filtered()` and `/tagrank/search-db` route wire DB
  Search through to a new TagRank API endpoint (`POST /search-options/filtered`) that doesn't
  exist yet - DB Search surfaces a clear "not available yet" error until it's added. The full
  request/response contract this was built against is written up at
  `tagrank/plans/undertow-filtered-search-api.md` (in the separate TagRank repo, untracked)
  for that endpoint's implementer.

## 1.7.0
- Removed the console TUI (`undertow/tui/`) and its `python -m undertow.tui` fallback in
  `menu.py` for when Flask isn't installed - both were unneeded overhead. Dropped the
  `textual` dependency.
- Removed the dashboard's old hacker/terminal theme (`#hacker-view`, matrix-rain/CRT chrome,
  the terminal cockpit CSS/JS) and the "Console UI" button - girly/kawaii is now the
  dashboard's only theme and only interface.
- Fixed a real bug found while removing the old theme: the disk/RAM threshold-breach banner
  (`#resource-banner`) was nested inside the hidden hacker-view div, so it never actually
  rendered in kawaii mode - moved it into the girly view where it now shows up correctly.
- Deleted the legacy `.ps1` daily-use scripts (`Launch-HydrusPipeline.ps1`,
  `Stop-HydrusPipelineServices.ps1`, `Configure-ApiKeys.ps1`, `Create-DesktopShortcut.ps1`,
  `Move-PipelineToICloud.ps1`) and the orphaned `undertow/stop_services.py`, all fully
  superseded by the Python port. `Setup-HydrusPipeline.ps1` is unaffected (still active).
- Repo cleanup: untracked the leftover `Undertow.exe - Shortcut.lnk` and added `*.lnk` to
  `.gitignore`, and moved the guide docs into `legacy/` as "Hydrus Pipeline Guide - version
  1.4.0" (docx + html).

## 1.6.2
- Repo cleanup: untracked the leftover `Undertow.exe - Shortcut.lnk` and added `*.lnk` to
  `.gitignore` (shortcuts are user-machine-specific, not app assets), and moved the guide
  docs into `legacy/` as "Hydrus Pipeline Guide - version 1.4.0" (docx + html).

## 1.6.1
- Slowed every dashboard auto-poll (previously 2s/8s/10s depending on panel - status, queue
  graph, sparkline, sector/fleet/hoststats/topprocs/netstat/netconn, subscriptions table,
  new-files check) to 60s in both classic and girly-mode UIs. Verified live that this dashboard
  polling was a real, sustained CPU cost (Undertow's own process was pinned 50-65% CPU, driven
  by the 2s subscriptions/queue-graph polls hitting the hydownloader daemon synchronously on
  every tick) - a real system-wide slowdown, not a false alarm. Action-triggered refreshes
  (add/pause/delete a subscription, switch to the Metrics tab, etc.) still fire immediately via
  their existing `refreshSubs`/`subsTableNav`/`metricsTabOpen` events - only the idle timer
  polling was slowed.

## 1.6.0
- TagRank's headless API now starts in the background as part of `start_required_services()`
  (Undertow launch, and every Diagnostics/status "restart services" click) instead of only
  being spawned the first time the TagRank tab is opened - it's had time to warm up before
  anyone actually clicks the tab, instead of eating its ~15-75s cold-start cost right there.
  Non-blocking and a no-op if already running/starting.

## 1.5.1
- Consolidated the TagRank pill-list filters: replaced the three separate per-group filter
  boxes (shipped moments earlier in 1.5.0) with one shared filter bar applying to Top/Random/
  Bottom at once, per feedback that per-section filters weren't warranted.

## 1.5.0
- TagRank's comparison window no longer flashes a separate, untitled console on launch
  (`CREATE_NEW_CONSOLE` -> `CREATE_NO_WINDOW`); its stdout/stderr is captured to
  `hydownloader-data/logs/tagrank-launch-*.log` instead and shown inline on the webui's "Building
  comparison pool..." loading screen.
- Added a single filter bar above TagRank's Top rated / Random / Bottom rated pill lists
  (tag substring, min files, min/max score) that applies to all three groups at once.

## 1.4.1
- Fixed the TagRank tab intermittently reporting a live server as down: its liveness check
  probed `GET /tags` (real work scaled to rated-tag history, measured 1.0-1.7s on a real
  library) against a 1.5s timeout, so a live server would occasionally read as not-running and
  Undertow would spawn a second subprocess on top of it - which failed its own port bind and
  surfaced as "TagRank didn't respond". Now probes `/health` with a 5s timeout instead.

## 1.4.0
- Added a TagRank tab: a subprocess-driven pill picker, head-to-head tag comparisons, and
  score graphs, launching straight into the clicked tag's pool with its own loading screen.
- Added a Scripts tab to run `undertow/scripts/*.py` from the webui, including live progress
  and support for interactive `input()` prompts in the on-page terminal.
- Added `tag_cleanup.py`, a filename-to-tag cleanup script for Hydrus: an interactive wizard
  that pulls services from Hydrus, caches the API key locally, dry-runs a random sample
  before the full library, batches writes by shared tag set, and (as of Phase 3) detects
  name blocks first with support for plain-tag names, comma/"&" lists, and initials.
  Dry-run previews are grouped by file into a local HTML report.
- Used the shortcut icon as the webui favicon, header logo, and console window icon.
- Fixed CPU-hog dashboard polling (visibility-gated, throttled GPU spawn/poll rates, cached
  psutil/config/tag reads, skip no-op table rebuilds) and an unbounded growth leak in the
  subscription-checks response cache.

## 1.3.0
- Fixed the version pill falsely showing "behind" almost permanently: untracked scratch
  files (settings.json, logs, dropped images) were counting as "dirty", and unpushed local
  commits (ahead of origin - the normal day-to-day state for this repo) were lumped in with
  genuinely behind/diverged as one generic "stale" status.
- Removed the top-bar changelog ribbon - the Changelog tab is the one place for it now.
- Moved the +Subscribe button and quick-add-by-URL form out of the header into the Home tab,
  right above the subscriptions list.
- Removed the Diagnostics modal button; its service/API status and maintenance actions
  (restart down services, cap file limits, fuzz intervals, block video) now live in a
  Health & Actions panel at the top of the System Metrics tab, renamed "Status", which was
  also reorganized with several more image gallery slots throughout.
- Subscription group headers now show the same downloaded/queued count pill as individual
  rows, summed across the group.
- Media tab: the "Connected" tags section now unions siblings/parents/children across every
  active search tag (not just the last one added), sorted by whole-library reference count,
  with a preview of how many results adding each one would produce. The current result count
  is now shown next to the active search. The shape filter (Square/Portrait/Landscape) now
  reruns the actual Hydrus search with a ratio predicate instead of hiding thumbnails
  client-side, so switching shapes fills a full page instead of pruning an already-loaded one.
- Tag Map redesigned as an actual hub-and-spoke diagram - the searched tag centered with
  ancestors/descendants fanning out in rings connected by lines - instead of a nested list.
- Built out the Bulk Tagging and Tag Migration tabs: add/remove a tag across every file
  matching a search, or migrate (add new + remove old) a tag across every file that has it,
  both reachable directly from the Tag Map. Note: this edits files that exist right now, not
  a real Hydrus tag sibling/parent relationship - the Hydrus Client API still has no write
  endpoint for those, so a permanent redirect still has to be set up in Hydrus itself.

## 1.2.0
- The version pill in the top bar is now itself the update-checker - click it to check, and
  it turns into a "reload to update" or "couldn't check, retry" pill in place (no separate
  button next to it anymore).
- Fixed the Changelog tab not scrolling internally - long changelogs now scroll within the
  panel instead of growing the whole page.
- Subscriptions: replaced the active/paused/due-now status pills with a single always-visible
  "downloaded/queued" count pill per subscription (grayed out at 0/0); removed the "Ungroup"
  button, so the grouped-by-source view is now the only view.
- Media tab: the siblings/parents/children of your most recently searched tag now show as a
  "Connected" section right beside your active search pills (previously siblings/parents only,
  on their own line below).
- Tag Relations: added a "Tag Map" view - a family-tree display of a tag's ancestors and
  descendants with siblings shown inline, with a selectable 1-4 level expansion depth and
  click-to-recenter on any tag in the tree.
- System Metrics tab: all of its polling (host/GPU stats, queue graph, network connections,
  activity feed, etc) now only runs while that tab is actually open, instead of continuously
  in the background regardless of which tab is selected.
- Removed the mascot images and all related UI/CSS - the mascot art assets were dropped from
  the project.

## 1.1.0
- Renamed the app to Undertow throughout the UI and consolidated the version number,
  update-check button, service status markers, and the pink info ribbon into the top bar.
- Added this changelog, a Changelog tab to read it in-app, and a "new version" indicator
  that shows the first time the dashboard loads after an update.
- Added a click-to-check-for-updates button next to the version number; when an update is
  available it turns into a "reload to update" button.
- Media tab: added Square / Portrait / Landscape card-size toggles so the grid only shows
  the shapes you want, with each image fit to its nearest size instead of a fixed square crop.
- User images with a transparent background now render directly on the page (no card,
  border, or background) instead of being boxed like a photo.
- Faster media tab: thumbnails are now requested with a bounded concurrency and the search
  grid paginates more aggressively, so large result sets no longer stall the first paint.

## 1.0.0
- Initial Python port of the PowerShell Hydrus Pipeline launcher: `undertow/` package,
  the Flask-based web dashboard, and the Textual console UI fallback.
