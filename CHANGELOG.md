# Changelog

All notable changes to Undertow are tracked here, one section per version. Newest first.

## 1.14.16
- Patch: Fixed TagRank hidden tags marker setup script - was searching inbox for the imported
  file instead of using the file hash returned by the import API. Also fixed missing
  `is_filtered_tag` import in tagrank/pool.py that crashed the TagRank server on startup.

## 1.14.15
- Patch: Add TagRank hidden tags marker setup script to Scripts tab - one-time setup to import
  the marker image into Hydrus and tag it with service:tagrank for use with sync_hidden_tags_to_marker.

## 1.14.14
- Patch: Add TagRank utility scripts to the Scripts tab - dashboard demo, E2E test, and hidden
  tags sync-to-marker script. All three use adjusted imports to locate TagRank project modules.

## 1.14.13
- Patch: TagRank's new file cache (1.14.12) was only saved once, after every new file's Hydrus
  metadata had been fetched - so killing the process mid-build (a crash, a forced daemon
  restart, closing the TagRank tab mid-build) lost that entire run's progress and forced the
  next build to redo the whole fetch from scratch. Fixed (`Pool-Limiter` commit f48cabf): the
  cache now checkpoints every 5000 newly-fetched files, so an interruption only costs the
  unsaved tail, verified with a simulated interrupted-then-resumed build. Also made the cache
  write atomic (temp file + rename) - now that saves happen far more often, an interruption
  during the write itself could otherwise have corrupted the cache and lost every earlier
  successful run's data too. No Undertow code changed.

## 1.14.12
- Significant: On the real 500k-file library this was tested against, nearly every file carries
  at least one rated tag, so 1.14.11's OR-batched search (`Pool-Limiter` commit 31c99a0) still
  resolved to almost the whole library - it didn't actually bound the per-file Hydrus metadata
  fetch cost. Fixed properly this time (`Pool-Limiter` commit 73fa367): TagRank now caches
  per-file metadata to `data/tag_index_file_cache.json` between runs, keyed by file_id, so a
  build only fetches metadata from Hydrus for files not already cached - typically just what's
  been imported since the last run - instead of paying the full per-file cost on every startup.
  `refresh_index()` gained an explicit cache-bypass path for when Hydrus-side re-tagging is
  known to have made cached data stale. Also caught and fixed a real regression from 1.14.11:
  its own test suite had silently broken (the OR-predicate shape change wasn't covered by a
  test run before pushing) and has now been fixed and verified green. No Undertow code changed.

## 1.14.11
- Patch: TagRank's tag-index build (`Pool-Limiter` commit 8340bb1, cited in 1.14.9/1.14.10) was
  correct but wasteful on a large library: it fetched Hydrus metadata for every file in the
  library (500k+ files on this install), most of which don't carry any rated tag. Superseded by
  commit 31c99a0: batches candidate tags (256 per request, verified against the bundled Hydrus
  client_api docs' documented OR-predicate syntax) into a handful of "any file carrying any of
  these tags" searches, unions the results, then runs the existing chunked metadata fetch over
  just that set - bounds the cost to files that could actually match a rated tag, not the whole
  library. No Undertow code changed.

## 1.14.10
- Patch: corrected the 1.14.9 changelog entry to point at TagRank's actual final fix
  (`Pool-Limiter` commit 8340bb1, a whole-library-scan rewrite) instead of the intermediate
  8-worker parallelization commit it originally cited, which was superseded before this entry
  was even written. No Undertow code changed.

## 1.14.9
- BugFix: The real cause of the TagRank tab taking 30+ minutes to load (~1760 rated tags) was
  on TagRank's own side, not Undertow's - `tagrank/tag_index.py`'s `_build_index` ran one
  `search_files()` Hydrus round trip per rated tag, fully serial. A first pass (TagRank repo,
  `Pool-Limiter` branch, commit 83de757) parallelized those searches across 8 workers, but that
  was still O(rated tags) separate whole-library searches and stayed in the minutes range.
  Superseded by commit 8340bb1: one `system:everything` search plus the existing chunked
  `/get_files/file_metadata` fetch (which already returns each file's full tag list), inverted
  locally into the tag -> file_ids mapping - O(1) search + O(library size / 1000) metadata
  calls, independent of how many tags are rated. Seconds instead of minutes.
- Patch: the console-log panes added in 1.14.8 (TagRank's starting/error screens) were capped
  at a fixed 220px and not resizable, too short to usefully read a real startup log or
  traceback. Bumped the default height to 420px (up to 80vh) and made them
  vertically resizable (`resize:vertical`) across all three TagRank console views
  (`tagrank_inner.html`, `tagrank_server_starting.html`, `tagrank_starting.html`).

## 1.14.8
- BugFix: TagRank's headless API server subprocess (`main.py --serve`, `tagrank_client._start_process`)
  sent its stdout/stderr straight to `DEVNULL`, so a startup failure (missing dependency, the
  8420 port already bound, an unhandled exception) left zero diagnostic trace anywhere -
  Undertow's own UI just showed a generic "TagRank's API didn't come up in time" message
  forever, indistinguishable from "the tab is simply broken." Investigated by tracing the whole
  tab-open flow end to end (header pill status, the `hx-trigger="load"` wiring shared by every
  tab in `index.html`, the `/partials/tagrank` -> `/tagrank/server-poll` startup poll loop) and
  confirming none of those were at fault - the gap was purely a missing log. Now captures the
  server subprocess's stdout/stderr to `tagrank-server-stdout.log`/`tagrank-server-stderr.log`
  (mirroring the GUI launcher's existing `tagrank-launch-*.log` pattern) and tails them inline
  on both the "Starting TagRank's API..." screen and the eventual timeout error, so a real
  startup failure is visible instead of silent.

## 1.14.7
- BugFix: TagRank's comparison-GUI launcher (`tagrank_client.launch_gui`, wired to the
  `/tagrank/launch` route) was pure fire-and-forget - no handle was ever kept, so every tag-pill
  click spawned a brand-new PySide6 subprocess (each eagerly rebuilding its own Hydrus
  tag/similarity index) on top of whatever comparison window was already running, with no
  cleanup path at all. Repeated launches across a session accumulated untracked `python.exe`
  processes, which is the likely source of Undertow's growing RAM footprint and stranded
  processes left running after the dashboard closes. Now tracks the last-launched GUI process,
  terminates (graceful, then kill) any still-running one before starting a new one, and reaps it
  via `atexit` on Undertow's own shutdown, mirroring the existing pattern already used for
  TagRank's headless API server. Investigated by first auditing every subprocess spawn site in
  `undertow/` (daemon, Hydrus, systray, VeraCrypt, scripts_runner, TagRank server/GUI) - the
  daemon/Hydrus/systray processes are intentionally *not* tied to a stored handle (they're
  looked up fresh via `psutil` instead) so they survive an Undertow crash by design; TagRank's
  GUI launcher was the one genuine untracked leak.

## 1.14.6
- BugFix: TagRank comparer's win-probability gauge was pinned to ~0%/100% on nearly every real
  pair instead of showing a calibrated confidence. `photo_score` and each tag's `score` are both
  TrueSkill's raw (mu - 3*sigma) scaled ×100 by TagRank's own `MMR_SCALE` (see `tagrank/rating.py`
  and the rating-details contract), but `_tagrank_compare_win_probability`'s logistic divisor of
  25 was calibrated for the *unscaled* number - so a "typical" raw-TrueSkill gap of 10-20 was
  actually landing as 1000-2000 and instantly saturating the curve. Found and fixed by actually
  driving a live comparison session end-to-end and inspecting the real gauge output (55%/45%,
  48%/52%, 31%/69% on live pairs afterward, instead of every pair reading ~0%/100%).
- Verified end-to-end against a live Undertow + TagRank + Hydrus session (not just template
  dry-runs): confirmed TagRank's badge store (`data/badges.json`) already holds 145 real earned
  picture badges, confirmed `GET /files/{id}/rating-details` resolves one correctly (rarest
  badge + icon/difficulty), and confirmed the comparer actually renders that badge as a pill
  over the correct image when that exact file lands in a live pair - this had never been
  confirmed with real data before, only with synthetic Jinja renders.

## 1.14.5
- BugFix: Tag Cleanup wizard's tag-service pickers (source/dest) offered "all known tags" as a
  choice, but Hydrus's `add_tags` API always 400s trying to add/delete tags on that virtual
  combined domain (same underlying issue as 1.14.3's file-domain fix, just on the tag-service
  side). Excluded it from the picker so the wizard's apply step no longer fails every batch with
  "Submitted changes for 0/N file(s)".

## 1.14.4
- TagRank comparer redesign: per-image tags moved above the picture and are now clickable
  (hx-post `/tagrank/compare/start`, same as the main "sorted by rating" pill list) instead of
  static text below it; the win-probability display is now a speedometer-style SVG gauge whose
  needle deflects left/right toward the favored side (further from center = more confident)
  instead of a two-tone bar; comparison images now sit in a fixed-height, centered pane
  (`.tagrank-comparer-image-pane`) so vertical space stays consistent between pairs regardless
  of either picture's aspect ratio, instead of the pane's height following the image; and the
  Filter tag/Min files/Namespace/service-picker panel is now full-bleed (same trick the
  comparer's images already used) so its background lines up with the full-viewport-width
  images below instead of stopping short at the dashboard's centered column.
- Picture/tag badges are now real end to end: implemented TagRank's `GET
  /files/{file_id}/rating-details` (see `tagrank/plans/undertow-comparer-rating-details.md`),
  which Undertow's comparer was already wired against but always got null back from since the
  route never existed server-side - the rarest-badge pill on each image and the per-tag badge
  stars now reflect real earned badges instead of always being empty.
- The idle "Click a tag pill above to start comparing" comparer card stays hidden (not just
  emptied) until a pill is actually clicked or a result/error comes back - if this is still
  visible after updating, do a full app restart (the version pill's Restart, or relaunching
  Undertow), not just a page refresh, since the backend template logic changed.

## 1.14.3
- BugFix: Tag Cleanup wizard's file-domain picker offered "all known files" as a searchable
  option, but Hydrus's `search_files` API always 400s on that virtual combined domain (it's not
  a real, searchable file service like "all local files"/"my files"). Excluded it from the
  picker so the wizard's search no longer fails with "Could not reach Hydrus... 400 Client Error".

## 1.14.2
- TagRank tab polish pass: the embedded comparer card ("Click a tag pill above to start
  comparing") now stays hidden until a pill is actually clicked, instead of sitting empty at
  the top of the tab; the File service/Tag service pickers moved into the same row as
  Filter tag/Min files/Namespace instead of a separate row below; the "sorted by rating" tag
  pill list now labels its Namespaced/Unnamespaced groups instead of relying on a barely-visible
  dashed divider, and both the score-color pill borders and namespace-hued tag text got lighter/
  higher-contrast HSL values so they read against the dark girly theme; and the Rating History
  section's "Not fetched yet" placeholder card is gone - just the "Fetch graphs" button until
  it's clicked.

## 1.14.1
- Fixed the "restart"/update pill leaving Undertow permanently stuck, most visibly right after
  clicking it: `version.restart_process()` re-execs the backend via `os.execv`, which on Windows
  actually spawns a brand-new OS process and lets the old one exit - it's not a true in-place
  restart, so the new process has to redo the whole startup sequence (Hydrus/hydownloader
  checks, subscription sync) before it's listening again. The version pill's own JS used to just
  blind-reload the page after a fixed 3 seconds; if that landed before the new process was
  ready, it hit a dead connection-refused page with nothing left to retry it - "closes and never
  reopens" in the WebView2 app frame. It now polls a new side-effect-free `/version/ping` route
  every second (up to 90s) and only reloads once something actually answers, with a manual
  retry pill if it genuinely times out.
- Fixed 1.13.9's own port-pileup fix being wrong in the case that actually matters most: it
  treated "the port is already bound" as "a healthy server is already running there" and skipped
  starting a new backend entirely - which is backwards when the existing listener is dead. Live-
  reproduced on this machine: a `python.exe` backend was force-killed, and Windows left its
  listening socket permanently stuck in a `LISTEN`/`CLOSE_WAIT`-forever state with no owning
  process at all (`Get-Process` found nothing, yet new connections kept getting silently
  swallowed) - a state no process-level cleanup can fix, since there's no process left to signal
  or kill, and one that can persist indefinitely. `run_webui()` now verifies actual HTTP
  liveness (not just that the port is bound) before deferring to an existing server; if it's
  bound but dead, it uses `psutil` to find and kill whatever process still owns it, and if that's
  not possible (the kernel-orphan case above), it now falls back to the next free port instead
  of staying wedged on one that can never bind again short of a reboot.
- The WebView2 app frame (`Undertow.exe`, `launcher/Program.cs`) no longer hardcodes port 8765
  for the whole app lifetime: it parses the real port the backend reports in its startup output
  and polls/navigates to that instead, so the port-fallback above actually reaches the window
  instead of the launcher polling a dead port forever. Verified end-to-end against the live
  stuck-port repro above: the launcher correctly detected the fallback to 8766 and loaded the
  dashboard normally.

## 1.14.0
- TagRank comparer now spans the full window width (breaks out of the dashboard's centered
  1400px column) instead of being capped at 70vh, so comparison images render as large as the
  viewport allows.
- Reached feature parity with TagRank's native comparison window in the embedded comparer and
  tag pill list: badge pills on images (the picture's rarest badge, colored by difficulty tier),
  per-tag badge-count stars, domain-colored tag text (grey for unnamespaced tags), TrueSkill
  photo-score display per side, and a win-probability prediction bar. The score/badge data comes
  from a new TagRank API endpoint (`/files/{id}/rating-details`) that doesn't exist yet - wired
  up end-to-end and degrades gracefully (comparer still works, just without the score/badge
  extras) until TagRank implements it; contract written to
  `tagrank/plans/undertow-comparer-rating-details.md`. Win-probability itself is Undertow's own
  logistic formula - no calibrated win-probability calculation exists in TagRank to port.
- Fixed DB Search queuing successive requests as you type instead of replacing them: typing now
  filters the already-displayed pool client-side immediately, waits 1500ms of no typing before
  firing a real server re-search, and aborts any in-flight search outright (via
  `AbortController`) the moment typing resumes, instead of racing it against a stale-response
  counter.
- Tag pill list and comparer tag rows now sort namespaced (`domain:tag`) tags before
  unnamespaced ones, with a visual divider between the two groups.

## 1.13.12
- Fixed TagRank DB Search's namespace toggle: it was checking whether *any* tag on a matching
  file had a namespace, instead of whether the candidate tag itself did, so "Namespaced" and
  "Unnamespaced" filtered essentially at random. Fixed in TagRank's `pool.py`.
- Fixed filtered searches under-reporting file counts (sometimes showing 0) for files whose
  Hydrus metadata never made it into TagRank's in-memory index: those files used to be dropped
  from every filtered count unconditionally, even when the active filters never needed their
  metadata at all. Fixed in TagRank's `pool.py`; added a regression test.
- Removed the DB Search filter bar's "Archive status" toggle and "Clear" button (declined by
  the client) and added a small spinner next to the search-status text so a live search has a
  visible "loading" indicator while it's in flight.

## 1.13.11
- TagRank tab's Rating History charts are now fetched on demand via a "Fetch graphs" button
  instead of always being rendered. Rendering them is real server-side work (TagRank builds
  matplotlib figures and ships them as base64 PNGs) that used to happen unconditionally on
  every tab open, every settings save, and every GUI-launch poll - regardless of whether the
  charts were ever scrolled to. Split the graphs markup out into its own
  `tagrank_graphs.html`, swapped into a `#tagrank-graphs-container` placeholder by the new
  `/tagrank/graphs` route.

## 1.13.10
- TagRank tab: DB Search is now live instead of a separate button. Every filter-bar change
  (tag text debounced ~150ms, namespace/archive/service toggles and the min-files stepper
  immediately) re-queries TagRank's in-memory tag index and swaps in just the tag-pill list
  (`#tagrank-results`), leaving the filter bar itself untouched so the field being typed into
  keeps focus and cursor position across requests. Split the pill-rendering macro out into its
  own `tagrank_pill_macro.html` so both the full tab render and the new results-only search
  response can share it.
- The search route also stopped re-fetching the Rating History graphs and settings on every
  search - those don't change with a tag search, and graphs in particular renders matplotlib
  figures server-side, which was most of what made "DB Search" feel slow even after the
  in-memory tag index landed.

## 1.13.9
- Fixed the WebView2 app frame (`Undertow.exe`) getting permanently stuck on "web dashboard
  running at ... this console window will now hide" instead of loading the dashboard.
  Werkzeug's dev server sets `allow_reuse_address`, which on Windows lets a *second* process
  bind and `LISTEN` on a port an earlier process is still actively serving - no bind error,
  just several `python.exe` backends from past launches (never cleanly killed) silently piling
  up on port 8765 at once, with the OS routing each new connection to one of them essentially
  at random. The WebView2 launcher's own dashboard-readiness poll, or a plain browser tab,
  could get load-balanced onto a stale/hung process from a previous run and hang forever
  waiting for a reply that a perfectly healthy process sitting right next to it would have
  answered instantly. `webui.run_webui()` now probes the port with a plain (non-reuse) socket
  bind before starting the Flask thread - which Windows *does* refuse if anything is already
  listening - and reuses that existing server instead of stacking another one on top of it.

## 1.13.8
- Fixed the TagRank tab's DB Search always spinning "Searching..." forever without failing or
  returning results: TagRank's `/search-options/filtered` narrowed every rated tag with a fresh,
  live Hydrus search per tag on every request (the same per-request cost that already forced the
  plain `/search-options` endpoint's timeout up to 180s) - on a real library this could run for
  minutes with no feedback. TagRank now builds an in-memory tag/file index once, eagerly, at
  server startup (`tagrank/tag_index.py`), and both search endpoints answer from that cache
  instead of re-querying Hydrus per tag per request; only judging a comparison still touches
  Hydrus/the rating store directly. Undertow's startup wait (`tagrank_client.py`) was bumped from
  75s to 240s to cover that one-time upfront cost.
- Fixed a broken `hx-vals` attribute on every tag pill in the TagRank tab rendering stray
  `= 1 ? '1' : '0'}">`-shaped text above each pill: `{{ opt.tag|tojson }}` was interpolated inside
  a double-quoted HTML attribute, and Flask's `tojson` filter (meant for `<script>` blocks) emits
  its own unescaped double quotes, silently truncating the attribute mid-value. Switched the
  attribute to single quotes.
- Removed the TagRank filter bar's six band/slider controls (Similarity, Score, Resolution x2,
  Rating count, Date added) entirely, per client feedback that they weren't functional enough to
  keep; DB Search's namespace/archive/service filters and the always-live tag-text/min-files
  filters are unaffected.
- Removed the redundant "Click a tag pill to start comparing" instruction card from the top of
  the TagRank tab.

## 1.13.7
- Tag Cleanup wizard (`scripts/tag_cleanup.py`) now asks for the two length thresholds that were
  previously hardcoded on `Config`: the minimum full raw-tag length worth parsing at all
  (`min_process_tag_length`, default 35) and the minimum length for an individual word to survive
  as its own tag (`min_token_len`, default 2). Both are saved/reused across runs the same way the
  existing namespace and truncation-drop prompts already are.

## 1.13.6
- Fixed the Inbox Triage report (`scripts/inbox_triage_report.py`) never finishing on large
  libraries: `hydrus_client.get_file_metadata()` unconditionally requested full tag data
  (`include_service_keys_to_tags`) for every file even though the report only reads
  `time_imported` from `file_services`, which Hydrus returns regardless. On a ~460k-file inbox
  this meant Hydrus computing and serializing every tag for every one of ~1,800 chunked API
  calls - the report never completed. Added an `include_tags` flag (default True, preserving the
  three existing single-file callers' behavior) and set it False for the bulk scan; same real
  data, several minutes instead of never finishing.
- Fixed `subscription_health_report.py` labeling every subscription "?" instead of its real name
  in all three of its sections - hydownloader subscription objects have no `name` field, only
  `keywords` (the same field the dashboard itself displays), but the script was reading
  `s.get("name", "?")` everywhere.
- Fixed the Queue Report (`scripts/queued_urls_report.py`) always crashing with a 500:
  `api_client.get_queued_urls()` sent a bodyless POST, and hydownloader's own
  `route_get_queued_urls` does `'from' in bottle.request.json` with no null-check - the same
  class of bug `get_subscriptions()` already works around by sending `{}`. Applied the same fix.
- All 8 Reports-group scripts now verified against live Hydrus/hydownloader data (204 real
  subscriptions, 460k+ inbox files) through the actual Scripts tab run/poll mechanism, not just
  read for correctness.

## 1.13.5
- Fixed two unhandled-exception crashes in the subscriptions UI from unvalidated numeric form
  input: quick-add's "hours" field (`/subscriptions/quick-add`) and edit-subscription's
  "max_files_initial"/"max_files_regular" fields (`/subscriptions/<id>/edit`) both called
  `float()`/`int()` on raw form text with no `try`/`except`, unlike every sibling route that
  parses the same kind of field - typing anything non-numeric (or pasting garbage) 500'd the
  request instead of showing a friendly validation message.
- Fixed a threading race in `tags.py`: `load_tags()` returned the live module-level cache dict
  instead of a copy, and `set_tags_for`/`remove` then mutated that same dict in place before
  writing it back. Since the web dashboard runs Flask `threaded=True`, one request iterating the
  cached dict (e.g. the tag-filter dropdown, or the subscriptions table rendering each row's
  tags) could race a concurrent edit/delete mutating it, raising `RuntimeError: dictionary
  changed size during iteration` or handing back a half-mutated tag map.

## 1.13.4
- Fixed Windows toast notifications being silently broken: the WM_DESTROY handler registered
  on the throwaway notification window returned `None` (a bare `lambda: win32gui.
  PostQuitMessage(0)`, whose own return value is `None`) instead of an int LRESULT. pywin32
  can't marshal `None` from a WNDPROC callback and raised a TypeError from inside the win32
  dispatch on every single toast (`send_windows_toast()` destroys its window on every call, so
  this fired every time) - invisible from the caller's side since it happened inside the
  callback, not in `send_windows_toast`'s own try/except.

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
