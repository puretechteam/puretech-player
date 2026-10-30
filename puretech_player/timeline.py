# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""A source: one camera's (or screen's) chunk files laid out on the wall clock.

Chunks are named HH-MM-SS[-n].ext inside YYYY-MM-DD day folders, so a chunk's start comes from its name and its
length from ffprobe. Probing is lazy (a day of one-minute chunks is 1440 files): a chunk is probed when playback
or a seek reaches it, and until then it is assumed to run up to the next chunk's start."""

import bisect, json, os, re, threading, time
from collections import namedtuple
from . import proc

EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")
GAP_TOLERANCE = 1.5     # seconds: names are whole seconds, so a smaller hole between chunks is rounding, not a gap
PREFETCH_LEAD = 3.0     # seconds of media before a chunk ends when the next chunk's decoders start
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[ _T](\d{2})[-.:](\d{2})[-.:](\d{2})")
_CLOCK = re.compile(r"^(\d{2})-(\d{2})-(\d{2})(?:-(\d+))?$")

def parse_start(path):
    """(wall-clock start in epoch seconds, same-second index) from <YYYY-MM-DD>/<HH-MM-SS[-n]>.ext or a name holding
    "YYYY-MM-DD HH-MM-SS"; (None, 0) if neither."""
    stem = os.path.splitext(os.path.basename(path))[0]
    day = os.path.basename(os.path.dirname(os.path.abspath(path)))
    m = _CLOCK.match(stem)
    if m and _DAY.match(day):
        try:
            t = time.mktime(time.strptime(f"{day} {m[1]}-{m[2]}-{m[3]}", "%Y-%m-%d %H-%M-%S"))
            return t, int(m[4] or 0)
        except ValueError:
            pass
    m = _STAMP.search(stem)
    if m:
        try:
            return time.mktime(time.strptime(f"{m[1]} {m[2]}-{m[3]}-{m[4]}", "%Y-%m-%d %H-%M-%S")), 0
        except ValueError:
            pass
    return None, 0

def _num(value, default=None):
    try:
        v = float(value)
        return v if v == v else default
    except (TypeError, ValueError):
        return default

def _rate(text):
    try:
        n, d = (text or "0/0").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except ValueError:
        return 0.0

def probe(path, ffprobe):
    """What playback needs to know about one file: duration, start_time, video and audio stream facts."""
    p = proc.run([ffprobe, "-v", "error", "-show_entries",
                  "format=duration,start_time:stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
                  "r_frame_rate,sample_aspect_ratio,pix_fmt,sample_rate,channels,start_time,duration",
                  "-of", "json", path], timeout=30)
    data = json.loads(p.stdout or "{}")
    fmt = data.get("format", {})
    info = {"duration": _num(fmt.get("duration")), "start_time": _num(fmt.get("start_time"), 0.0),
            "video": None, "audio": None}
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and info["video"] is None:
            sar = s.get("sample_aspect_ratio", "1:1")
            try:
                sn, sd = (int(x) for x in sar.split(":"))
                sar = sn / sd if sn and sd else 1.0
            except ValueError:
                sar = 1.0
            fps = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
            info["video"] = {"codec": s.get("codec_name"), "width": int(s.get("width") or 0),
                             "height": int(s.get("height") or 0), "fps": fps if 0 < fps < 1000 else 30.0,
                             "sar": sar, "pix_fmt": s.get("pix_fmt")}
        elif kind == "audio" and info["audio"] is None:
            info["audio"] = {"codec": s.get("codec_name"), "rate": int(s.get("sample_rate") or 0),
                             "channels": int(s.get("channels") or 0)}
    if p.returncode != 0 and info["video"] is None and info["audio"] is None:
        raise OSError(f"ffprobe could not read {path}: {(p.stderr or '').strip()[-200:]}")
    return info

def keyframe_times(path, ffprobe):
    """Sorted keyframe times (file time) of the first video stream, from the packet index (no decoding)."""
    p = proc.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
                  "-of", "csv=p=0", path], timeout=60)
    keys = []
    for line in (p.stdout or "").splitlines():
        t, _, flags = line.strip().partition(",")
        if "K" in flags:
            v = _num(t)
            if v is not None:
                keys.append(v)
    return sorted(keys)

class Chunk:
    """One file on the wall clock. duration/info are probed on first use unless given."""
    def __init__(self, path, start, duration=None, info=None, order=0, ffprobe=None):
        self.path, self.start, self.order = path, float(start), order
        self._duration, self._info, self._ffprobe = duration, info, ffprobe
        self._lock = threading.Lock()
        self._keys = None
        self._keys_lock = threading.Lock()

    def __repr__(self):
        return f"Chunk({os.path.basename(self.path)!r}, start={self.start:.2f}, duration={self._duration})"

    @property
    def probed(self):
        return self._info is not None

    @property
    def info(self):
        with self._lock:
            if self._info is None:
                if not self._ffprobe:
                    self._info = {"duration": self._duration, "start_time": 0.0, "video": None, "audio": None}
                else:
                    self._info = probe(self.path, self._ffprobe)
                if self._duration is None:
                    self._duration = self._info["duration"]
            return self._info

    def keyframes(self):
        """Keyframe times (file time, sorted), read once on first call (tens of milliseconds: call it off the
        render thread); [] if unknown."""
        with self._keys_lock:
            if self._keys is None:
                try:
                    self._keys = keyframe_times(self.path, self._ffprobe) if self._ffprobe else []
                except (OSError, ValueError):
                    self._keys = []
            return self._keys

    def keyframe_before(self, t):
        """The last keyframe at or before file time t, if the index is read and covers t (a file still being
        written may have grown past it), else None. Never reads the index."""
        keys = self._keys
        if not keys or t < keys[0]:
            return None
        i = bisect.bisect_right(keys, t + 1e-6) - 1
        if i + 1 == len(keys) and (len(keys) < 2 or t - keys[i] > keys[i] - keys[i - 1]):
            return None
        return keys[i]

    @property
    def duration(self):
        """Seconds of media, or None for a file still being written (no length yet)."""
        if self._duration is None and self._info is None:
            self.info
        return self._duration

    @property
    def end(self):
        d = self.duration
        return None if d is None else self.start + d

    @property
    def has_audio(self):
        return self.info["audio"] is not None

    @property
    def has_video(self):
        return self.info["video"] is not None

class Spot(namedtuple("Spot", "index offset gap ended", defaults=(None, False))):
    """Where a wall time falls. index: the chunk to play; offset: seconds into it. gap: (start, end) when the time
    is in a hole between chunks (index/offset then point at the start of the chunk after it); ended: past the end."""
    __slots__ = ()

class Source:
    """Chunks of one source, sorted on the wall clock."""
    def __init__(self, chunks, name="", gap_tolerance=GAP_TOLERANCE):
        self.chunks = sorted(chunks, key=lambda c: (c.start, c.order, c.path))
        if not self.chunks:
            raise ValueError("a source needs at least one media file")
        self.name = name
        self.gap_tolerance = gap_tolerance
        self._starts = [c.start for c in self.chunks]

    def __len__(self):
        return len(self.chunks)

    def __getitem__(self, i):
        return self.chunks[i]

    # ---- building ----------------------------------------------------------------------------------------------
    @classmethod
    def coerce(cls, value, ffprobe=None):
        """A Source from a Source, a folder, one file, or a list of paths / (path, start[, duration]) tuples."""
        if isinstance(value, Source):
            return value
        if isinstance(value, (str, os.PathLike)):
            value = os.fspath(value)
            if os.path.isdir(value):
                return cls.from_folder(value, ffprobe)
            return cls.from_files([value], ffprobe)
        items = list(value)
        if items and isinstance(items[0], Chunk):
            return cls(items)
        if items and isinstance(items[0], (tuple, list)):
            chunks = [Chunk(it[0], it[1], it[2] if len(it) > 2 else None, ffprobe=ffprobe, order=i)
                      for i, it in enumerate(items)]
            return cls(chunks)
        return cls.from_files(items, ffprobe)

    @classmethod
    def from_files(cls, paths, ffprobe=None, name=""):
        """Starts from the names. Files whose names hold no time follow the file before them (probing its length),
        so a plain list of clips plays back to back."""
        chunks, prev = [], None
        named = [(p, *parse_start(p)) for p in paths]
        if any(t is not None for _, t, _ in named):
            named.sort(key=lambda x: (x[1] if x[1] is not None else float("inf"), x[2]))
        for path, start, order in named:
            if start is None:
                if prev is None:
                    start = 0.0
                else:
                    start = prev.end if prev.end is not None else prev.start
            prev = Chunk(path, start, order=order, ffprobe=ffprobe)
            chunks.append(prev)
        return cls(chunks, name=name)

    @classmethod
    def from_folder(cls, folder, ffprobe=None):
        """Media files in folder, and in its YYYY-MM-DD subfolders (a camera's folder of days)."""
        paths = []
        for root in [folder] + sorted(os.path.join(folder, d) for d in os.listdir(folder)
                                      if _DAY.match(d) and os.path.isdir(os.path.join(folder, d))):
            for f in sorted(os.listdir(root)):
                if f.lower().endswith(EXTENSIONS) and os.path.isfile(os.path.join(root, f)):
                    paths.append(os.path.join(root, f))
        if not paths:
            raise ValueError(f"no media files in {folder}")
        return cls.from_files(paths, ffprobe, name=os.path.basename(os.path.normpath(folder)))

    # ---- the wall clock --------------------------------------------------------------------------------------
    @property
    def start(self):
        return self.chunks[0].start

    @property
    def end(self):
        """Wall time the last chunk ends (its start if its length is unknown)."""
        last = self.chunks[-1]
        return last.end if last.end is not None else last.start

    def end_of(self, i):
        """End of chunk i: probed, else assumed to run until the next chunk starts."""
        c = self.chunks[i]
        if c.end is not None:
            return c.end
        return self.chunks[i + 1].start if i + 1 < len(self.chunks) else c.start

    def locate(self, wall):
        """Spot for a wall time. Before the first chunk: a gap up to it; in an overlap the earlier chunk wins."""
        i = bisect.bisect_right(self._starts, wall) - 1
        if i < 0:
            if self.start - wall > self.gap_tolerance:
                return Spot(0, 0.0, gap=(wall, self.start))
            return Spot(0, 0.0)
        # an earlier chunk that overlaps this one still covers wall (follow() starts the later one where it ends)
        if i > 0:
            prev = self.chunks[i - 1]
            if prev.end is not None and prev.end > wall:
                return Spot(i - 1, wall - prev.start)
        c = self.chunks[i]
        end = c.end
        if end is None or wall < end:
            return Spot(i, max(0.0, wall - c.start))
        if i + 1 >= len(self.chunks):
            return Spot(i, max(0.0, (end - c.start)), ended=True)
        nxt = self.chunks[i + 1]
        if nxt.start - end <= self.gap_tolerance:
            return Spot(i + 1, 0.0)
        return Spot(i + 1, 0.0, gap=(end, nxt.start))

    def follow(self, i):
        """What comes after chunk i: (next index, offset into it, gap or None), or None at the end. An overlap
        starts the next chunk where this one ends so no moment plays twice."""
        if i + 1 >= len(self.chunks):
            return None
        end, nxt = self.chunks[i].end, self.chunks[i + 1]
        if end is None:
            return i + 1, 0.0, None
        if end > nxt.start:
            return i + 1, min(end - nxt.start, nxt.duration or 0.0), None
        if nxt.start - end > self.gap_tolerance:
            return i + 1, 0.0, (end, nxt.start)
        return i + 1, 0.0, None

    def gaps(self):
        """(start, end) of every hole longer than the tolerance (probes every chunk)."""
        out = []
        for i in range(len(self.chunks) - 1):
            f = self.follow(i)
            if f and f[2]:
                out.append(f[2])
        return out

    def wall(self, i, offset):
        return self.chunks[i].start + offset

def prefetch_at(source, i, speed=1.0, lead=PREFETCH_LEAD):
    """Wall time at which chunk i's successor should start decoding, or None (last chunk / unknown length). The lead
    scales with speed: at 8x three seconds of media go by in under half a second."""
    if i + 1 >= len(source.chunks):
        return None
    end = source.chunks[i].end
    if end is None:
        return None
    return max(source.chunks[i].start, end - lead * max(1.0, speed))

def fmt_wall(t, seconds=True):
    """14:02:08 for a wall time (local clock)."""
    return time.strftime("%H:%M:%S" if seconds else "%H:%M", time.localtime(t))
