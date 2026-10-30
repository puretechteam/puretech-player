# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Lossless clips: the packets between an in and an out time copied (-c copy) through FFmpeg's concat demuxer,
across as many chunks as the clip spans. Nothing is re-encoded, so a clip can only start on a keyframe:
plan_clip() finds the real start and end first, so the UI can say "Clip will run 14:02:08 to 14:02:41"."""

import os, tempfile
from . import proc
from .decode import concat_path
from .timeline import Source, fmt_wall

END_SCAN = 1.0          # seconds of packets read past the out point to find the frame boundary
KEY_SCAN = 600.0        # seconds read forward looking for the next keyframe (screen GOPs are a minute)

def packets(ffprobe, path, start=None, length=None):
    """[(pts seconds in file time, keyframe)] of the first video stream, sorted by pts. start/length (file time)
    limit the read: ffprobe seeks to the keyframe at or before start, so that keyframe is always included."""
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
           "-of", "csv=p=0"]
    if start is not None:
        cmd += ["-read_intervals", f"{max(0.0, start):.6f}%" + (f"+{length:.6f}" if length else "")]
    cmd.append(path)
    p = proc.run(cmd, timeout=120)
    out = []
    for line in p.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            out.append((float(parts[0]), "K" in parts[1]))
        except ValueError:
            continue
    out.sort()
    return out

class ClipPart:
    """One file's share of a clip. inpoint/outpoint: file time (None: from the start / to the end, exclusive)."""
    def __init__(self, path, inpoint=None, outpoint=None, signature=()):
        self.path, self.inpoint, self.outpoint, self.signature = path, inpoint, outpoint, signature

    def __repr__(self):
        return f"ClipPart({self.path!r}, inpoint={self.inpoint}, outpoint={self.outpoint})"

class ClipPlan:
    """requested: (in, out) wall times asked for; start: wall time the clip really starts (a keyframe); end: where
    it really ends; gaps: holes inside the clip, which the clip closes up."""
    def __init__(self, requested, start, end, parts=None, gaps=None, container=".mp4"):
        self.requested, self.start, self.end = requested, start, end
        self.parts, self.gaps, self.container = parts or [], gaps or [], container

    def __repr__(self):
        return f"ClipPlan(start={self.start}, end={self.end}, parts={len(self.parts)}, gaps={self.gaps})"

    @property
    def duration(self):
        """Seconds of media in the clip (gaps are not in the file)."""
        return max(0.0, self.end - self.start - sum(b - a for a, b in self.gaps))

    def describe(self):
        return f"Clip will run {fmt_wall(self.start)} to {fmt_wall(self.end)}"

    def groups(self):
        """Runs of parts that can share one file (same codecs and sizes): concat copy cannot mix codecs."""
        out = []
        for part in self.parts:
            if out and out[-1][-1].signature == part.signature:
                out[-1].append(part)
            else:
                out.append([part])
        return out

def _signature(info):
    v, a = info.get("video") or {}, info.get("audio") or {}
    return (v.get("codec"), v.get("width"), v.get("height"), v.get("pix_fmt"), a.get("codec"), a.get("rate"),
            a.get("channels"))

def snap_start(pkts, t):
    """Pts of the last keyframe at or before t (file time), or the first keyframe if none is."""
    keys = [p for p, k in pkts if k]
    if not keys:
        return pkts[0][0] if pkts else t
    before = [k for k in keys if k <= t + 1e-6]
    return before[-1] if before else keys[0]

def snap_end(pkts, t, keyframe=False):
    """Exclusive out point at or after t (file time): the next frame boundary, or with keyframe=True the next
    keyframe (no frame then depends on one left out). None: t is past the last frame (keep to the end)."""
    later = [p for p, k in pkts if p >= t - 1e-6 and (k or not keyframe)]
    return later[0] if later else None

def plan_clip(source, t_in, t_out, ffprobe=None, keyframe_end=False):
    """Where a stream-copied clip from t_in to t_out (wall times) really starts and ends."""
    ffprobe = ffprobe or proc.find_tool("ffprobe")
    if not isinstance(source, Source):
        source = Source.coerce(source, ffprobe)
    if t_out <= t_in:
        raise ValueError("the clip's end must come after its start")
    a, b = source.locate(t_in), source.locate(t_out)
    if a.ended:
        raise ValueError("the clip starts after the recording ends")
    i_in, i_out = a.index, b.index
    if b.gap:                       # ends in a hole: stop where the chunk before it stops
        i_out, b = b.index - 1, None
    if i_out < i_in:
        raise ValueError("there is no footage between those times")
    parts, gaps = [], []
    start = end = None
    for i in range(i_in, i_out + 1):
        c = source[i]
        info = c.info
        st = info.get("start_time") or 0.0
        part = ClipPart(c.path, signature=_signature(info))
        if i == i_in and a.offset > 0:
            near = packets(ffprobe, c.path, st + a.offset, 0.5)
            k = snap_start(near, st + a.offset)
            if k > st + a.offset + 1e-6:       # the seek missed: read from the top to find it
                k = snap_start(packets(ffprobe, c.path, None, None), st + a.offset)
            part.inpoint = k
            start = c.start + (k - st)
        elif i == i_in:
            start = c.start
        if i == i_out and b is not None and not b.ended:
            t = st + b.offset
            near = packets(ffprobe, c.path, t, KEY_SCAN if keyframe_end else END_SCAN)
            out = snap_end(near, t, keyframe_end)
            part.outpoint = out
            end = c.start + (out - st) if out is not None else source.end_of(i)
        elif i == i_out:
            end = source.end_of(i)
        if i > i_in:
            follow = source.follow(i - 1)
            if follow and follow[2]:
                gaps.append(follow[2])
        parts.append(part)
    ext = os.path.splitext(source[i_in].path)[1].lower() or ".mp4"
    return ClipPlan((t_in, t_out), start, end, parts, gaps, ext)

def concat_list(parts):
    lines = ["ffconcat version 1.0"]
    for p in parts:
        lines.append(f"file {concat_path(p.path)}")
        if p.inpoint is not None:
            lines.append(f"inpoint {p.inpoint:.6f}")
        if p.outpoint is not None:
            lines.append(f"outpoint {p.outpoint:.6f}")
    return "\n".join(lines) + "\n"

def clip_cmd(ffmpeg, list_path, out_path):
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-n", "-f", "concat", "-safe", "0",
           "-i", list_path, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    if out_path.lower().endswith((".mp4", ".mov")):
        cmd += ["-movflags", "+faststart"]
    return cmd + [out_path]

def unused_path(path):
    """path, or "name (2).ext", "name (3).ext"... so nothing existing is ever overwritten."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"

def export_clip(plan, out_path, ffmpeg=None):
    """Write the clip; returns the new files (several only when the codec changes inside the clip)."""
    ffmpeg = ffmpeg or proc.find_tool("ffmpeg")
    groups = plan.groups()
    stem, ext = os.path.splitext(out_path)
    ext = ext or plan.container
    written = []
    for n, group in enumerate(groups, 1):
        target = unused_path(f"{stem}{ext}" if len(groups) == 1 else f"{stem}-{n}{ext}")
        fd, list_path = tempfile.mkstemp(prefix="ptp-clip-", suffix=".ffconcat")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(concat_list(group))
            p = proc.run(clip_cmd(ffmpeg, list_path, target), timeout=3600)
        finally:
            os.remove(list_path)
        if p.returncode != 0 or not os.path.exists(target):
            raise OSError(f"clip export failed: {(p.stderr or '').strip()[-300:]}")
        written.append(target)
    return written
