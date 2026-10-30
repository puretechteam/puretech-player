# Pure Tech Player - (C) 2025 Pure Tech
# Licensed under the MIT License
"""Direct3D 11 presenter: the decoder sends planar YUV 4:2:0 at (about) the source size and the graphics card does
the colour conversion and the scaling, as ffplay's SDL renderer does. Against GDI that removes the BGRA conversion
and most of the scaling from FFmpeg and moves 2.7x fewer bytes through the pipe (measured on H.264 1080p30 in a
1280x720 window: 22.7% of a core for the BGRA decoder, 12.7% for native YUV).

Plain ctypes COM: a flip-model swap chain on the window, three R8 textures (Y, U, V) filled with UpdateSubresource,
and two small shaders compiled at start-up by d3dcompiler_47 (part of Windows 10 and 11)."""

import ctypes, threading
from ctypes import POINTER, byref, c_float, c_uint, c_void_p, wintypes
from .present import Presenter

DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM = 61, 87
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SWAP_EFFECT_FLIP_DISCARD = 4
D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_SDK_VERSION = 7
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_FILTER_MIN_MAG_MIP_LINEAR = 0x15
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST = 4
SLACK = 1.5     # decode up to 1.5x the window before FFmpeg scales down: scaling there costs more than it saves

SHADERS = b"""
Texture2D ty : register(t0); Texture2D tu : register(t1); Texture2D tv : register(t2);
SamplerState s : register(s0);
struct VO { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
VO vs(uint id : SV_VertexID) {
    VO o; float2 uv = float2((id << 1) & 2, id & 2);
    o.uv = uv; o.pos = float4(uv * float2(2, -2) + float2(-1, 1), 0, 1); return o;
}
float4 ps(VO i) : SV_Target {
    float y = (ty.Sample(s, i.uv).r - 16.0 / 255.0) * (255.0 / 219.0);
    float u = (tu.Sample(s, i.uv).r - 128.0 / 255.0) * (255.0 / 224.0);
    float v = (tv.Sample(s, i.uv).r - 128.0 / 255.0) * (255.0 / 224.0);
    float3 rgb = float3(y + RV * v, y - GU * u - GV * v, y + BU * u);
    return float4(saturate(rgb), 1);
}
"""
# limited-range YUV to RGB: BT.709 for HD, BT.601 for SD (what ffplay does with untagged video)
MATRICES = {"bt709": (1.5748, 0.187324, 0.468124, 1.8556), "bt601": (1.402, 0.344136, 0.714136, 1.772)}

class _Rational(ctypes.Structure):
    _fields_ = [("Numerator", c_uint), ("Denominator", c_uint)]

class _ModeDesc(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("RefreshRate", _Rational), ("Format", c_uint),
                ("ScanlineOrdering", c_uint), ("Scaling", c_uint)]

class _SampleDesc(ctypes.Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]

class _SwapChainDesc(ctypes.Structure):
    _fields_ = [("BufferDesc", _ModeDesc), ("SampleDesc", _SampleDesc), ("BufferUsage", c_uint),
                ("BufferCount", c_uint), ("OutputWindow", wintypes.HWND), ("Windowed", wintypes.BOOL),
                ("SwapEffect", c_uint), ("Flags", c_uint)]

class _Texture2DDesc(ctypes.Structure):
    _fields_ = [("Width", c_uint), ("Height", c_uint), ("MipLevels", c_uint), ("ArraySize", c_uint),
                ("Format", c_uint), ("SampleDesc", _SampleDesc), ("Usage", c_uint), ("BindFlags", c_uint),
                ("CPUAccessFlags", c_uint), ("MiscFlags", c_uint)]

class _SamplerDesc(ctypes.Structure):
    _fields_ = [("Filter", c_uint), ("AddressU", c_uint), ("AddressV", c_uint), ("AddressW", c_uint),
                ("MipLODBias", c_float), ("MaxAnisotropy", c_uint), ("ComparisonFunc", c_uint),
                ("BorderColor", c_float * 4), ("MinLOD", c_float), ("MaxLOD", c_float)]

class _Viewport(ctypes.Structure):
    _fields_ = [("TopLeftX", c_float), ("TopLeftY", c_float), ("Width", c_float), ("Height", c_float),
                ("MinDepth", c_float), ("MaxDepth", c_float)]

def _com(obj, index, *argtypes, restype=ctypes.c_long):
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
    return lambda *args: fn(obj, *args)

def _hr(value, what):
    if value < 0:
        raise OSError(f"{what} failed: HRESULT 0x{value & 0xFFFFFFFF:08X}")

def _release(obj):
    if obj is not None and obj.value:
        _com(obj, 2, restype=wintypes.ULONG)()
        obj.value = None

def _compile(source, entry, target, defines):
    d3dc = ctypes.WinDLL("d3dcompiler_47")
    code, errors = c_void_p(), c_void_p()
    macros = (ctypes.c_char_p * (len(defines) * 2 + 2))()
    for i, (k, v) in enumerate(defines.items()):
        macros[2 * i], macros[2 * i + 1] = k.encode(), str(v).encode()
    hr = d3dc.D3DCompile(source, len(source), None, macros, None, entry.encode(), target.encode(), 0, 0,
                         byref(code), byref(errors))
    if hr < 0:
        msg = ""
        if errors.value:
            msg = ctypes.string_at(_com(errors, 3, restype=c_void_p)(), _com(errors, 4, restype=ctypes.c_size_t)())
            _release(errors)
        raise OSError(f"shader compile failed: {msg!r}")
    return code

IID_IDXGIDevice = bytes.fromhex("fa77ec547713e6448c3288fd5f44c84c")
IID_IDXGIFactory = bytes.fromhex("ec66717bc721ae44b21ac9ae321ae369")

class _Device:
    """The process's Direct3D 11 device, shaders and sampler, shared by every presenter (grid tiles included).
    Its immediate context is not thread-safe, so every presenter draws under `lock`."""
    def __init__(self):
        self.lock = threading.RLock()
        self.dev, self.ctx, self.factory = c_void_p(), c_void_p(), c_void_p()
        self.vs, self.ps, self.sampler = c_void_p(), {}, c_void_p()
        d3d = ctypes.WinDLL("d3d11")
        level = c_uint()
        _hr(d3d.D3D11CreateDevice(None, D3D_DRIVER_TYPE_HARDWARE, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0,
                                  D3D11_SDK_VERSION, byref(self.dev), byref(level), byref(self.ctx)), "D3D11 device")
        dxgi_dev, adapter = c_void_p(), c_void_p()
        try:
            _hr(_com(self.dev, 0, c_void_p, POINTER(c_void_p))((ctypes.c_ubyte * 16)(*IID_IDXGIDevice), byref(dxgi_dev)),
                "DXGI device")
            _hr(_com(dxgi_dev, 7, POINTER(c_void_p))(byref(adapter)), "adapter")
            _hr(_com(adapter, 6, c_void_p, POINTER(c_void_p))((ctypes.c_ubyte * 16)(*IID_IDXGIFactory),
                                                               byref(self.factory)), "DXGI factory")
        finally:
            _release(adapter)
            _release(dxgi_dev)
        blob = _compile(SHADERS, "vs", "vs_4_0", {"RV": 0, "GU": 0, "GV": 0, "BU": 0})
        try:
            _hr(_com(self.dev, 12, c_void_p, ctypes.c_size_t, c_void_p, POINTER(c_void_p))(
                _com(blob, 3, restype=c_void_p)(), _com(blob, 4, restype=ctypes.c_size_t)(), None, byref(self.vs)),
                "vertex shader")
        finally:
            _release(blob)
        for name, (rv, gu, gv, bu) in MATRICES.items():
            blob = _compile(SHADERS, "ps", "ps_4_0", {"RV": rv, "GU": gu, "GV": gv, "BU": bu})
            ps = c_void_p()
            try:
                _hr(_com(self.dev, 15, c_void_p, ctypes.c_size_t, c_void_p, POINTER(c_void_p))(
                    _com(blob, 3, restype=c_void_p)(), _com(blob, 4, restype=ctypes.c_size_t)(), None, byref(ps)),
                    "pixel shader")
            finally:
                _release(blob)
            self.ps[name] = ps
        sd = _SamplerDesc(Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR, AddressU=D3D11_TEXTURE_ADDRESS_CLAMP,
                          AddressV=D3D11_TEXTURE_ADDRESS_CLAMP, AddressW=D3D11_TEXTURE_ADDRESS_CLAMP,
                          ComparisonFunc=1, MaxLOD=3.4e38)
        _hr(_com(self.dev, 23, POINTER(_SamplerDesc), POINTER(c_void_p))(byref(sd), byref(self.sampler)), "sampler")
        c = self.ctx
        self.update = _com(c, 48, c_void_p, c_uint, c_void_p, c_void_p, c_uint, c_uint, restype=None)
        self.set_targets = _com(c, 33, c_uint, POINTER(c_void_p), c_void_p, restype=None)
        self.clear_rt = _com(c, 50, c_void_p, POINTER(c_float * 4), restype=None)
        self.viewports = _com(c, 44, c_uint, POINTER(_Viewport), restype=None)
        self.topology = _com(c, 24, c_uint, restype=None)
        self.vs_set = _com(c, 11, c_void_p, c_void_p, c_uint, restype=None)
        self.ps_set = _com(c, 9, c_void_p, c_void_p, c_uint, restype=None)
        self.ps_srv = _com(c, 8, c_uint, c_uint, POINTER(c_void_p), restype=None)
        self.ps_samp = _com(c, 10, c_uint, c_uint, POINTER(c_void_p), restype=None)
        self.draw = _com(c, 13, c_uint, c_uint, restype=None)
        self.samplers = (c_void_p * 1)(self.sampler.value)

_shared = None
_shared_error = None
_shared_ready = threading.Event()
_shared_lock = threading.Lock()

def _make_shared():
    global _shared, _shared_error
    try:
        _shared = _Device()
    except Exception as e:
        _shared_error = str(e) or type(e).__name__
    finally:
        _shared_ready.set()

def warm_up():
    """Start Direct3D 11 in the background now. The first device in a process takes ~200 ms (the graphics driver
    loads) and later ones reuse it, so an app that calls this at start-up never waits for it when a video opens.
    VideoView calls it too. Safe to call any number of times."""
    with _shared_lock:
        if _shared_ready.is_set() or getattr(warm_up, "_started", False):
            return
        warm_up._started = True
    threading.Thread(target=_make_shared, name="d3d11-device", daemon=True).start()

def shared_device(timeout=10):
    warm_up()
    _shared_ready.wait(timeout)
    if _shared is None:
        raise OSError(_shared_error or "Direct3D 11 did not start")
    return _shared

class D3D11Presenter(Presenter):
    """Draws yuv420p frames (decode.Frame with fmt "yuv420p") into a window through Direct3D 11.

    All presenters share one device (see warm_up); each has its own swap chain and textures, made on a helper
    thread while the first frame decodes. Should Direct3D fail, the presenter turns into a GDI one: pixel_format
    becomes "bgra", and the player, which checks it, restarts its decoder for that."""
    pixel_format = "yuv420p"
    slack = SLACK

    def __init__(self, hwnd, wait=False):
        self.hwnd = hwnd
        self.last = None
        self.error = None
        self._gdi = None
        self._closed = False
        self._d = None
        self._lock = threading.RLock()  # replaced by the shared device's lock once it exists
        self._swap, self._rtv = c_void_p(), c_void_p()
        self._tex = []              # [(texture, srv, w, h)] for Y, U, V
        self._buffer_size = (0, 0)
        self._ready = threading.Event()
        threading.Thread(target=self._init, name="d3d11-init", daemon=True).start()
        if wait:
            self._ready.wait()
            if self.error:
                raise OSError(self.error)

    def _init(self):
        try:
            d = shared_device()
            with d.lock:
                self._d, self._lock = d, d.lock
                self._create()
        except Exception as e:
            self.error = str(e) or type(e).__name__
            self._destroy()
            from .present import GdiPresenter
            self._gdi = GdiPresenter(self.hwnd)
            self.pixel_format, self.slack = "bgra", 1.0
        finally:
            self._ready.set()

    # ---- set-up ----------------------------------------------------------------------------------------------
    def _create(self):
        d = self._d
        w, h = self.size()
        desc = _SwapChainDesc()
        desc.BufferDesc.Width, desc.BufferDesc.Height = max(1, w), max(1, h)
        desc.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM
        desc.SampleDesc.Count = 1
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT
        desc.BufferCount = 2
        desc.OutputWindow = self.hwnd
        desc.Windowed = True
        desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD
        _hr(_com(d.factory, 10, c_void_p, POINTER(_SwapChainDesc), POINTER(c_void_p))(d.dev, byref(desc),
                                                                                     byref(self._swap)), "swap chain")
        self._buffer_size = (desc.BufferDesc.Width, desc.BufferDesc.Height)
        self._make_target()
        self._present = _com(self._swap, 8, c_uint, c_uint)
        self._resize_buffers = _com(self._swap, 13, c_uint, c_uint, c_uint, c_uint, c_uint)
        self._black = (c_float * 4)(0, 0, 0, 1)

    def _make_target(self):
        back = c_void_p()
        iid = (ctypes.c_ubyte * 16)(*bytes.fromhex("f2aa156f08d2894e9ab4489535d34f9c"))   # ID3D11Texture2D
        _hr(_com(self._swap, 9, c_uint, c_void_p, POINTER(c_void_p))(0, iid, byref(back)), "back buffer")
        try:
            _hr(_com(self._d.dev, 9, c_void_p, c_void_p, POINTER(c_void_p))(back, None, byref(self._rtv)), "target")
        finally:
            _release(back)

    def _textures(self, w, h):
        """Y, U and V textures for w x h frames (remade when the size changes)."""
        cw, ch = (w + 1) // 2, (h + 1) // 2
        sizes = [(w, h), (cw, ch), (cw, ch)]
        if [(t[2], t[3]) for t in self._tex] == sizes:
            return
        self._free_textures()
        for tw, th in sizes:
            desc = _Texture2DDesc(Width=tw, Height=th, MipLevels=1, ArraySize=1, Format=DXGI_FORMAT_R8_UNORM,
                                  SampleDesc=_SampleDesc(1, 0), Usage=0, BindFlags=D3D11_BIND_SHADER_RESOURCE)
            tex, srv = c_void_p(), c_void_p()
            _hr(_com(self._d.dev, 5, POINTER(_Texture2DDesc), c_void_p, POINTER(c_void_p))(byref(desc), None,
                                                                                          byref(tex)), "texture")
            _hr(_com(self._d.dev, 7, c_void_p, c_void_p, POINTER(c_void_p))(tex, None, byref(srv)), "texture view")
            self._tex.append((tex, srv, tw, th))
        self._srvs = (c_void_p * 3)(*[t[1].value for t in self._tex])

    def _free_textures(self):
        for tex, srv, _, _ in self._tex:
            _release(srv)
            _release(tex)
        self._tex = []

    def _destroy(self):
        """This window's swap chain and textures (the shared device stays for the next presenter)."""
        if self._d is not None and self._rtv.value:
            self._d.set_targets(0, None, None)
        self._free_textures()
        for obj in (self._rtv, self._swap):
            try:
                _release(obj)
            except OSError:
                pass

    # ---- drawing --------------------------------------------------------------------------------------------
    def size(self):
        r = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(wintypes.HWND(self.hwnd), byref(r))
        return r.right - r.left, r.bottom - r.top

    def _fit_buffers(self):
        w, h = self.size()
        if w <= 0 or h <= 0:
            return None
        if (w, h) != self._buffer_size:
            self._d.set_targets(0, None, None)
            _release(self._rtv)
            _hr(self._resize_buffers(0, w, h, 0, 0), "resize")
            self._buffer_size = (w, h)
            self._make_target()
        return w, h

    def _render(self, frame):
        size = self._fit_buffers()
        if size is None:
            return
        cw, ch = size
        rtv = (c_void_p * 1)(self._rtv.value)
        self._d.set_targets(1, rtv, None)
        self._d.clear_rt(self._rtv, byref(self._black))
        if frame is not None:
            scale = min(cw / frame.w, ch / frame.h)
            dw, dh = frame.w * scale, frame.h * scale
            self._d.viewports(1, byref(_Viewport((cw - dw) / 2, (ch - dh) / 2, dw, dh, 0, 1)))
            self._d.topology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
            self._d.vs_set(self._d.vs, None, 0)
            self._d.ps_set(self._d.ps["bt709" if frame.h > 576 else "bt601"], None, 0)
            self._d.ps_srv(0, 3, self._srvs)
            self._d.ps_samp(0, 1, self._d.samplers)
            self._d.draw(3, 0)
        self._present(0, 0)

    def _upload(self, frame):
        self._textures(frame.w, frame.h)
        base = ctypes.addressof((ctypes.c_char * len(frame.buf)).from_buffer(frame.buf))
        offset = 0
        for tex, _, tw, th in self._tex:
            self._d.update(tex, 0, None, base + offset, tw, 0)
            offset += tw * th

    def ready(self, timeout=None):
        return self._ready.wait(timeout)

    def present(self, frame):
        self._ready.wait()
        if self._gdi is not None:
            if frame.fmt == "bgra":
                self._gdi.present(frame)
            else:
                frame.release()          # decoded for Direct3D before it failed; the player is restarting
            return
        with self._lock:
            if self._closed:
                frame.release()
                return
            self._upload(frame)
            self._render(frame)
            if self.last is not None and self.last is not frame:
                self.last.release()
            self.last = frame

    def redraw(self):
        if not self._ready.is_set():
            return
        if self._gdi is not None:
            return self._gdi.redraw()
        with self._lock:
            if not self._closed:
                self._render(self.last if self._tex else None)

    def clear(self):
        if self._gdi is not None:
            return self._gdi.clear()
        with self._lock:
            if self.last is not None:
                self.last.release()
            self.last = None
            if self._ready.is_set() and not self._closed:
                self._render(None)

    def close(self):
        self._ready.wait(5)
        if self._gdi is not None:
            return self._gdi.close()
        with self._lock:
            if self.last is not None:
                self.last.release()
            self.last = None
            self._closed = True
            self._destroy()

def make_presenter(hwnd, prefer="d3d11"):
    """The Direct3D 11 presenter, or GDI where Direct3D is not available (remote desktop without a GPU, old
    drivers)."""
    from .present import GdiPresenter
    if prefer == "d3d11":
        return D3D11Presenter(hwnd)     # becomes GDI by itself if Direct3D 11 cannot start
    return GdiPresenter(hwnd)
