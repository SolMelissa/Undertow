# Kawaii theme mascots

Drop your own art in here to change the characters shown in the pastel/kawaii theme. Keep the
**filenames exactly as listed** (extension can change - `.svg`, `.png`, or `.webp` all work,
just update the `<img>` `src` in `templates/index.html` if you change the extension) and the
dashboard picks up your replacement automatically on next page load. No code changes needed.

| File | Where it shows up | Suggested size |
| --- | --- | --- |
| `header.svg` | Small icon next to the "UNDERTOW" title in the top bar | square, ~64x64 |
| `corner-companion.svg` | Floating character docked in the bottom-right corner of the whole page (has a gentle idle bounce animation via CSS - any art works, but something with a clear "front-facing" pose reads best while bouncing) | square, ~200x200 |

Placeholder art shipped here is original, hand-drawn-in-code SVG (simple chibi line art, fully
clothed, no copyrighted characters) — safe defaults, not final art. Replace either file with
your own character(s) whenever you like.

Transparent background strongly recommended (PNG/WebP with alpha, or SVG with no `<rect>`
background) - both slots sit on top of colored panels/gradients, not a plain background.
