# Changelog

All notable changes to Undertow are tracked here, one section per version. Newest first.

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
