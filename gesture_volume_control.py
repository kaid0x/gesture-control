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

  PLAYBACK (one-shot, hold briefly, when armed):
    Open palm  -> Play / Pause  (anything playing)
    Peace      -> Next track     (Spotify)
    Point      -> Previous track (Spotify)

  NOW PLAYING:
    Shows current Spotify track, artist, and album art at the bottom.

Optional, for universal Play/Pause (YouTube, Apple Music, ...):
    pip install pyobjc-framework-Quartz pyobjc-framework-Cocoa

Quit with the 'q' key.

Requirements:
  pip install opencv-python mediapipe numpy
"""

import time
import threading
import subprocess
import urllib.request

import cv2
import numpy as np
import mediapipe as mp


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
CAM_INDEX = 0
CAM_WIDTH = 1280
CAM_HEIGHT = 720
TARGET_FPS = 30

MIN_DIST = 30
MAX_DIST = 200
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
        if self.running:
            threading.Thread(target=self._update, daemon=True).start()

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
        time.sleep(0.1)
        self.cap.release()


# ---------------------------------------------------------------------------
# Volume worker
# ---------------------------------------------------------------------------
desired_vol = 50
_applied_vol = -1
_running = True


def set_system_volume(level):
    subprocess.run(["osascript", "-e", f"set volume output volume {int(level)}"],
                   check=False)


def volume_worker():
    global _applied_vol
    while _running:
        v = desired_vol
        if v != _applied_vol:
            set_system_volume(v)
            _applied_vol = v
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Playback actions
# ---------------------------------------------------------------------------
def _media_key(key_code):
    from AppKit import NSEvent
    from Quartz import CGEventPost, kCGHIDEventTap
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (key_code << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, data1, -1)
        CGEventPost(kCGHIDEventTap, ev.CGEvent())


def _spotify(cmd):
    subprocess.run(["osascript", "-e", f'tell application "Spotify" to {cmd}'],
                   check=False)


def do_action(name):
    try:
        if name == "playpause":
            try:
                _media_key(16)
            except Exception:
                _spotify("playpause")
        elif name == "next":
            _spotify("next track")
        elif name == "prev":
            _spotify("previous track")
    except Exception:
        pass


def fire(name):
    threading.Thread(target=do_action, args=(name,), daemon=True).start()


# ---------------------------------------------------------------------------
# Now-playing poller (Spotify)
# ---------------------------------------------------------------------------
now_playing = {"state": "", "track": "", "artist": "", "img": None}
np_lock = threading.Lock()

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


def fetch_spotify():
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
    except Exception:
        return None


def download_image(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=5).read()
        arr = np.frombuffer(data, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def now_playing_poller():
    last_url, cached = None, None
    while _running:
        info = fetch_spotify()
        if info:
            if info["art_url"] and info["art_url"] != last_url:
                img = download_image(info["art_url"])
                if img is not None:
                    cached, last_url = img, info["art_url"]
            with np_lock:
                now_playing.update(state=info["state"], track=info["track"],
                                   artist=info["artist"], img=cached)
        else:
            with np_lock:
                now_playing.update(state="", track="", artist="", img=None)
            last_url, cached = None, None
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Hand tracking
# ---------------------------------------------------------------------------
mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils
hands = mp_hands.Hands(
    static_image_mode=False, max_num_hands=1, model_complexity=1,
    min_detection_confidence=0.6, min_tracking_confidence=0.5)


def finger_states(lm):
    return (lm[8].y < lm[6].y, lm[12].y < lm[10].y,
            lm[16].y < lm[14].y, lm[20].y < lm[18].y)


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
    x1, y1 = int(lm[4].x * w), int(lm[4].y * h)
    x2, y2 = int(lm[8].x * w), int(lm[8].y * h)
    return (x1, y1), (x2, y2), float(np.hypot(x2 - x1, y2 - y1))


def fit(text, n):
    return text if len(text) <= n else text[:max(0, n - 3)] + "..."


def draw_corner_brackets(frame, w, h):
    s, m = 32, 16
    for x, y, sx, sy in ((m, m, 1, 1), (w - m, m, -1, 1),
                         (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
        cv2.line(frame, (x, y), (x + sx * s, y), CYAN, 2)
        cv2.line(frame, (x, y), (x, y + sy * s), CYAN, 2)


def draw_now_playing(frame, w, h):
    with np_lock:
        st, track, artist, img = (now_playing["state"], now_playing["track"],
                                  now_playing["artist"], now_playing["img"])
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
# Main loop
# ---------------------------------------------------------------------------
def main():
    global desired_vol, _running

    cam = CameraStream(CAM_INDEX, CAM_WIDTH, CAM_HEIGHT, TARGET_FPS)
    if not cam.running:
        print("Could not open the webcam. Check Privacy & Security > Camera.")
        return

    threading.Thread(target=volume_worker, daemon=True).start()
    threading.Thread(target=now_playing_poller, daemon=True).start()

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

    while True:
        ok, frame = cam.read()
        if not ok or frame is None:
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
                (tx, ty), (ix, iy), dist = pinch(lm, w, h)
                target = float(np.interp(dist, [MIN_DIST, MAX_DIST], [0, 100]))
                if not was_volume:
                    smoothed_vol = target
                smoothed_vol = (1 - SMOOTHING) * target + SMOOTHING * smoothed_vol
                desired_vol = int(np.clip(smoothed_vol, 0, 100))
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
                        fire(gesture)
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

        draw_now_playing(frame, w, h)

        if now < flash_until:
            cv2.putText(frame, flash_text, (int(w * 0.30), int(h * 0.50)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, GREEN, 3)

        cv2.imshow("Gesture Media + Volume", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        if key == ord('a'):
            master_on = not master_on

    _running = False
    cam.release()
    cv2.destroyAllWindows()
    hands.close()


if __name__ == "__main__":
    main()
