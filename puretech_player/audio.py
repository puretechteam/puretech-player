# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Sound: per-chunk audio decoders strung into one stream (AudioFeed), played by a WASAPI shared-mode render client
through ctypes (WasapiOutput), driven by one thread (AudioEngine) whose played-frame count is the master clock."""

import ctypes, sys, threading, time
from ctypes import POINTER, byref, c_int, c_uint32, c_uint64, c_void_p, wintypes
from .clock import AudioClock
from .decode import AudioDecoder, Silence, inpoint, RATE, CHANNELS, FRAME_BYTES

BUFFER_MS = 200            # WASAPI buffer: refilled every TICK, so a stall of up to ~150 ms is inaudible
# 10 ms cost 2.2% of a core more in Python for nothing (HEVC 1080p30), 25 ms 0.25% more than 50 (HEVC 720p15)
TICK = 0.05
PAD_LIMIT = 30.0           # seconds: a chunk whose sound is this much shorter than its video is not padded out

S_OK = 0
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0
STREAMFLAGS_AUTOCONVERTPCM, STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x80000000, 0x08000000
WAVE_FORMAT_IEEE_FLOAT = 3
DEVICE_ERRORS = 0x8889     # AUDCLNT_E_*: device unplugged, disabled or switched

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

def guid(text):
    h = text.replace("-", "")
    return GUID(int(h[:8], 16), int(h[8:12], 16), int(h[12:16], 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(h[16:])))

CLSID_MMDeviceEnumerator = guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioRenderClient = guid("F294ACFC-3146-4483-A7BF-ADDCA7C260E2")
IID_IAudioClock = guid("CD63314F-3FBA-4A1B-812C-EF96358728E7")
IID_ISimpleAudioVolume = guid("87CE5498-68D6-44E5-9215-6DA47EF883D8")

class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]

def _com(obj, index, *argtypes):
    """Bound method `index` of a COM interface pointer."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(obj, *args)

def _release(obj):
    if obj and obj.value:
        _com(obj, 2)()
        obj.value = None

def _hr(value):
    if value < 0:
        raise OSError(f"HRESULT 0x{value & 0xFFFFFFFF:08X}")
    return value

if sys.platform == "win32":
    ole32 = ctypes.windll.ole32
    _k32 = ctypes.windll.kernel32

def qpc_100ns():
    """Now on the clock IAudioClock stamps positions with (QueryPerformanceCounter in 100 ns units)."""
    count, freq = ctypes.c_int64(), ctypes.c_int64()
    _k32.QueryPerformanceCounter(byref(count))
    _k32.QueryPerformanceFrequency(byref(freq))
    return count.value * 10_000_000 // freq.value

class WasapiOutput:
    """The default output device in shared mode, 48 kHz float stereo (Windows converts to the mix format). Created
    and used on one thread, which must have called CoInitializeEx."""
    def __init__(self, buffer_ms=BUFFER_MS):
        self.enumerator, self.client = c_void_p(), c_void_p()
        self.render, self.clock, self.volume = c_void_p(), c_void_p(), c_void_p()
        try:
            _hr(ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                                       byref(IID_IMMDeviceEnumerator), byref(self.enumerator)))
            device = c_void_p()
            _hr(_com(self.enumerator, 4, c_int, c_int, POINTER(c_void_p))(0, 1, byref(device)))  # eRender, eMultimedia
            try:
                _hr(_com(device, 3, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
                    byref(IID_IAudioClient), CLSCTX_ALL, None, byref(self.client)))
            finally:
                _release(device)
            fmt = WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT, CHANNELS, RATE, RATE * FRAME_BYTES, FRAME_BYTES, 32, 0)
            _hr(_com(self.client, 3, c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, POINTER(WAVEFORMATEX),
                     c_void_p)(0, STREAMFLAGS_AUTOCONVERTPCM | STREAMFLAGS_SRC_DEFAULT_QUALITY, buffer_ms * 10_000, 0,
                               byref(fmt), None))
            size = c_uint32()
            _hr(_com(self.client, 4, POINTER(c_uint32))(byref(size)))
            self.buffer_frames = size.value
            service = _com(self.client, 14, POINTER(GUID), POINTER(c_void_p))
            _hr(service(byref(IID_IAudioRenderClient), byref(self.render)))
            _hr(service(byref(IID_IAudioClock), byref(self.clock)))
            if service(byref(IID_ISimpleAudioVolume), byref(self.volume)) != S_OK:
                self.volume = c_void_p()
            freq = c_uint64()
            _hr(_com(self.clock, 3, POINTER(c_uint64))(byref(freq)))
            self.freq = freq.value or 1
        except OSError:
            self.close()
            raise
        self._padding = _com(self.client, 6, POINTER(c_uint32))
        self._get = _com(self.render, 3, c_uint32, POINTER(c_void_p))
        self._put = _com(self.render, 4, c_uint32, wintypes.DWORD)
        self._position = _com(self.clock, 4, POINTER(c_uint64), POINTER(c_uint64))
        self._start, self._stop, self._reset = _com(self.client, 10), _com(self.client, 11), _com(self.client, 12)

    def free_frames(self):
        pad = c_uint32()
        _hr(self._padding(byref(pad)))
        return self.buffer_frames - pad.value

    def write(self, data):
        n = len(data) // FRAME_BYTES
        if not n:
            return 0
        ptr = c_void_p()
        _hr(self._get(n, byref(ptr)))
        ctypes.memmove(ptr, data, n * FRAME_BYTES)
        _hr(self._put(n, 0))
        return n

    def position(self):
        """(sample frames played, the qpc_100ns() time that was true)."""
        pos, qpc = c_uint64(), c_uint64()
        _hr(self._position(byref(pos), byref(qpc)))
        return pos.value * RATE / self.freq, qpc.value

    def start(self):
        _hr(self._start())

    def stop(self):
        _hr(self._stop())

    def reset(self):
        _hr(self._reset())

    def set_volume(self, level):
        if self.volume:
            _com(self.volume, 3, ctypes.c_float, c_void_p)(max(0.0, min(1.0, float(level))), None)

    def close(self):
        for obj in (self.volume, self.clock, self.render, self.client, self.enumerator):
            try:
                _release(obj)
            except OSError:
                pass

class AudioFeed:
    """The sound from source chunk `index` at `offset` onwards as one byte stream: each chunk's decoder in turn,
    silence for chunks without sound (and for gaps when they are played through). read() also reports where each
    piece starts, which the engine turns into clock marks."""
    def __init__(self, source, index, offset, pol, ffmpeg, skip_gaps=True, opener=None):
        self.source, self.pol, self.ffmpeg, self.skip_gaps = source, pol, ffmpeg, skip_gaps
        self._open = opener or (lambda c, off: AudioDecoder(ffmpeg, pol).start(c.path, inpoint(c, off)))
        self._plan = self._pieces(index, offset)
        self._cur = self._next = None      # (wall, reader, frame limit or None)
        self._cur_frames = 0
        self.exhausted = False

    def _pieces(self, i, off):
        speed = self.pol.speed
        while True:
            c = self.source[i]
            length = None if c.duration is None else max(0.0, c.duration - off) / speed * RATE
            if c.has_audio:
                yield c.start + off, lambda c=c, off=off: self._open(c, off), length
            else:
                yield c.start + off, lambda length=length: Silence(length or 0), length
            f = self.source.follow(i)
            if f is None:
                return
            i, off, gap = f
            if gap and not self.skip_gaps:
                frames = (gap[1] - gap[0]) / speed * RATE
                yield gap[0], lambda frames=frames: Silence(frames), frames

    def _take(self):
        if self._next is not None:
            piece, self._next = self._next, None
            return piece
        piece = next(self._plan, None)
        if piece is None:
            return None
        wall, make, limit = piece
        return wall, make(), limit

    def _prefetch(self):
        """Start the next decoder once the current one has decoded everything (it runs ~1 s ahead of the sound)."""
        if self._next is None and self._cur and self._cur[1].eof:
            piece = next(self._plan, None)
            if piece:
                wall, make, limit = piece
                self._next = (wall, make(), limit)

    def read(self, nbytes):
        """(bytes, [(byte offset, wall time)]) of up to nbytes available now."""
        out, marks = bytearray(), []
        while len(out) < nbytes:
            if self._cur is None:
                self._cur = self._take()
                self._cur_frames = 0
                if self._cur is None:
                    self.exhausted = True
                    break
                marks.append((len(out), self._cur[0]))
            wall, reader, limit = self._cur
            want = nbytes - len(out)
            if limit is not None:
                want = min(want, (int(limit) - self._cur_frames) * FRAME_BYTES)
            data = reader.read(want) if want > 0 else b""
            if data:
                out += data
                self._cur_frames += len(data) // FRAME_BYTES
                continue
            if want <= 0 or reader.exhausted:
                missing = 0 if limit is None else int(limit) - self._cur_frames
                if want > 0 and 0 < missing < PAD_LIMIT * RATE:
                    # sound shorter than the chunk: pad it so the next chunk's sound starts with its video
                    reader.close()
                    self._cur = (wall, Silence(missing), None)
                    continue
                reader.close()
                self._cur = None
                continue
            break                           # decoder still starting: try again next tick
        self._prefetch()
        return bytes(out), marks

    def wait(self, timeout):
        if self._cur:
            self._cur[1].wait(timeout)

    def close(self):
        for piece in (self._cur, self._next):
            if piece and hasattr(piece[1], "close"):
                piece[1].close()
        self._cur = self._next = None

class AudioEngine:
    """Plays an AudioFeed on a device and keeps the clock. Runs its own thread (start()), or is driven one step at
    a time with pump() (tests do that with a fake device)."""
    def __init__(self, device_factory=WasapiOutput, threaded=True):
        self._factory = device_factory
        self.device = None
        self.clock = AudioClock(self.played)
        self.failed = None            # why sound is unavailable, if it is
        self.underruns = 0
        self.feed = None
        self.playing = False
        self.drained = False          # the feed ended and the device played all of it
        self._running = False         # device started
        self._written = 0             # frames written since the last reset
        self._pos = (0.0, 0)          # (frames played, qpc) last read from the device
        self._volume = 1.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._jobs = []
        self.ready = threading.Event()
        if threaded:
            threading.Thread(target=self._run, name="audio-out", daemon=True).start()

    # ---- control (any thread) ----------------------------------------------------------------------------
    def _post(self, job):
        with self._lock:
            self._jobs.append(job)
        self._wake.set()

    def play(self, feed, wall, speed):
        """Replace whatever plays with feed, starting at wall. The clock holds wall until sound comes out."""
        self._restart(wall, speed)          # here so the clock is right at once, and again on the device thread
        self._post(("play", (feed, wall, speed)))

    def _restart(self, wall, speed):
        with self._lock:
            self._pos = (0.0, 0)
            self._written = 0
        self.drained = False
        self.clock.reset(wall, speed)

    def pause(self):
        self._post(("pause", None))

    def resume(self):
        self._post(("resume", None))

    def halt(self):
        """Stop and drop the stream (muted speeds, or no sound at this point)."""
        self._post(("halt", None))

    def set_volume(self, level):
        self._volume = level
        self._post(("volume", level))

    def close(self):
        self._closed = True
        self._post(("close", None))

    def played(self):
        """Frames played, extrapolated from the last device reading to now, never past what was written."""
        with self._lock:
            frames, qpc = self._pos
            written, running = self._written, self._running and self.playing
        if running and qpc:
            frames += (qpc_100ns() - qpc) * RATE / 10_000_000
        return min(frames, written)

    # ---- the device thread -------------------------------------------------------------------------------
    def open(self):
        try:
            self.device = self._factory()
            self.device.set_volume(self._volume)
        except Exception as e:
            self.failed = str(e) or type(e).__name__
        self.ready.set()

    def _run(self):
        if sys.platform == "win32":
            ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        try:
            self.open()
            while not self._closed:
                try:
                    self.pump()
                except Exception as e:     # never take the player down: it carries on with a monotonic clock
                    self.failed = f"{type(e).__name__}: {e}"
                    self.playing, self.drained = False, True
                    self._drop()
                wait = TICK if self.playing else 0.5
                self._wake.wait(wait)
                self._wake.clear()
        finally:
            self._drop()
            if self.device:
                self.device.close()
            if sys.platform == "win32":
                ole32.CoUninitialize()

    def _drop(self):
        if self.feed:
            self.feed.close()
            self.feed = None

    def _stop_device(self, reset):
        if self.device and self._running:
            self.device.stop()
            self._running = False
        if reset and self.device:
            self.device.reset()

    def pump(self):
        """One step: run queued commands, read the device clock, top the buffer up."""
        with self._lock:
            jobs, self._jobs = self._jobs, []
        last_play = max((i for i, (kind, _) in enumerate(jobs) if kind == "play"), default=-1)
        for i, (kind, arg) in enumerate(jobs):
            if kind == "play" and i < last_play:
                arg[0].close()              # a newer seek already replaced this one
            elif kind == "play":
                self._drop()
                self._stop_device(reset=True)
                feed, wall, speed = arg
                self._restart(wall, speed)
                self.feed, self.playing = feed, True
            elif kind == "pause":
                self.playing = False
                self._stop_device(reset=False)
                if self.device and self._written:
                    frames, qpc = self.device.position()
                    with self._lock:
                        self._pos = (frames, qpc)
            elif kind == "resume":
                self.playing = bool(self.feed)
            elif kind == "halt":
                self.playing = False
                self._drop()
                self._stop_device(reset=True)
            elif kind == "volume" and self.device:
                self.device.set_volume(arg)
            elif kind == "close":
                self.playing = False
        if not self.device or not self.playing or not self.feed:
            return
        try:
            self._fill()
        except OSError as e:
            if f"0x{DEVICE_ERRORS:04X}" not in str(e):
                raise
            self._reopen()

    def _fill(self):
        dev = self.device
        free = dev.free_frames()
        if self._running:
            frames, qpc = dev.position()
            with self._lock:
                self._pos = (frames, qpc)
            if free >= dev.buffer_frames and not self.feed.exhausted:
                self.underruns += 1
        if free >= RATE // 200:          # 5 ms or more of room
            data, marks = self.feed.read(free * FRAME_BYTES)
            for off, wall in marks:
                self.clock.mark(self._written + off // FRAME_BYTES, wall)
            if data:
                n = dev.write(data)
                with self._lock:
                    self._written += n
            if not self._running and self._written:
                dev.start()
                with self._lock:
                    self._running = True
            if self.feed.exhausted and self._written and free >= dev.buffer_frames:
                self.drained = True

    def _reopen(self):
        """The output device went away (unplugged, or the user switched): carry on from here on the new default."""
        wall = self.clock.now()
        try:
            self.device.close()
        except OSError:
            pass
        self.device, self._running = None, False
        self.open()
        with self._lock:
            self._written, self._pos = 0, (0.0, 0)
        self.clock.reset(wall)
