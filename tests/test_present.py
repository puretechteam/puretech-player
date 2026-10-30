# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Presenters against a real Tk window (skipped where there is no display)."""

import time
import pytest
tk = pytest.importorskip("tkinter")
from puretech_player import present_d3d
from puretech_player.decode import Frame, frame_bytes

@pytest.fixture
def window():
    root = None
    for _ in range(3):                  # Tk occasionally fails to start right after another test's window closed
        try:
            root = tk.Tk()
            break
        except tk.TclError:
            time.sleep(0.2)
    if root is None:
        pytest.skip("no display")
    frame = tk.Frame(root, width=320, height=180, background="black")
    frame.pack()
    root.update()
    yield frame
    root.destroy()

def test_direct3d_presenter_draws_yuv_frames(window):
    p = present_d3d.D3D11Presenter(window.winfo_id())
    p._ready.wait(5)
    if p.error:
        pytest.skip(f"no Direct3D 11 here: {p.error}")
    assert p.pixel_format == "yuv420p" and p.size() == (320, 180)
    buf = bytearray(frame_bytes(160, 90, "yuv420p"))
    p.present(Frame(0.0, buf, 160, 90, fmt="yuv420p"))
    p.redraw()
    assert [(t[2], t[3]) for t in p._tex] == [(160, 90), (80, 45), (80, 45)]
    p.close()

def test_direct3d_failure_falls_back_to_gdi(window, monkeypatch):
    def broken(self):
        raise OSError("no device")
    monkeypatch.setattr(present_d3d.D3D11Presenter, "_create", broken)
    p = present_d3d.make_presenter(window.winfo_id())
    p._ready.wait(5)
    assert p.error == "no device" and p.pixel_format == "bgra" and p.slack == 1.0
    p.present(Frame(0.0, bytearray(frame_bytes(32, 18, "bgra")), 32, 18, fmt="bgra"))
    p.close()

def test_gdi_backend_on_request(window):
    from puretech_player.present import GdiPresenter
    assert isinstance(present_d3d.make_presenter(window.winfo_id(), prefer="gdi"), GdiPresenter)

def test_closing_the_window_while_playing_stops_cleanly(window, camera, monkeypatch):
    import threading
    from conftest import FFMPEG
    from puretech_player import Player, VideoView
    if not FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    view = VideoView(window, width=320, height=180)
    view.pack()
    window.update()
    player = Player(camera[0], ffmpeg=FFMPEG, view=view, volume=0.0)
    player.play()
    end = time.time() + 1.0
    while time.time() < end:
        window.update()
        time.sleep(0.01)
    view.destroy()                          # closes the player, then the presenter
    assert not player._thread.is_alive() and errors == []
