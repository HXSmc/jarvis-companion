#!/usr/bin/python3
"""X11 screen -> H.264 access units on stdout (mini PC stream helper).

Run with the system python (needs python3-gi + GStreamer `va` plugin), under
the `render` group for /dev/dri/renderD128:

    DISPLAY=:1 /usr/bin/python3 linux/stream_helper.py --fps 30 --qp 26

Pipeline: ximagesrc -> videoconvert (BGRx->NV12) -> vah264lpenc (CQP,
no B-frames, GOP 1024) -> h264parse (SPS/PPS before every IDR) -> appsink.
Ideas from RustDesk (re-implemented, no code copied): skip unchanged frames,
keyframes only on request, live quality/fps changes. Falls back to openh264enc
(screen mode) when VA-API is missing.

stdout framing (shared with gateway/stream.py and the Mac/Windows helpers):
    <I payload_len><B flags (bit0 = keyframe)><d ts_ms>  then Annex-B AU bytes
stdin control, one JSON object per line:
    {"keyframe": true} | {"qp": 18..45} | {"fps": 1..60}
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import time

HEADER = struct.Struct("<IBd")
FLAG_KEY = 1
QP_MIN, QP_MAX = 18, 45
FPS_MIN, FPS_MAX = 1, 60


def pack(payload: bytes, key: bool, ts_ms: float) -> bytes:
    return HEADER.pack(len(payload), FLAG_KEY if key else 0, ts_ms) + payload


class Gate:
    """Decide per raw frame: pass to the encoder or drop (fps cap / unchanged)."""

    def __init__(self, fps: int) -> None:
        self.interval = 1.0 / fps
        self.last_pass = 0.0
        self.last_bytes: bytes | None = None
        self.force = True  # first frame always goes through

    def admit(self, now: float, data: bytes) -> bool:
        if now - self.last_pass < self.interval * 0.9:
            return False
        if not self.force and data == self.last_bytes:
            return False  # unchanged screen: encode and send nothing
        self.force = False
        self.last_bytes = data
        self.last_pass = now
        return True


def qp_to_bps(qp: int) -> int:
    # openh264 has no live QP: map QP to a bitrate (x2 per -6 QP, 4 Mbps at QP 26)
    return int(4_000_000 * 2 ** ((26 - qp) / 6))


def build_pipeline(Gst, display: str, fps: int, qp: int, use_va: bool):
    src = f"ximagesrc display-name={display} use-damage=false show-pointer=true ! video/x-raw,framerate={fps}/1"
    if use_va:
        enc = (
            # no vapostproc on this iHD build (no VPP entrypoint): convert on CPU
            "videoconvert ! video/x-raw,format=NV12 ! "
            f"vah264lpenc name=enc rate-control=cqp qpi={qp} qpp={qp} b-frames=0 "
            "key-int-max=1024 ref-frames=1 aud=false"
        )
    else:
        enc = (
            "videoconvert ! video/x-raw,format=I420 ! "
            f"openh264enc name=enc usage-type=screen complexity=low rate-control=bitrate "
            f"bitrate={qp_to_bps(qp)} gop-size=100000"
        )
    # constrained-baseline: what WebCodecs decodes everywhere (iOS included)
    profile = ",profile=constrained-baseline" if use_va else ""
    desc = (
        f"{src} ! queue max-size-buffers=1 leaky=downstream name=rawq ! {enc} ! "
        f"h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au{profile} ! "
        "appsink name=sink sync=false emit-signals=false max-buffers=0"
    )
    return Gst.parse_launch(desc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", default=None)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--qp", type=int, default=26)
    ap.add_argument("--software", action="store_true", help="force openh264enc")
    args = ap.parse_args()

    import os

    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo

    Gst.init(None)
    display = args.display or os.environ.get("DISPLAY", ":0")
    use_va = not args.software and Gst.ElementFactory.find("vah264lpenc") is not None
    pipeline = build_pipeline(Gst, display, args.fps, args.qp, use_va)
    enc = pipeline.get_by_name("enc")
    sink = pipeline.get_by_name("sink")
    gate = Gate(args.fps)
    lock = threading.Lock()
    out = sys.stdout.buffer

    def raw_probe(_pad, info):
        buf = info.get_buffer()
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        try:
            data = bytes(m.data)
        finally:
            buf.unmap(m)
        with lock:
            admitted = gate.admit(time.monotonic(), data)
        return Gst.PadProbeReturn.OK if admitted else Gst.PadProbeReturn.DROP

    pipeline.get_by_name("rawq").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, raw_probe)

    def request_keyframe() -> None:
        with lock:
            gate.force = True
        event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        sink.send_event(event)  # a sink element sends upstream events out its sink pad

    def set_qp(qp: int) -> None:
        qp = max(QP_MIN, min(QP_MAX, int(qp)))
        if use_va:
            enc.set_property("qpi", qp)
            enc.set_property("qpp", qp)
        else:
            enc.set_property("bitrate", qp_to_bps(qp))

    def control_loop() -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("keyframe"):
                request_keyframe()
            if isinstance(msg.get("qp"), (int, float)):
                set_qp(msg["qp"])
            if isinstance(msg.get("fps"), (int, float)):
                with lock:
                    gate.interval = 1.0 / max(FPS_MIN, min(FPS_MAX, msg["fps"]))
        pipeline.send_event(Gst.Event.new_eos())  # stdin closed: parent is gone

    threading.Thread(target=control_loop, daemon=True).start()
    pipeline.set_state(Gst.State.PLAYING)
    print(json.dumps({"helper": "x11", "encoder": "vah264lpenc" if use_va else "openh264enc", "display": display}),
          file=sys.stderr, flush=True)
    try:
        while True:
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                if sink.get_property("eos"):
                    break
                bus_msg = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
                if bus_msg is not None:
                    err, _ = bus_msg.parse_error()
                    print(f"pipeline error: {err.message}", file=sys.stderr, flush=True)
                    return 1
                continue
            buf = sample.get_buffer()
            ok, m = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
                ts = buf.pts / Gst.MSECOND if buf.pts != Gst.CLOCK_TIME_NONE else 0.0
                out.write(pack(bytes(m.data), key, ts))
                out.flush()
            finally:
                buf.unmap(m)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
