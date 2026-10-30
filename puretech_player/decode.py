# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Decoder processes: FFmpeg turns one chunk into raw BGRA frames (fitted to the view) or 48 kHz float stereo.

Starting FFmpeg costs ~55 ms on Windows (process and DLL start-up) before it even opens the file, so decoders are
started ahead of need with no input: each waits for an ffconcat script on stdin naming the file and the in point.
Spares (one video, one audio) are kept ready, so a seek or the next chunk only pays for opening and decoding.

rawvideo carries no timestamps, so the video filter chain ends with showinfo, which prints each frame's pts and
size on stderr; frame k on stdout gets the k-th line. That keeps variable frame rate recordings (FrameKeep drops
repeated screen frames) on time, which a frame counter could not, and lets FFmpeg pick the fitted size itself.

Seeking: the concat in point puts the frames before it at negative times (decoding has to start at the keyframe
before it anyway); select drops them, except the keyframe itself, which comes out first as a preview so a seek
shows a picture as soon as ffplay would, then the exact frame."""

import collections, os, re, subprocess, threading
from . import proc

RATE, CHANNELS = 48000, 2
FRAME_BYTES = 4 * CHANNELS            # one float32 per channel
AUDIO_AHEAD = RATE * FRAME_BYTES      # bytes (1 s) an audio decoder may run ahead of playback
VIDEO_QUEUE = 1                       # decoded frames waiting per decoder (plus one shown, one being filled);
                                      # the pipe and FFmpeg hold more: 2 only cost a frame (3 MB at 1080p)
# Decoder threads. FFmpeg defaults to one per core, and each holds frames: at 1080p30 four threads used 25-90 MB
# less than the default (H.264 250 -> 175, HEVC 274 -> 183, AV1 210 -> 185 MB) and kept up with no drops.
DECODE_THREADS = 4
# H.266 seeks decode a whole keyframe interval before the target, and 8 threads did it faster (exact frame 547 ->
# 453 ms at 1080p30) for 51 -> 75% of a core while playing (ffplay: 107%)
CODEC_THREADS = {"vvc": 8}
# AV1 (dav1d) keeps several frames in flight by default; one is enough to keep up and used less than half the
# memory (1080p30: 112 -> 50 MB, CPU 14.8 -> 12.1%). Other decoders ignore the option (FFmpeg warns, no error).
LEAN_INPUT = ["-max_frame_delay", "1"]
SCALE_FLAGS = "bilinear"
INPUT = ["-copyts", "-protocol_whitelist", "file,pipe,fd", "-f", "concat", "-safe", "0", "-i", "-"]
_SHOWINFO = re.compile(rb"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(\S+).*?\bs:(\d+)x(\d+)")

def fit(src_w, src_h, sar, box_w, box_h):
    """Largest even size with the source's display aspect that fits the box (what the scale filter computes)."""
    if src_w <= 0 or src_h <= 0 or box_w < 2 or box_h < 2:
        return max(2, box_w // 2 * 2), max(2, box_h // 2 * 2)
    aspect = src_w * (sar or 1.0) / src_h
    w, h = box_w, round(box_w / aspect)
    if h > box_h:
        w, h = round(box_h * aspect), box_h
    return max(2, int(w) // 2 * 2), max(2, int(h) // 2 * 2)

def concat_path(path):
    """A path quoted for an ffconcat script."""
    return "'" + os.path.abspath(path).replace("\\", "/").replace("'", "'\\''") + "'"

def concat_script(path, inpoint=None):
    """inpoint: file time (start_time + offset) to start at, None for the start of the file."""
    text = f"ffconcat version 1.0\nfile {concat_path(path)}\n"
    if inpoint is not None:
        text += f"inpoint {inpoint:.6f}\n"
    return text

def frame_bytes(w, h, fmt):
    if fmt == "yuv420p":
        return w * h + 2 * ((w + 1) // 2) * ((h + 1) // 2)
    return w * h * 4

def video_filter(box_w, box_h, pol, fmt="bgra"):
    """bgra: fitted to the box (a GDI window shows it 1:1). yuv420p: for a presenter that scales on the graphics card,
    so it only shrinks pictures bigger than the box and otherwise leaves the size alone."""
    keep = "eq(n\\,0)+gte(t\\,0)"           # the keyframe (preview), then everything from the in point
    if pol.min_gap:
        # and from 4x only once min_gap of media has passed since the last kept frame (never duplicates)
        keep = f"({keep})*(isnan(prev_selected_t)+gte(t-prev_selected_t\\,{pol.min_gap}))"
    if fmt == "yuv420p":
        w, h = f"'min(iw,{box_w})'", f"'min(ih,{box_h})'"
    else:
        w, h = box_w, box_h
    return ",".join([f"select='{keep}'",
                     f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
                     f"reset_sar=1:flags={SCALE_FLAGS}", f"format={fmt}", "showinfo=checksum=0"])

def video_cmd(ffmpeg, box_w, box_h, pol, threads=None, fmt="bgra"):
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info"]
    if threads:
        cmd += ["-threads", str(threads)]
    if pol.keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += LEAN_INPUT
    cmd += INPUT + ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf", video_filter(box_w, box_h, pol, fmt)]
    if fmt == "yuv420p":
        cmd += ["-filter_threads", "1"]     # nothing to scale (the graphics card does it): no slice threads
    # one frame waiting for the pipe instead of FFmpeg's 8: playback paces the pipe, so that queue always stays full
    # (decoder private memory at 1080p30: AV1 91 -> 70, H.264 106 -> 82, HEVC 113 -> 92 MB)
    return cmd + ["-fps_mode", "passthrough", "-thread_queue_size", "1", "-f", "rawvideo", "pipe:1"]

def audio_cmd(ffmpeg, pol):
    af = ["atrim=start=0"] + [f"atempo={f:g}" for f in pol.atempo]
    return [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-threads", "1"] + INPUT + [
        "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", ",".join(af), "-ac", str(CHANNELS), "-ar", str(RATE),
        "-f", "f32le", "pipe:1"]

class Frame:
    """wall: wall-clock time this frame shows. preview: the keyframe before a seek target, shown while the exact
    frame decodes. fmt: "bgra", or "yuv420p" (Y plane, then U, then V, each half size). owner: the decoder whose
    buffer this is (release() gives it back)."""
    __slots__ = ("wall", "buf", "w", "h", "owner", "preview", "fmt")

    def __init__(self, wall, buf, w, h, owner=None, preview=False, fmt="bgra"):
        self.wall, self.buf, self.w, self.h = wall, buf, w, h
        self.owner, self.preview, self.fmt = owner, preview, fmt

    def __repr__(self):
        return f"Frame(wall={self.wall:.3f}, {self.w}x{self.h} {self.fmt}{', preview' if self.preview else ''})"

    def release(self):
        if self.owner is not None:
            self.owner._give_back(self.buf)
            self.owner = None

class VideoDecoder:
    """One FFmpeg turning a chunk into frames (fmt: "bgra" or "yuv420p") within box_w x box_h. Started idle;
    start() gives it the file. wall0: wall time of the in point (frame times count from there)."""
    def __init__(self, ffmpeg, box_w, box_h, pol, threads=None, queue=VIDEO_QUEUE, fmt="bgra"):
        self.box, self.pol, self.threads, self.fmt = (box_w, box_h), pol, threads, fmt
        self.key = (box_w, box_h, pol.video_key, threads, fmt)
        self.cmd = video_cmd(ffmpeg, box_w, box_h, pol, threads, fmt)
        self.path, self.wall0 = None, 0.0
        self.done = False             # no more frames will come
        self.frames_out = 0
        self.log = collections.deque(maxlen=8)
        self._limit = queue + 2       # buffers alive at once: queued, one shown, one being filled
        self._alive = 0
        self._ready = collections.deque()
        self._free = []
        self._info = collections.deque()  # (pts, w, h) per frame, from showinfo
        self._cond = threading.Condition()
        self._closed = False
        rfd, wfd = proc.pipe(4 << 20)          # a whole 1080p YUV frame per read: 13% less Python CPU than 1 MB
        # The log is read by the frame reader between frames, in batches. Its pipe holds far more than FFmpeg can
        # print while one frame waits to be read, so FFmpeg never blocks on it.
        erfd, ewfd = proc.pipe(256 << 10)
        try:
            self.proc = proc.spawn(self.cmd, stdin=subprocess.PIPE, stdout=wfd, stderr=ewfd)
        finally:
            os.close(wfd)
            os.close(ewfd)
        self._out = os.fdopen(rfd, "rb", buffering=0)
        self._err = os.fdopen(erfd, "rb", buffering=0)
        threading.Thread(target=self._read_frames, name="video-read", daemon=True).start()

    def start(self, path, wall0, inpoint=None):
        self.path, self.wall0 = path, wall0
        try:
            self.proc.stdin.write(concat_script(path, inpoint).encode("utf-8"))
            self.proc.stdin.close()
        except OSError:
            self.close()
        return self

    # ---- consumer side -----------------------------------------------------------------------------------
    def peek(self, timeout=0.0):
        """The next frame without taking it, or None (not decoded yet, or done)."""
        with self._cond:
            if not self._ready and not self.done and timeout:
                self._cond.wait_for(lambda: self._ready or self.done, timeout)
            return self._ready[0] if self._ready else None

    def pop(self):
        with self._cond:
            f = self._ready.popleft() if self._ready else None
            self._cond.notify_all()
            return f

    @property
    def exhausted(self):
        with self._cond:
            return self.done and not self._ready

    def _give_back(self, buf):
        with self._cond:
            if not self._closed:
                self._free.append(buf)
                self._cond.notify_all()

    def close(self):
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self.done = True
            self._ready.clear()
            self._free.clear()
            self._cond.notify_all()
        proc.reap(self.proc)

    # ---- reader threads ----------------------------------------------------------------------------------
    def _take_buffer(self, size):
        with self._cond:
            while True:
                if self._closed:
                    return None
                for i, b in enumerate(self._free):
                    if len(b) == size:
                        return self._free.pop(i)
                if self._free:              # wrong size (the source changed resolution): let it go
                    self._free.pop()
                    self._alive -= 1
                    continue
                if self._alive < self._limit:
                    self._alive += 1
                    return bytearray(size)
                self._cond.wait()

    def _next_info(self):
        """(pts, w, h) of the next frame from showinfo on stderr, None once the log ends."""
        while not self._info:
            data = self._err.read(65536)
            if not data:
                return None
            lines = (self._partial + data).split(b"\n")
            self._partial = lines.pop()
            for raw in lines:
                m = _SHOWINFO.search(raw)
                if m:
                    try:
                        self._info.append((float(m[1]), int(m[2]), int(m[3])))
                    except ValueError:
                        pass
                elif b"rror" in raw or b"nvalid" in raw:    # showinfo's other lines are skipped undecoded
                    self.log.append(raw.decode("utf-8", "replace").strip())
        return self._info.popleft()

    def _read_frames(self):
        out = self._out
        self._partial = b""
        try:
            while True:
                info = self._next_info()
                if info is None:
                    return
                pts, w, h = info
                size = frame_bytes(w, h, self.fmt)
                buf = self._take_buffer(size)
                if buf is None:
                    return
                view, got = memoryview(buf), 0
                while got < size:
                    n = out.readinto(view[got:])
                    if not n:
                        return
                    got += n
                with self._cond:
                    if self._closed:
                        return
                    self._ready.append(Frame(self.wall0 + pts, buf, w, h, self, pts < -1e-6, self.fmt))
                    self.frames_out += 1
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.done = True
                self._cond.notify_all()
            for f in (out, self._err):
                try:
                    f.close()
                except OSError:
                    pass

class AudioDecoder:
    """One FFmpeg turning a chunk's sound into 48 kHz float stereo, read without blocking. Started idle."""
    def __init__(self, ffmpeg, pol, ahead=AUDIO_AHEAD):
        self.pol, self.key = pol, pol.atempo
        self.cmd = audio_cmd(ffmpeg, pol)
        self.ahead = ahead
        self.eof = False
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self.proc = proc.spawn(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        threading.Thread(target=self._read, name="audio-read", daemon=True).start()

    def start(self, path, inpoint=None):
        try:
            self.proc.stdin.write(concat_script(path, inpoint).encode("utf-8"))
            self.proc.stdin.close()
        except OSError:
            self.close()
        return self

    def _read(self):
        try:
            out = self.proc.stdout
            while True:
                with self._cond:
                    self._cond.wait_for(lambda: len(self._buf) < self.ahead or self._closed)
                    if self._closed:
                        return
                data = out.read1(1 << 16)
                if not data:
                    return
                with self._cond:
                    self._buf += data
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.eof = True
                self._cond.notify_all()

    def read(self, n):
        """Up to n bytes (whole sample frames) of what is decoded so far."""
        with self._cond:
            n = min(n, len(self._buf)) // FRAME_BYTES * FRAME_BYTES
            data = bytes(self._buf[:n])
            del self._buf[:n]
            self._cond.notify_all()
            return data

    def wait(self, timeout):
        """Block until something is buffered or the stream ended."""
        with self._cond:
            self._cond.wait_for(lambda: len(self._buf) >= FRAME_BYTES or self.eof, timeout)

    @property
    def exhausted(self):
        with self._cond:
            return self.eof and len(self._buf) < FRAME_BYTES

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        proc.reap(self.proc)

class Silence:
    """Stands in for a decoder where there is no sound (a chunk without audio, or a gap played through)."""
    eof = True

    def __init__(self, frames):
        self.left = max(0, int(frames)) * FRAME_BYTES

    def read(self, n):
        n = min(n, self.left) // FRAME_BYTES * FRAME_BYTES
        self.left -= n
        return bytes(n)

    def wait(self, timeout):
        pass

    @property
    def exhausted(self):
        return self.left < FRAME_BYTES

    def close(self):
        pass

def inpoint(chunk, offset):
    """File time to start chunk at offset seconds into it (None: from the top, no probe needed)."""
    if offset <= 1e-6:
        return None
    return (chunk.info.get("start_time") or 0.0) + offset

class Spares:
    """Keeps one idle video decoder and one idle audio decoder started, replacing each as it is taken."""
    def __init__(self, ffmpeg, threads=None):
        self.ffmpeg, self.threads = ffmpeg, threads
        self._video = self._audio = None
        self._want_video = self._want_audio = None
        self._lock = threading.Lock()
        self._closed = False

    def video(self, box_w, box_h, pol, fmt="bgra", threads=None):
        threads = threads or self.threads
        key = (box_w, box_h, pol.video_key, threads, fmt)
        with self._lock:
            dec, self._video = self._video, None
            self._want_video = (box_w, box_h, pol, fmt, threads)
        if dec is not None and dec.key != key:
            dec.close()
            dec = None
        if dec is None or dec.done:
            dec = VideoDecoder(self.ffmpeg, box_w, box_h, pol, threads, fmt=fmt)
        self._refill()
        return dec

    def audio(self, pol):
        with self._lock:
            dec, self._audio = self._audio, None
            self._want_audio = pol
        if dec is not None and (dec.key != pol.atempo or dec.eof):
            dec.close()
            dec = None
        if dec is None:
            dec = AudioDecoder(self.ffmpeg, pol)
        self._refill()
        return dec

    def warm(self, box_w, box_h, pol, sound, fmt="bgra"):
        """Start spares now (before the first open)."""
        with self._lock:
            self._want_video = (box_w, box_h, pol, fmt, self.threads)
            if sound:
                self._want_audio = pol
        self._refill()

    def _refill(self):
        threading.Thread(target=self._fill, name="spares", daemon=True).start()

    def _fill(self):
        with self._lock:
            want_v = self._want_video if self._video is None else None
            want_a = self._want_audio if self._audio is None else None
        made_v = None
        if want_v and not self._closed:
            box_w, box_h, pol, fmt, threads = want_v
            made_v = VideoDecoder(self.ffmpeg, box_w, box_h, pol, threads, fmt=fmt)
        made_a = AudioDecoder(self.ffmpeg, want_a) if want_a and not self._closed else None
        with self._lock:
            if made_v is not None and self._video is None and not self._closed and self._want_video == want_v:
                self._video, made_v = made_v, None
            if made_a is not None and self._audio is None and not self._closed and self._want_audio == want_a:
                self._audio, made_a = made_a, None
        for leftover in (made_v, made_a):
            if leftover is not None:
                leftover.close()

    def close(self):
        with self._lock:
            self._closed = True
            dv, da = self._video, self._audio
            self._video = self._audio = None
        for d in (dv, da):
            if d is not None:
                d.close()
