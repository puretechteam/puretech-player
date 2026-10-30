# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Clock math and the audio engine, driven step by step against a fake sound device."""

import time
import pytest
from puretech_player.audio import AudioEngine, AudioFeed
from puretech_player.clock import AudioClock, MonotonicClock
from puretech_player.decode import FRAME_BYTES, RATE
from puretech_player.policy import policy
from puretech_player.timeline import Chunk, Source

class FakeDevice:
    """A sound card whose playback position the test moves by hand."""
    buffer_frames = RATE // 5

    def __init__(self):
        self.written = self.played = 0
        self.running = False
        self.volume = None
        self.starts = 0

    def free_frames(self):
        return self.buffer_frames - (self.written - self.played)

    def write(self, data):
        n = len(data) // FRAME_BYTES
        assert n <= self.free_frames()
        self.written += n
        return n

    def position(self):
        return float(self.played), 0          # no qpc: the engine then does not extrapolate

    def play(self, frames):
        self.played = min(self.written, self.played + frames)

    def start(self):
        self.running = True
        self.starts += 1

    def stop(self):
        self.running = False

    def reset(self):
        self.written = self.played = 0

    def set_volume(self, level):
        self.volume = level

    def close(self):
        pass

class FakeReader:
    """Stands in for an AudioDecoder: `frames` of sound, all decoded at once."""
    def __init__(self, frames, tag=0):
        self.data = bytearray([tag]) * (frames * FRAME_BYTES)
        self.eof = True
        self.closed = False

    def read(self, n):
        n = min(n, len(self.data)) // FRAME_BYTES * FRAME_BYTES
        out = bytes(self.data[:n])
        del self.data[:n]
        return out

    def wait(self, timeout):
        pass

    @property
    def exhausted(self):
        return self.eof and len(self.data) < FRAME_BYTES

    def close(self):
        self.closed = True

def chunks(*spans, audio=True):
    out = []
    for i, (start, dur) in enumerate(spans):
        info = {"duration": dur, "start_time": 0.0, "video": {"fps": 30.0, "width": 64, "height": 36, "sar": 1.0},
                "audio": {"codec": "aac", "rate": 48000, "channels": 2} if audio else None}
        out.append(Chunk(f"c{i}.mp4", start, dur, info=info, order=i))
    return Source(out)

def engine():
    dev = FakeDevice()
    eng = AudioEngine(lambda: dev, threaded=False)
    eng.open()
    return eng, dev

# ---- clocks -------------------------------------------------------------------------------------------------
def test_monotonic_clock_pauses_and_changes_speed():
    c = MonotonicClock(100.0)
    assert c.now() == 100.0
    c.start()
    time.sleep(0.05)
    c.set_speed(4.0)
    time.sleep(0.05)
    c.stop()
    t = c.now()
    assert 100.0 + 0.04 + 0.16 < t < 100.0 + 0.1 + 0.4
    time.sleep(0.03)
    assert c.now() == t

def test_audio_clock_maps_frames_to_wall_time():
    played = [0]
    c = AudioClock(lambda: played[0])
    c.reset(1000.0)
    assert c.now() == 1000.0
    played[0] = RATE * 2
    assert c.now() == pytest.approx(1002.0)
    c.mark(RATE * 3, 1050.0)                # next chunk starts at wall 1050 after a skipped gap
    played[0] = RATE * 3 + RATE // 2
    assert c.now() == pytest.approx(1050.5)
    played[0] = RATE * 2.5
    assert c.now() == pytest.approx(1002.5)  # before the mark, the old piece still rules

def test_audio_clock_at_speed():
    played = [RATE]
    c = AudioClock(lambda: played[0])
    c.reset(0.0, speed=2.0)                 # atempo squeezed 2 s of media into each second of sound
    assert c.now() == pytest.approx(2.0)
    c.reset(0.0, speed=0.25)
    assert c.now() == pytest.approx(0.25)

# ---- the engine against a fake device -----------------------------------------------------------------------
def feed_for(src, readers, skip_gaps=True, speed=1.0, index=0, offset=0.0):
    it = iter(readers)
    return AudioFeed(src, index, offset, policy(speed), "ffmpeg", skip_gaps, opener=lambda path, off: next(it))

def test_engine_fills_starts_and_clock_follows_device():
    src = chunks((1000.0, 1.0), (1001.0, 1.0))
    eng, dev = engine()
    eng.play(feed_for(src, [FakeReader(RATE), FakeReader(RATE)]), 1000.0, 1.0)
    eng.pump()
    assert dev.running and dev.written == dev.buffer_frames
    assert eng.clock.now() == 1000.0        # nothing played yet: the picture waits
    dev.play(RATE // 10)
    eng.pump()
    assert eng.clock.now() == pytest.approx(1000.1)

def test_engine_marks_chunk_boundaries_and_skipped_gaps():
    src = chunks((1000.0, 1.0), (1010.0, 1.0))  # a 9 s hole, skipped
    eng, dev = engine()
    eng.play(feed_for(src, [FakeReader(RATE), FakeReader(RATE)]), 1000.0, 1.0)
    for _ in range(20):
        eng.pump()
        dev.play(RATE // 10)
    eng.pump()
    assert eng.clock.at(RATE * 0.5) == pytest.approx(1000.5)
    assert eng.clock.at(RATE * 1.5) == pytest.approx(1010.5)
    assert eng.feed.exhausted and eng.drained and eng.underruns == 0

def test_gap_played_through_is_silence():
    src = chunks((1000.0, 1.0), (1010.0, 1.0))
    eng, dev = engine()
    eng.play(feed_for(src, [FakeReader(RATE, 1), FakeReader(RATE, 2)], skip_gaps=False), 1000.0, 1.0)
    eng.pump()
    assert eng.clock.at(RATE * 5) == pytest.approx(1005.0)      # inside the 9 s of silence
    for _ in range(120):
        dev.play(RATE // 10)
        eng.pump()
    assert eng.clock.at(RATE * 10.5) == pytest.approx(1010.5)

def test_short_sound_is_padded_to_its_chunk():
    src = chunks((1000.0, 2.0), (1002.0, 1.0))
    eng, dev = engine()
    eng.play(feed_for(src, [FakeReader(RATE * 3 // 2), FakeReader(RATE)]), 1000.0, 1.0)
    for _ in range(40):
        eng.pump()
        dev.play(RATE // 10)
    assert eng.clock.at(RATE * 2.25) == pytest.approx(1002.25)  # the next chunk's sound starts on time

def test_chunk_without_sound_plays_silence_for_its_length():
    src = chunks((1000.0, 1.0), (1001.0, 2.0))
    src[1]._info["audio"] = None
    eng, dev = engine()
    eng.play(feed_for(src, [FakeReader(RATE)]), 1000.0, 1.0)
    total = 0
    for _ in range(50):
        eng.pump()
        dev.play(RATE // 10)
    assert dev.written == RATE * 3 and eng.drained

def test_pause_holds_and_underrun_is_counted():
    src = chunks((0.0, 10.0))
    reader = FakeReader(RATE // 10)
    reader.eof = False                       # a decoder that stalls after 0.1 s
    eng, dev = engine()
    eng.play(feed_for(src, [reader]), 0.0, 1.0)
    eng.pump()
    dev.play(RATE)
    eng.pump()
    assert eng.underruns == 1
    eng.pause()
    eng.pump()
    assert not dev.running
    assert eng.clock.now() == pytest.approx(0.1)

def test_newest_seek_wins():
    src = chunks((0.0, 10.0))
    eng, dev = engine()
    first = FakeReader(RATE)
    f1 = feed_for(src, [first])
    f2 = feed_for(src, [FakeReader(RATE)], offset=5.0, index=0)
    eng.play(f1, 0.0, 1.0)
    eng.play(f2, 5.0, 1.0)
    eng.pump()
    assert eng.feed is f2 and eng.clock.now() == 5.0

def test_engine_without_device_reports_failure():
    def broken():
        raise OSError("no sound output")
    eng = AudioEngine(broken, threaded=False)
    eng.open()
    assert eng.failed == "no sound output"
