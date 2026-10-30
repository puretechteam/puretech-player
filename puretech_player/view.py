# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""VideoView: a Tk frame a Player draws into. Works inside customtkinter windows too (it is a plain tk.Frame)."""

import tkinter as tk
from .present_d3d import make_presenter, warm_up

class VideoView(tk.Frame):
    """backend: "d3d11" (default, falls back to GDI where Direct3D 11 is missing) or "gdi"."""
    def __init__(self, parent, backend="d3d11", **kw):
        self.backend = backend
        if backend == "d3d11":
            warm_up()
        kw.setdefault("background", "black")
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("borderwidth", 0)
        super().__init__(parent, **kw)
        self._players = []
        self._presenter = None
        self.bind("<Configure>", self._configured, add="+")
        self.bind("<Expose>", self._exposed, add="+")
        self.bind("<Destroy>", self._destroyed, add="+")
        # Start the presenter now: Direct3D takes ~190 ms to start, which an app opening a file later never waits for
        self.after_idle(self.presenter)

    def presenter(self):
        """The presenter drawing into this frame's window, kept for every Player shown in it (Player(view=...)
        calls this)."""
        if self._presenter is None:
            self._presenter = make_presenter(self.winfo_id(), self.backend)
        return self._presenter

    def attach(self, player):
        self._players.append(player)

    def detach(self, player):
        if player in self._players:
            self._players.remove(player)

    def _configured(self, event):
        for p in self._players:
            p.resize()

    def _exposed(self, event):
        for p in self._players:
            p.refresh()
        if not self._players and self._presenter is not None:
            self._presenter.redraw()

    def _destroyed(self, event):
        """The window is going: stop whatever plays in it before its presenter goes."""
        if event.widget is not self:
            return
        for p in list(self._players):
            p.close()
        if self._presenter is not None:
            self._presenter.close()
            self._presenter = None
