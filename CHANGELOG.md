# Changelog

All notable changes to Undertow are tracked here, one section per version. Newest first.

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
