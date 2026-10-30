# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Sample recordings made with FFmpeg (testsrc2 picture, sine sound) in temp folders. Never real recordings."""

import os, shutil, subprocess, sys, time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not on PATH")

def make_clip(path, seconds, codec="h264", freq=440, audio=True, size="320x180", fps=30, gop=30, threads=2):
    """A testsrc2 clip with a keyframe every `gop` frames (and no B-frame surprises from scene cuts)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}"]
    if codec == "hevc":
        cmd += ["-c:v", "libx265", "-preset", "ultrafast",
                "-x265-params", f"keyint={gop}:min-keyint={gop}:scenecut=0:log-level=error"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    cmd += ["-pix_fmt", "yuv420p", "-threads", str(threads)]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"]
    cmd.append(path)
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    return path

def stamp(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)), time.strftime("%H-%M-%S", time.localtime(t))

T0 = time.mktime((2025, 3, 4, 14, 2, 0, 0, 0, -1))

@pytest.fixture(scope="session")
def camera(tmp_path_factory):
    """A camera folder: three back-to-back 3 s chunks (440/660/880 Hz), a 10 s hole, then an HEVC chunk."""
    root = str(tmp_path_factory.mktemp("camera"))
    layout = [(0, "h264", 440), (3, "h264", 660), (6, "h264", 880), (19, "hevc", 550)]
    paths = []
    for offset, codec, freq in layout:
        day, clock = stamp(T0 + offset)
        paths.append(make_clip(os.path.join(root, day, f"{clock}.mp4"), 3, codec, freq))
    return root, paths
