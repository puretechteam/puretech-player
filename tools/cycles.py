# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""CPU use from processor cycle counts (QueryProcessCycleTime) instead of Windows' CPU times.

Windows charges CPU time in whole clock ticks (15.6 ms) to the thread running when the tick lands, so a player
that works in short bursts once a frame reads anywhere from a third to three times its real use. Cycle counts are
exact; dividing by the cycles a fully busy core runs per second gives "% of one core" without that noise."""

import ctypes, threading, time
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryProcessCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
_k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
_k32.GetCurrentThread.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_rate = None

def core_rate(seconds=0.4):
    """Cycles per second one fully busy core is charged (measured once by spinning a thread)."""
    global _rate
    if _rate is None:
        out = []

        def spin():
            me, c0, c1 = _k32.GetCurrentThread(), ctypes.c_uint64(), ctypes.c_uint64()
            _k32.QueryThreadCycleTime(me, ctypes.byref(c0))
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < seconds:
                pass
            _k32.QueryThreadCycleTime(me, ctypes.byref(c1))
            out.append((c1.value - c0.value) / (time.perf_counter() - t0))
        for _ in range(3):
            t = threading.Thread(target=spin)
            t.start()
            t.join()
        _rate = max(out)                   # the least interrupted spin is the closest to a whole core
    return _rate

class Cycles:
    """Cycle counter of one process (by pid)."""
    def __init__(self, pid):
        self.handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        self.last = self.read()
        self.t = time.perf_counter()

    def read(self):
        value = ctypes.c_uint64()
        if not self.handle or not _k32.QueryProcessCycleTime(self.handle, ctypes.byref(value)):
            return None
        return value.value

    def percent(self):
        """% of one core since the last call."""
        now, t = self.read(), time.perf_counter()
        if now is None or self.last is None:
            return 0.0
        pct = (now - self.last) / core_rate() / (t - self.t) * 100
        self.last, self.t = now, t
        return pct

    def close(self):
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None
