# Girly-mode image gallery

Twelve+ spots on the girly dashboard show your own pictures instead of placeholder art. Just
drop image files into the folders below - **any number of files, any filenames** - and refresh
the dashboard. No code changes, no restart needed.

| Folder | Shown at (on-page size) | Where it appears |
| --- | --- | --- |
| `icon/` | ~60x60, square | small stickers scattered around the header/sections/footer |
| `avatar/` | ~110x110, circular crop | hero banner accent |
| `sm/` | ~160x160, square | small cards near the subscriptions list |
| `md/` | ~220x220, square | sidebar cards near the subscriptions list |
| `lg/` | ~320x320, square | big feature spot at the bottom of the Home tab |
| `wide/` | ~480x150, wide banner | section-divider banners |
| `tall/` | ~200x300, portrait | beside Network Connections on the System Metrics tab |

Supported formats: `.png`, `.jpg`/`.jpeg`, `.webp`, `.gif`, `.svg`.

**Selection is random every time the page loads** (not cached), so with several files in a
folder you'll see a different one each time you open the dashboard. If a folder has more than
one image, ones whose own aspect ratio is closer to that slot's target ratio (see the table
above) get picked more often than badly-mismatched ones - a portrait photo dropped into `wide/`
can still show up, just less frequently than a proper landscape shot, since the on-page CSS
crops to fill the spot (`object-fit: cover`) and a bad mismatch would crop away most of the image.

Each folder ships a `default.svg` placeholder - leave it there (it's only ever shown when the
folder has no other images in it) or delete it once you've added your own.
