"""Synthetic Hardware Mode scenes: a drawn motherboard corner with three SATA ports and
one SATA cable, rendered with PIL. No real camera frames or screenshots, ever: every
pixel here is generated, and each scene comes with ground-truth boxes for the parts.

The repair the suites script: "move the SATA cable from SATA1 to SATA2".
States: "in_port1" (start), "unplugged" (step 1 done), "in_port2" (step 2 done),
"in_port3" (wrong port).
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1024, 768
TASK = "move the SATA cable from the SATA1 port to the SATA2 port"
STEPS_HINT = "There are exactly 2 steps: 1) unplug the cable from SATA1, 2) plug it into SATA2."
PORTS = {  # x1, y1, x2, y2 of each socket, in image pixels
    "SATA1": (300, 300, 420, 350),
    "SATA2": (300, 420, 420, 470),
    "SATA3": (300, 540, 420, 590),
}


@dataclass
class Scene:
    jpeg: bytes
    boxes: dict[str, tuple[int, int, int, int]]  # part name -> ground-truth box
    state: str


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _connector_box(state: str) -> tuple[int, int, int, int]:
    if state.startswith("in_port"):
        x1, y1, x2, y2 = PORTS[f"SATA{state[-1]}"]
        return (x1 - 6, y1 - 10, x2 + 50, y2 + 10)  # seated: the plug covers the socket
    return (700, 600, 810, 660)  # lying loose, off the board


def render(state: str, *, noise: float = 0.0, brightness: float = 1.0, shift: tuple[int, int] = (0, 0),
           blur: float = 0.0, seed: int = 0) -> Scene:
    """One frame. `noise` 0..1 (sensor grain), `brightness` (0.4 = dim room), `shift` (hand shake)."""
    img = Image.new("RGB", (W, H), (40, 40, 44))  # case interior
    d = ImageDraw.Draw(img)
    d.rectangle((120, 120, 620, 700), fill=(22, 92, 48), outline=(15, 60, 30), width=6)  # board
    for i in range(14):  # traces
        y = 140 + i * 40
        d.line((140, y, 600, y + 12), fill=(30, 110, 58), width=2)
    f = _font(26)
    for name, (x1, y1, x2, y2) in PORTS.items():
        d.rectangle((x1, y1, x2, y2), fill=(150, 150, 155), outline=(90, 90, 95), width=4)
        d.rectangle((x1 + 10, y1 + 12, x2 - 30, y2 - 12), fill=(30, 30, 32))  # opening
        d.rectangle((x2 - 30, y1 + 12, x2 - 12, y1 + 26), fill=(30, 30, 32))  # L-key
        d.text((x1 - 110, y1 + 10), name, fill=(235, 235, 235), font=f)  # silkscreen
    cx1, cy1, cx2, cy2 = _connector_box(state)
    # red flat cable from the right edge to the connector
    d.line((W, 200, cx2 + 60, 200), fill=(200, 30, 30), width=18)
    d.line((cx2 + 60, 200, cx2 + 10, (cy1 + cy2) // 2), fill=(200, 30, 30), width=18)
    d.rectangle((cx1, cy1, cx2, cy2), fill=(15, 15, 15), outline=(200, 30, 30), width=5)  # connector body
    d.text((cx1 + 8, cy1 + 8), "SATA", fill=(220, 220, 220), font=_font(18))
    d.text((640, 130), "drive bay", fill=(180, 180, 180), font=_font(20))

    if shift != (0, 0):
        img = img.transform(img.size, Image.AFFINE, (1, 0, -shift[0], 0, 1, -shift[1]), fillcolor=(40, 40, 44))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if brightness != 1.0:
        img = Image.eval(img, lambda v: max(0, min(255, int(v * brightness))))
    if noise:
        rnd = random.Random(seed)
        px = img.load()
        amp = int(80 * noise)
        for _ in range(int(W * H * 0.15)):
            x, y = rnd.randrange(W), rnd.randrange(H)
            r, g, b = px[x, y]
            n = rnd.randint(-amp, amp)
            px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))

    sx, sy = shift
    boxes = {name: (x1 + sx, y1 + sy, x2 + sx, y2 + sy) for name, (x1, y1, x2, y2) in PORTS.items()}
    boxes["connector"] = (cx1 + sx, cy1 + sy, cx2 + sx, cy2 + sy)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return Scene(buf.getvalue(), boxes, state)


def video(states: list[str], frames_per_state: int = 3) -> list[Scene]:
    """A short "video": each state held for a few frames with hand shake."""
    out = []
    for si, state in enumerate(states):
        for k in range(frames_per_state):
            out.append(render(state, shift=((k * 7) % 15 - 7, (k * 5) % 11 - 5), noise=0.15, seed=si * 10 + k))
    return out


def iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def center_in(box, truth, margin: int = 20) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return truth[0] - margin <= cx <= truth[2] + margin and truth[1] - margin <= cy <= truth[3] + margin


if __name__ == "__main__":  # write a preview set for a human look: python3 tests/hwmode_synth.py OUTDIR
    import pathlib
    import sys

    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for st in ("in_port1", "unplugged", "in_port2", "in_port3"):
        (out / f"{st}.jpg").write_bytes(render(st).jpeg)
    (out / "in_port1_dim_noisy.jpg").write_bytes(render("in_port1", noise=0.5, brightness=0.45, seed=3).jpeg)
