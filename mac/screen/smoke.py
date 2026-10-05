"""SC1 smoke gate: prove the screen driver end to end on this Mac.

  PYTHONPATH=. python -m mac.screen.smoke

With Screen Recording + Accessibility granted to the running python binary:
prints `tcc_ok True`, writes one JPEG frame to /tmp/jarvis_screen_smoke.jpg
with its byte size and sha1, and posts one harmless `move` to screen center.
Without the TCC grant: prints `tcc_ok False`, skips capture and input, still
exits 0 — no crash, no traceback.
"""

from __future__ import annotations

import hashlib

from mac.screen import driver

SMOKE_JPEG = "/tmp/jarvis_screen_smoke.jpg"


def main() -> int:
    ok = driver.tcc_ok()
    print(f"tcc_ok {ok}")
    print(f"display_asleep {driver.display_asleep()}")
    if not ok:
        print("skipping capture + input (no TCC grant)")
        return 0
    jpeg, w, h = driver.capture_jpeg(1170, 55)
    with open(SMOKE_JPEG, "wb") as f:
        f.write(jpeg)
    print(f"frame {len(jpeg)} bytes {w}x{h} "
          f"sha1 {hashlib.sha1(jpeg).hexdigest()} -> {SMOKE_JPEG}")
    driver.send_events([{"type": "move", "x": 0.5, "y": 0.5}])
    print("move (0.5, 0.5) posted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
