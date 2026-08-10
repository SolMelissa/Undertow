# Changelog

All notable changes to Undertow are tracked here, one section per version. Newest first.

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
