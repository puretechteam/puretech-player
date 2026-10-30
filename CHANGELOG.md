# Changelog

## 0.1.0 - 2026-10-30

- First version of the engine: plays a folder of chunked recordings (HH-MM-SS files in YYYY-MM-DD day folders) as one timeline, crossing from file to file without a pause and keeping sound continuous.
- Plays H.264, H.265, AV1 and H.266 through FFmpeg; a codec change between chunks just works.
- Drawn with Direct3D 11: FFmpeg sends YUV and the graphics card converts and scales it. Falls back to GDI by itself.
- Sound through WASAPI shared mode; the sound card's clock leads and the picture follows. Files without sound play on a steady clock.
- Speeds from 0.25x to 16x: sound up to 2x (pitch kept), frames thinned from 4x, keyframes only from 8x.
- Frame-accurate seeking that shows the keyframe at once and then the exact frame, also where FFmpeg's H.266 decoder starts late or a screen recording has no frame at the target, and stepping one frame forward or back (also across chunk boundaries).
- Gaps between recordings are reported, and skipped or waited through.
- Grid: several sources on one clock, sound from the one you pick.
- Lossless clip export (stream copy across chunks) that first tells you the real keyframe-snapped start and end.
- `demo.py` to open a folder and try it all; `tools/measure.py` to compare against ffplay.
- `warm_up()` starts Direct3D in the background when your app starts, and every view shares one device, so a video's first frame never waits for the graphics driver (about 200 ms).
- H.266 seeks start on the right keyframe, from a keyframe index each file gets in the background: the exact frame shows in 251 ms instead of 452 ms at 1080p30.
- Less memory: FFmpeg keeps one finished frame waiting instead of eight, and the player one queued frame instead of two (AV1 1080p30 135 to 112 MB, H.264 151 to 124 MB).
- Less CPU: FFmpeg's log is read by the frame reader between frames, and the sound thread wakes every 50 ms.
- Fixed: a seek that landed exactly on a keyframe could show the frame without reporting its time (`on_time`).
- Measured against ffplay 9.0.1 on 8 files (H.264, H.265, AV1, H.266 at 1080p30 and 720p15): lower or equal CPU on all 8 (H.264 1080p30 16.9% of a core against 21.9%, H.266 52.0% against 107.9%), less memory on 7 (H.264 1080p30 124 against 166 MB), faster seeks on 6 (29 to 54 ms against 41 to 64 ms), faster or equal first frame on all 8, no dropped frames, and 128 KB of Python against a 15.1 MB ffplay.exe. H.266 seeks (230 against 182 ms to the first picture) and H.266 1080p30 memory (732 against 713 MB) are still behind; the README has every number.
