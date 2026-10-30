# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Playback clocks, all in wall-clock seconds of the recording. Video follows whichever one the player uses: the
audio clock (what the sound card has actually played) when there is sound, else a monotonic one."""

import bisect, threading, time

RATE = 48000

class MonotonicClock:
    def __init__(self, wall=0.0, speed=1.0):
        self._wall, self._speed = float(wall), float(speed)
        self._t0 = None             # monotonic time the clock started running, None while stopped
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._t0 is not None

    def now(self):
        with self._lock:
            if self._t0 is None:
                return self._wall
            return self._wall + (time.monotonic() - self._t0) * self._speed

    def set(self, wall):
        with self._lock:
            self._wall = float(wall)
            if self._t0 is not None:
                self._t0 = time.monotonic()

    def start(self):
        with self._lock:
            if self._t0 is None:
                self._t0 = time.monotonic()

    def stop(self):
        with self._lock:
            if self._t0 is not None:
                self._wall += (time.monotonic() - self._t0) * self._speed
                self._t0 = None

    def set_speed(self, speed):
        with self._lock:
            if self._t0 is not None:
                now = time.monotonic()
                self._wall += (now - self._t0) * self._speed
                self._t0 = now
            self._speed = float(speed)

class AudioClock:
    """Maps sample frames the device has played to wall time. The sound is one continuous stream across chunks, so
    each chunk (or stretch of silence) marks the device frame where it begins and the wall time it shows; between
    marks time advances at speed (atempo squeezed speed seconds of media into each second of sound)."""
    def __init__(self, played, speed=1.0, rate=RATE):
        self.played = played        # () -> sample frames played so far (may be fractional)
        self.speed, self.rate = float(speed), rate
        self._frames, self._walls = [], []
        self._lock = threading.Lock()

    def reset(self, wall, speed=None):
        with self._lock:
            if speed is not None:
                self.speed = float(speed)
            self._frames, self._walls = [0], [float(wall)]

    def mark(self, frame, wall):
        """From device frame `frame` on, the sound shows wall time `wall`."""
        with self._lock:
            if self._frames and frame <= self._frames[-1]:
                self._frames[-1], self._walls[-1] = frame, float(wall)
            else:
                self._frames.append(frame)
                self._walls.append(float(wall))

    def at(self, played):
        with self._lock:
            if not self._frames:
                return None
            i = max(0, bisect.bisect_right(self._frames, played) - 1)
            return self._walls[i] + (played - self._frames[i]) / self.rate * self.speed

    def now(self):
        return self.at(self.played())

class ExternalClock:
    """Another player's clock (Grid followers read the audio player's)."""
    def __init__(self, now):
        self.now = now
