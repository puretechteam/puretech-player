# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
import math
import pytest
from puretech_player.decode import audio_cmd, concat_script, fit, video_cmd, video_filter
from puretech_player.policy import atempo_chain, clamp, policy, step_speed

@pytest.mark.parametrize("speed", [0.25, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0])
def test_atempo_chain_multiplies_to_speed_within_limits(speed):
    chain = atempo_chain(speed)
    assert all(0.5 <= f <= 2.0 for f in chain)
    assert math.prod(chain) == pytest.approx(speed)

def test_atempo_chain_shapes():
    assert atempo_chain(1.0) == ()
    assert atempo_chain(0.25) == (0.5, 0.5)
    assert atempo_chain(2.0) == (2.0,)

def test_speed_range_is_clamped():
    assert clamp(0.1) == 0.25 and clamp(40) == 16.0

@pytest.mark.parametrize("speed,audio,dropping,keys", [
    (0.25, True, False, False), (1.0, True, False, False), (2.0, True, False, False),
    (3.0, False, False, False), (4.0, False, True, False), (8.0, False, True, True), (16.0, False, True, True)])
def test_policy_thresholds(speed, audio, dropping, keys):
    p = policy(speed)
    assert p.audio is audio
    assert bool(p.min_gap) is dropping
    assert p.keyframes_only is keys
    assert (p.atempo != ()) is (audio and speed != 1.0)

def test_drop_keeps_30_frames_per_second_of_screen_time():
    assert policy(4.0).min_gap == pytest.approx(4 / 30, abs=1e-6)
    assert policy(16.0).min_gap == pytest.approx(16 / 30, abs=1e-6)

def test_video_key_only_changes_at_thresholds():
    assert policy(1.0).video_key == policy(2.0).video_key == policy(3.0).video_key
    assert policy(3.0).video_key != policy(4.0).video_key
    assert policy(4.0).video_key != policy(8.0).video_key

def test_step_speed_walks_the_list():
    assert step_speed(1.0, 1) == 1.5 and step_speed(1.0, -1) == 0.5
    assert step_speed(16.0, 1) == 16.0 and step_speed(0.25, -1) == 0.25

def test_video_command_per_speed():
    cmd = video_cmd("ffmpeg", 640, 360, policy(1.0))
    # started idle: the file comes later as an ffconcat script on stdin, at the original timestamps
    assert cmd[cmd.index("-i") + 1] == "-" and cmd[cmd.index("-f") + 1] == "concat" and "-copyts" in cmd
    assert "-nostdin" not in cmd and "-skip_frame" not in cmd
    assert cmd[cmd.index("-fps_mode") + 1] == "passthrough" and cmd[-3:] == ["-f", "rawvideo", "pipe:1"]
    assert cmd[cmd.index("-thread_queue_size") + 1] == "1" and cmd.index("-thread_queue_size") > cmd.index("-i")
    vf = video_filter(640, 360, policy(1.0))
    assert vf.startswith("select='eq(n\\,0)+gte(t\\,0)'")       # keyframe preview, then from the in point
    assert "scale=w=640:h=360:force_original_aspect_ratio=decrease" in vf and vf.endswith("showinfo=checksum=0")
    fast = video_cmd("ffmpeg", 640, 360, policy(8.0))
    assert fast[fast.index("-skip_frame") + 1] == "nokey" and fast.index("-skip_frame") < fast.index("-i")
    assert "prev_selected_t" in video_filter(640, 360, policy(4.0))
    assert "prev_selected_t" not in vf
    assert cmd.index("-max_frame_delay") < cmd.index("-i")             # AV1 keeps one frame in flight
    assert "-filter_threads" not in cmd                                # BGRA is scaled here: keep slice threads
    yuv = video_cmd("ffmpeg", 640, 360, policy(1.0), fmt="yuv420p")
    assert yuv[yuv.index("-filter_threads") + 1] == "1" and yuv.index("-filter_threads") > yuv.index("-i")

def test_concat_script_quotes_and_in_point():
    assert concat_script(r"C:\rec\it's.mp4") == "ffconcat version 1.0\nfile 'C:/rec/it'\\''s.mp4'\n"
    assert concat_script(r"C:\a.mkv", 12.5).endswith("inpoint 12.500000\n")

def test_audio_command():
    cmd = audio_cmd("ffmpeg", policy(0.25))
    assert cmd[cmd.index("-af") + 1] == "atrim=start=0,atempo=0.5,atempo=0.5"
    assert cmd[-7:] == ["-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"]
    assert audio_cmd("ffmpeg", policy(1.0))[cmd.index("-af") + 1] == "atrim=start=0"

def test_fit_keeps_aspect_and_even_sizes():
    assert fit(1920, 1080, 1.0, 1280, 800) == (1280, 720)
    assert fit(1920, 1080, 1.0, 1000, 1000) == (1000, 562)
    assert fit(720, 576, 16 / 15, 1920, 1080) == (1440, 1080)
    assert fit(1280, 720, 1.0, 1, 1) == (2, 2)
