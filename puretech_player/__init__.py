# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Pure Tech Player: plays chunked recordings (a folder of HH-MM-SS files per day) as one timeline, with FFmpeg
decoder processes, WASAPI sound and GDI drawing into a Tk frame. Pure Python on ctypes; no other packages."""

__version__ = "0.1.0"

from .timeline import Chunk, Source, Spot, fmt_wall, parse_start
from .policy import policy, SPEEDS, MIN_SPEED, MAX_SPEED
from .player import Player
from .grid import Grid
from .clip import plan_clip, export_clip, ClipPlan
from .present import Presenter, GdiPresenter, NullPresenter
from .present_d3d import D3D11Presenter, make_presenter, warm_up

def VideoView(parent, **kw):
    """A Tk frame to play into (imported lazily so headless use needs no Tk)."""
    from .view import VideoView as _View
    return _View(parent, **kw)

__all__ = ["Chunk", "Source", "Spot", "fmt_wall", "parse_start", "policy", "SPEEDS", "MIN_SPEED", "MAX_SPEED",
           "Player", "Grid", "VideoView", "plan_clip", "export_clip", "ClipPlan", "Presenter", "GdiPresenter",
           "NullPresenter", "D3D11Presenter", "make_presenter", "warm_up", "__version__"]
