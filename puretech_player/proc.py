# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Finding FFmpeg and running it: no console windows, big pipes, and a job object so no decoder outlives us."""

import ctypes, os, shutil, subprocess, sys, threading
from ctypes import wintypes

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
POPEN_FLAGS = CREATE_NO_WINDOW if sys.platform == "win32" else 0
# Decoders get no console at all: CREATE_NO_WINDOW still starts a hidden conhost.exe (7.5 MB) per process.
SPAWN_FLAGS = DETACHED_PROCESS if sys.platform == "win32" else 0
JOB_LIMIT_KILL_ON_CLOSE = 0x2000
JOB_EXTENDED_LIMIT_INFO = 9

def find_tool(name, near=None):
    """Path of ffmpeg/ffprobe: next to `near` (an explicit ffmpeg path) first, then PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    if near:
        if os.path.basename(near).lower().startswith(name) and os.path.isfile(near):
            return near
        candidate = os.path.join(os.path.dirname(near), exe)
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} not found: pass ffmpeg=<path to ffmpeg.exe> or put it on PATH")
    return found

def pipe(size):
    """(read fd, write fd) of an anonymous pipe with a `size`-byte buffer. Python's default pipe holds 4 KB, so a
    1080p frame (8 MB) would take two thousand reads."""
    if sys.platform != "win32":
        return os.pipe()
    import _winapi, msvcrt
    read, write = _winapi.CreatePipe(None, size)
    return msvcrt.open_osfhandle(read, os.O_RDONLY), msvcrt.open_osfhandle(write, 0)

class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
                ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]

_job = None
_job_lock = threading.Lock()

def _job_handle():
    """A job that kills its processes when this process ends (even on a crash), created on first use."""
    global _job
    with _job_lock:
        if _job is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            job = k32.CreateJobObjectW(None, None)
            info = _JobLimits(LimitFlags=JOB_LIMIT_KILL_ON_CLOSE)
            if job and k32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
                _job = (k32, job)
            else:
                _job = (k32, None)
        return _job

def spawn(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    """Start an FFmpeg process that dies with this one."""
    proc = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, creationflags=SPAWN_FLAGS)
    if sys.platform == "win32":
        k32, job = _job_handle()
        if job:
            k32.AssignProcessToJobObject(job, int(proc._handle))
    return proc

def run(cmd, timeout=60):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=POPEN_FLAGS)

def reap(proc):
    """Kill a process and collect it off the calling thread (a kill returns at once, the wait may not)."""
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    threading.Thread(target=proc.wait, name="reap", daemon=True).start()
