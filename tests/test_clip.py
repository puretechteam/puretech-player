# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
import os
from puretech_player.clip import ClipPart, ClipPlan, clip_cmd, concat_list, snap_end, snap_start, unused_path

# keyframes every second, frames every 1/30 s
PKTS = [(round(i / 30, 6), i % 30 == 0) for i in range(300)]

def test_snap_start_goes_back_to_keyframe():
    assert snap_start(PKTS, 4.5) == 4.0
    assert snap_start(PKTS, 4.0) == 4.0
    assert snap_start(PKTS, 3.99) == 3.0
    assert snap_start([(2.0, True), (2.5, False)], 1.0) == 2.0      # nothing before: the first keyframe

def test_snap_end_frame_or_keyframe():
    assert abs(snap_end(PKTS, 4.51) - 16 / 30 - 4) < 1e-6           # next frame boundary
    assert snap_end(PKTS, 4.51, keyframe=True) == 5.0
    assert snap_end(PKTS, 99.0) is None                             # past the end: keep to the end

def test_concat_list_quotes_paths_and_points():
    parts = [ClipPart(r"C:\rec\it's\14-00-00.mp4", inpoint=4.0), ClipPart(r"C:\rec\14-01-00.mp4", outpoint=12.5)]
    text = concat_list(parts)
    assert text.startswith("ffconcat version 1.0\n")
    assert "file 'C:/rec/it'\\''s/14-00-00.mp4'\ninpoint 4.000000\n" in text
    assert "file 'C:/rec/14-01-00.mp4'\noutpoint 12.500000\n" in text

def test_clip_command_copies_and_never_overwrites():
    cmd = clip_cmd("ffmpeg", "list.ffconcat", "out.mp4")
    assert cmd[cmd.index("-c") + 1] == "copy" and "-n" in cmd and "-y" not in cmd
    assert cmd[cmd.index("-f") + 1] == "concat" and "+faststart" in cmd
    assert "+faststart" not in clip_cmd("ffmpeg", "l", "out.mkv")

def test_codec_change_splits_into_files():
    a, b = ("h264", 1920, 1080, "yuv420p", "aac", 48000, 2), ("hevc", 1920, 1080, "yuv420p", "aac", 48000, 2)
    plan = ClipPlan((0, 10), 0, 10, [ClipPart("1", signature=a), ClipPart("2", signature=a), ClipPart("3", signature=b)])
    assert [len(g) for g in plan.groups()] == [2, 1]

def test_plan_text_and_duration_without_gaps():
    import time
    t = time.mktime((2025, 3, 4, 14, 2, 8, 0, 0, -1))
    plan = ClipPlan((t, t + 40), t, t + 33, gaps=[(t + 10, t + 13)])
    assert plan.describe() == "Clip will run 14:02:08 to 14:02:41"
    assert plan.duration == 30

def test_unused_path(tmp_path):
    p = tmp_path / "clip.mp4"
    assert unused_path(str(p)) == str(p)
    p.write_bytes(b"x")
    assert unused_path(str(p)) == str(tmp_path / "clip (2).mp4")
