# jarvis-companion

Personal, single-user, never published. The Mac is the brain; thin clients on the Windows
PC and iPhone are driven over the owner's Tailscale tailnet.

Six features: query/control the PC, flight-booking one-tap handoff, sign-in one-tap
handoff, vault passwords on the phone (Apple Passwords CSV + iCloud Keychain), "Hey
Jarvis" wake word (Mac + Windows via openWakeWord, iPhone via a Siri Shortcut), and an
iPhone Mirroring tap-through driver.

## Setup

Copy `.env.example` to `.env` and fill in your deployment's URLs and accounts
(the gateway loads it at startup; deploy scripts source it). Required vars are
named in the error message when one is missing. Tests run without a `.env`.

Privacy guard: run `scripts/install-hooks.sh` after cloning. This points git
at `.githooks/` so every commit and push is scanned for private strings
(`.env` values, an optional `.privacy-patterns` list, and generic host/path/key
regexes) before it can reach the public repo.

## Stack

Python 3.11+ (FastAPI + websockets gateway on 127.0.0.1:8788, `tailscale serve` for
TLS), SwiftUI iPhone app (free Apple ID), Python tray agent on Windows packaged with
PyInstaller. Voice reuses the jarvis-phone stack (whisper.cpp, Kokoro, openWakeWord
`hey_jarvis`).

No cloud. No paid APIs. Not for distribution.

## Remote screen

- open the screen: `https://<ts-host>:8790/screen` (from the app or any Bearer-capable client; session cookie lasts 12 h)
- the page now also controls the mini PC desktop over noVNC through the gateway (`gateway/vnc.py`); `gateway/screen.py`/`gateway/vnc.py` edits need `launchctl kickstart -k gui/$(id -u)/com.jarvis.companion.gateway` to take effect, while `screen.html` is live-reloaded
- disable: set `SCREEN_CONTROL=0` and restart the gateway
- smoke: `.venv/bin/python -m mac.screen.smoke`
