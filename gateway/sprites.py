"""Role sprites: one pixel grid per role, the single source of truth for both
the Agency Floor (canvas, wants SVG) and the iOS Sessions board (SwiftUI
Canvas, cannot decode SVG from the network — wants a plain color grid).

Ask 2 (fable2-watchdog-roles-app-side.md): pixel-art per role with a distinct
prop (designer palette/beret, marketer megaphone, builder hard hat, planner
clipboard for ARCHITECT+SPEC_WRITER, reviewer magnifier, researcher books,
security shield, fixer toolbox, verifier checklist, scout binoculars, GLM
worker a distinct colour, owner a desk). Served by gateway/central.py at
GET /central/sprites/{role}.svg and GET /central/sprites/{role}.json, both on
the public (no-bearer) router — an <img>/Canvas fetch cannot send a bearer,
and the sprite carries no data or secret, same reasoning as /central/floor.

Grid is GRID_W x GRID_H cells; a cell is a palette hex string or None
(transparent). Built procedurally (circles/rects, no external art), shared
body shape + a per-role prop stamp — this keeps every role visually distinct
without hand-drawing 13 separate full sprites.
"""

from __future__ import annotations

import struct
import zlib

GRID_W = 16
GRID_H = 18
# Apple asset-catalog scale convention: @1x is the logical/design size, @2x
# and @3x are exact integer multiples for retina. Each grid cell is this many
# raw pixels at @1x (so @1x comes out 48x54, @2x 96x108, @3x 144x162).
PNG_SCALE_1X = 3
PNG_SCALES = {"1x": PNG_SCALE_1X, "2x": PNG_SCALE_1X * 2, "3x": PNG_SCALE_1X * 3}

# Same hex values as gateway/static/floor.html's ROLES map (kept in sync by
# hand; the two can't literally share a constant across Python/JS). Palette
# below only lists the colours this module actually stamps.
SKIN = "#e8c39e"
OUTLINE = None  # flat colour, no outline — matches the Floor's existing low-fidelity style


def _grid() -> list[list[str | None]]:
    return [[None] * GRID_W for _ in range(GRID_H)]


def _px(g: list[list[str | None]], x: int, y: int, color: str) -> None:
    if 0 <= x < GRID_W and 0 <= y < GRID_H:
        g[y][x] = color


def _rect(g, x: int, y: int, w: int, h: int, color: str) -> None:
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            _px(g, xx, yy, color)


def _circle(g, cx: int, cy: int, r: float, color: str) -> None:
    for yy in range(int(cy - r), int(cy + r) + 1):
        for xx in range(int(cx - r), int(cx + r) + 1):
            if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                _px(g, xx, yy, color)


def _line(g, x0: int, y0: int, x1: int, y1: int, color: str) -> None:
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(steps + 1):
        _px(g, round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps), color)


HEAD_CX, HEAD_CY, HEAD_R = 7, 5, 3
TORSO_X, TORSO_Y, TORSO_W, TORSO_H = 4, 8, 7, 6
LEG_Y, LEG_H = 14, 3


def _base(shirt: str, hair: str, skin: str = SKIN, pants: str | None = None) -> list[list[str | None]]:
    """Shared body every role starts from: head, hair, torso, legs."""
    g = _grid()
    _rect(g, TORSO_X - 1, LEG_Y, TORSO_W + 1, LEG_H, pants or "#2a2c33")
    _rect(g, TORSO_X, TORSO_Y, TORSO_W, TORSO_H, shirt)
    _circle(g, HEAD_CX, HEAD_CY, HEAD_R, skin)
    for yy in range(HEAD_CY - HEAD_R, HEAD_CY):  # hair = top half of the head circle
        for xx in range(HEAD_CX - HEAD_R, HEAD_CX + HEAD_R + 1):
            if (xx - HEAD_CX) ** 2 + (yy - HEAD_CY) ** 2 <= HEAD_R * HEAD_R and yy <= HEAD_CY - 1:
                _px(g, xx, yy, hair)
    return g


# Prop zone: to the right of the body, roughly x in [11,15], y in [1,9].
PROP_X, PROP_Y = 11, 2


def _prop_clipboard(g, prop: str, prop2: str) -> None:  # ARCHITECT / SPEC_WRITER -> "Planner"
    _rect(g, PROP_X, PROP_Y, 4, 6, "#e8e2d4")
    _rect(g, PROP_X, PROP_Y - 1, 2, 1, "#9a9488")
    for yy in range(PROP_Y + 1, PROP_Y + 5):
        _rect(g, PROP_X + 1, yy, 2, 1, prop if yy % 2 else prop2)


def _prop_books(g, prop: str, prop2: str) -> None:  # RESEARCHER
    _rect(g, PROP_X, PROP_Y + 3, 5, 2, prop)
    _rect(g, PROP_X, PROP_Y + 1, 4, 2, prop2)
    _rect(g, PROP_X + 1, PROP_Y - 1, 3, 2, prop)


def _prop_magnifier(g, prop: str, _: str) -> None:  # REVIEWER
    cx, cy = PROP_X + 1, PROP_Y + 2
    for yy in range(cy - 2, cy + 3):
        for xx in range(cx - 2, cx + 3):
            if abs((xx - cx) ** 2 + (yy - cy) ** 2 - 4) <= 2:
                _px(g, xx, yy, prop)
    _line(g, PROP_X + 3, PROP_Y + 4, PROP_X + 5, PROP_Y + 6, prop)


def _prop_hardhat(g, prop: str, _: str) -> None:  # BUILDER — sits on the head, not beside it
    _rect(g, HEAD_CX - 4, HEAD_CY - HEAD_R - 1, 9, 2, prop)
    _rect(g, HEAD_CX - 3, HEAD_CY - HEAD_R - 2, 7, 1, prop)


def _prop_binoculars(g, prop: str, prop2: str) -> None:  # SCOUT
    _circle(g, PROP_X, PROP_Y + 2, 1.6, prop)
    _circle(g, PROP_X + 3, PROP_Y + 2, 1.6, prop)
    _rect(g, PROP_X, PROP_Y + 1, 4, 1, prop2)


def _prop_toolbox(g, prop: str, prop2: str) -> None:  # FIXER
    _rect(g, PROP_X, PROP_Y + 2, 5, 4, prop)
    _rect(g, PROP_X + 1, PROP_Y + 1, 3, 1, prop2)
    _rect(g, PROP_X, PROP_Y + 3, 5, 1, prop2)


def _prop_checklist(g, prop: str, prop2: str) -> None:  # VERIFIER
    _rect(g, PROP_X, PROP_Y - 1, 4, 7, "#e8e2d4")
    for i, yy in enumerate(range(PROP_Y, PROP_Y + 5)):
        _px(g, PROP_X + 1, yy, prop if i % 2 == 0 else prop2)
        _px(g, PROP_X + 2, yy, prop if i % 2 == 0 else prop2)


def _prop_shield(g, prop: str, prop2: str) -> None:  # SECURITY
    rows = [5, 5, 4, 4, 3, 2, 1]
    for i, w in enumerate(rows):
        offset = (6 - w) // 2
        _rect(g, PROP_X - 1 + offset, PROP_Y + i, w, 1, prop if i < 4 else prop2)


def _prop_palette_beret(g, prop: str, prop2: str) -> None:  # DESIGNER — beret on head + palette beside
    _rect(g, HEAD_CX - 3, HEAD_CY - HEAD_R - 1, 7, 2, prop2)
    _circle(g, PROP_X + 1, PROP_Y + 3, 2.2, "#caa472")
    for i, c in enumerate((prop, prop2, "#5fb0c4", "#e8622f")):
        _px(g, PROP_X + (i % 2), PROP_Y + 2 + (i // 2), c)


def _prop_megaphone(g, prop: str, prop2: str) -> None:  # MARKETER — horn bell, entirely clear of the head
    _rect(g, PROP_X, PROP_Y + 3, 2, 2, prop2)
    _rect(g, PROP_X + 2, PROP_Y + 2, 1, 4, prop)
    _rect(g, PROP_X + 3, PROP_Y + 1, 1, 6, prop)
    _rect(g, PROP_X + 4, PROP_Y, 1, 8, prop2)


def _prop_chip(g, prop: str, prop2: str) -> None:  # GLM_WORKER
    _rect(g, PROP_X, PROP_Y + 1, 5, 5, prop2)
    _rect(g, PROP_X + 1, PROP_Y + 2, 3, 3, prop)
    for i in range(3):
        _px(g, PROP_X - 1, PROP_Y + 2 + i, prop2)
        _px(g, PROP_X + 5, PROP_Y + 2 + i, prop2)


def _prop_desk(g, prop: str, prop2: str) -> None:  # OWNER — desk + a small lamp, offset so it's not a cross
    _rect(g, PROP_X, PROP_Y + 5, 5, 1, prop)
    _px(g, PROP_X, PROP_Y + 6, prop)
    _px(g, PROP_X + 4, PROP_Y + 6, prop)
    _rect(g, PROP_X + 1, PROP_Y + 2, 1, 3, prop2)
    _rect(g, PROP_X, PROP_Y + 1, 3, 1, prop2)


# role -> (shirt, hair, pants|None, prop, prop2, prop_fn|None)
_ROLE_SPEC: dict[str, tuple[str, str, str | None, str, str, object]] = {
    "ARCHITECT": ("#a78bfa", "#cfd2d8", None, "#4b4f8a", "#8b84b8", _prop_clipboard),
    "SPEC_WRITER": ("#d4b483", "#5a3a22", None, "#4b4f8a", "#8b84b8", _prop_clipboard),
    "RESEARCHER": ("#2f8f7a", "#e8913a", None, "#2f8f7a", "#c9a227", _prop_books),
    "REVIEWER": ("#2f5fb0", "#7a4e28", None, "#2f5fb0", None, _prop_magnifier),
    "BUILDER": ("#e8622f", "#141414", None, "#f2c94a", None, _prop_hardhat),
    "SCOUT": ("#9aa0ad", "#3a3a3a", None, "#2a2c33", "#9aa0ad", _prop_binoculars),
    "FIXER": ("#c94a3c", "#2a2a2a", None, "#c94a3c", "#6b7a99", _prop_toolbox),
    "VERIFIER": ("#3fae5a", "#5a3a22", None, "#3fae5a", "#2a2c33", _prop_checklist),
    "SECURITY": ("#2c2c3a", "#141414", "#c9a227", "#2c2c3a", "#c9a227", _prop_shield),
    "DESIGNER": ("#d4528e", "#2a1a12", None, "#d4528e", "#2a1a12", _prop_palette_beret),
    "MARKETER": ("#5a6b8a", "#2fb0c4", None, "#5a6b8a", "#2fb0c4", _prop_megaphone),
    "EXPLORER": ("#d4b94a", "#3a2a1a", None, "#d4b94a", None, _prop_binoculars),
    "WORKER": ("#6b7a99", "#2a2a2a", None, None, None, None),
    "GLM_WORKER": ("#f2a33a", "#1e1e1e", None, "#f2a33a", "#1e1e1e", _prop_chip),
    "OWNER": ("#e5e7eb", "#1e1e1e", None, "#e5e7eb", "#22d3ee", _prop_desk),
}

ROLES: tuple[str, ...] = tuple(_ROLE_SPEC.keys())


def grid_for(role: str) -> list[list[str | None]]:
    """The pixel grid for a role, or WORKER's generic grid for an unknown one."""
    shirt, hair, pants, prop, prop2, prop_fn = _ROLE_SPEC.get(role, _ROLE_SPEC["WORKER"])
    g = _base(shirt, hair, pants=pants)
    if prop_fn is not None:
        prop_fn(g, prop, prop2)
    return g


def svg_for(role: str) -> str:
    """<svg> for the role: one <rect> per horizontal run of same-colour cells."""
    g = grid_for(role)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID_W} {GRID_H}" '
             f'shape-rendering="crispEdges">']
    for y, row in enumerate(g):
        x = 0
        while x < GRID_W:
            color = row[x]
            if color is None:
                x += 1
                continue
            run = 1
            while x + run < GRID_W and row[x + run] == color:
                run += 1
            parts.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{color}"/>')
            x += run
    parts.append("</svg>")
    return "".join(parts)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_bytes_for(role: str, scale: int) -> bytes:
    """Rasterize the grid to an uncompressed-filter RGBA PNG, `scale` raw
    pixels per grid cell. Hand-rolled (stdlib zlib + struct only, no Pillow —
    pyproject.toml gates Pillow to win32) so this is the fallback used when
    gateway/static/sprites/ doesn't have a real @Nx.png for the role yet
    (that directory is fable 2's worktree, not touched here)."""
    grid = grid_for(role)
    w, h = GRID_W * scale, GRID_H * scale
    raw = bytearray()
    for row in grid:
        pixel_row = bytearray()
        for color in row:
            if color is None:
                pixel_row += b"\x00\x00\x00\x00"
            else:
                v = int(color[1:], 16)
                pixel_row += bytes(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF, 255))
        scaled_row = b"\x00" + pixel_row * scale  # filter type 0 (none) per scanline
        raw += scaled_row * scale
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit depth, colour type 6 = RGBA
    idat = zlib.compress(bytes(raw), 9)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")
