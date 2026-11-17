# Jarvis Companion: what each part can do

This sheet is the source for questions about Jarvis, Jarvis Companion and its parts. Answer from it. When an answer about what a part can do rests only on reading code, not on a line here or a live check, start it with "from the code, " instead of a flat yes or no.

## Card
- iPhone app: sessions board, cards, Hardware Mode, focused inbox
- Gateway on your own machines, reached over Tailscale
- Mac overlay: voice and cards (this app)
- Session router: Claude Code sessions on the mini PC
- Screens: mini PC sessions click Mac and Windows; mini PC view-only
- Windows PC agent; "Hey Jarvis" on Mac and Windows

## iPhone app
- Sessions board: every Claude Code session across the Mac, the Windows PC and the mini PC, with sub-sessions.
- Cards from sessions, a focused inbox that keeps only mail that needs the owner, and the central chat.
- Remote screen for the Mac and the Windows PC. The mini PC screen is view-only in the app.

## Gateway
- Runs on the owner's own machines and is reached over Tailscale.
- Passes only allowlisted routes to the session router and the devices.

## Mac overlay
- Voice: "Hey Jarvis", spoken answers, stop by voice at any time.
- Cards on screen: facts, numbers, steps, pictures, videos and charts.
- Maps and routes between places, and live TfL bus and tube times and line status.
- Drawings of a thing on request.
- YouTube: answers questions about a video, and can watch a video and sum it up.
- Commands: can run commands for the owner, asking first unless full auto is on. Some commands always ask, even in full auto: admin rights, deleting or overwriting files, network tools, and anything that reads secrets.
- Not available yet: 3D objects, and place search (nearby places, cinema times, the best way to a named place).

## Mini PC
- The session router starts and runs Claude Code sessions on the mini PC.

## Windows PC
- A Windows agent answers and acts on the PC by voice or from a Claude session.
- "Hey Jarvis" works on the Windows PC too.

## Screens
- Mini PC Claude sessions can see and click the Mac and Windows screens through jarvis-screen.
- Any other agent is refused with 403.
- The mini PC screen is view-only in the app.

## Hardware Mode
- On the iPhone: point the camera at a part, talk, and Jarvis answers out loud and draws on the part step by step.
- Other sessions can ask the owner for hands; the summary goes back to that session.
