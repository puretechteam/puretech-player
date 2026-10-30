# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""What each playback speed does to the decoders. Pure functions, so the rules are tested without FFmpeg."""

from collections import namedtuple

MIN_SPEED, MAX_SPEED = 0.25, 16.0
SPEEDS = (0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 8.0, 16.0)   # the steps faster()/slower() walk through
AUDIO_MAX = 2.0          # audio plays (pitch kept by atempo) up to here, muted above
DROP_FROM = 4.0          # from here the decoder drops frames so at most DISPLAY_FPS are converted and drawn
KEYS_FROM = 8.0          # from here only keyframes are decoded
DISPLAY_FPS = 30.0

class Policy(namedtuple("Policy", "speed audio atempo min_gap keyframes_only")):
    """audio: play sound (else the clock is monotonic). atempo: atempo factors, each within the filter's 0.5..2
    range. min_gap: 0, or media seconds kept between shown frames."""
    __slots__ = ()

    @property
    def video_key(self):
        """Decoder settings that need a new video decoder when they change (speed alone does not)."""
        return (self.min_gap, self.keyframes_only)

def clamp(speed):
    return min(MAX_SPEED, max(MIN_SPEED, float(speed)))

def atempo_chain(speed):
    """atempo factors whose product is speed, each in 0.5..2 (one filter covers 0.5x to 2x; 0.25x needs two)."""
    factors, s = [], float(speed)
    while s < 0.5:
        factors.append(0.5)
        s /= 0.5
    while s > 2.0:
        factors.append(2.0)
        s /= 2.0
    if abs(s - 1.0) > 1e-9 or not factors:
        factors.append(round(s, 6))
    return tuple(f for f in factors if abs(f - 1.0) > 1e-9)

def policy(speed):
    s = clamp(speed)
    audio = s <= AUDIO_MAX
    return Policy(speed=s, audio=audio, atempo=atempo_chain(s) if audio else (),
                  min_gap=round(s / DISPLAY_FPS, 6) if s >= DROP_FROM else 0.0, keyframes_only=s >= KEYS_FROM)

def step_speed(speed, direction):
    """Next listed speed up (direction > 0) or down from speed."""
    if direction > 0:
        return next((s for s in SPEEDS if s > speed + 1e-9), SPEEDS[-1])
    return next((s for s in reversed(SPEEDS) if s < speed - 1e-9), SPEEDS[0])
