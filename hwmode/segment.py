"""The part's outline for Hardware Mode: SAM 2.1 (Hiera-Tiny, ONNX) on this box's CPU turns
the brain's box into a mask, so the phone can paint the part itself instead of a rectangle.

Optional by design: without `onnxruntime`/Pillow or the model files the phone keeps the box.
Model files (sam2.1_hiera_tiny.encoder.onnx, sam2.1_hiera_tiny.decoder.onnx, Apache 2.0) come
from the Hugging Face repo vietanhdev/segment-anything-2.1-onnx-models (sam2.1_hiera_tiny_20260221.zip);
they live in HWMODE_SAM_DIR (default ~/.local/share/sam2.1-tiny), not in the repo.
Cost on this box: ~1.3 s to encode a frame (run while the brain thinks), ~30 ms per mask.
"""
from __future__ import annotations

import base64
import io
import os
import threading
from dataclasses import dataclass
from pathlib import Path

SIDE = 1024  # the encoder's input size
MASK_MAX = 128  # longest side of the mask image sent to the phone
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)
# Mask levels in the PNG: the phone paints it with these as opacity.
INSIDE, EDGE = 110, 255


@dataclass
class Embedding:
    feats: tuple  # (high_res_feats_0, high_res_feats_1, image_embed)
    width: int
    height: int


class Segmenter:
    def __init__(self) -> None:
        self.dir = Path(os.environ.get("HWMODE_SAM_DIR", str(Path.home() / ".local/share/sam2.1-tiny")))
        self._enc = self._dec = None
        self._lock = threading.Lock()  # one encode at a time: onnxruntime already uses the cores

    @property
    def encoder(self) -> Path:
        return self.dir / "sam2.1_hiera_tiny.encoder.onnx"

    @property
    def decoder(self) -> Path:
        return self.dir / "sam2.1_hiera_tiny.decoder.onnx"

    def available(self) -> bool:
        if os.environ.get("HWMODE_SAM") == "0" or not (self.encoder.is_file() and self.decoder.is_file()):
            return False
        try:
            import onnxruntime  # noqa: F401
            import PIL  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self) -> None:
        if self._enc is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 3  # the export's harmless shape-merge warning
            self._enc = ort.InferenceSession(str(self.encoder), opts, providers=["CPUExecutionProvider"])
            self._dec = ort.InferenceSession(str(self.decoder), opts, providers=["CPUExecutionProvider"])

    def embed(self, jpeg: bytes) -> Embedding:
        """Encode one frame (blocking, ~1.3 s)."""
        import numpy as np
        from PIL import Image

        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
        x = np.asarray(im.resize((SIDE, SIDE), Image.BILINEAR), dtype=np.float32) / 255.0
        x = ((x - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
        with self._lock:
            self._load()
            feats = self._enc.run(None, {self._enc.get_inputs()[0].name: x})
        return Embedding(tuple(feats), im.width, im.height)

    def _decode(self, emb: Embedding, pts: list, labels: list):
        import numpy as np

        f0, f1, image_embed = emb.feats
        with self._lock:
            self._load()
            return self._dec.run(None, {
                "image_embed": image_embed, "high_res_feats_0": f0, "high_res_feats_1": f1,
                "point_coords": np.array([pts], np.float32), "point_labels": np.array([labels], np.float32),
                "mask_input": np.zeros((1, 1, SIDE // 4, SIDE // 4), np.float32),
                "has_mask_input": np.zeros(1, np.float32)})

    def mask(self, emb: Embedding, box: list[float]) -> dict | None:
        """{"target_px": the mask's tight box, "mask_png": base64 PNG of the mask inside that box,
        "mask_score": SAM's own guess, near 0 when the corner clicks were needed} for the part in
        `box` (pixels of the frame), or None when the mask is not believable (then the phone keeps
        the brain's box)."""
        import numpy as np
        from PIL import Image, ImageFilter

        w, h = emb.width, emb.height
        x1, y1, x2, y2 = (min(max(v, 0), lim) for v, lim in zip(box, (w, h, w, h)))
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        sx, sy = SIDE / w, SIDE / h
        # Negative clicks just inside the box's corners: on a dim, cluttered table SAM otherwise
        # often paints the background around the part (the corners are almost always background).
        ix, iy = (x2 - x1) * 0.04, (y2 - y1) * 0.04
        inner = [[x1 + ix, y1 + iy], [x2 - ix, y1 + iy], [x1 + ix, y2 - iy], [x2 - ix, y2 - iy]]
        cands = []
        for pts, labels in (([[x1, y1], [x2, y2]], [2, 3]),  # 2, 3 = box corners
                            ([[x1, y1], [x2, y2], *inner], [2, 3, 0, 0, 0, 0])):  # 0 = not the part
            masks, scores = self._decode(emb, [[px * sx, py * sy] for px, py in pts], labels)
            # With negative clicks SAM's own quality guess collapses to ~0 even for good masks: only
            # the box-only guesses are filtered on it.
            cands += [(float(scores[0][j]), masks[0][j]) for j in range(masks.shape[1])
                      if labels[2:] or scores[0][j] >= 0.5]
        box_area = (x2 - x1) * (y2 - y1)
        bx1, by1 = int(x1), int(y1)
        bx2, by2 = max(bx1 + 1, int(round(x2)) - 1), max(by1 + 1, int(round(y2)) - 1)
        gx, gy = (x2 - x1) * 0.1, (y2 - y1) * 0.1  # the brain's box is rarely exact: let the part run a bit past it
        grown = (int(max(0, x1 - gx)), int(max(0, y1 - gy)), int(min(w - 1, x2 + gx)), int(min(h - 1, y2 + gy)))
        cx1, cx2 = int(x1 + 0.4 * (x2 - x1)), int(x1 + 0.6 * (x2 - x1)) + 1
        cy1, cy2 = int(y1 + 0.4 * (y2 - y1)), int(y1 + 0.6 * (y2 - y1)) + 1
        best = None
        for score, low in cands:
            full = np.asarray(Image.fromarray(low.astype(np.float32)).resize((w, h), Image.BILINEAR)) > 0
            # The brain boxes the part around its middle; a mask that leaves the middle empty is the
            # background around the part (the dim-table failure), never the part.
            if full[cy1:cy2, cx1:cx2].mean() < 0.5:
                continue
            full = _main_part(full, grown)
            if full is None or full.sum() < 0.05 * box_area:
                continue
            centre = full[cy1:cy2, cx1:cx2].mean()
            if centre < 0.5:
                continue
            # Share of the box's outline the mask runs along: a part touches its box at a few
            # points, the background around it runs along whole sides.
            rim = np.concatenate([full[by1, bx1:bx2], full[by2, bx1:bx2], full[by1:by2, bx1], full[by1:by2, bx2]]).mean()
            if best is None or centre - rim > best[0]:
                best = (centre - rim, score, full)
        if best is None:  # nothing believable: the plain box is better than a wrong shape
            return None
        _, score, full = best
        ys, xs = np.nonzero(full)
        mx1, my1, mx2, my2 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        crop = Image.fromarray((full[my1:my2, mx1:mx2] * 255).astype(np.uint8))
        scale = min(1.0, MASK_MAX / max(crop.size))
        if scale < 1.0:
            crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.BILINEAR)
        inside = np.asarray(crop) >= 128
        core = np.asarray(Image.fromarray(inside.astype(np.uint8) * 255).filter(ImageFilter.MinFilter(3))) >= 128
        levels = np.where(inside & ~core, EDGE, np.where(inside, INSIDE, 0)).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(levels, "L").save(buf, "PNG", optimize=True)
        return {"target_px": [mx1, my1, mx2, my2], "mask_png": base64.b64encode(buf.getvalue()).decode(),
                "mask_score": round(score, 3)}


def _main_part(full, box: tuple[int, int, int, int]):
    """The mask cut to `box`, as one solid piece: the largest connected blob with its small holes
    filled. Stray specks on the table read as a broken highlight. None when nothing is left."""
    import numpy as np
    from PIL import Image

    x1, y1, x2, y2 = box
    region = full[y1:y2 + 1, x1:x2 + 1]
    if not region.any():
        return None
    # Label at <=96 px (the phone gets <=128 px anyway): plain-Python flood fill stays cheap.
    k = min(1.0, 96 / max(region.shape))
    small = np.asarray(Image.fromarray(region.astype(np.uint8) * 255).resize(
        (max(1, round(region.shape[1] * k)), max(1, round(region.shape[0] * k))), Image.BILINEAR)) >= 128
    labels, sizes = _components(small)
    if not sizes:
        return None
    keep = labels == (1 + int(np.argmax(sizes)))
    bg, bg_sizes = _components(~keep)
    edge_ids = set(np.unique(np.concatenate([bg[0], bg[-1], bg[:, 0], bg[:, -1]]).tolist()))
    # Small holes (glare, a label) are filled; a big one is something else lying on the part.
    holes = [n for n, size in enumerate(bg_sizes, 1) if n not in edge_ids and size < 0.1 * keep.sum()]
    keep |= np.isin(bg, holes)
    out = np.zeros_like(full)
    out[y1:y2 + 1, x1:x2 + 1] = np.asarray(Image.fromarray(keep.astype(np.uint8) * 255).resize(
        (region.shape[1], region.shape[0]), Image.BILINEAR)) >= 128
    return out if out.any() else None


def _components(m):
    """4-connected labels (0 = off) and each label's size, labels numbered from 1."""
    from collections import deque

    import numpy as np

    h, w = m.shape
    labels = np.zeros((h, w), np.int32)
    sizes: list[int] = []
    for y, x in zip(*np.nonzero(m)):
        if labels[y, x]:
            continue
        n = len(sizes) + 1
        labels[y, x] = n
        q, size = deque([(y, x)]), 0
        while q:
            cy, cx = q.popleft()
            size += 1
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = n
                    q.append((ny, nx))
        sizes.append(size)
    return labels, sizes


_segmenter: Segmenter | None = None


def get_segmenter() -> Segmenter | None:
    """The process-wide segmenter, or None when it is not installed (the phone draws the box)."""
    global _segmenter
    if _segmenter is None:
        _segmenter = Segmenter()
    return _segmenter if _segmenter.available() else None
