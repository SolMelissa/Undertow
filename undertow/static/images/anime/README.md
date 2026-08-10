# Girly-mode image pool

Drop **any number of your own images, any filenames, all in this one folder** to fill the
picture spots scattered across the girly dashboard - a Home tab banner, a big feature spot on
Home, a section-divider banner on System Metrics, and a portrait spot beside Network
Connections (see `templates/index.html`'s `#girly-view`).

Every spot is large (500px or more on its shortest side) - there are no icon/avatar/thumbnail
spots anymore, so nothing here ever gets shrunk down to decoration size.

Supported formats: `.png`, `.jpg`/`.jpeg`, `.webp`, `.gif`.

**There's no sub-folder to sort into.** Every spot on the page just asks for "an image shaped
like this" (its target aspect ratio) and the backend (`_anime_pick()` in `undertow/webui.py`)
picks whichever image in this one folder fits that shape best - a tall portrait photo will
naturally get picked for the tall spot, a wide landscape shot for the banner spot, a roughly
square one for the big square spots. You never have to decide "which folder does this go in."

If nothing in the folder is shaped closely enough for a given spot, that spot shows text telling
you what aspect ratio would fill it instead of cramming in a badly-cropped image. Spot shapes
currently on the page:

| Shape needed | Roughly |
| --- | --- |
| square, ~1:1 | e.g. 520×520 or larger - the big feature spots |
| wide banner, ~3:1 | e.g. 1200×400 - section-divider banners |
| tall portrait, ~1:1.8 | e.g. 600×1080 - beside Network Connections on the System Metrics tab |

**Selection re-rolls on every page load** (not cached) - the whole page, i.e. every time you
open or refresh the dashboard, not just once per app run. With several images in the pool,
lower-usage images are favored over ones shown a lot already, so a big folder settles into
roughly even rotation across all your pictures instead of the same few always winning.
