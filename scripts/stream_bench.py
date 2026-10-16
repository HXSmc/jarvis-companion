"""Phase 1 screen-stream benchmark on the mini PC: JPEG polling vs noVNC vs H.264 stream.

Runs against a THROWAWAY X display (default :2, an Xtigervnc on rfbport 5902),
never the owner's desktop. Needs: Playwright + Chromium, Pillow (run with a
venv that has them), the gateway venv (--gateway-python), websockify + noVNC
in /usr/share/novnc, ffplay, xsetroot. The display's window manager must not
composite (e.g. `xfwm4 --compositor=off`): a compositor paints over the root
window, so the xsetroot latency probe never changes colour.

    ~/venv/bin/python scripts/stream_bench.py --display :2 --rfbport 5902 \
        --gateway-python .venv/bin/python

Metrics, same probe for every path:
  latency  xsetroot flips the root colour; time until the viewer's canvas
           pixel shows it (capture + encode + transport + decode + draw).
  fps      distinct canvas changes per second over a testsrc2 ffplay window
           (H.264 also reports decoded_fps: VideoDecoder outputs per second).
  kbps     bytes received per second on the wire (WS frames / JPEG bytes).
  cpu      host-side CPU% of the encoder process (helper / Xtigervnc).
JPEG polling is EMULATED in-process (ImageGrab + JPEG w=1170 q=55, the same
200 ms / 1000 ms delays as screen.html): it has no browser decode and no
HTTP leg, so its latency is a lower bound.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROBE = (1700, 900)  # a root-window pixel outside the ffplay window
STRIP = (400, 300, 200, 40)  # x, y, w, h inside the ffplay testsrc2 window
COLOURS = [("#ff0000", (255, 0, 0)), ("#0000ff", (0, 0, 255))]
TOKEN = "bench-ios-token"  # throwaway: the bench gateway never reads the vault

# Injected into the viewer page: watches the probe pixel and the fps strip.
WATCH_JS = """
([px, py, sx, sy, sw, sh]) => {
  const st = window.__bench = { hits: [], changes: 0, want: null, last: "" };
  const find = () => document.querySelector("canvas");
  let ctx = null;
  function tick() {
    const c = find();
    if (c && c.width > px) {
      ctx = ctx || c.getContext("2d", { willReadFrequently: true });
      const p = ctx.getImageData(px, py, 1, 1).data;
      if (st.want && Math.abs(p[0] - st.want[0]) < 40 && Math.abs(p[2] - st.want[2]) < 40) {
        st.hits.push(performance.timeOrigin + performance.now()); st.want = null;
      }
      const s = ctx.getImageData(sx, sy, sw, sh).data;
      let h = 0; for (let i = 0; i < s.length; i += 16) h = (h * 31 + s[i]) | 0;
      const key = String(h);
      if (key !== st.last) { st.changes++; st.last = key; }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 15.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"port {port} never opened")


def xsetroot(display: str, colour: str) -> None:
    subprocess.run(["xsetroot", "-solid", colour], env={**os.environ, "DISPLAY": display}, check=True)


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def pids(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return [int(p) for p in out.split() if int(p) != os.getpid()]


def summarise(name: str, lat: list[float], fps: float, kbps: float, cpu: float | None) -> dict:
    return {
        "path": name, "trials": len(lat),
        "latency_median_ms": round(statistics.median(lat), 1) if lat else None,
        "latency_p90_ms": round(sorted(lat)[int(len(lat) * 0.9) - 1], 1) if lat else None,
        "fps": round(fps, 1), "kbps": round(kbps), "cpu_pct": None if cpu is None else round(cpu, 1),
    }


def browser_run(page, args, name: str, url: str, cpu_pid_pattern: str) -> dict:
    """Shared probe for noVNC and the H.264 page (both draw into a <canvas>)."""
    wire = {"bytes": 0}

    def on_ws(ws):
        ws.on("framereceived", lambda payload: wire.__setitem__("bytes", wire["bytes"] + len(payload)))

    page.on("websocket", on_ws)
    page.goto(url)
    try:
        page.wait_for_function("() => { const c = document.querySelector('canvas'); return c && c.width > 1000; }",
                               timeout=20000)
    except Exception as exc:  # one broken path must not lose the other results
        page.remove_listener("websocket", on_ws)
        return {"path": name, "error": f"viewer never drew a frame: {exc}"}
    page.evaluate(WATCH_JS, [*PROBE, *STRIP])
    time.sleep(2)
    # fps / bandwidth / cpu under the testsrc2 load
    cpu_pids = pids(cpu_pid_pattern)
    c0 = sum(cpu_seconds(p) for p in cpu_pids)
    decoded = "window.__stream ? window.__stream.decoded : null"  # H.264 page only
    b0, ch0, d0, t0 = wire["bytes"], page.evaluate("window.__bench.changes"), page.evaluate(decoded), time.time()
    time.sleep(args.seconds)
    dt = time.time() - t0
    fps = (page.evaluate("window.__bench.changes") - ch0) / dt
    d1 = page.evaluate(decoded)
    kbps = (wire["bytes"] - b0) * 8 / 1000 / dt
    cpu = (sum(cpu_seconds(p) for p in cpu_pids) - c0) / dt * 100 if cpu_pids else None
    # latency trials
    lat = []
    for i in range(args.trials):
        colour, rgb = COLOURS[i % 2]
        page.evaluate(f"window.__bench.want = {list(rgb)}")
        sent = time.time() * 1000
        xsetroot(args.display, colour)
        try:
            page.wait_for_function("window.__bench.hits.length > %d" % len(lat), timeout=5000)
            lat.append(page.evaluate("window.__bench.hits[window.__bench.hits.length - 1]") - sent)
        except Exception:
            page.evaluate("window.__bench.hits.push(-1)")  # keep the index in step
        time.sleep(0.7 + (i % 3) * 0.13)  # de-phase from any fixed capture clock
    page.remove_listener("websocket", on_ws)
    out = summarise(name, [x for x in lat if x >= 0], fps, kbps, cpu)
    if d0 is not None:
        out["decoded_fps"] = round((d1 - d0) / dt, 1)  # decoder output rate, independent of the canvas probe
    return out


def jpeg_run(args, delay_s: float) -> dict:
    """Emulate screen.html polling: capture, resize to w=1170, JPEG q=55, sleep delay_s."""
    from PIL import ImageGrab

    def grab():
        im = ImageGrab.grab(xdisplay=args.display)
        probe = im.getpixel(PROBE)
        strip = im.crop((STRIP[0], STRIP[1], STRIP[0] + STRIP[2], STRIP[1] + STRIP[3])).tobytes()
        small = im.resize((1170, round(im.height * 1170 / im.width)))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=55)
        return probe, strip, buf.tell()

    state = {"stop": False, "frames": 0, "bytes": 0, "changes": 0, "last": b"", "want": None, "hit": None}

    def loop():
        while not state["stop"]:
            probe, strip, size = grab()
            state["frames"] += 1
            state["bytes"] += size
            if strip != state["last"]:
                state["changes"] += 1
                state["last"] = strip
            want = state["want"]
            if want and abs(probe[0] - want[0]) < 40 and abs(probe[2] - want[2]) < 40:
                state["hit"], state["want"] = time.time() * 1000, None
            time.sleep(delay_s)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    time.sleep(2)
    ch0, b0, t0 = state["changes"], state["bytes"], time.time()
    c0 = cpu_seconds(os.getpid())
    time.sleep(args.seconds)
    dt = time.time() - t0
    fps = (state["changes"] - ch0) / dt
    kbps = (state["bytes"] - b0) * 8 / 1000 / dt
    cpu = (cpu_seconds(os.getpid()) - c0) / dt * 100
    lat = []
    for i in range(args.trials):
        colour, rgb = COLOURS[i % 2]
        state["hit"], state["want"] = None, rgb
        sent = time.time() * 1000
        xsetroot(args.display, colour)
        end = time.time() + 5
        while state["hit"] is None and time.time() < end:
            time.sleep(0.002)
        if state["hit"] is not None:
            lat.append(state["hit"] - sent)
        time.sleep(0.7 + (i % 3) * 0.13 + delay_s)
    state["stop"] = True
    t.join()
    return summarise(f"JPEG polling (emulated, {int(delay_s * 1000)} ms delay)", lat, fps, kbps, cpu)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", default=":2")
    ap.add_argument("--rfbport", type=int, default=5902)
    ap.add_argument("--gateway-python", default=str(REPO.parent.parent.parent / ".venv" / "bin" / "python"))
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--only", default="jpeg,novnc,stream,stream-sw")
    ap.add_argument("--out", default=None, help="write results JSON here")
    args = ap.parse_args()
    if args.display in (":0", ":1"):
        sys.exit("refusing to bench a real desktop display; use a throwaway one like :2")
    only = set(args.only.split(","))
    env = {**os.environ, "DISPLAY": args.display}
    procs: list[subprocess.Popen] = []
    results: list[dict] = []
    try:
        procs.append(subprocess.Popen(
            ["ffplay", "-loglevel", "quiet", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-window_title", "bench-load", "-left", "100", "-top", "100"], env=env))
        time.sleep(2)
        xsetroot(args.display, "#000000")

        if "jpeg" in only:
            results.append(jpeg_run(args, 0.2))
            results.append(jpeg_run(args, 1.0))

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            if "novnc" in only:
                wsport = free_port()
                procs.append(subprocess.Popen(
                    ["websockify", "--web", "/usr/share/novnc", f"127.0.0.1:{wsport}", f"127.0.0.1:{args.rfbport}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                wait_port(wsport)
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                url = f"http://127.0.0.1:{wsport}/vnc_lite.html?host=127.0.0.1&port={wsport}&scale=false"
                results.append(browser_run(page, args, "noVNC (websockify, default tight)", url,
                                           f"Xtigervnc {args.display} "))
                page.close()
            for variant in [v for v in ("stream", "stream-sw") if v in only]:
                port = free_port()
                helper = f"/usr/bin/python3 {REPO / 'linux' / 'stream_helper.py'}"
                if variant == "stream-sw":
                    helper += " --software"
                gw = subprocess.Popen(
                    [args.gateway_python, "-c",
                     "import sys, uvicorn; sys.path.insert(0, %r)\n"
                     "from common import config\n"
                     "config.get_token = lambda path=None, name='token': %r if name == 'ios_token' else 'bench-shared'\n"
                     "from gateway.app import app\n"
                     "uvicorn.run(app, host='127.0.0.1', port=%d, log_level='warning')" % (str(REPO), TOKEN, port)],
                    cwd=REPO, env={**os.environ, "STREAM_DISPLAY": args.display, "STREAM_HELPER_CMD": helper,
                                   "SCREEN_PUBLIC_BASE": f"http://127.0.0.1:{port}"})
                procs.append(gw)
                wait_port(port)
                ctx = browser.new_context(viewport={"width": 1920, "height": 1080},
                                          extra_http_headers={"Authorization": f"Bearer {TOKEN}"})
                page = ctx.new_page()
                name = "H.264 stream (VA-API vah264lpenc)" if variant == "stream" else "H.264 stream (openh264, software)"
                results.append(browser_run(page, args, name, f"http://127.0.0.1:{port}/screen/stream?target=minipc",
                                           "stream_helper.py"))
                results[-1]["decode_errors"] = page.evaluate("window.__stream && window.__stream.errors")
                ctx.close()  # closing the page must end the helper (no leak per session)
                time.sleep(3)
                results[-1]["helpers_left_after_close"] = len(pids("linux/stream_helper.py"))
                gw.terminate()
                gw.wait(10)
            browser.close()
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        xsetroot(args.display, "#000000")
    text = json.dumps(results, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
