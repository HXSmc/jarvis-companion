# jarvis-companion

Personal, single-user, never published. The Mac is the brain; thin clients on the Windows
PC and iPhone are driven over the owner's Tailscale tailnet.

Six features: query/control the PC, flight-booking one-tap handoff, sign-in one-tap
handoff, vault passwords on the phone (Apple Passwords CSV + iCloud Keychain), "Hey
Jarvis" wake word (Mac + Windows via openWakeWord, iPhone via a Siri Shortcut), and an
iPhone Mirroring tap-through driver.

## Doc map

- `docs/DESIGN.md` — architecture, feature flows, auth, out of scope
- `docs/PLAN.md` — phased build tasks with acceptance commands
- `resume.md` — current state, decisions, how to resume the build loop

## Stack

Python 3.11+ (FastAPI + websockets gateway on 127.0.0.1:8788, `tailscale serve` for
TLS), SwiftUI iPhone app (free Apple ID), Python tray agent on Windows packaged with
PyInstaller. Voice reuses the jarvis-phone stack (whisper.cpp, Kokoro, openWakeWord
`hey_jarvis`).

No cloud. No paid APIs. Not for distribution.

## Remote screen

- open the screen: `https://<ts-host>:8790/screen` (from the app or any Bearer-capable client; session cookie lasts 12 h)
- disable: set `SCREEN_CONTROL=0` and restart the gateway
- smoke: `.venv/bin/python -m mac.screen.smoke`
