# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Grid: several sources (cameras, screens) on one wall clock, sound from one of them."""

import threading
from .clock import MonotonicClock
from .player import Player, PLAYING, GAP, ENDED

class GridClock:
    """The sound player's clock while it makes sound, else a monotonic clock carried on from it."""
    def __init__(self):
        self.lead = None
        self.mono = MonotonicClock()

    def now(self):
        p = self.lead
        if p is not None and p._use_audio and p.playing and p.state in (PLAYING, GAP):
            t = p._audio.clock.now()
            if t is not None:
                self.mono.set(t)
                return t
        return self.mono.now()

class Grid:
    """sources: one entry per tile (anything Player accepts). views: a VideoView per tile, or None (headless).
    audio: index of the tile whose sound plays, or None for silence."""
    def __init__(self, sources, ffmpeg=None, views=None, audio=0, threads=None, presenters=None):
        self.clock = GridClock()
        self.on_state = None         # on_state(tile index, state, info)
        self.players = []
        self._lock = threading.Lock()
        self._stepping = False       # paused by step(): the clock follows the frames the lead tile steps to
        self._clock_lock = threading.Lock()
        for i, src in enumerate(sources):
            p = Player(src, ffmpeg=ffmpeg, view=views[i] if views else None,
                       presenter=presenters[i] if presenters else None, audio=(i == audio), skip_gaps=False,
                       clock=self.clock, threads=threads)
            p.on_state = lambda state, info, i=i: self._state(i, state, info)
            p._on_shown = self._shown
            self.players.append(p)
        if audio is not None and 0 <= audio < len(self.players):
            self.clock.lead = self.players[audio]
        starts = [p.source.start for p in self.players]
        self.clock.mono.set(min(starts))
        self.start = min(starts)
        self.end = max(p.source.end for p in self.players)

    @property
    def position(self):
        return self.clock.now()

    def play(self):
        with self._clock_lock:
            self._stepping = False
            self.clock.mono.start()
        for p in self.players:
            p.play()

    def pause(self):
        self.clock.mono.stop()
        for p in self.players:
            p.pause()

    def seek(self, wall_time):
        with self._clock_lock:
            self._stepping = False
            self.clock.mono.set(wall_time)
        for p in self.players:
            p.seek(wall_time)

    def set_speed(self, speed):
        self.clock.mono.set_speed(speed)
        for p in self.players:
            p.set_speed(speed)

    def step(self, direction=1):
        """One frame forward (+1) or back (-1) in every tile; pauses first. Once the lead tile (the one with the
        sound, else the first) shows its new frame, the clock and position are that frame's time."""
        with self._clock_lock:
            self._stepping = True
            self.clock.mono.stop()
        for p in self.players:
            p.step(direction)

    def set_audio(self, index):
        """Take the sound (and the clock) from another tile."""
        for i, p in enumerate(self.players):
            p._audio_wanted = (i == index)
        self.clock.lead = self.players[index] if index is not None else None
        self.seek(self.position)

    def close(self):
        for p in self.players:
            p.close()

    def _shown(self, player, wall):
        # render thread of `player`: a stepped frame of the lead tile is where play goes on from
        if player is not (self.clock.lead or self.players[0]):
            return
        with self._clock_lock:
            if self._stepping:
                self.clock.mono.set(wall)

    def _state(self, i, state, info):
        if self.on_state:
            try:
                self.on_state(i, state, info)
            except Exception:
                pass
        if state not in (GAP, ENDED):
            return
        with self._lock:
            # every tile is waiting on a hole (or done): jump to where the first of them has footage again
            states = [p.state for p in self.players]
            if all(s in (GAP, ENDED) for s in states):
                upcoming = [p.next_media() for p in self.players if p.state == GAP and p.next_media()]
                if upcoming:
                    self.seek(min(upcoming))
