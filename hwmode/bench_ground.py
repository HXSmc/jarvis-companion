"""Phase 0 bench: how well and how fast the lean Claude brain finds parts on real photos.

Usage: python3 -m hwmode.bench_ground PHOTOS_DIR OUT_DIR [--models sonnet,haiku]
PHOTOS_DIR holds the images named in CASES. Writes results.json plus one
annotated image per case (red = first pass, green = zoomed second pass) to OUT_DIR.
Needs Pillow (bench only; the brain itself has no dependencies).
"""
from __future__ import annotations

import argparse
import io
import json
import pathlib

from PIL import Image, ImageDraw

from hwmode.brain import LeanSession

# (image, what to find, small part?) — photos from Wikimedia Commons, see photos/manifest.json
CASES = [
    ("p00.jpg", "the leftmost SATA port", True),
    ("p01.jpg", "the 40-pin GPIO header", False),
    ("p01.jpg", "the mini HDMI port", False),
    ("p02.jpg", "the USB-C port", True),
    ("p04.jpg", "the USB port on the Arduino", False),
    ("p05.jpg", "the retention latch at the right end of the top RAM slot", True),
    ("p06.jpg", "the latch at the near end of the yellow RAM slot", True),
    ("p07.jpg", "the USB-C power port", True),
    ("p09.jpg", "the reset button", True),
    ("p10.jpg", "the stepper motor shaft", False),
    ("p12.jpg", "the L-shaped key notch of the SATA connector", True),
    ("p13.jpg", "the CPU cooler fan", False),
    ("p14.jpg", "the SD card slot", False),
]

ZOOM_PROMPT = ("This is a zoomed-in crop of the previous view. Locate {target} again, tightly, "
               "in THIS image's pixel coordinates.")


def jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def box_of(reply) -> list[float] | None:
    ov = (reply.data or {}).get("overlay") or {}
    b = ov.get("target_px")
    return [float(v) for v in b] if isinstance(b, list) and len(b) == 4 else None


def zoom_crop(im: Image.Image, box: list[float], scale: float = 3.0, min_side: int = 200):
    """Crop around box (box grown `scale`x, at least min_side px), upscale to ~768 px long edge."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w = max((x2 - x1) * scale, min_side)
    h = max((y2 - y1) * scale, min_side)
    left, top = max(0, cx - w / 2), max(0, cy - h / 2)
    right, bottom = min(im.width, cx + w / 2), min(im.height, cy + h / 2)
    crop = im.crop((int(left), int(top), int(right), int(bottom)))
    k = 768 / max(crop.size)
    crop = crop.resize((max(1, int(crop.width * k)), max(1, int(crop.height * k))))
    return crop, (left, top, k)


def unzoom(box: list[float], origin) -> list[float]:
    left, top, k = origin
    return [box[0] / k + left, box[1] / k + top, box[2] / k + left, box[3] / k + top]


def run(photos: pathlib.Path, out: pathlib.Path, model: str) -> list[dict]:
    rows = []
    session = LeanSession(model=model)
    try:
        for i, (fname, target, small) in enumerate(CASES):
            if session.turns >= 12:  # keep history (and resent images) small
                session.close()
                session = LeanSession(model=model)
            im = Image.open(photos / fname).convert("RGB")
            r1 = session.ask(f"New view. Show me {target}.", jpeg(im))
            b1, b2, r2 = box_of(r1), None, None
            if small and b1:
                crop, origin = zoom_crop(im, b1)
                r2 = session.ask(ZOOM_PROMPT.format(target=target), jpeg(crop))
                zb = box_of(r2)
                b2 = unzoom(zb, origin) if zb else None
            draw = ImageDraw.Draw(im)
            for b, colour in ((b1, "red"), (b2, "lime")):
                if b:
                    draw.rectangle(b, outline=colour, width=max(3, im.width // 300))
            draw.text((8, 8), f"{model}: {target}", fill="yellow")
            im.save(out / f"{model}_{i:02d}.jpg", quality=85)
            rows.append({"model": model, "case": i, "image": fname, "target": target, "small": small,
                         "pass1_box": b1, "pass2_box": b2,
                         "say": (r1.data or {}).get("say"), "label": ((r1.data or {}).get("overlay") or {}).get("label"),
                         "pass1_ttft_s": r1.ttft_s and round(r1.ttft_s, 2), "pass1_total_s": round(r1.total_s, 2),
                         "pass2_total_s": r2 and round(r2.total_s, 2),
                         "json_ok": r1.data is not None})
            print(json.dumps(rows[-1]), flush=True)
    finally:
        session.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("photos", type=pathlib.Path)
    ap.add_argument("out", type=pathlib.Path)
    ap.add_argument("--models", default="sonnet,haiku")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = [r for m in a.models.split(",") for r in run(a.photos, a.out, m)]
    (a.out / "results.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
