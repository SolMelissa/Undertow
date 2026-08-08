# Girly-mode image pool

Drop **any number of images, any filenames, all in this one folder** to fill the picture spots
scattered across the girly dashboard (hero, sticker rail, section banners, sidebar cards, the
big feature spot on the Home tab, the portrait spot on the System Metrics tab, footer stickers -
14 spots total, see `templates/index.html`'s `#girly-view`).

Supported formats: `.png`, `.jpg`/`.jpeg`, `.webp`, `.gif`.

**There's no sub-folder to sort into.** Every spot on the page just asks for "an image shaped
like this" (its target aspect ratio) and the backend (`_anime_pick()` in `undertow/webui.py`)
picks whichever image in this one folder fits that shape best - a tall portrait photo will
naturally get picked for the tall spot, a wide landscape shot for the banner spots, a roughly
square one for everything else. You never have to decide "which folder does this go in."

If nothing in the folder is shaped closely enough for a given spot, that spot shows text telling
you what aspect ratio would fill it instead of cramming in a badly-cropped image. Spot shapes
currently on the page:

| Shape needed | Roughly |
| --- | --- |
| square, ~1:1 | e.g. 200×200 up to 700×700 - used the most (header stickers, hero accent, sidebar cards, big feature spot) |
| wide banner, ~3.2:1 | e.g. 960×300 - section-divider banners |
| tall portrait, ~2:3 | e.g. 500×750 - beside Network Connections on the System Metrics tab |

**Selection re-rolls on every page load** (not cached) - the whole page, i.e. every time you
open or refresh the dashboard, not just once per app run. With several images in the pool,
lower-usage images are favored over ones shown a lot already, so a big folder settles into
roughly even rotation across all your pictures instead of the same few always winning.
