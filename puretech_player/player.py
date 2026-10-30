# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Player: one source on screen. A render thread owns the decoders and presents frames when the clock reaches them;
the audio engine (when there is sound) is the clock. Every public method only queues a command, so the UI thread
never waits on FFmpeg. Callbacks (on_time, on_state) run on the render thread."""

import ctypes, queue, sys, threading, time
from . import proc
from .audio import AudioEngine, AudioFeed
from .clock import MonotonicClock
from .decode import CODEC_THREADS, DECODE_THREADS, Silence, Spares, inpoint, RATE
from .policy import clamp, policy
from .present import NullPresenter
from .timeline import Source, Spot, prefetch_at

IDLE, LOADING, PLAYING, PAUSED, GAP, ENDED, ERROR = "idle", "loading", "playing", "paused", "gap", "ended", "error"
MAX_WAIT = 0.05          # render thread sleeps at most this long between looks
STEP_BACK_MAX = 120.0    # seconds searched back for the previous frame (sparse screen recordings)
SEEK_RETRY = 3.0         # seconds decoded before a target the decoder overshot (see _tick)
# Codecs whose seeks in MP4 land a keyframe late: the demuxer seeks by decode time, and an H.266 keyframe is
# decoded about a second before it shows (its leading pictures follow it), so a seek between two keyframes starts
# on the later one. Seeks there start on the keyframe before the target, from the chunk's keyframe index (read in
# the background), or from SEEK_RETRY earlier until it is read.
LATE_START = {"vvc"}
RESIZE_DELAY = 0.15      # a window being dragged sends many sizes; decode at the one it settles on

class Player:
    def __init__(self, sources, ffmpeg=None, view=None, presenter=None, audio=True, skip_gaps=True, clock=None,
                 threads=None, volume=1.0, audio_engine=None):
        self.ffmpeg = proc.find_tool("ffmpeg", ffmpeg)
        self.ffprobe = proc.find_tool("ffprobe", self.ffmpeg)
        self.source = Source.coerce(sources, self.ffprobe)
        self.skip_gaps, self.threads = skip_gaps, threads
        self.on_time = None          # on_time(wall) after each shown frame and each seek
        self.on_state = None         # on_state(state, info dict)
        self.state = IDLE
        self.speed = 1.0
        self.pol = policy(1.0)
        self.stats = {"shown": 0, "dropped": 0, "previews": 0, "decoders": 0, "seeks": 0, "gaps": 0}
        self.presenter = presenter or (view.presenter() if view is not None else NullPresenter())
        self._view = view
        self._own_presenter = presenter is None and view is None
        if view is not None:
            view.attach(self)
        self._external = clock       # a shared clock (Grid): followed whenever this player's own sound is not playing
        self._mono = MonotonicClock(self.source.start)
        self._audio_wanted = audio
        self._volume = volume
        self._audio = audio_engine
        if self._audio is None and audio:
            self._audio = AudioEngine()  # opens the device on its own thread, in parallel with the first decode
            self._audio.set_volume(volume)
        self._spares = Spares(self.ffmpeg, threads or DECODE_THREADS)
        self._use_audio = False      # the audio clock is master right now
        self._audio_valid = False    # the paused audio stream still matches the picture (resume without re-seek)
        self._position = self.source.start
        self._playing = False
        self._cur = None             # (chunk index, VideoDecoder)
        self._nxt = None             # (chunk index, offset, gap, VideoDecoder) started ahead
        self._first = False          # show the next frame now, whatever the clock says (after a seek)
        self._seek_target = None     # wall time the decoder was asked to start at, until its first frame
        self._steps = 0              # frames to step forward while paused
        self._back = None            # (target wall, best frame so far, window) while stepping back
        self._placeholder = False    # show the first frame of a step-back search (a seek that searches back)
        self._gap = None
        self._size = None            # decode size
        self._resize_at = None       # monotonic time a pending resize takes effect
        self._gen = 0                # bumped whenever the decoders are replaced, so a late prefetch is thrown away
        self._prefetching = False
        self._lock = threading.Lock()
        self._cmds = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="player", daemon=True)
        self._thread.start()

    # ---- public API (any thread) -------------------------------------------------------------------------
    def play(self):
        self._cmds.put(("play", None))

    def pause(self):
        self._cmds.put(("pause", None))

    def toggle(self):
        self._cmds.put(("toggle", None))

    def seek(self, wall_time):
        """Go to a wall-clock time (epoch seconds): keyframe seek, then an accurate decode to that frame."""
        self._cmds.put(("seek", float(wall_time)))

    def set_speed(self, speed):
        self._cmds.put(("speed", clamp(speed)))

    def step(self, direction=1):
        """One frame forward (+1) or back (-1); pauses first."""
        self._cmds.put(("step", 1 if direction > 0 else -1))

    def skip_gap(self):
        """Jump over the hole between chunks playback is waiting in."""
        self._cmds.put(("skip_gap", None))

    def set_volume(self, level):
        self._volume = level
        if self._audio:
            self._audio.set_volume(level)

    def resize(self):
        """The view changed size: decode at the new size (debounced)."""
        self._cmds.put(("resize", time.monotonic()))

    def refresh(self):
        """Repaint the current frame (window uncovered)."""
        self.presenter.redraw()

    def close(self):
        self._cmds.put(("close", None))
        self._thread.join(3)

    @property
    def position(self):
        """Wall-clock time on screen now."""
        if self._playing and self.state in (PLAYING, GAP):
            return self.clock_now()
        return self._position

    @property
    def playing(self):
        return self._playing

    def clock_now(self):
        if self._use_audio:
            t = self._audio.clock.now()
            if t is not None:
                return t
        if self._external is not None:
            return self._external.now()
        return self._mono.now()

    def next_media(self):
        """Wall time of the next chunk while waiting in a gap, else None."""
        return self._gap[1] if self._gap else None

    # ---- events ------------------------------------------------------------------------------------------
    def _set_state(self, state, **info):
        if state == self.state and not info:
            return
        self.state = state
        if self.on_state:
            try:
                self.on_state(state, info)
            except Exception:
                pass

    def _tell_time(self, wall):
        if self.on_time:
            try:
                self.on_time(wall)
            except Exception:
                pass

    # ---- render thread -----------------------------------------------------------------------------------
    def _run(self):
        wait = 0.0
        timer = sys.platform == "win32" and ctypes.windll.winmm.timeBeginPeriod(1) == 0   # 1 ms waits, as SDL does
        try:
            self._probe_ahead(0)
            self._start_at(self.source.start, show=True, spot=Spot(0, 0.0))  # needs no probe: decoding starts now
            self._spares.warm(*self._box(), self.pol, sound=self._audio_wanted, fmt=self._fmt)
            while True:
                cmds = []
                try:
                    cmds.append(self._cmds.get(timeout=max(0.0, min(wait, MAX_WAIT))))
                    while True:
                        cmds.append(self._cmds.get_nowait())
                except queue.Empty:
                    pass
                if not self._handle(cmds):
                    break
                wait = self._tick()
        finally:
            self._close_decoders()
            self._spares.close()
            if self._audio:
                self._audio.close()
            if self._own_presenter:
                self.presenter.close()      # ours; a view's or the caller's presenter outlives this player
            else:
                self.presenter.clear()
            if self._view is not None:
                self._view.detach(self)
            if timer:
                ctypes.windll.winmm.timeEndPeriod(1)

    def _handle(self, cmds):
        seeks = [c for c in cmds if c[0] == "seek"]
        last_seek = seeks[-1] if seeks else None
        for cmd in cmds:
            kind, arg = cmd
            if kind == "close":
                self._closed = True
                return False
            if kind == "seek" and cmd is last_seek:
                self.stats["seeks"] += 1
                self._back, self._steps = None, 0
                self._start_at(arg, show=True)
            elif kind == "play" or (kind == "toggle" and not self._playing):
                self._resume()
            elif kind == "pause" or (kind == "toggle" and self._playing):
                self._halt()
            elif kind == "speed":
                self._change_speed(arg)
            elif kind == "step":
                self._halt()
                if arg > 0:
                    self._steps += 1
                else:
                    self._step_back()
            elif kind == "skip_gap" and self._gap:
                self._start_at(self._gap[1], show=True)
            elif kind == "resize":
                self._resize_at = arg + RESIZE_DELAY
        return True

    # ---- clocks and sound --------------------------------------------------------------------------------
    def _ensure_audio(self):
        if self._audio is None and self._audio_wanted:
            self._audio = AudioEngine()
            self._audio.set_volume(self._volume)
        if self._audio is not None:
            self._audio.ready.wait(3)
        return self._audio is not None and not self._audio.failed

    def _start_sound(self, spot, wall):
        """Start sound from spot (playing) or decide the clock is monotonic (no sound here)."""
        self._use_audio = False
        if not (self._audio_wanted and self.pol.audio and self._playing):
            if self._audio:
                self._audio.halt()
            return
        chunk = self.source[spot.index]
        if not chunk.has_audio or not self._ensure_audio():
            if self._audio:
                self._audio.halt()
            return
        pol, spares = self.pol, self._spares
        feed = AudioFeed(self.source, spot.index, spot.offset, pol, self.ffmpeg, self.skip_gaps,
                         opener=lambda c, off: spares.audio(pol).start(c.path, inpoint(c, off)))
        if spot.gap and not self.skip_gaps:
            feed = _Lead(feed, wall, (spot.gap[1] - wall) / self.pol.speed * RATE)
        self._audio.play(feed, wall, self.pol.speed)
        self._use_audio = True
        self._audio_valid = True

    def _resume(self):
        if self._playing:
            return
        if self.state == ENDED:
            self._start_at(self.source.start, show=True)
        self.presenter.ready(0.5)          # don't start the clock while the first frame cannot be drawn yet
        self._playing = True
        self._back, self._steps = None, 0
        wall = self._position
        self._mono.set(wall)
        self._mono.start()
        if self._use_audio and self._audio_valid:
            self._audio.resume()
        else:
            self._start_sound(self.source.locate(wall), wall)
        if self.state not in (LOADING, GAP):
            self._set_state(PLAYING)

    def _halt(self):
        if not self._playing:
            return
        if self.state == GAP:               # else _position already holds the frame on screen
            self._position = self.clock_now()
        self._playing = False
        self._mono.stop()
        self._mono.set(self._position)
        if self._use_audio:
            self._audio.pause()
        if self.state not in (LOADING,):
            self._set_state(PAUSED)

    def _change_speed(self, speed):
        old = self.pol
        self.speed, self.pol = speed, policy(speed)
        wall = self.position
        self._mono.set_speed(speed)
        if self.pol.video_key != old.video_key and self._cur:
            self._restart_video(wall, show=not self._playing)
        if self._playing:
            self._position = wall
            self._mono.set(wall)
            self._start_sound(self.source.locate(wall), wall)
        else:
            self._audio_valid = False

    # ---- decoders ----------------------------------------------------------------------------------------
    def _box(self):
        """The area frames are fitted into (FFmpeg keeps the aspect ratio). A presenter that scales on the graphics
        card takes frames up to `slack` times its size."""
        w, h = self.presenter.size()
        if w < 16 or h < 16:
            w, h = 1280, 720            # not on screen yet: a resize follows once it is
        slack = getattr(self.presenter, "slack", 1.0)
        return int(w * slack) // 2 * 2, int(h * slack) // 2 * 2

    @property
    def _fmt(self):
        return getattr(self.presenter, "pixel_format", "bgra")

    def _open(self, index, offset):
        chunk = self.source[index]
        box = self._box()
        self._size = box
        self.stats["decoders"] += 1
        threads = None
        if chunk.probed and self.threads is None:     # the codec is known once probed (seeks always are)
            threads = CODEC_THREADS.get((chunk.info.get("video") or {}).get("codec"))
        return self._spares.video(*box, self.pol, self._fmt, threads).start(chunk.path, chunk.start + offset,
                                                                             inpoint(chunk, offset))

    def _close_decoders(self):
        with self._lock:
            self._gen += 1
            cur, nxt = self._cur, self._nxt
            self._cur = self._nxt = None
        if cur:
            cur[1].close()
        if nxt:
            nxt[3].close()

    def _spot(self, wall):
        spot = self.source.locate(wall)
        if spot.ended:
            c = self.source[spot.index]
            fps = (c.info["video"] or {}).get("fps") or 30.0
            spot = Spot(spot.index, max(0.0, (c.duration or 0.0) - 1.0 / fps))
        return spot

    def _start_at(self, wall, show, spot=None):
        """Seek: new decoders from wall (sound too, if playing)."""
        self._close_decoders()
        spot = spot or self._spot(wall)
        self._gap = None
        if spot.gap and self.skip_gaps:
            wall = self.source[spot.index].start
            spot = Spot(spot.index, 0.0)
        elif spot.gap:
            self._gap = spot.gap
        else:
            wall = self.source.wall(spot.index, spot.offset)
        self._position = wall
        self._mono.set(wall)
        codec = ((self.source[spot.index].info.get("video") or {}).get("codec") if spot.offset > 0 else None)
        if show and not spot.gap and codec in LATE_START:
            dt = self._frame_time(spot.index)
            target = inpoint(self.source[spot.index], spot.offset)
            key = self.source[spot.index].keyframe_before(target)
            # from just before the keyframe, so the in point keeps it (frames before an in point are dropped)
            window = dt + target - key + 0.0005 if key is not None else SEEK_RETRY
            self._position = wall + dt
            self._step_back(window, placeholder=True)
            self._position = wall
        else:
            self._cur = (spot.index, self._open(spot.index, spot.offset))
            self._first = show and not spot.gap
        self._probe_ahead(spot.index)           # this chunk too: its keyframe index, for the next seek
        self._seek_target = wall if self._first and spot.offset > 0 else None
        self._audio_valid = False
        if self._playing:
            self._start_sound(spot, wall)
        elif self._audio:
            self._audio.halt()
            self._use_audio = False
        if spot.gap:
            self.stats["gaps"] += 1
            self.presenter.clear()          # nothing was recorded here
            self._set_state(GAP, start=spot.gap[0], end=spot.gap[1], skipped=False)
        else:
            self._set_state(LOADING)
        self._tell_time(wall)

    def _restart_video(self, wall, show):
        """New video decoder at wall, sound untouched (resize, or a speed change that needs other decoding)."""
        self._close_decoders()
        spot = self._spot(wall - (0.001 if show else 0.0))
        if spot.gap:
            spot = Spot(spot.index, 0.0)
        self._cur = (spot.index, self._open(spot.index, spot.offset))
        self._first = show

    def _resized(self):
        if not self._cur:
            return
        if self._box() != self._size:
            self._restart_video(self.position, show=not self._playing)
        else:
            self.presenter.redraw()

    def _prefetch(self, now):
        """Start the next chunk's decoder ahead of time, on a helper thread (probing and spawning FFmpeg take tens of
        milliseconds, which the render thread cannot spare)."""
        if self._nxt is not None or self._prefetching or not self._cur:
            return
        index, dec = self._cur
        if index + 1 >= len(self.source):
            return
        at = prefetch_at(self.source, index, self.pol.speed) if self.source[index].probed else None
        if (at is not None and now >= at) or dec.done:
            self._prefetching = True
            threading.Thread(target=self._prefetch_job, args=(index, self._gen), name="prefetch", daemon=True).start()

    def _prefetch_job(self, index, gen):
        dec = None
        try:
            follow = self.source.follow(index)
            if follow:
                ni, noff, gap = follow
                self.source[ni].info
                dec = self._open(ni, noff)
                with self._lock:
                    if gen == self._gen and self._nxt is None:
                        self._nxt, dec = (ni, noff, gap, dec), None
        except Exception:
            pass
        finally:
            self._prefetching = False
            if dec is not None:
                dec.close()

    def _probe_ahead(self, index):
        """Probe the next chunks in the background so crossing into them never waits on ffprobe."""
        def job():
            for i in range(index, min(index + 3, len(self.source))):
                try:
                    if (self.source[i].info.get("video") or {}).get("codec") in LATE_START:
                        self.source[i].keyframes()
                except Exception:
                    pass
        threading.Thread(target=job, name="probe", daemon=True).start()

    def _advance(self):
        """The current chunk's frames are all out: move to the next chunk (prefetched if possible)."""
        index, dec = self._cur
        follow = self.source.follow(index)
        if follow is None:
            return False
        ni, noff, gap = follow
        with self._lock:
            pre, self._nxt = self._nxt, None
        if pre and pre[0] == ni and pre[3].pol.video_key == self.pol.video_key:
            nxt = pre[3]
        else:
            if pre:
                pre[3].close()
            nxt = self._open(ni, noff)
        dec.close()
        self._cur = (ni, nxt)
        self._probe_ahead(ni + 1)
        if gap:
            self.stats["gaps"] += 1
            if self.skip_gaps:
                if not self._use_audio and self._external is None:
                    # keep the moment left of this chunk, but never land past the next one's first frame
                    self._mono.set(min(self._mono.now() + (gap[1] - gap[0]), gap[1]))
                self._set_state(GAP, start=gap[0], end=gap[1], skipped=True)
                self._set_state(PLAYING if self._playing else PAUSED)
            else:
                self._gap = gap
                self._set_state(GAP, start=gap[0], end=gap[1], skipped=False)
        return True

    def _head(self):
        """Next frame to show, crossing into the next chunk when this one is done. None: not decoded yet."""
        while self._cur:
            dec = self._cur[1]
            f = dec.peek()
            if f is not None:
                return f
            if not dec.exhausted:
                return None
            if not self._advance():
                return None
        return None

    def _show(self, frame):
        frame.preview = False        # it may have been up as a seek's placeholder: now it is the frame itself
        self.presenter.present(frame)
        self._position = frame.wall
        self.stats["shown"] += 1
        if self._gap or self.state in (LOADING, GAP):
            self._gap = None
            self._set_state(PLAYING if self._playing else PAUSED)
        self._tell_time(frame.wall)

    def _ended(self):
        if self.state != ENDED:
            self._playing = False
            self._mono.stop()
            if self._audio:
                self._audio.halt()
            self._use_audio = False
            self._set_state(ENDED)

    def _tick(self):
        """Show what is due. Returns seconds until something may be due."""
        if self._resize_at is not None and time.monotonic() >= self._resize_at:
            self._resize_at = None
            self._resized()
        if not self._cur:
            return MAX_WAIT
        if self._cur[1].fmt != self._fmt:   # the presenter changed what it draws (Direct3D fell back to GDI)
            self._restart_video(self.position, show=not self._playing)
        if self._use_audio and self._audio.drained:
            # the sound ran out before the picture (end of the source): finish on the monotonic clock
            self._mono.set(self._audio.clock.now())
            self._use_audio = False
        frame = self._head()
        if frame is None:
            dec = self._cur[1] if self._cur else None
            if dec is None or dec.exhausted:
                if self._back:
                    self._finish_back(None)
                else:
                    self._ended()
                return MAX_WAIT
            dec.peek(timeout=0.02)
            return 0.0
        if self._back:
            return self._back_tick(frame)
        if self._first:
            self._cur[1].pop()
            if frame.preview:            # the keyframe: shown now, the exact frame follows straight after
                self.presenter.present(frame)
                self.stats["previews"] += 1
                return 0.0
            self._first = False
            target, self._seek_target = self._seek_target, None
            dt = self._frame_time()
            if target is not None and frame.wall > target + 1.5 * dt:
                # The decoder's first frame is frames after the target: the demuxer started on a later keyframe (see
                # LATE_START), or a sparse recording has no frame there.
                # Show this one meanwhile, then decode from earlier and keep the last frame at or before target.
                frame.preview = True
                self.presenter.present(frame)
                self._position = target + dt
                self._step_back(SEEK_RETRY)
                return 0.0
            self._show(frame)
            if self._playing and not self._use_audio and self._external is None:
                self._mono.set(frame.wall)
            return 0.0
        if not self._playing:
            if self._steps:
                self._steps -= 1
                self._cur[1].pop()
                self._show(frame)
                self._audio_valid = False
                return 0.0
            return MAX_WAIT
        now = self.clock_now()
        self._prefetch(now)
        if frame.wall > now:
            return (frame.wall - now) / self.pol.speed
        dec = self._cur[1]
        dec.pop()
        if frame.preview:                # a keyframe preview nobody waited for (decoder restarted while playing)
            frame.release()
            return 0.0
        nxt = dec.peek()
        while nxt is not None and nxt.wall <= now:
            frame.release()
            self.stats["dropped"] += 1
            frame = dec.pop()
            nxt = dec.peek()
        self._show(frame)
        return 0.0

    # ---- stepping back -----------------------------------------------------------------------------------
    def _frame_time(self, index=None):
        if index is None:
            index = self._cur[0] if self._cur else 0
        c = self.source[index]
        return 1.0 / ((c.info["video"] or {}).get("fps") or 30.0)

    def _step_back(self, window=None, placeholder=False):
        """Show the last frame before the one at _position, decoding from `window` seconds earlier. placeholder:
        show the first frame decoded meanwhile (a seek), so the picture changes as soon as there is one."""
        target = self._position
        dt = self._frame_time()
        window = window or 2.5 * dt
        if target <= self.source.start + dt / 2:
            return                          # already at the first frame
        self._close_decoders()
        start = max(self.source.start, target - window)
        spot = self._spot(start)
        if spot.gap:                        # the previous frame is the last one before the gap
            prev = self.source[spot.index - 1]
            spot = Spot(spot.index - 1, max(0.0, (prev.duration or 0.0) - window))
        self._cur = (spot.index, self._open(spot.index, spot.offset))
        self._first = False
        self._back = (target, None, window)
        self._placeholder = placeholder

    def _back_tick(self, frame):
        target, best, window = self._back
        if self._placeholder:
            self._placeholder = False
            if frame.wall < target - self._frame_time() / 2:
                frame.preview = True            # shown now, but still the candidate for the frame before target
                self.presenter.present(frame)
                self.stats["previews"] += 1
        if frame.preview and self._back[1] is None and frame.wall >= target - self._frame_time() / 2:
            self._cur[1].pop()               # a keyframe preview at or after the target: not the frame before
            frame.release()
            return 0.0
        if frame.wall < target - self._frame_time() / 2:
            self._cur[1].pop()
            if best is not None:
                best.release()
            self._back = (target, frame, window)
            return 0.0
        self._finish_back(frame)
        return 0.0

    def _finish_back(self, after):
        target, best, window = self._back
        self._back = None
        if best is not None:
            self._show(best)
            # the decoder sits just past `best`: a step forward or play continues from here
            self._audio_valid = False
            return
        start = max(self.source.start, target - window)
        if start <= self.source.start + 1e-6 or window * 4 > STEP_BACK_MAX:
            self._audio_valid = False       # nothing earlier: stay on the first frame
            if after is not None:
                self._first = True
            return
        self._step_back(window * 4)

class _Lead:
    """Silence before a feed (playing through a gap toward the next chunk)."""
    def __init__(self, feed, wall, frames):
        self.feed, self.wall, self.lead = feed, wall, Silence(frames)
        self.exhausted = False
        self._started = False

    def read(self, nbytes):
        marks = []
        if not self._started:
            self._started = True
            marks.append((0, self.wall))
        data = self.lead.read(nbytes)
        if len(data) >= nbytes:
            return data, marks
        more, m2 = self.feed.read(nbytes - len(data))
        marks += [(off + len(data), w) for off, w in m2]
        self.exhausted = self.feed.exhausted
        return data + more, marks

    def wait(self, timeout):
        self.feed.wait(timeout)

    def close(self):
        self.feed.close()
