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

    def mask(self, emb: Embedding, box: list[float]) -> dict | None:
        """{"target_px": the mask's tight box, "mask_png": base64 PNG of the mask inside that box,
        "mask_score": ...} for the part in `box` (pixels of the frame), or None when the mask
        is not believable (then the phone keeps the brain's box)."""
        import numpy as np
        from PIL import Image, ImageFilter

        w, h = emb.width, emb.height
        x1, y1, x2, y2 = (min(max(v, 0), lim) for v, lim in zip(box, (w, h, w, h)))
        if x2 - x1 < 2 or y2 - y1 < 2:
            return None
        f0, f1, image_embed = emb.feats
        pts = np.array([[[x1 / w * SIDE, y1 / h * SIDE], [x2 / w * SIDE, y2 / h * SIDE]]], np.float32)
        with self._lock:
            self._load()
            masks, scores = self._dec.run(None, {
                "image_embed": image_embed, "high_res_feats_0": f0, "high_res_feats_1": f1,
                "point_coords": pts, "point_labels": np.array([[2, 3]], np.float32),  # 2, 3 = box corners
                "mask_input": np.zeros((1, 1, SIDE // 4, SIDE // 4), np.float32),
                "has_mask_input": np.zeros(1, np.float32)})
        best = int(np.argmax(scores[0]))
        score = float(scores[0][best])
        full = np.asarray(Image.fromarray(masks[0][best].astype(np.float32)).resize((w, h), Image.BILINEAR)) > 0
        ys, xs = np.nonzero(full)
        box_area = (x2 - x1) * (y2 - y1)
        if score < 0.5 or len(xs) < 0.05 * box_area:
            return None
        mx1, my1, mx2, my2 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        if (mx2 - mx1) * (my2 - my1) > 1.6 * box_area:  # spilled well past the part the brain meant
            return None
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


_segmenter: Segmenter | None = None


def get_segmenter() -> Segmenter | None:
    """The process-wide segmenter, or None when it is not installed (the phone draws the box)."""
    global _segmenter
    if _segmenter is None:
        _segmenter = Segmenter()
    return _segmenter if _segmenter.available() else None
