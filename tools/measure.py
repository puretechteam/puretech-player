# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Pure Tech Player against ffplay, on the same generated files, measured the same way.

    python tools/measure.py [--work DIR] [--seconds 60] [--only av1-1080p30,...] [--out docs/perf.json]

For each file (testsrc2 + sine, 70 s, 1080p30 and 720p15, in AV1, H.265, H.266 and H.264):
  * time to first frame: process launch until the picture is on screen (both players read off the screen with
    the same pixel check), and for ours also the in-process open-to-first-frame time
  * seek latency: 10 seeks while playing, from the command until the screen shows the target second (each
    sample frame carries its second as 7 black/white boxes, read off the screen; ffplay lands on the keyframe
    before the target, which counts as arrived for both players; ours then also reports the exact frame)
  * CPU and RAM at 1x over --seconds: ffplay's process vs our decoder processes plus how much the Python
    process grew over its idle baseline (Python and Tk are already running in the app that embeds the player).
    CPU comes from processor cycle counts (tools/cycles.py): Windows' CPU times are charged per 15.6 ms clock tick
    and read a third to three times the real use of work done in short bursts, like a player's
  * dropped frames (ffplay's fd= counter vs our late-frame counter)
Windows only. Needs psutil (measurement only; the player needs nothing). Plays silently (volume 0 in both)."""

import argparse, ctypes, json, os, random, re, shutil, statistics, subprocess, sys, threading, time
from ctypes import wintypes
from cycles import Cycles, core_rate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TITLE_OURS, TITLE_FFPLAY = "PTP-MEASURE-OURS", "PTP-MEASURE-FFPLAY"
CODECS = {
    "av1": ["-c:v", "libsvtav1", "-preset", "10", "-crf", "40", "-svtav1-params", "lp=6"],
    "hevc": ["-c:v", "libx265", "-preset", "fast", "-crf", "26", "-x265-params", "pools=6:log-level=error"],
    "vvc": ["-c:v", "libvvenc", "-preset", "faster", "-qp", "32", "-threads", "6"],
    "h264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "6"],
}
FORMATS = {"1080p30": ("1920x1080", 30, (1280, 720)), "720p15": ("1280x720", 15, (1280, 720))}
LENGTH = 70
GOP_SECONDS = 2
TTFF_RUNS = 5
SEEK_TARGETS = (35, 8, 52, 20, 62, 14, 44, 27, 58, 5)    # seconds, each far from the one before
BITS = 7                                                  # time code: whole seconds 0..127

# ---- screen reading ---------------------------------------------------------------------------------------------
user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]

class BMI(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD), ("pad", wintypes.DWORD * 4)]

class Screen:
    """Grabs a window's client area as composited on screen (what the user sees) and reduces it to a sample."""
    def __init__(self, hwnd):
        self.hwnd = hwnd
        r = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        self.x, self.y, self.w, self.h = pt.x, pt.y, r.right, r.bottom
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        bmi = BMI(biSize=40, biWidth=self.w, biHeight=-self.h, biPlanes=1, biBitCount=32)
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem, self.bmp)

    def sample(self):
        """Every 24th pixel of the middle 80% as bytes (B, G, R per pixel)."""
        gdi32.BitBlt(self.mem, 0, 0, self.w, self.h, self.screen, self.x, self.y, 0x00CC0020)
        raw = ctypes.string_at(self.bits, self.w * self.h * 4)
        out = bytearray()
        for row in range(self.h // 10, self.h * 9 // 10, 24):
            line = raw[row * self.w * 4:(row + 1) * self.w * 4]
            px = line[self.w // 10 * 4:self.w * 9 // 10 * 4]
            out += px[0::96] + px[1::96] + px[2::96]
        return bytes(out)

    def close(self):
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.mem)
        user32.ReleaseDC(None, self.screen)

def code_boxes(w, h):
    """(x, y, size) of each time code box, in video pixels."""
    size = h // 12
    return [(size // 2 + k * size * 5 // 4, h - size * 2, size) for k in range(BITS)]

def code_filter(w, h):
    boxes = []
    for k, (x, y, s) in enumerate(code_boxes(w, h)):
        boxes.append(f"drawbox=x={x}:y={y}:w={s}:h={s}:color=black:t=fill")
        boxes.append(f"drawbox=x={x}:y={y}:w={s}:h={s}:color=white:t=fill:enable='eq(mod(floor(t/{2 ** k}),2),1)'")
    return ",".join(boxes)

class Timecode:
    """Reads the second a window shows from the time code boxes (only their strip is copied off the screen)."""
    def __init__(self, hwnd, video_w, video_h):
        r = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        scale = min(r.right / video_w, r.bottom / video_h)
        ox, oy = pt.x + (r.right - video_w * scale) / 2, pt.y + (r.bottom - video_h * scale) / 2
        boxes = code_boxes(video_w, video_h)
        self.points = [(int(ox + (x + s / 2) * scale), int(oy + (y + s / 2) * scale)) for x, y, s in boxes]
        self.x0, self.y0 = self.points[0][0] - 2, self.points[0][1] - 2
        self.w, self.h = self.points[-1][0] - self.x0 + 3, 5
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        bmi = BMI(biSize=40, biWidth=self.w, biHeight=-self.h, biPlanes=1, biBitCount=32)
        self.bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        gdi32.SelectObject(self.mem, self.bmp)

    def read(self):
        gdi32.BitBlt(self.mem, 0, 0, self.w, self.h, self.screen, self.x0, self.y0, 0x00CC0020)
        raw = ctypes.string_at(self.bits, self.w * self.h * 4)
        value = 0
        for k, (x, y) in enumerate(self.points):
            i = ((y - self.y0) * self.w + (x - self.x0)) * 4
            if raw[i] + raw[i + 1] + raw[i + 2] > 3 * 128:
                value |= 1 << k
        return value

    def close(self):
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.mem)
        user32.ReleaseDC(None, self.screen)

def bright(sample):
    return sum(sample) / max(1, len(sample))

def find_window(title, timeout=15.0, proc=None):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        if proc is not None and proc.poll() is not None:
            return None
        time.sleep(0.002)
    return None

def topmost(hwnd):
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)

def wait_picture(hwnd, t0, timeout=15.0):
    """Seconds from t0 until the window shows a picture (testsrc2 is bright; an empty window is black)."""
    scr = None
    end = t0 + timeout
    try:
        while time.perf_counter() < end:
            if scr is None:
                r = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(r))
                if r.right < 64 or r.bottom < 64:
                    time.sleep(0.002)
                    continue
                scr = Screen(hwnd)
            if bright(scr.sample()) > 60:
                return time.perf_counter() - t0
        return None
    finally:
        if scr:
            scr.close()

# ---- the two players ------------------------------------------------------------------------------------------
class Ours:
    name = "ours"

    def __init__(self, path, size, quick=False):
        w, h = size
        self.proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--runner", path, str(w), str(h)]
                                     + (["--quick"] if quick else []),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        self.lines = []
        self.base_rss = self.base_uss = None
        self.first_ms = None
        self._cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            with self._cond:
                if line.startswith("BASE "):
                    self.base_rss = int(line.split()[1])
                    if len(line.split()) > 2:
                        self.base_uss = int(line.split()[2])
                elif line.startswith("FIRST "):
                    self.first_ms = float(line.split()[1])
                self.lines.append(line)
                self._cond.notify_all()

    def expect(self, prefix, timeout=10.0):
        with self._cond:
            ok = self._cond.wait_for(lambda: any(l.startswith(prefix) for l in self.lines), timeout)
            if not ok:
                return None
            for i, l in enumerate(self.lines):
                if l.startswith(prefix):
                    return self.lines.pop(i)

    def send(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def window(self):
        return find_window(TITLE_OURS, proc=self.proc)

    def pause(self):
        self.send("pause")

    def play(self):
        self.send("play")

    def seek(self, frac, duration):
        self.send(f"seek {frac * duration:.3f}")

    def dropped(self):
        self.send("stats")
        line = self.expect("STATS ", 5)
        return json.loads(line[6:]) if line else {}

    def procs(self, psutil):
        p = psutil.Process(self.proc.pid)
        return [p] + p.children(recursive=True)

    def close(self):
        try:
            self.send("quit")
            self.proc.wait(5)
        except Exception:
            self.proc.kill()

class FFplay:
    name = "ffplay"
    VK = {"p": (0x50, 0x19)}

    def __init__(self, path, size, ffplay):
        w, h = size
        self.proc = subprocess.Popen([ffplay, "-hide_banner", "-loglevel", "info", "-x", str(w), "-y", str(h),
                                      "-left", "100", "-top", "100", "-window_title", TITLE_FFPLAY, "-volume", "0",
                                      path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.fd = 0
        self.hwnd = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            chunk = self.proc.stderr.read(512)
            if not chunk:
                return
            buf = (buf + chunk)[-4096:]
            for m in re.finditer(rb"fd=\s*(\d+)", buf):
                self.fd = int(m[1])

    def window(self):
        self.hwnd = find_window(TITLE_FFPLAY, proc=self.proc)
        return self.hwnd

    def _key(self, ch):
        vk, sc = self.VK[ch]
        user32.PostMessageW(self.hwnd, 0x0100, vk, 1 | (sc << 16))
        user32.PostMessageW(self.hwnd, 0x0101, vk, 1 | (sc << 16) | (3 << 30))

    def pause(self):
        self._key("p")

    play = pause

    def seek(self, frac, duration):
        r = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(r))
        x, y = int(frac * r.right), r.bottom // 2
        lp = (y << 16) | x
        user32.PostMessageW(self.hwnd, 0x0200, 0, lp)           # WM_MOUSEMOVE: SDL takes the click's x from here
        user32.PostMessageW(self.hwnd, 0x0204, 0x0002, lp)      # WM_RBUTTONDOWN: ffplay seeks to x / width
        user32.PostMessageW(self.hwnd, 0x0205, 0, lp)

    def dropped(self):
        return {"dropped": self.fd}

    def procs(self, psutil):
        return [psutil.Process(self.proc.pid)]

    def close(self):
        self.proc.kill()
        self.proc.wait(5)

# ---- measurements -----------------------------------------------------------------------------------------------
def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]

def first_frame(make):
    times, inner = [], []
    for _ in range(TTFF_RUNS):
        t0 = time.perf_counter()
        p = make()
        try:
            hwnd = p.window()
            if hwnd:
                topmost(hwnd)
                t = wait_picture(hwnd, t0)
                if t is not None:
                    times.append(t)
                if getattr(p, "first_ms", None) is None and isinstance(p, Ours):
                    p.expect("FIRST ", 3)
                if getattr(p, "first_ms", None) is not None:
                    inner.append(p.first_ms / 1000)
        finally:
            p.close()
        time.sleep(0.5)
    out = {"launch_to_picture_ms": round(statistics.median(times) * 1000, 1) if times else None, "runs": len(times)}
    if inner:
        out["open_to_first_frame_ms"] = round(statistics.median(inner) * 1000, 1)
    return out

def seeks(make, duration, video_size):
    p = make()
    lat, exact, preview = [], [], []
    try:
        hwnd = p.window()
        topmost(hwnd)
        wait_picture(hwnd, time.perf_counter())
        time.sleep(1.5)
        tc = Timecode(hwnd, *video_size)
        try:
            for target in SEEK_TARGETS:
                if isinstance(p, Ours):
                    with p._cond:
                        p.lines.clear()
                t0 = time.perf_counter()
                p.seek(target / duration, duration)
                got = None
                while time.perf_counter() - t0 < 10:
                    v = tc.read()
                    if target - GOP_SECONDS - 1 <= v <= target + 1:
                        got = time.perf_counter() - t0
                        break
                if got is not None:
                    lat.append(got)
                if isinstance(p, Ours):
                    line = p.expect("SEEKED ", 5)
                    if line:
                        exact.append(float(line.split()[1]) / 1000)
                    line = p.expect("PREVIEW ", 0.01)
                    if line:
                        preview.append(float(line.split()[1]) / 1000)
                time.sleep(1.0)
        finally:
            tc.close()
    finally:
        p.close()
    out = {"n": len(lat), "of": len(SEEK_TARGETS)}
    if lat:
        out.update(median_ms=round(statistics.median(lat) * 1000, 1), p95_ms=round(pct(lat, 0.95) * 1000, 1))
    if exact:
        out.update(exact_frame_median_ms=round(statistics.median(exact) * 1000, 1),
                   exact_frame_p95_ms=round(pct(exact, 0.95) * 1000, 1))
    if preview:
        out.update(keyframe_preview_median_ms=round(statistics.median(preview) * 1000, 1))
    return out

def steady(make, seconds, psutil):
    p = make()
    try:
        hwnd = p.window()
        topmost(hwnd)
        wait_picture(hwnd, time.perf_counter())
        time.sleep(3.0)                          # past start-up
        tracked, counters = {}, {}
        cpu, ticks, rss, main_rss, child_rss, uss, main_uss = [], [], [], [], [], [], []
        base = getattr(p, "base_rss", None)
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            try:
                procs = p.procs(psutil)
            except psutil.NoSuchProcess:
                break
            for q in procs:
                if q.pid not in tracked:
                    tracked[q.pid] = q
                    q.cpu_percent(None)
                    counters[q.pid] = Cycles(q.pid)
            procs = [tracked[q.pid] for q in procs]      # the same objects, so cpu_percent has a last reading
            time.sleep(1.0)
            # private memory (USS) walks the whole working set and can stall the process measured: every 10 s only
            full = len(cpu) % 10 == 5
            total_cpu = total_ticks = total_rss = main = total_uss = m_uss = 0.0
            for q in procs:
                try:
                    total_ticks += q.cpu_percent(None)
                    total_cpu += counters[q.pid].percent()
                    info = q.memory_full_info() if full else q.memory_info()
                except psutil.NoSuchProcess:
                    continue
                total_rss += info.rss
                total_uss += getattr(info, "uss", 0)
                if q.pid == p.proc.pid:
                    main, m_uss = info.rss, getattr(info, "uss", 0)
            cpu.append(total_cpu)
            ticks.append(total_ticks)
            rss.append(total_rss)
            main_rss.append(main)
            child_rss.append(total_rss - main)
            if full:
                uss.append(total_uss)
                main_uss.append(m_uss)
        stats = p.dropped()
    finally:
        p.close()
    mb = 1 / 2 ** 20
    for c in counters.values():
        c.close()
    out = {"cpu_pct_of_one_core": round(statistics.mean(cpu), 1), "cpu_pct_ticks": round(statistics.mean(ticks), 1), "cpu_pct_of_machine": round(statistics.mean(cpu) /
           (os.cpu_count() or 1), 2), "ram_mb_mean": round(statistics.mean(rss) * mb, 1),
           "ram_mb_peak": round(max(rss) * mb, 1), "private_mb_mean": round(statistics.mean(uss) * mb, 1),
           "dropped": stats.get("dropped"), "samples": len(cpu)}
    if base is not None:
        grow = [m - base for m in main_rss]
        out["python_growth_mb_mean"] = round(statistics.mean(grow) * mb, 1)
        out["decoders_mb_mean"] = round(statistics.mean(child_rss) * mb, 1)
        out["added_ram_mb_mean"] = round((statistics.mean(grow) + statistics.mean(child_rss)) * mb, 1)
        out["added_ram_mb_peak"] = round(max(g + c for g, c in zip(grow, child_rss)) * mb, 1)
        out["python_baseline_mb"] = round(base * mb, 1)
        base_uss = getattr(p, "base_uss", None)
        if base_uss is not None:
            out["added_private_mb_mean"] = round((statistics.mean(uss) - base_uss) * mb, 1)
        out["shown"] = stats.get("shown")
        out["audio_underruns"] = stats.get("audio_underruns")
    return out

# ---- sample files ---------------------------------------------------------------------------------------------
def make_samples(ffmpeg, work, only):
    os.makedirs(work, exist_ok=True)
    files = {}
    for fmt, (size, fps, _) in FORMATS.items():
        for codec, args in CODECS.items():
            key = f"{codec}-{fmt}"
            if only and key not in only:
                continue
            path = os.path.join(work, f"{key}.mp4")
            if not os.path.exists(path):
                t0 = time.perf_counter()
                cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                       f"testsrc2=size={size}:rate={fps}:duration={LENGTH}", "-f", "lavfi", "-i",
                       f"sine=frequency=440:sample_rate=48000:duration={LENGTH}", "-vf", code_filter(*map(int, size.split("x"))),
                       *args, "-g", str(fps * GOP_SECONDS),
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", path + ".part.mp4"]
                subprocess.run(cmd, check=True)
                os.replace(path + ".part.mp4", path)
                print(f"  made {key} in {time.perf_counter() - t0:.0f} s", flush=True)
            files[key] = path
    return files

def tool_version(exe):
    out = subprocess.run([exe, "-version"], capture_output=True, text=True).stdout.splitlines()
    return out[0] if out else "?"

def package_size():
    pkg = os.path.join(ROOT, "puretech_player")
    total = sum(os.path.getsize(os.path.join(pkg, f)) for f in os.listdir(pkg) if f.endswith(".py"))
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(pkg)):
            if f.endswith(".py"):
                z.write(os.path.join(pkg, f), f"puretech_player/{f}")
    return total, len(buf.getvalue())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=os.path.join(ROOT, "work", "measure-tc"))
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "perf.json"))
    ap.add_argument("--skip", default="", help="comma list of: ttff,seek,steady")
    ap.add_argument("--players", default="ffplay,ours")
    args = ap.parse_args()
    import psutil
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    ffmpeg, ffplay = shutil.which("ffmpeg"), shutil.which("ffplay")
    only = set(filter(None, args.only.split(",")))
    skip = set(filter(None, args.skip.split(",")))
    print("making sample files", flush=True)
    files = make_samples(ffmpeg, args.work, only)
    src_bytes, zip_bytes = package_size()
    ffplay_bytes = os.path.getsize(ffplay)
    results = {"ffmpeg": tool_version(ffmpeg), "machine": {"cpus": os.cpu_count(),
               "core_ghz": round(core_rate() / 1e9, 2)}, "seconds": args.seconds,
               "files": {}, "size": {"ffplay_exe_bytes": ffplay_bytes, "package_py_bytes": src_bytes,
                                     "package_zip_bytes": zip_bytes}}
    for key, path in files.items():
        fmt = key.split("-", 1)[1]
        size = FORMATS[fmt][2]
        print(f"{key}", flush=True)
        row = {}
        for player in args.players.split(","):
            make = (lambda: FFplay(path, size, ffplay)) if player == "ffplay" else (lambda: Ours(path, size))
            quick = (lambda: FFplay(path, size, ffplay)) if player == "ffplay" else (lambda: Ours(path, size, True))
            r = {}
            if "ttff" not in skip:
                r["first_frame"] = first_frame(quick)
            if "seek" not in skip:
                r["seek"] = seeks(make, LENGTH, tuple(map(int, FORMATS[fmt][0].split("x"))))
            if "steady" not in skip:
                r["steady"] = steady(make, args.seconds, psutil)
            row[player] = r
            print(f"  {player}: {json.dumps(r)}", flush=True)
        results["files"][key] = row
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
    print("wrote", args.out)

# ---- our side: a minimal window around the engine, driven over stdin -------------------------------------------
def runner(path, w, h, quick=False):
    """quick: a time-to-first-frame run, which skips the memory baseline (psutil would add to the time)."""
    import tkinter as tk
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    sys.path.insert(0, os.environ.get("PTP_ENGINE", ROOT))     # PTP_ENGINE: measure another copy of the engine
    import puretech_player
    from puretech_player import Player, VideoView
    if hasattr(puretech_player, "warm_up"):
        puretech_player.warm_up()   # what an app does at start-up (FrameKeep, demo.py)
    root = tk.Tk()
    root.title(TITLE_OURS)
    root.geometry(f"+100+100")
    view = VideoView(root, width=w, height=h)
    view.pack()
    root.update()
    if not quick:
        import psutil
        mem = psutil.Process().memory_full_info()
        print("BASE", mem.rss, mem.uss, flush=True)
    state = {"seek_t0": None, "opened": time.perf_counter(), "first": False}

    def on_time(wall):
        pass

    player = Player(path, view=view, volume=0.0)
    orig = player.presenter.present

    def present(frame):
        orig(frame)
        if not state["first"]:
            state["first"] = True
            print("FIRST", round((time.perf_counter() - state["opened"]) * 1000, 1), flush=True)
        if state["seek_t0"] is not None:
            ms = round((time.perf_counter() - state["seek_t0"]) * 1000, 1)
            if getattr(frame, "preview", False):
                print("PREVIEW", ms, flush=True)
            else:
                print("SEEKED", ms, flush=True)
                state["seek_t0"] = None
    player.presenter.present = present
    player.play()

    def commands():
        for line in sys.stdin:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "pause":
                player.pause()
            elif parts[0] == "play":
                player.play()
            elif parts[0] == "seek":
                state["seek_t0"] = time.perf_counter()
                player.seek(player.source.start + float(parts[1]))
            elif parts[0] == "stats":
                stats = dict(player.stats)
                if player._audio:
                    stats["audio_underruns"] = player._audio.underruns
                print("STATS " + json.dumps(stats), flush=True)
            elif parts[0] == "quit":
                break
        root.after(0, root.destroy)
    threading.Thread(target=commands, daemon=True).start()
    root.mainloop()
    player.close()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--runner":
        runner(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), quick="--quick" in sys.argv[5:])
    else:
        main()
