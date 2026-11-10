"""Hardware Mode part finding (SAM): the mask only tightens the brain's box ("target_px").
The real-model tests run only where the SAM files are installed; scenes are synthetic."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from hwmode.segment import Segmenter, _components, _main_part


def test_stray_specks_are_dropped_and_small_holes_filled():
    import numpy as np

    m = np.zeros((100, 100), bool)
    m[30:70, 30:70] = True  # the part
    m[48:52, 48:52] = False  # glare on it
    m[5:9, 85:90] = True  # a speck of table, inside the box
    out = _main_part(m, (0, 0, 99, 99))
    assert out[30:70, 30:70].all()  # one solid piece, hole filled
    assert not out[5:9, 85:90].any()  # the speck is gone
    assert not out[:, 95:].any() and _main_part(np.zeros((10, 10), bool), (0, 0, 9, 9)) is None


def test_components_labels_blobs_4_connected_with_their_sizes():
    import numpy as np

    m = np.zeros((6, 8), bool)
    m[0:2, 0:2] = True  # one blob of 4
    m[4, 5:8] = True  # another blob of 3
    m[2, 3] = True  # touches the first only diagonally: still its own blob (4-connected)
    labels, sizes = _components(m)
    assert sizes == [4, 1, 3]
    assert labels[0, 0] == 1 and labels[1, 1] == 1
    assert labels[2, 3] == 2
    assert labels[4, 5] == 3 and labels[4, 7] == 3
    assert labels[3, 3] == 0  # background stays 0


# ---------------------------------------------------------------- the real model, if installed

@pytest.fixture(scope="module")
def segmenter():
    seg = Segmenter()
    if not seg.available():
        pytest.skip("SAM 2.1 model files or onnxruntime not installed on this box")
    return seg


@pytest.mark.live_mask
def test_real_mask_on_a_dim_table_is_the_part_not_the_table(segmenter):
    """The owner's screenshot: on a dim, grainy wood table SAM liked to paint the table around the
    dock. The mask must be the dock itself, or no mask at all (the phone then shows the box)."""
    from hwmode_synth import iou, render_dim_table

    scene = render_dim_table()
    emb = segmenter.embed(scene.jpeg)
    t = scene.boxes["dock"]
    loose = [t[0] - 70, t[1] - 55, t[2] + 70, t[3] + 55]  # a loose box takes in plenty of table
    m = segmenter.mask(emb, loose)
    assert m, "no mask for the dock"
    assert set(m) == {"target_px", "mask_score"}  # the mask image is gone: the box only tightens
    print(f"  dock: loose={loose} mask={m['target_px']} truth={t} iou={iou(m['target_px'], t):.2f}")
    assert iou(m["target_px"], t) > 0.8


def test_get_segmenter_prewarms_once_without_an_embed(monkeypatch):
    import hwmode.segment as seg

    calls = []
    monkeypatch.setattr(seg, "_segmenter", None)
    monkeypatch.setattr(seg, "_prewarm", None)
    monkeypatch.setattr(Segmenter, "available", lambda self: True)
    monkeypatch.setattr(Segmenter, "_load", lambda self: calls.append(1))
    first = seg.get_segmenter()
    assert first is seg.get_segmenter()
    seg._prewarm.join(timeout=5)
    assert not seg._prewarm.is_alive()
    assert calls == [1]


def test_no_prewarm_thread_when_models_are_absent(monkeypatch):
    import hwmode.segment as seg

    monkeypatch.setattr(seg, "_segmenter", None)
    monkeypatch.setattr(seg, "_prewarm", None)
    monkeypatch.setattr(Segmenter, "available", lambda self: False)
    assert seg.get_segmenter() is None
    assert seg._prewarm is None


def test_failed_load_is_logged_leaves_no_half_state(monkeypatch, caplog):
    import sys
    import types

    class Boom(Exception):
        pass

    def session(path, *a, **k):
        if str(path).endswith("decoder.onnx"):
            raise Boom("decoder broken")
        return object()

    fake = types.SimpleNamespace(SessionOptions=lambda: types.SimpleNamespace(), InferenceSession=session)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    s = Segmenter()
    with caplog.at_level("WARNING", logger="hwmode.segment"):
        s.prewarm()
    assert s._enc is None and s._dec is None
    assert len([r for r in caplog.records if "prewarm failed" in r.message]) == 1
    with pytest.raises(Boom):  # the lazy path behaves as before
        s._load()
