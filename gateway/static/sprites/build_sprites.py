#!/usr/bin/env python3
"""Pixel-art role sprite set for the Jarvis Companion Floor + Sessions board.

Single source of truth for this directory: every file here (per-role SVGs,
the sprites.svg symbol sheet, PNGs at 1x/2x/3x, sheet.png, roles.json,
preview.html) is generated from the pixel grids defined below. Re-run after
any edit to this script -- nothing else in this directory is hand-maintained.

    python3 build_sprites.py

Colours are lifted from gateway/static/floor.html's ROLES map (top/hair/skin
per role) so this cast reads as the same characters already on the Floor,
with three deliberate overrides from the design-review FIX pass: GLM_WORKER
(off orange, was colliding with BUILDER) and EXPLORER (off its floor.html
tan, was colliding with SPEC_WRITER) use a different shirt hue than
floor.html's ROLES map; floor.html itself is untouched by this script, so
that divergence is real until someone updates floor.html to match (follow-up,
outside this script's file scope).

This is a separate, higher-fidelity 32x32 asset set with an auto outline and
silhouette-breaking props -- gateway/sprites.py's existing 16x18 server-
rendered, no-outline sprites (served live at /central/sprites/{role}.svg)
are a different artifact for a different purpose (also drives the iOS
Sessions board via its .json grid route). Wiring one to replace the other,
if ever wanted, is follow-up work outside this script's job.

Review fix pass (2026-10-16): props were originally drawn across the full
chest-to-knee band (grid rows ~10-27). A scratch render of floor.html's own
drawDesk() showed the desk box is painted *after* the avatar and its front
face covers the bottom ~35-40% of the avatar's destination rect (painter's-
algorithm overlap, not something a sprite's own proportions can fix) -- so
every prop below is now bounded to PROP_TOP..PROP_BAND_BOT (roughly chest/
head height) to stay clear of that line. Legs/feet still sink behind the
desk, same as any person standing behind a desk; that part is fine as-is.
"""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

GRID = 32  # both axes -- keeps 1x/2x/3x PNG scaling (32/64/96px) pixel-exact

# ---- shared palette (limited, reused across roles on purpose) -------------
SKIN = "#e8c39e"
# Was #121318 -- darker than the Floor's own --bg (#17181c), so the outline
# vanished against the app's real (dark-only) background. Lightened to a
# visible rim instead (review item 6).
OUTLINE = "#4a4f5c"
# Was #2a2c33 -- an exact match for the Floor's --line token, so default
# trousers read as "part of the floor grid" rather than clothing (review
# item 6). Shifted off it, still a plain dark trouser colour.
PANTS_DEFAULT = "#23252c"
SHOE = "#15161a"
PAPER = "#e8e2d4"       # Planner paper (ARCHITECT / SPEC_WRITER) only
PAPER_COOL = "#dce8de"  # VERIFIER's checklist -- cooler/greener so it doesn't
                        # cluster with the Planner paper at a glance (review item 5)
INK = "#1c1d22"
METAL = "#9aa0ad"
GOLD = "#c9a227"
CYAN = "#22d3ee"        # reserved for OWNER only (review item 9)
EMERALD = "#34d399"

ROLE_ORDER = [
    "ARCHITECT", "SPEC_WRITER", "RESEARCHER", "REVIEWER", "BUILDER", "SCOUT", "EXPLORER",
    "FIXER", "VERIFIER", "SECURITY", "DESIGNER", "MARKETER", "WORKER", "GLM_WORKER", "OWNER",
]

LABELS = {
    "ARCHITECT": "Planner", "SPEC_WRITER": "Planner", "RESEARCHER": "Researcher",
    "REVIEWER": "Reviewer", "BUILDER": "Builder", "SCOUT": "Scout", "EXPLORER": "Explorer",
    "FIXER": "Fixer", "VERIFIER": "Verifier", "SECURITY": "Security", "DESIGNER": "Designer",
    "MARKETER": "Marketer", "WORKER": "Worker", "GLM_WORKER": "GLM worker", "OWNER": "Owner",
}

# role -> (shirt, hair, skin, pants). Matches floor.html's ROLES map exactly,
# except GLM_WORKER and EXPLORER's shirt (see module docstring).
BODY = {
    "ARCHITECT": ("#a78bfa", "#cfd2d8", SKIN, None),
    "SPEC_WRITER": ("#d4b483", "#5a3a22", SKIN, None),
    "RESEARCHER": ("#2f8f7a", "#e8913a", SKIN, None),
    "REVIEWER": ("#2f5fb0", "#7a4e28", SKIN, None),
    "BUILDER": ("#e8622f", "#141414", "#8a5a3c", None),
    "SCOUT": ("#9aa0ad", "#3a3a3a", SKIN, None),
    "EXPLORER": ("#8a9c3e", "#3a2a1a", SKIN, None),       # floor.html: #d4b94a -- shifted off SPEC_WRITER's #d4b483 (item 8)
    "FIXER": ("#c94a3c", "#2a2a2a", SKIN, None),
    "VERIFIER": ("#3fae5a", "#5a3a22", SKIN, None),
    "SECURITY": ("#2c2c3a", "#141414", SKIN, GOLD),
    "DESIGNER": ("#d4528e", "#2a1a12", SKIN, None),
    "MARKETER": ("#5a6b8a", "#2fb0c4", SKIN, None),
    "WORKER": ("#6b7a99", "#2a2a2a", SKIN, None),
    "GLM_WORKER": ("#3fa0c4", "#1e1e1e", SKIN, None),     # floor.html: #f2a33a -- off orange, was colliding with BUILDER (item 1)
    "OWNER": ("#e5e7eb", "#1e1e1e", SKIN, None),
}

# ---- grid primitives --------------------------------------------------------

def _grid():
    return [[None] * GRID for _ in range(GRID)]


def _px(g, x, y, c):
    if 0 <= x < GRID and 0 <= y < GRID:
        g[y][x] = c


def _rect(g, x, y, w, h, c):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            _px(g, xx, yy, c)


def _circle(g, cx, cy, r, c):
    for yy in range(int(cy - r), int(cy + r) + 1):
        for xx in range(int(cx - r), int(cx + r) + 1):
            if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                _px(g, xx, yy, c)


def _ring(g, cx, cy, r, thickness, c):
    for yy in range(int(cy - r - 1), int(cy + r + 2)):
        for xx in range(int(cx - r - 1), int(cx + r + 2)):
            d = ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5
            if r - thickness <= d <= r:
                _px(g, xx, yy, c)


def _line(g, x0, y0, x1, y1, c, w=1):
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(steps + 1):
        x = round(x0 + (x1 - x0) * i / steps)
        y = round(y0 + (y1 - y0) * i / steps)
        for dx in range(w):
            for dy in range(w):
                _px(g, x + dx, y + dy, c)


def _outline(g):
    """Stamp a 1px outline into every transparent cell touching a filled
    one (8-neighbour). Runs once, after a role's body + prop are drawn, so
    every sprite gets the same treatment with no manual outline pixels."""
    h = len(g)
    w = len(g[0])
    stamp = []
    for y in range(h):
        for x in range(w):
            if g[y][x] is not None:
                continue
            hit = False
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and g[ny][nx] is not None:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                stamp.append((x, y))
    for x, y in stamp:
        g[y][x] = OUTLINE


# ---- shared body ------------------------------------------------------------
HEAD_CX, HEAD_CY, HEAD_R = 13, 9, 5
HEAD_TOP = HEAD_CY - HEAD_R          # 4
TORSO_X, TORSO_Y, TORSO_W, TORSO_H = 8, 14, 10, 8
LEGS_X, LEGS_Y, LEGS_W, LEGS_H = 9, 22, 8, 6
# Beside-body prop zone. PROP_TOP..PROP_BAND_BOT (rows 7-19, chest/head
# height) is what the desk's front face leaves visible on the Floor -- see
# the module docstring. Rows below ~20 exist (legs sink there) but no prop
# pixels should.
PROP_X = 21
PROP_TOP = 7
PROP_BAND_BOT = 19


def _base(shirt, hair, skin, pants):
    g = _grid()
    _rect(g, LEGS_X, LEGS_Y, LEGS_W, LEGS_H, pants or PANTS_DEFAULT)
    _rect(g, LEGS_X, LEGS_Y + LEGS_H, 3, 1, SHOE)
    _rect(g, LEGS_X + LEGS_W - 3, LEGS_Y + LEGS_H, 3, 1, SHOE)
    # arms (left plain, right leads toward the prop zone)
    _rect(g, 5, 15, 3, 6, shirt)
    _rect(g, 5, 21, 3, 2, skin)
    _rect(g, TORSO_X + TORSO_W, 15, 3, 5, shirt)
    _rect(g, TORSO_X + TORSO_W + 1, 20, 3, 2, skin)
    _rect(g, TORSO_X, TORSO_Y, TORSO_W, TORSO_H, shirt)
    _circle(g, HEAD_CX, HEAD_CY, HEAD_R, skin)
    for yy in range(HEAD_TOP, HEAD_CY):
        for xx in range(HEAD_CX - HEAD_R, HEAD_CX + HEAD_R + 1):
            if (xx - HEAD_CX) ** 2 + (yy - HEAD_CY) ** 2 <= HEAD_R * HEAD_R:
                _px(g, xx, yy, hair)
    _px(g, HEAD_CX - 2, HEAD_CY, INK)  # eyes -- small, keeps it a face not a blob
    _px(g, HEAD_CX + 2, HEAD_CY, INK)
    return g


# ---- props (one function per role; each breaks the body silhouette, all
# bounded to PROP_TOP..PROP_BAND_BOT so the desk front never eats them) ----

def _prop_clipboard(g, _hair):  # ARCHITECT -- flat board, blueprint grid
    _rect(g, PROP_X, 7, 7, 9, PAPER)
    _rect(g, PROP_X + 2, 6, 3, 1, METAL)
    for i, yy in enumerate(range(9, 15)):
        _px(g, PROP_X + 1 + (i % 4), yy, "#5f6fb0" if i % 2 else "#8b84b8")
        _px(g, PROP_X + 3 + (i % 3), yy, "#5f6fb0")


def _prop_scroll(g, _hair):  # SPEC_WRITER -- rolled blueprint, not a flat board
    _circle(g, PROP_X + 2, 9, 2, PAPER)
    _rect(g, PROP_X, 9, 5, 8, PAPER)
    _circle(g, PROP_X + 2, 17, 2, PAPER)
    _rect(g, PROP_X, 11, 5, 2, "#5a3a22")
    _rect(g, PROP_X, 15, 5, 2, "#5a3a22")


def _prop_books(g, _hair):  # RESEARCHER -- offset stack, 3 spines
    _rect(g, PROP_X - 1, 15, 8, 3, "#2f8f7a")
    _rect(g, PROP_X, 11, 7, 3, "#c9a227")
    _rect(g, PROP_X - 1, 7, 6, 3, "#e8913a")


def _prop_magnifier(g, _hair):  # REVIEWER -- oversized glass, handle breaks frame
    _ring(g, PROP_X + 4, 11, 3.5, 1.4, "#2f5fb0")
    _circle(g, PROP_X + 4, 11, 2.1, "#cfe3ff")
    _line(g, PROP_X + 6, 13, PROP_X + 9, 16, "#7a4e28", w=2)


def _prop_binoculars(g, _hair):  # SCOUT -- high-contrast METAL ring on dark barrels (item 2)
    _circle(g, PROP_X + 1, 10, 3, INK)
    _circle(g, PROP_X + 7, 10, 3, INK)
    _ring(g, PROP_X + 1, 10, 3, 1.3, METAL)
    _ring(g, PROP_X + 7, 10, 3, 1.3, METAL)
    _rect(g, PROP_X + 2, 9, 4, 2, METAL)


def _prop_compass(g, _hair):  # EXPLORER -- brass-rimmed compass, red/dark needle
    _circle(g, PROP_X + 4, 11, 4.3, GOLD)
    _circle(g, PROP_X + 4, 11, 3.1, "#f4f2ec")
    _line(g, PROP_X + 4, 11, PROP_X + 4, 8, "#c94a3c", w=1)
    _line(g, PROP_X + 4, 11, PROP_X + 4, 14, INK, w=1)
    _px(g, PROP_X + 4, 11, INK)


def _prop_toolbox(g, _hair):  # FIXER -- steel case, red kept only on the lid stripe (item 4)
    _rect(g, PROP_X - 1, 11, 9, 7, "#3c4049")
    _rect(g, PROP_X - 1, 11, 9, 1, "#c94a3c")
    _rect(g, PROP_X + 2, 9, 3, 1, "#2a2a2a")
    _rect(g, PROP_X + 2, 10, 1, 1, "#2a2a2a")
    _rect(g, PROP_X + 5, 10, 1, 1, "#2a2a2a")
    _px(g, PROP_X + 3, 14, GOLD)


def _prop_checklist(g, _hair):  # VERIFIER -- cooler/greener paper (item 5)
    _rect(g, PROP_X, 7, 6, 10, PAPER_COOL)
    _rect(g, PROP_X + 1, 6, 4, 1, METAL)
    for i, yy in enumerate((9, 12, 15)):
        done = i < 2
        _rect(g, PROP_X + 1, yy, 2, 2, "#3fae5a" if done else "#c7c1b2")
        _rect(g, PROP_X + 4, yy, 1, 1, "#8b8578")
        if done:
            _line(g, PROP_X + 1, yy + 1, PROP_X + 2, yy + 2, INK)
            _line(g, PROP_X + 2, yy + 2, PROP_X + 3, yy, INK)


def _prop_shield(g, _hair):  # SECURITY -- gold trim so it reads against dark bg
    rows = [7, 7, 7, 6, 6, 5, 4, 3, 1]
    cx = PROP_X + 4
    for i, w in enumerate(rows):
        y = 7 + i
        half = w // 2
        _rect(g, cx - half, y, w, 1, GOLD if i in (0, len(rows) - 1) else "#2c2c3a")
    for i, w in enumerate(rows[1:-1], start=1):
        y = 7 + i
        half = w // 2
        _px(g, cx - half, y, GOLD)
        _px(g, cx + half - 1, y, GOLD)


def _prop_palette(g, _hair):  # DESIGNER -- kidney palette, colour dabs
    _circle(g, PROP_X + 4, 13, 4.2, "#caa472")
    _circle(g, PROP_X + 2, 12, 1.1, INK)  # thumb hole
    for i, c in enumerate(("#d4528e", "#2fb0c4", "#f2c94a", "#3fae5a")):
        _px(g, PROP_X + 5 + (i % 2) * 2, 11 + (i // 2) * 3, c)


def _prop_megaphone(g, _hair):  # MARKETER -- bell flares up and out, held to mouth
    _rect(g, PROP_X - 2, 9, 2, 2, "#2fb0c4")
    _rect(g, PROP_X, 8, 2, 4, "#5a6b8a")
    _rect(g, PROP_X + 2, 7, 2, 6, "#5a6b8a")
    _rect(g, PROP_X + 4, 5, 2, 9, "#2fb0c4")
    _rect(g, PROP_X + 4, 5, 1, 1, "#eaf6fa")  # highlight


def _prop_laptop(g, _hair):  # WORKER -- generic laptop; default fallback, most-seen sprite (item 7)
    _rect(g, PROP_X - 1, 15, 8, 2, METAL)
    _rect(g, PROP_X, 8, 6, 7, "#2a2c33")
    _rect(g, PROP_X + 1, 9, 4, 5, "#3fae5a")
    _px(g, PROP_X + 3, 15, INK)


def _prop_chip(g, _hair):  # GLM_WORKER -- circuit chip; LED off cyan, now emerald (item 9)
    _rect(g, PROP_X, 11, 7, 7, "#1e1e1e")
    _rect(g, PROP_X + 1, 12, 5, 5, "#3fa0c4")
    for i in range(3):
        _px(g, PROP_X - 1, 12 + i, "#1e1e1e")
        _px(g, PROP_X + 7, 12 + i, "#1e1e1e")
    _rect(g, HEAD_CX - 1, HEAD_TOP - 3, 1, 3, "#1e1e1e")
    _px(g, HEAD_CX - 1, HEAD_TOP - 4, EMERALD)


def _prop_desk_star(g, _hair):  # OWNER -- a pinned star, crown carries the rest
    cx, cy = PROP_X + 3, 12
    for yy in range(cy - 4, cy + 5):
        for xx in range(cx - 4, cx + 5):
            dx, dy = xx - cx, yy - cy
            ang = (dx * dx + dy * dy) ** 0.5
            if ang <= 1.6 or (ang <= 4 and abs(dx) < 1.4) or (ang <= 4 and abs(dy) < 1.4):
                _px(g, xx, yy, GOLD)


PROP_FN = {
    "ARCHITECT": _prop_clipboard, "SPEC_WRITER": _prop_scroll, "RESEARCHER": _prop_books,
    "REVIEWER": _prop_magnifier, "SCOUT": _prop_binoculars, "EXPLORER": _prop_compass,
    "FIXER": _prop_toolbox, "VERIFIER": _prop_checklist, "SECURITY": _prop_shield,
    "DESIGNER": _prop_palette, "MARKETER": _prop_megaphone, "WORKER": _prop_laptop,
    "GLM_WORKER": _prop_chip, "OWNER": _prop_desk_star,
}


def _hat_hardhat(g):  # BUILDER
    _rect(g, HEAD_CX - 6, HEAD_TOP - 2, 13, 2, "#f2c94a")
    _rect(g, HEAD_CX - 4, HEAD_TOP - 3, 9, 1, "#f2c94a")
    _px(g, HEAD_CX, HEAD_TOP - 4, "#f2c94a")


def _hat_beret(g):  # DESIGNER -- flat disc, own colour (not the hair colour)
    cx, cy, rx, ry = HEAD_CX, HEAD_TOP - 2, 7, 3
    for yy in range(cy - ry, cy + ry + 1):
        for xx in range(cx - rx, cx + rx + 1):
            if ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1:
                _px(g, xx, yy, "#7a2b3f")
    _px(g, cx + rx - 1, cy - ry - 1, "#7a2b3f")


def _hat_crown(g):  # OWNER
    base_y = HEAD_TOP - 1
    _rect(g, HEAD_CX - 6, base_y, 13, 2, GOLD)
    for dx in (-5, -1, 3):
        for i in range(3):
            _px(g, HEAD_CX + dx + i, base_y - 1 - i, GOLD)
    for dx in (-5, -1, 3):
        _px(g, HEAD_CX + dx + 1, base_y - 3, CYAN)


HAT_FN = {"BUILDER": _hat_hardhat, "DESIGNER": _hat_beret, "OWNER": _hat_crown}


def _wrench(g):  # BUILDER's held tool -- >=3px shaft + gold accent so it doesn't
    # melt into dark pants/background (item 3). Distinct silhouette from GLM's chip.
    _rect(g, PROP_X, 8, 3, 11, METAL)
    _rect(g, PROP_X - 1, 7, 5, 2, METAL)
    _rect(g, PROP_X - 1, 18, 5, 2, METAL)
    _rect(g, PROP_X, 12, 3, 2, "#f2c94a")  # non-metal grip accent, matches the hard hat


def grid_for(role: str):
    shirt, hair, skin, pants = BODY[role]
    g = _base(shirt, hair, skin, pants)
    hat = HAT_FN.get(role)
    if hat:
        hat(g)
    if role == "BUILDER":
        _wrench(g)
    else:
        PROP_FN[role](g, hair)
    _outline(g)
    return g


# ---- SVG -------------------------------------------------------------------

def svg_body(g) -> str:
    parts = []
    for y, row in enumerate(g):
        x = 0
        while x < GRID:
            c = row[x]
            if c is None:
                x += 1
                continue
            run = 1
            while x + run < GRID and row[x + run] == c:
                run += 1
            parts.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{c}"/>')
            x += run
    return "".join(parts)


def svg_for(role: str) -> str:
    body = svg_body(grid_for(role))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}" '
            f'shape-rendering="crispEdges">{body}</svg>')


def sprites_sheet() -> str:
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" style="display:none">']
    for role in ROLE_ORDER:
        body = svg_body(grid_for(role))
        parts.append(f'<symbol id="sprite-{role}" viewBox="0 0 {GRID} {GRID}">{body}</symbol>')
    parts.append("</svg>")
    return "".join(parts)


# ---- PNG (stdlib only: zlib + struct, no Pillow) ---------------------------

def write_png(path: Path, width: int, height: int, pixels) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        for x in range(width):
            raw += bytes(pixels[y * width + x])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _hex_to_rgba(c, alpha=255):
    if c is None:
        return (0, 0, 0, 0)
    c = c.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return (r, g, b, alpha)


def render_pixels(g, scale: int):
    size = GRID * scale
    pixels = [(0, 0, 0, 0)] * (size * size)
    for gy in range(GRID):
        for gx in range(GRID):
            rgba = _hex_to_rgba(g[gy][gx])
            if rgba[3] == 0:
                continue
            for dy in range(scale):
                for dx in range(scale):
                    px = (gx * scale + dx)
                    py = (gy * scale + dy)
                    pixels[py * size + px] = rgba
    return pixels, size


def role_pngs(role: str):
    g = grid_for(role)
    for scale, suffix in ((1, ""), (2, "@2x"), (3, "@3x")):
        pixels, size = render_pixels(g, scale)
        write_png(HERE / f"{role}{suffix}.png", size, size, pixels)


def write_sheet_png():
    scale = 3
    size = GRID * scale
    width = size * len(ROLE_ORDER)
    pixels = [(0, 0, 0, 0)] * (width * size)
    for i, role in enumerate(ROLE_ORDER):
        g = grid_for(role)
        tile, _ = render_pixels(g, scale)
        for y in range(size):
            for x in range(size):
                pixels[y * width + i * size + x] = tile[y * size + x]
    write_png(HERE / "sheet.png", width, size, pixels)


# ---- preview.html + roles.json ---------------------------------------------

def write_roles_json():
    data = [{"role": r, "label": LABELS[r], "file": f"{r}.svg"} for r in ROLE_ORDER]
    (HERE / "roles.json").write_text(json.dumps(data, indent=2) + "\n")


PREVIEW_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Role sprites preview</title>
<style>
  body {{ margin: 0; font: 12px ui-monospace, "SF Mono", Menlo, monospace; }}
  section {{ padding: 24px; }}
  section.light {{ background: #f4f2ec; color: #1c1d22; }}
  /* dark matches gateway/static/floor.html's --bg exactly -- this app is
     dark-only, so this is the background that actually matters (review). */
  section.dark {{ background: #17181c; color: #d9dce3; }}
  h2 {{ letter-spacing: .08em; font-size: 13px; margin: 0 0 16px; }}
  .row {{ display: flex; gap: 20px; margin-bottom: 28px; flex-wrap: wrap; }}
  .cell {{ display: flex; flex-direction: column; align-items: center; gap: 6px; width: 100px; }}
  .cell img {{ image-rendering: pixelated; }}
  .label {{ font-weight: 600; letter-spacing: .04em; }}
  .file {{ opacity: .55; font-size: 10px; }}
  .scale {{ opacity: .5; font-size: 10px; margin-bottom: 8px; }}
</style>
</head>
<body>
{sections}
<script>
  (function () {{
    var only = (location.hash || "").replace("#", "");
    if (only === "light" || only === "dark") {{
      document.querySelectorAll("section").forEach(function (s) {{
        s.style.display = s.classList.contains(only) ? "" : "none";
      }});
    }}
  }})();
</script>
</body>
</html>
"""


def _section(theme: str) -> str:
    rows = []
    for px, label in ((32, "1x - 32px"), (64, "2x - 64px"), (128, "4x - 128px")):
        cells = []
        for role in ROLE_ORDER:
            cells.append(
                f'<div class="cell"><img src="{role}.svg" width="{px}" height="{px}" alt="{role}">'
                f'<div class="label">{LABELS[role]}</div><div class="file">{role}.svg</div></div>'
            )
        rows.append(f'<div class="scale">{label}</div><div class="row">{"".join(cells)}</div>')
    return f'<section class="{theme}"><h2>{theme.upper()}</h2>{"".join(rows)}</section>'


def write_preview_html():
    sections = _section("light") + _section("dark")
    (HERE / "preview.html").write_text(PREVIEW_TEMPLATE.format(sections=sections))


def main():
    for role in ROLE_ORDER:
        (HERE / f"{role}.svg").write_text(svg_for(role))
        role_pngs(role)
    (HERE / "sprites.svg").write_text(sprites_sheet())
    write_sheet_png()
    write_roles_json()
    write_preview_html()
    print(f"Generated {len(ROLE_ORDER)} roles -> svg + png(1x/2x/3x) + sprites.svg + sheet.png + roles.json + preview.html")


if __name__ == "__main__":
    main()
