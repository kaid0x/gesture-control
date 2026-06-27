"""
Gesture Media + Volume Control for macOS  (with now-playing HUD + master toggle)
--------------------------------------------------------------------------------
Control volume and playback with one hand, see what's playing, and arm/disarm
the whole thing with a gesture.

  MASTER ON/OFF:
    Rock (index + pinky up, middle + ring down), held briefly -> arm / disarm.
    Or press the 'a' key while the window is focused.
    While DISARMED, no gesture does anything (talk with your hands freely).

  VOLUME (continuous, when armed):
    Pinch pose = middle + ring up, pinky DOWN.
    Pinch thumb + index close -> quieter, spread apart -> louder.
    The pinch distance is normalised by your palm size, so it works the
    same whether your hand is near or far from the camera.

  PLAYBACK (one-shot, hold briefly, when armed):
    Open palm  -> Play / Pause  (anything playing)
    Peace      -> Next track     (Spotify)
    Point      -> Previous track (Spotify)

  NOW PLAYING:
    Shows current Spotify track, artist, and album art at the bottom.

Optional, for universal Play/Pause (YouTube, Apple Music, ...):
    pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa

Quit with the 'q' key.

Usage:
    python gesture_volume_control.py [--camera N] [--width W] [--height H]
                                     [--no-spotify] [--debug] [--help]

Requirements:
  pip install opencv-python mediapipe numpy
"""

import argparse
import logging
import math
import subprocess
import threading
import time
import urllib.request

import cv2
import numpy as np
import mediapipe as mp


log = logging.getLogger("gesture")


# ---------------------------------------------------------------------------
# Settings (defaults; most are overridable from the command line)
# ---------------------------------------------------------------------------
CAM_INDEX = 0
CAM_WIDTH = 1280
CAM_HEIGHT = 720
TARGET_FPS = 30

# Pinch distance is expressed as a ratio of palm size, so these are unitless
# and independent of how far the hand is from the camera.
MIN_RATIO = 0.20            # thumb + index touching   -> 0%
MAX_RATIO = 1.30            # thumb + index spread wide -> 100%
SMOOTHING = 0.4

GESTURE_HOLD = 0.30
TOGGLE_HOLD = 0.65
COOLDOWN = 0.9

CYAN = (255, 255, 0)
GREEN = (80, 255, 80)
WHITE = (255, 255, 255)
DIM = (120, 120, 120)
RED = (60, 60, 255)
AMBER = (0, 200, 255)


# ---------------------------------------------------------------------------
# Threaded camera
# ---------------------------------------------------------------------------
class CameraStream:
    def __init__(self, src, width, height, fps):
        self.cap = cv2.VideoCapture(src)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.ret, self.frame = self.cap.read()
        self.running = self.ret
        self.lock = threading.Lock()
        self.thread = None
        if self.running:
            self.thread = threading.Thread(target=self._update, daemon=True)
            self.thread.start()

    def _update(self):
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.ret, self.frame = ret, frame

    def read(self):
        with self.lock:
            if self.frame is None:
                return False, None
            return self.ret, self.frame.copy()

    def release(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        self.cap.release()


# ---------------------------------------------------------------------------
# Backend: everything OS-specific lives here, so the core loop stays portable.
# To support another OS, implement the same five methods.
# ---------------------------------------------------------------------------
SPOTIFY_SCRIPT = '''
if application "Spotify" is running then
  tell application "Spotify"
    try
      set s to player state as string
      set n to name of current track
      set a to artist of current track
      set u to artwork url of current track
      return s & "|;|" & n & "|;|" & a & "|;|" & u
    on error
      return "none"
    end try
  end tell
else
  return "closed"
end if
'''


class MacOSBackend:
    """Volume, transport, and now-playing via osascript / Quartz media keys."""

    def __init__(self):
        self._send_media_key = self._load_media_key()

    @staticmethod
    def _load_media_key():
        """Return a media-key sender, or None if pyobjc isn't available."""
        try:
            from AppKit import NSEvent
            from Quartz import CGEventPost, kCGHIDEventTap
        except Exception as exc:
            log.debug("media-key support unavailable, will fall back to Spotify: %s", exc)
            return None

        def send(key_code):
            for down in (True, False):
                flags = 0xA00 if down else 0xB00
                data1 = (key_code << 16) | ((0xA if down else 0xB) << 8)
                ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                    14, (0, 0), flags, 0, 0, None, 8, data1, -1)
                CGEventPost(kCGHIDEventTap, ev.CGEvent())

        return send

    @staticmethod
    def _spotify(cmd):
        subprocess.run(["osascript", "-e", f'tell application "Spotify" to {cmd}'],
                       check=False)

    def set_volume(self, level):
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {int(level)}"],
            check=False)

    def playpause(self):
        if self._send_media_key is not None:
            try:
                self._send_media_key(16)
                return
            except Exception as exc:
                log.debug("media key play/pause failed, falling back to Spotify: %s", exc)
        self._spotify("playpause")

    def next(self):
        self._spotify("next track")

    def prev(self):
        self._spotify("previous track")

    def now_playing(self):
        """Return {state, track, artist, art_url} for Spotify, or None."""
        try:
            r = subprocess.run(["osascript", "-e", SPOTIFY_SCRIPT],
                               capture_output=True, text=True, timeout=4)
            out = r.stdout.strip()
            if out in ("", "none", "closed"):
                return None
            parts = out.split("|;|")
            if len(parts) < 4:
                return None
            return {"state": parts[0], "track": parts[1],
                    "artist": parts[2], "art_url": parts[3]}
        except Exception as exc:
            log.debug("now-playing query failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Volume applier: debounced background writer so the camera loop never blocks.
# ---------------------------------------------------------------------------
class VolumeApplier:
    def __init__(self, backend, initial=50):
        self.backend = backend
        self._desired = int(initial)
        self._applied = -1
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def set(self, level):
        with self._lock:
            self._desired = int(level)

    def _loop(self):
        while self._running:
            with self._lock:
                v = self._desired
            if v != self._applied:
                self.backend.set_volume(v)
                self._applied = v
            time.sleep(0.05)

    def stop(self):
        self._running = False
        self._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Transport actions, fired off-thread so a slow AppleScript can't stutter video.
# ---------------------------------------------------------------------------
def fire(backend, name):
    def run():
        try:
            getattr(backend, {"playpause": "playpause",
                              "next": "next", "prev": "prev"}[name])()
        except Exception as exc:
            log.debug("action %r failed: %s", name, exc)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Now-playing poller (Spotify)
# ---------------------------------------------------------------------------
def download_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=5).read()
        arr = np.frombuffer(data, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception as exc:
        log.debug("album art download failed: %s", exc)
        return None


class NowPlaying:
    def __init__(self, backend, enabled=True):
        self.backend = backend
        self._data = {"state": "", "track": "", "artist": "", "img": None}
        self._lock = threading.Lock()
        self._running = True
        self._thread = None
        if enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def snapshot(self):
        with self._lock:
            d = self._data
            return d["state"], d["track"], d["artist"], d["img"]

    def _loop(self):
        last_url, cached = None, None
        while self._running:
            info = self.backend.now_playing()
            if info:
                if info["art_url"] and info["art_url"] != last_url:
                    img = download_image(info["art_url"])
                    if img is not None:
                        cached, last_url = img, info["art_url"]
                with self._lock:
                    self._data.update(state=info["state"], track=info["track"],
                                      artist=info["artist"], img=cached)
            else:
                with self._lock:
                    self._data.update(state="", track="", artist="", img=None)
                last_url, cached = None, None
            time.sleep(1.0)

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Hand tracking helpers
# ---------------------------------------------------------------------------
mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils


def _dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def finger_states(lm):
    """Extended-finger flags using distance-from-wrist, so the test still
    holds when the hand is rotated (a fingertip that is farther from the
    wrist than its PIP joint is extended)."""
    wrist = lm[0]
    return tuple(_dist(lm[tip], wrist) > _dist(lm[pip], wrist)
                 for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)))


def classify(fs):
    I, M, R, P = fs
    if I and M and R and P:
        return "playpause"
    if M and R and not P:
        return "volume"
    if I and M and not R and not P:
        return "next"
    if I and not M and not R and not P:
        return "prev"
    if I and not M and not R and P:
        return "toggle"            # rock
    return "neutral"


def pinch(lm, w, h):
    """Thumb-tip / index-tip endpoints (in pixels) and their separation as a
    ratio of palm size (wrist -> middle-finger MCP). The ratio is scale-
    invariant, so volume mapping is unaffected by camera distance."""
    p1 = (int(lm[4].x * w), int(lm[4].y * h))
    p2 = (int(lm[8].x * w), int(lm[8].y * h))
    raw = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    palm = math.hypot((lm[9].x - lm[0].x) * w, (lm[9].y - lm[0].y) * h)
    ratio = raw / palm if palm > 1e-6 else 0.0
    return p1, p2, ratio


def fit(text, n):
    return text if len(text) <= n else text[:max(0, n - 3)] + "..."


# ---------------------------------------------------------------------------
# HUD drawing
# ---------------------------------------------------------------------------
def draw_corner_brackets(frame, w, h):
    s, m = 32, 16
    for x, y, sx, sy in ((m, m, 1, 1), (w - m, m, -1, 1),
                         (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
        cv2.line(frame, (x, y), (x + sx * s, y), CYAN, 2)
        cv2.line(frame, (x, y), (x, y + sy * s), CYAN, 2)


def draw_now_playing(frame, w, h, snapshot):
    st, track, artist, img = snapshot
    px0, py0, px1, py1 = 120, h - 150, w - 20, h - 30
    overlay = frame.copy()
    cv2.rectangle(overlay, (px0, py0), (px1, py1), (20, 20, 20), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    cv2.rectangle(frame, (px0, py0), (px1, py1), (70, 70, 70), 1)

    ax, ay, asz = px0 + 10, py0 + 5, 110
    if img is not None:
        art = cv2.resize(img, (asz, asz))
        frame[ay:ay + asz, ax:ax + asz] = art
    else:
        cv2.rectangle(frame, (ax, ay), (ax + asz, ay + asz), (50, 50, 50), -1)
        cv2.putText(frame, "no art", (ax + 22, ay + 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, DIM, 1)

    tx = ax + asz + 20
    if track:
        icon = ">" if st == "playing" else "||"
        ic = GREEN if st == "playing" else AMBER
        cv2.putText(frame, icon, (tx, py0 + 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, ic, 2)
        cv2.putText(frame, fit(track, 34), (tx + 36, py0 + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, WHITE, 2)
        cv2.putText(frame, fit(artist, 40), (tx, py0 + 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
    else:
        cv2.putText(frame, "Nothing playing (open Spotify)", (tx, py0 + 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, DIM, 1)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Gesture media + volume control for macOS.")
    p.add_argument("--camera", type=int, default=CAM_INDEX,
                   help="camera index (default: %(default)s)")
    p.add_argument("--width", type=int, default=CAM_WIDTH,
                   help="capture width (default: %(default)s)")
    p.add_argument("--height", type=int, default=CAM_HEIGHT,
                   help="capture height (default: %(default)s)")
    p.add_argument("--fps", type=int, default=TARGET_FPS,
                   help="capture fps (default: %(default)s)")
    p.add_argument("--min-ratio", type=float, default=MIN_RATIO,
                   help="pinch/palm ratio mapped to 0%% (default: %(default)s)")
    p.add_argument("--max-ratio", type=float, default=MAX_RATIO,
                   help="pinch/palm ratio mapped to 100%% (default: %(default)s)")
    p.add_argument("--smoothing", type=float, default=SMOOTHING,
                   help="volume smoothing 0..1, higher = smoother (default: %(default)s)")
    p.add_argument("--no-spotify", action="store_true",
                   help="disable the Spotify now-playing poller")
    p.add_argument("--debug", action="store_true",
                   help="verbose logging of backend errors")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    cam = CameraStream(args.camera, args.width, args.height, args.fps)
    if not cam.running:
        log.error("Could not open the webcam. Check Privacy & Security > Camera.")
        return

    backend = MacOSBackend()
    volume = VolumeApplier(backend, initial=50)
    nowplaying = NowPlaying(backend, enabled=not args.no_spotify)
    hands = mp_hands.Hands(
        static_image_mode=False, max_num_hands=1, model_complexity=1,
        min_detection_confidence=0.6, min_tracking_confidence=0.5)

    smoothed_vol = 50.0
    was_volume = False
    master_on = True

    pending = None
    pending_since = 0.0
    fired = False
    last_fire = 0.0
    flash_text, flash_until = "", 0.0

    prev_t = time.time()
    fps = 0.0

    try:
        while True:
            ok, frame = cam.read()
            if not ok or frame is None:
                if not cam.running:
                    log.error("Camera stopped delivering frames; exiting.")
                    break
                time.sleep(0.005)
                continue

            frame = cv2.flip(frame, 1)
            h, w, _ = frame.shape
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            result = hands.process(rgb)

            now = time.time()
            gesture = "none"
            is_volume = False

            if result.multi_hand_landmarks:
                hand = result.multi_hand_landmarks[0]
                lm = hand.landmark
                mp_draw.draw_landmarks(
                    frame, hand, mp_hands.HAND_CONNECTIONS,
                    mp_draw.DrawingSpec(color=(0, 120, 120), thickness=1, circle_radius=2),
                    mp_draw.DrawingSpec(color=(60, 60, 60), thickness=1))
                gesture = classify(finger_states(lm))

                # Volume (only when armed)
                if master_on and gesture == "volume":
                    is_volume = True
                    (tx, ty), (ix, iy), ratio = pinch(lm, w, h)
                    target = float(np.interp(
                        ratio, [args.min_ratio, args.max_ratio], [0, 100]))
                    if not was_volume:
                        smoothed_vol = target
                    smoothed_vol = ((1 - args.smoothing) * target
                                    + args.smoothing * smoothed_vol)
                    desired = int(np.clip(smoothed_vol, 0, 100))
                    volume.set(desired)
                    cv2.circle(frame, (tx, ty), 10, (255, 0, 255), -1)
                    cv2.circle(frame, (ix, iy), 10, (255, 0, 255), -1)
                    cv2.line(frame, (tx, ty), (ix, iy), GREEN, 3)

                # One-shot gestures (toggle always; playback only when armed)
                if gesture in ("playpause", "next", "prev", "toggle"):
                    if gesture != pending:
                        pending, pending_since, fired = gesture, now, False
                    hold_needed = TOGGLE_HOLD if gesture == "toggle" else GESTURE_HOLD
                    allowed = master_on or gesture == "toggle"
                    if (not fired and allowed
                            and now - pending_since >= hold_needed
                            and now - last_fire >= COOLDOWN):
                        if gesture == "toggle":
                            master_on = not master_on
                            flash_text = "ARMED" if master_on else "DISARMED"
                        else:
                            fire(backend, gesture)
                            flash_text = {"playpause": "|>  PLAY / PAUSE",
                                          "next": ">>  NEXT", "prev": "<<  PREVIOUS"}[gesture]
                        last_fire, fired, flash_until = now, True, now + 1.2
                else:
                    pending, fired = None, False

                was_volume = is_volume
            else:
                was_volume = False
                pending, fired = None, False

            # ---- HUD ----
            draw_corner_brackets(frame, w, h)
            active = master_on
            track_top, track_bot = int(h * 0.18), int(h * 0.74)
            bar = int(np.interp(smoothed_vol, [0, 100], [track_bot, track_top]))
            barcol = GREEN if is_volume else (CYAN if active else DIM)
            overlay = frame.copy()
            cv2.rectangle(overlay, (50, bar), (88, track_bot), barcol, -1)
            cv2.addWeighted(overlay, 0.4, frame, 0.6, 0, frame)
            cv2.rectangle(frame, (50, track_top), (88, track_bot), barcol, 2)
            cv2.putText(frame, f"{int(smoothed_vol)}%", (40, track_bot + 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, WHITE, 2)
            cv2.putText(frame, "VOL", (52, track_top - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, barcol, 2)

            # banner + arm indicator
            cv2.rectangle(frame, (0, 0), (w, 42), (25, 25, 25), -1)
            if gesture == "none":
                status, sc = "Show your hand", DIM
            elif not master_on:
                status, sc = "DISARMED - rock to wake", RED
            elif is_volume:
                status, sc = "VOLUME - pinch to set", GREEN
            elif gesture in ("playpause", "next", "prev"):
                status, sc = "hold to confirm...", AMBER
            else:
                status, sc = "READY", CYAN
            cv2.putText(frame, status, (14, 29),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, sc, 2)
            cv2.circle(frame, (w - 210, 21), 8, GREEN if master_on else RED, -1)
            cv2.putText(frame, "ARMED" if master_on else "DISARMED",
                        (w - 195, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        GREEN if master_on else RED, 2)

            # legend + fps
            cv2.putText(frame, "pinch=vol  palm=play  peace=next  point=prev  rock=on/off",
                        (14, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.48, DIM, 1)
            now2 = time.time()
            fps = 0.9 * fps + 0.1 * (1.0 / max(now2 - prev_t, 1e-6))
            prev_t = now2
            cv2.putText(frame, f"FPS {fps:.0f}  a=arm  q=quit", (14, 86),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.48, DIM, 1)

            draw_now_playing(frame, w, h, nowplaying.snapshot())

            if now < flash_until:
                cv2.putText(frame, flash_text, (int(w * 0.30), int(h * 0.50)),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.1, GREEN, 3)

            cv2.imshow("Gesture Media + Volume", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            if key == ord('a'):
                master_on = not master_on
    except KeyboardInterrupt:
        pass
    finally:
        volume.stop()
        nowplaying.stop()
        cam.release()
        cv2.destroyAllWindows()
        hands.close()


if __name__ == "__main__":
    main()
