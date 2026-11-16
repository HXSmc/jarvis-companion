---
name: svg-drawing
description: Write a clean, labelled SVG diagram (a device with its parts, a process, a cross-section, a simple figure or icon) that passes the Jarvis Companion SVG sanitiser. Use when a session or the Mac overlay's drawing path must draw something as SVG.
---

# SVG drawing

The overlay shows a drawing only after `common/svgsafe.py` has rebuilt it from an allow-list.
Anything outside the list is removed, and some mistakes refuse the whole drawing. Write inside
the list from the start.

## Allowed

- Elements: `svg g defs title desc rect circle ellipse line polyline polygon path text tspan
  marker linearGradient radialGradient stop use`.
- Attributes: geometry (`x y x1 y1 x2 y2 cx cy r rx ry width height points d transform dx dy`),
  paint (`fill stroke stroke-width stroke-linecap stroke-linejoin stroke-dasharray opacity
  fill-opacity stroke-opacity stop-color stop-opacity offset`), text (`font-size font-weight
  font-family text-anchor dominant-baseline`), markers (`markerWidth markerHeight refX refY
  orient marker-start marker-mid marker-end`), `gradientUnits`, `id`, and `viewBox`.
- `url(#id)` only (a gradient or marker defined in the same file). `<use href="#id">` only.

## Never

`style` (attribute or element), `class`, `<image>`, `<foreignObject>`, `<script>`, `on*`
attributes, links (`<a>` is unwrapped), `filter`, `mask`, `pattern`, `clipPath`, fonts, animation,
external or `data:` URLs, a DOCTYPE or ENTITY (the drawing is refused). No web fonts: use the
local stack (`-apple-system, Helvetica, Arial, sans-serif`).

## Frame and layers

- One root `<svg viewBox="0 0 W H">`, no `width` or `height`. Size the viewBox to the content
  plus about 30 units of padding on every side. The overlay uses `0 0 1200 800`.
- A short `<title>`. Group parts in named `<g id="...">` layers.
- Back to front: background, connector arrows, opaque mask rects (the same box as each
  component, so lines never show through), component boxes, then text.
- Text size 24 or more (smaller sizes are raised to 22).

## Labels

- Numbered callouts: a filled circle with its number and a thin leader line to the part. At most
  12, numbered in the order of the legend.
- Keep the legend outside every component box; grow the viewBox height to fit it.

## Diagrams, figures, icons

- Process: boxes left to right with fixed gaps; one `marker` with `orient="auto"` reused by every
  arrow; draw the lines first so the boxes sit above them.
- Cross-section: an outline, then layered regions, each with a callout.
- Colours from a small named palette, not a new hex per shape. On the dark card: outlines
  `#FFFFFF`, secondary `#9A9A9A`, fills `#2A2A2A`, alert and callouts `#FF3B30`.
- Figures: build part by part (torso, limbs, head) and check after each part; round line caps,
  and a circle at every joint drawn after the limbs.
- Icons: a 24 x 24 grid, 2 px stroke, round caps and joins, `fill="none"`, `stroke="currentColor"`.

## Check loop

1. Write the SVG to a file in your job folder.
2. From the repository root, run the sanitiser on it:

   ```
   python3 -c 'import sys; from common import svgsafe; r = svgsafe.sanitize(open(sys.argv[1]).read()); print(r.reason, r.kept, r.removed, r.attrs_removed); sys.exit(r.reason != "ok")' drawing.svg
   ```

   `ok` with `removed 0` and `attrs_removed 0` means nothing was cut. Anything else: read the
   reason, fix the SVG, run it again.
3. Render it to a PNG with a headless browser you already have and look at it. Fix overlaps,
   unreadable text and callouts that point at nothing.
4. Passes per kind: icon 1-2, diagram 2-3, scene 3-5. After a sanitiser failure, retry at most
   2 times, then stop and say the drawing failed.

Never install packages or run a script that downloads anything while drawing.
