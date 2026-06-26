# Gesture Control

Control your Mac's **volume** and **music playback** with hand gestures, using nothing but your webcam. A Jarvis-style on-screen HUD shows what's playing and reacts to your hands in real time.

Built with [OpenCV](https://opencv.org/) + [MediaPipe](https://developers.google.com/mediapipe) for hand tracking, and AppleScript / macOS media keys for control.

> **Platform:** macOS only (for now). A Windows version is on the roadmap — see [Roadmap](#roadmap).

---

## Features

- 🔊 **Volume** — pinch your thumb and index finger to set the level (continuous control)
- ⏯️ **Play / Pause** — open palm; works on anything playing (Spotify, Apple Music, YouTube, …)
- ⏭️ **Next / Previous track** — for Spotify
- 🎵 **Now-playing HUD** — shows the current Spotify track, artist, and album art
- 🟢 **Master on/off** — a single gesture arms or disarms the whole thing, so you can talk with your hands without anything reacting
- 🖐️ **Reliable by design** — gestures only register when you mean them, with on-screen feedback so nothing is ever a mystery

---

## Gestures

| Gesture | Action |
|---|---|
| Pinch thumb + index (middle & ring up, pinky down) | Set volume |
| Open palm (all fingers up) | Play / Pause |
| Peace ✌️ (index + middle) | Next track (Spotify) |
| Point ☝️ (index only) | Previous track (Spotify) |
| Rock 🤘 (index + pinky), held briefly | Arm / disarm everything |
| `a` key | Arm / disarm (backup) |
| `q` key | Quit |

While **disarmed**, no gesture does anything — handy when you're just gesturing in conversation.

---

## Install

There are two ways to get this, depending on who you are:

- **Just want to use it?** A prebuilt, double-click Mac app is **planned** (see [Roadmap](#roadmap)). When it lands, you'll download it straight from the [Releases](../../releases) page and run it — no Python, no terminal. *(Not available yet.)*
- **Want to run or change the code?** Follow the developer setup below — it runs locally on your Mac from source.

---

## Setup (run from source)

Requires **Python 3.9–3.12** (MediaPipe doesn't yet ship wheels for the newest releases).

```bash
# 1. Create an isolated environment
python3 -m venv gesture-env
source gesture-env/bin/activate

# 2. Install the core dependencies
pip install -r requirements.txt

# 3. (Optional) For universal Play/Pause beyond Spotify (YouTube, Apple Music, etc.)
pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa

# 4. Run it
python gesture_volume_control.py
```

### macOS permissions

- **Camera** — macOS will prompt the first time. Allow it under *System Settings → Privacy & Security → Camera*.
- **Accessibility** — needed only for universal Play/Pause via the media key. If it doesn't work, grant your terminal/IDE access under *System Settings → Privacy & Security → Accessibility*.

### Notes

- **Spotify** must be open for next/previous and the now-playing panel to work.
- Hold your hand roughly **upright** (fingers pointing up) — the pose detection assumes this.
- The first time a new song appears, the album art downloads over the network, so it may take a second to show.

---

## Roadmap

Planned for upcoming releases:

- 👉 **Swipe left/right** for next/previous (replacing the peace/point poses — more distinct, feels like flicking through tracks)
- 🎯 **Auto-calibration** — learns your pinch range automatically, so no more hand-tuning distances when you move closer or further
- 🪄 **One-euro filter** — adaptive smoothing for steadier-when-still, snappier-when-moving tracking
- 📍 **Menu-bar / background mode** — run quietly from the menu bar instead of a window
- 💤 **Idle sleep** — drop to a low-power heartbeat when no hand is around, so it can run all day
- 📦 **Double-click Mac app** — package the whole thing into a `.app` bundle (via py2app / PyInstaller) that ships Python and every dependency inside it, so end users just download from [Releases](../../releases) and double-click — no Python, no `pip`, no terminal. Pairs with menu-bar mode as the "make it a real app" milestone. *(Note: an unsigned app triggers macOS Gatekeeper, so users right-click → Open the first time; code-signing needs a paid Apple Developer account.)*
- 🪟 **Windows version** — the gesture engine is already cross-platform; only the volume/media layer is macOS-specific today. A Windows port (volume via `pycaw`, media-key playback) is planned for a future release — **on the roadmap, but not imminent.**

---

## How it works

The webcam feed is mirrored and run through MediaPipe Hands to get 21 hand landmarks. Finger states (up/down) are read from the landmarks to classify the current pose; the thumb-to-index distance drives the volume. Volume is applied via `osascript` on a background thread (so it never stalls the video), playback uses Spotify AppleScript and macOS media keys, and a separate thread polls Spotify once a second for the now-playing info and album art.

Everything runs **locally** on your own Mac — your webcam feed never leaves the machine, and there's no server involved. Once packaged (see Roadmap), the same code reaches two audiences from the **Releases** page: source code for developers who want to tinker, and a prebuilt double-click app for everyone else.

---

## License

MIT — see [LICENSE](LICENSE).
