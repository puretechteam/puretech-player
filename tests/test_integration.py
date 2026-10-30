# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""The engine against real FFmpeg on generated clips (see conftest: 3 s chunks at 30 fps, keyframe every second,
chunks at +0, +3, +6 s, a hole from +9 to +19 s, then an HEVC chunk)."""

import json, os, subprocess, threading, time
import pytest
from conftest import FFMPEG, FFPROBE, T0, make_clip, needs_ffmpeg, stamp
from puretech_player import Grid, NullPresenter, Player, Source, export_clip, plan_clip
from puretech_player.timeline import Chunk
from puretech_player.audio import AudioFeed
from puretech_player.decode import RATE, Spares, VideoDecoder
from puretech_player.policy import policy

pytestmark = needs_ffmpeg
DT = 1 / 30

def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False

class Probe:
    """A headless player recording every frame it shows."""
    def __init__(self, source, **kw):
        self.walls, self.states, self.previews = [], [], []
        self.lock = threading.Lock()
        kw.setdefault("audio", False)
        self.player = Player(source, ffmpeg=FFMPEG, presenter=NullPresenter(320, 180, on_frame=self._frame), **kw)
        self.player.on_state = lambda s, info: self.states.append((s, info))

    def _frame(self, f):
        with self.lock:
            (self.previews if f.preview else self.walls).append(f.wall - T0)

    def count(self):
        with self.lock:
            return len(self.walls)

    def last(self):
        with self.lock:
            return self.walls[-1] if self.walls else None

    def shown_after(self, n, timeout=10.0):
        assert wait_for(lambda: self.count() > n, timeout), "no frame came"
        return self.last()

    def close(self):
        self.player.close()

@pytest.fixture
def probe(camera):
    made = []
    def make(source=None, **kw):
        p = Probe(source or camera[0], **kw)
        made.append(p)
        p.shown_after(0)            # opening shows the first frame
        return p
    yield make
    for p in made:
        p.close()

def test_source_from_camera_folder(camera):
    src = Source.from_folder(camera[0], FFPROBE)
    assert [round(c.start - T0) for c in src.chunks] == [0, 3, 6, 19]
    assert [(round(a - T0), round(b - T0)) for a, b in src.gaps()] == [(9, 19)]

def test_opens_on_first_frame(probe):
    p = probe()
    assert p.walls[0] == pytest.approx(0.0, abs=1e-6)
    assert p.player.state == "paused"

def test_plays_across_chunk_boundaries_without_drops(probe):
    p = probe()
    p.player.seek(T0 + 2.0)
    p.shown_after(1)
    n = p.count()
    p.player.play()
    assert wait_for(lambda: (p.last() or 0) >= 4.5, 8)
    p.player.pause()
    walls = p.walls[n - 1:]
    steps = [b - a for a, b in zip(walls, walls[1:])]
    assert walls[0] == pytest.approx(2.0, abs=DT)
    # every frame, including across 3.0 s. One late frame is allowed: run alone this shows 0 drops in a row of
    # runs, but in the full suite other tests' processes shutting down can delay the render thread once
    assert sum(1 for st in steps if st > DT + 0.002) <= 1 and max(steps) < 2 * DT + 0.002
    assert any(abs(w - 3.0) < 0.002 for w in walls)
    assert p.player.stats["dropped"] <= 1

def test_seek_is_frame_accurate(probe):
    p = probe()
    for target in (4.4, 0.77, 7.05, 1.0):
        n = p.count()
        p.player.seek(T0 + target)
        got = p.shown_after(n)
        assert target - 1e-6 <= got < target + DT + 1e-6, (target, got)

def test_seek_shows_the_keyframe_first_then_the_exact_frame(probe):
    p = probe()
    p.player.seek(T0 + 4.4)
    assert p.shown_after(1) == pytest.approx(4.4, abs=DT)
    assert p.previews and p.previews[-1] == pytest.approx(4.0, abs=1e-3)   # keyframes sit on whole seconds
    n = len(p.previews)
    p.player.seek(T0 + 5.0)                                               # a keyframe: no preview needed
    assert p.shown_after(2) == pytest.approx(5.0, abs=1e-3)
    assert len(p.previews) == n

def test_seek_in_a_file_with_offset_timestamps(tmp_path):
    day, clock = stamp(T0)
    path = str(tmp_path / day / f"{clock}.mkv")
    os.makedirs(os.path.dirname(path))
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:d=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-output_ts_offset", "100", path], check=True)
    p = Probe(os.path.dirname(os.path.dirname(path)))
    try:
        assert p.shown_after(0) == pytest.approx(0.0, abs=1e-3)           # wall times count from the file start
        p.player.seek(T0 + 2.4)
        assert p.shown_after(1) == pytest.approx(2.4, abs=DT)
    finally:
        p.close()

def _keyframes(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    return sorted(float(l.split(",")[0]) for l in out if ",K" in l)

@pytest.mark.parametrize("indexed", [True, False])
def test_vvc_seek_lands_on_the_exact_frame_even_where_ffmpeg_overshoots(tmp_path, monkeypatch, indexed):
    day, clock = stamp(T0)
    path = str(tmp_path / "vvc" / day / f"{clock}.mp4")
    os.makedirs(os.path.dirname(path))
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=5",
                    "-c:v", "libvvenc", "-preset", "faster", "-qp", "32", "-threads", "2", path], check=True)
    keys = [k for k in _keyframes(path) if 0.5 < k < 4.0]
    assert keys
    if not indexed:                     # the keyframe index not read (yet): seeks search back SEEK_RETRY instead
        monkeypatch.setattr(Chunk, "keyframes", lambda self: [])
    p = Probe(os.path.dirname(os.path.dirname(path)))
    try:
        p.shown_after(0)
        chunk = p.player.source[0]
        assert wait_for(lambda: (chunk._keys is not None) is indexed or not indexed, 10)
        assert (chunk.keyframe_before(keys[0] + 0.3) == pytest.approx(keys[0], abs=1e-3)) is indexed
        # just after a keyframe, mid-way, and just before the next one (where the demuxer picks the next keyframe)
        for k in keys:
            for into in (0.0, 0.3, 0.9):
                n = p.count()
                p.player.seek(T0 + k + into)
                assert wait_for(lambda: p.count() > n and p.player.state == "paused" and not p.player._back, 10), (k, into, p.player.state, p.player._back, p.count() - n, p.walls[-3:])
                assert k + into - 1e-6 <= p.last() < k + into + DT + 1e-6, (k, into, p.last())
    finally:
        p.close()

def test_sparse_recording_shows_the_frame_on_screen_at_the_target(tmp_path):
    day, clock = stamp(T0)
    path = str(tmp_path / "sparse" / day / f"{clock}.mkv")
    os.makedirs(os.path.dirname(path))
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:d=6",
                    "-vf", r"select='not(mod(n\,60))'", "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast",
                    path], check=True)                                    # frames at 0, 2 and 4 s only
    p = Probe(os.path.dirname(os.path.dirname(path)))
    try:
        p.shown_after(0)
        n = p.count()
        p.player.seek(T0 + 3.0)
        assert wait_for(lambda: p.count() > n and not p.player._back, 10)
        assert p.last() == pytest.approx(2.0, abs=1e-3)
    finally:
        p.close()

def test_step_forward_and_back(probe):
    p = probe()
    p.player.seek(T0 + 1.5)
    start = p.shown_after(1)
    p.player.step(1)
    assert p.shown_after(2) == pytest.approx(start + DT, abs=1e-3)
    p.player.step(-1)
    assert p.shown_after(3) == pytest.approx(start, abs=1e-3)
    p.player.step(-1)
    assert p.shown_after(4) == pytest.approx(start - DT, abs=1e-3)

def test_step_back_after_playing_goes_to_the_frame_before_the_one_shown(probe):
    p = probe()
    p.player.seek(T0 + 1.0)
    p.shown_after(1)
    p.player.play()
    time.sleep(0.5)
    p.player.pause()
    time.sleep(0.2)
    n, shown = p.count(), p.last()
    p.player.step(-1)
    assert p.shown_after(n) == pytest.approx(shown - DT, abs=1e-3)

def test_step_back_across_a_chunk_boundary(probe):
    p = probe()
    p.player.seek(T0 + 3.0)
    assert p.shown_after(1) == pytest.approx(3.0, abs=1e-3)
    p.player.step(-1)
    assert p.shown_after(2) == pytest.approx(3.0 - DT, abs=1e-3)

def test_keyframes_only_from_8x(probe):
    p = probe()
    p.player.set_speed(8)
    p.player.seek(T0 + 0.2)
    p.shown_after(1)
    n = p.count()
    p.player.play()
    assert wait_for(lambda: (p.last() or 0) >= 5.0, 5)
    p.player.pause()
    shown = p.walls[n:]
    assert len(shown) >= 3
    assert all(abs(w - round(w)) < 1e-3 for w in shown)      # keyframes sit on whole seconds

def test_frames_dropped_from_4x(probe):
    p = probe()
    p.player.set_speed(4)
    p.player.seek(T0)
    p.shown_after(1)
    n = p.count()
    p.player.play()
    assert wait_for(lambda: (p.last() or 0) >= 2.5, 5)
    p.player.pause()
    shown = p.walls[n:]
    steps = [b - a for a, b in zip(shown, shown[1:])]
    assert min(steps) >= 4 / 30 - 1e-3                        # at most 30 shown per second of playback
    assert p.player.stats["dropped"] <= 1

def test_gap_is_skipped_and_codec_change_plays(probe):
    p = probe()
    p.player.seek(T0 + 8.0)
    p.shown_after(1)
    p.player.play()
    assert wait_for(lambda: (p.last() or 0) >= 19.3, 6)      # the hole (9 to 19 s) took no time
    p.player.pause()
    gaps = [info for s, info in p.states if s == "gap"]
    assert gaps and gaps[0]["skipped"] and round(gaps[0]["start"] - T0) == 9
    assert min(w for w in p.walls if w > 9) == pytest.approx(19.0, abs=1e-3)

def test_gap_waits_when_not_skipping_and_skip_gap_jumps(probe):
    p = probe(skip_gaps=False)
    p.player.seek(T0 + 12.0)
    assert wait_for(lambda: p.player.state == "gap", 3)
    assert p.player.next_media() == pytest.approx(T0 + 19.0)
    n = p.count()
    p.player.skip_gap()
    assert p.shown_after(n) == pytest.approx(19.0, abs=1e-3)

def test_speed_change_keeps_position(probe):
    p = probe()
    p.player.seek(T0 + 1.0)
    p.shown_after(1)
    p.player.set_speed(0.5)
    p.player.play()
    time.sleep(1.0)
    p.player.pause()
    assert 1.2 < p.last() < 1.8                               # ~0.5 s of media in 1 s

def test_variable_frame_rate_keeps_real_timestamps(tmp_path):
    path = str(tmp_path / "vfr.mkv")
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:d=2",
                    "-vf", r"select='not(mod(n\,3))+eq(n\,1)'", "-fps_mode", "vfr", "-c:v", "libx264", "-preset",
                    "ultrafast", str(path)], check=True)
    want = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                           "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    dec = VideoDecoder(FFMPEG, 160, 90, policy(1.0)).start(path, 0.0)
    got = []
    assert wait_for(lambda: dec.peek() is not None or dec.exhausted, 5)
    while not dec.exhausted:
        f = dec.peek(timeout=1.0)
        if f is None:
            continue
        got.append(f.wall)
        dec.pop().release()
    dec.close()
    assert [round(g, 3) for g in got] == [round(float(w.split(",")[0]), 3) for w in want]
    assert got[2] - got[1] == pytest.approx(2 / 30, abs=1e-3)   # the uneven spacing survived

def test_spares_hand_out_a_started_decoder_and_follow_size_changes():
    spares = Spares(FFMPEG, 2)
    try:
        first = spares.video(320, 180, policy(1.0))
        assert wait_for(lambda: spares._video is not None, 5)
        ready = spares._video
        assert spares.video(320, 180, policy(1.0)) is ready          # the started spare, not a new process
        assert wait_for(lambda: spares._video is not None, 5)
        other = spares.video(640, 360, policy(1.0))                   # new size: the old spare is not reused
        assert other is not ready and other.key[:2] == (640, 360)
        for d in (first, ready, other):
            d.close()
    finally:
        spares.close()

def test_audio_feed_is_continuous_across_chunks(camera):
    src = Source.from_folder(camera[0], FFPROBE)
    feed = AudioFeed(src, 0, 1.0, policy(1.0), FFMPEG, skip_gaps=True)
    total, marks = 0, []
    deadline = time.monotonic() + 20
    while not feed.exhausted and time.monotonic() < deadline:
        data, m = feed.read(RATE * 8)
        marks += [(total + off // 8, round(w - T0, 3)) for off, w in m]
        total += len(data) // 8
        if not data:
            feed.wait(0.05)
    feed.close()
    walls = [w for _, w in marks]
    assert walls == [1.0, 3.0, 6.0, 19.0]
    frames = [f for f, _ in marks]
    assert frames[1] == pytest.approx(2 * RATE, abs=RATE * 0.01)      # chunk 1 from 1 s: 2 s of sound
    assert frames[3] == pytest.approx(8 * RATE, abs=RATE * 0.01)      # gap skipped: no silence written
    assert total == pytest.approx(11 * RATE, abs=RATE * 0.02)

def test_real_sound_device_drives_the_clock(probe):
    p = probe(audio=True, volume=0.0)
    p.player.seek(T0 + 1.5)
    p.shown_after(1)
    n = p.count()
    p.player.play()
    ok = wait_for(lambda: (p.last() or 0) >= 4.0, 8)
    p.player.pause()
    eng = p.player._audio
    if eng is None or eng.failed:
        pytest.skip(f"no sound output: {eng and eng.failed}")
    assert ok and p.player._use_audio
    assert eng.underruns == 0
    walls = p.walls[n:]
    steps = [b - a for a, b in zip(walls, walls[1:])]
    assert max(steps) == pytest.approx(DT, abs=0.002) and p.player.stats["dropped"] <= 1

def _probe_json(path, *entries):
    out = subprocess.run([FFPROBE, "-v", "error", *entries, "-of", "json", path], capture_output=True, text=True)
    return json.loads(out.stdout)

def test_clip_plan_snaps_to_keyframe_and_exports_stream_copy(camera, tmp_path):
    src = Source.from_folder(camera[0], FFPROBE)
    plan = plan_clip(src, T0 + 1.5, T0 + 7.5, FFPROBE)
    assert plan.start == pytest.approx(T0 + 1.0)                   # keyframe before 1.5 s
    assert plan.end == pytest.approx(T0 + 7.5, abs=1e-3)           # frame boundary
    assert plan.describe() == f"Clip will run 14:02:01 to 14:02:07"
    out = export_clip(plan, str(tmp_path / "clip.mp4"), FFMPEG)
    assert out == [str(tmp_path / "clip.mp4")]
    info = _probe_json(out[0], "-show_entries", "format=duration:stream=codec_name,codec_type")
    assert {s["codec_type"]: s["codec_name"] for s in info["streams"]} == {"video": "h264", "audio": "aac"}
    assert float(info["format"]["duration"]) == pytest.approx(plan.duration, abs=0.1)
    # stream copy: the clip starts on the source's 1 s keyframe and its packets are the source's packets (the first
    # one carries the codec parameters in-band, so its size differs), and every decoded frame is bit-identical
    def sizes(path, start=None):
        args = ["-select_streams", "v:0", "-show_entries", "packet=size,flags"]
        if start is not None:
            args += ["-read_intervals", f"{start}%+0.3"]
        return [(p["size"], p["flags"][0]) for p in _probe_json(path, *args)["packets"]][:8]
    assert sizes(out[0])[1:] == sizes(src[0].path, 1.0)[1:]
    assert sizes(out[0])[0][1] == "K"
    def frame_md5(path, start=None):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"] + (["-ss", str(start)] if start else [])
        cmd += ["-i", path, "-map", "0:v:0", "-frames:v", "45", "-f", "framemd5", "-"]
        lines = subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()
        return [l.rsplit(",", 1)[1].strip() for l in lines if l and not l.startswith("#")]
    assert frame_md5(out[0]) == frame_md5(src[0].path, 1.0)
    again = export_clip(plan, str(tmp_path / "clip.mp4"), FFMPEG)
    assert again == [str(tmp_path / "clip (2).mp4")]            # never overwrites

def test_clip_across_a_codec_change_writes_one_file_per_codec(camera, tmp_path):
    src = Source.from_folder(camera[0], FFPROBE)
    plan = plan_clip(src, T0 + 7.2, T0 + 20.5, FFPROBE)
    assert [(round(a - T0), round(b - T0)) for a, b in plan.gaps] == [(9, 19)]
    out = export_clip(plan, str(tmp_path / "x.mp4"), FFMPEG)
    codecs = [_probe_json(f, "-show_entries", "stream=codec_name", "-select_streams", "v:0")["streams"][0]["codec_name"]
              for f in out]
    assert codecs == ["h264", "hevc"]

def test_clip_with_offset_timestamps(tmp_path):
    day, clock = stamp(T0)
    path = str(tmp_path / day / f"{clock}.mkv")
    os.makedirs(os.path.dirname(path))
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:d=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-sc_threshold", "0",
                    "-output_ts_offset", "100", path], check=True)
    src = Source.from_files([path], FFPROBE)
    plan = plan_clip(src, T0 + 2.4, T0 + 3.5, FFPROBE)
    assert plan.start == pytest.approx(T0 + 2.0, abs=1e-3)
    assert plan.end == pytest.approx(T0 + 3.5, abs=0.02)

def test_grid_keeps_tiles_on_one_clock(camera, tmp_path):
    second = str(tmp_path / "cam2")
    for off in (1, 4):
        day, clock = stamp(T0 + off)
        make_clip(os.path.join(second, day, f"{clock}.mp4"), 3, "h264", 300)
    seen = [[], []]
    pres = [NullPresenter(160, 90, on_frame=lambda f, i=i: seen[i].append((time.monotonic(), f.wall)))
            for i in range(2)]
    grid = Grid([camera[0], second], ffmpeg=FFMPEG, presenters=pres, audio=None)
    try:
        assert wait_for(lambda: seen[0] and seen[1], 5)
        grid.seek(T0 + 2.0)
        time.sleep(0.5)
        grid.play()
        time.sleep(2.0)
        grid.pause()
        time.sleep(0.2)
        a, b = seen[0][-1][1], seen[1][-1][1]
        assert abs(a - b) < 0.1 and a > T0 + 3.5
    finally:
        grid.close()
