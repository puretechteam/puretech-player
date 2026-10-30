# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Putting frames on screen. The player only talks to the Presenter interface, so a Direct3D 11 presenter can
replace the GDI one without touching playback."""

import ctypes, sys, threading
from ctypes import wintypes

class Presenter:
    """Draws frames (decode.Frame) into a target. Called from the player's render thread; redraw() may also come
    from the UI thread, so implementations lock. pixel_format: what the decoder should send ("bgra" fitted to
    size(), or "yuv420p"); slack: how much bigger than size() frames may be (a presenter that scales on the
    graphics card is cheaper fed near the source size)."""
    pixel_format = "bgra"
    slack = 1.0

    def size(self):
        """(width, height) of the drawing area in device pixels."""
        raise NotImplementedError

    def ready(self, timeout=None):
        """True once present() will not stall (a presenter may still be starting up)."""
        return True

    def present(self, frame):
        """Show frame and keep it for redraw(); the frame shown before it is released."""
        raise NotImplementedError

    def redraw(self):
        """Paint the last frame again (after the window was uncovered or resized)."""

    def clear(self):
        """Drop the last frame and paint the area black."""

    def close(self):
        self.clear()

class NullPresenter(Presenter):
    """No window: counts frames (benchmarks, tests, headless use). on_frame(frame) sees each one."""
    def __init__(self, w=1280, h=720, on_frame=None, pixel_format="bgra", slack=1.0):
        self.w, self.h, self.on_frame = w, h, on_frame
        self.pixel_format, self.slack = pixel_format, slack
        self.frames = 0
        self.last = None
        self._lock = threading.Lock()

    def size(self):
        return self.w, self.h

    def present(self, frame):
        with self._lock:
            if self.last is not None and self.last is not frame:
                self.last.release()
            self.last = frame
            self.frames += 1
        if self.on_frame:
            self.on_frame(frame)

    def clear(self):
        with self._lock:
            if self.last is not None:
                self.last.release()
            self.last = None

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

if sys.platform == "win32":
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE]
    gdi32.GetStockObject.restype = wintypes.HANDLE
    gdi32.SetDIBitsToDevice.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.UINT]
    gdi32.StretchDIBits.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT, wintypes.DWORD]
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]

BLACK_BRUSH, SRCCOPY, DIB_RGB_COLORS, HALFTONE = 4, 0x00CC0020, 0, 4

class GdiPresenter(Presenter):
    """SetDIBitsToDevice into a window (a Tk frame's HWND). The decoder already scaled the frame to the window, so
    this is one copy per frame; StretchDIBits only bridges the moment between a resize and the resized decoder."""
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.last = None
        self._closed = False
        self._bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biPlanes=1, biBitCount=32)
        self._black = gdi32.GetStockObject(BLACK_BRUSH)
        self._layout = None         # (client size, frame size) the bars were last painted for
        self._lock = threading.Lock()

    def size(self):
        r = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top

    def _place(self, cw, ch, w, h):
        """Destination rectangle of a w x h frame in a cw x ch area: centred, shrunk to fit if it is bigger."""
        if w <= cw and h <= ch:
            return (cw - w) // 2, (ch - h) // 2, w, h
        scale = min(cw / w, ch / h)
        dw, dh = max(1, round(w * scale)), max(1, round(h * scale))
        return (cw - dw) // 2, (ch - dh) // 2, dw, dh

    def _bars(self, hdc, cw, ch, x, y, w, h):
        for rect in ((0, 0, cw, y), (0, y + h, cw, ch), (0, y, x, y + h), (x + w, y, cw, y + h)):
            if rect[2] > rect[0] and rect[3] > rect[1]:
                user32.FillRect(hdc, ctypes.byref(wintypes.RECT(*rect)), self._black)

    def _draw(self, frame, force_bars=False):
        cw, ch = self.size()
        if cw <= 0 or ch <= 0:
            return
        hdc = user32.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            x, y, dw, dh = self._place(cw, ch, frame.w, frame.h)
            layout = (cw, ch, x, y, dw, dh)
            if force_bars or layout != self._layout:
                self._bars(hdc, cw, ch, x, y, dw, dh)
                self._layout = layout
            self._bmi.biWidth, self._bmi.biHeight = frame.w, -frame.h       # negative: rows run top to bottom
            bits = (ctypes.c_char * len(frame.buf)).from_buffer(frame.buf)
            if (dw, dh) == (frame.w, frame.h):
                gdi32.SetDIBitsToDevice(hdc, x, y, frame.w, frame.h, 0, 0, 0, frame.h, bits, ctypes.byref(self._bmi),
                                        DIB_RGB_COLORS)
            else:
                gdi32.SetStretchBltMode(hdc, HALFTONE)
                gdi32.StretchDIBits(hdc, x, y, dw, dh, 0, 0, frame.w, frame.h, bits, ctypes.byref(self._bmi),
                                    DIB_RGB_COLORS, SRCCOPY)
        finally:
            user32.ReleaseDC(self.hwnd, hdc)

    def present(self, frame):
        with self._lock:
            if self._closed:
                frame.release()
                return
            self._draw(frame)
            if self.last is not None and self.last is not frame:
                self.last.release()
            self.last = frame

    def redraw(self):
        with self._lock:
            if self._closed:
                return
            if self.last is not None:
                self._draw(self.last, force_bars=True)
            else:
                self._paint_black()

    def _paint_black(self):
        cw, ch = self.size()
        hdc = user32.GetDC(self.hwnd)
        if hdc:
            user32.FillRect(hdc, ctypes.byref(wintypes.RECT(0, 0, cw, ch)), self._black)
            user32.ReleaseDC(self.hwnd, hdc)
        self._layout = None

    def clear(self):
        with self._lock:
            if self.last is not None:
                self.last.release()
            self.last = None
            if not self._closed:
                self._paint_black()

    def close(self):
        self.clear()
        with self._lock:
            self._closed = True
