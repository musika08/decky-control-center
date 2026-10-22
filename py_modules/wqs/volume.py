"""Master volume and mute of the default playback device through Core Audio (IAudioEndpointVolume), using ctypes.

No comtypes/pywin32 needed: the three COM interfaces are called through their vtables. No admin needed.
Every call initialises COM on the calling thread and releases everything it got, so it is safe from a worker thread.
"""
import ctypes
import uuid
from typing import Any, Callable, Optional, Protocol

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
E_RENDER = 0
E_CONSOLE = 0

# vtable slots (IUnknown takes 0-2)
_RELEASE = 2
_ENUM_GET_DEFAULT_AUDIO_ENDPOINT = 4
_DEVICE_ACTIVATE = 3
_VOL_SET_MASTER_SCALAR = 7
_VOL_GET_MASTER_SCALAR = 9
_VOL_SET_MUTE = 14
_VOL_GET_MUTE = 15


class Endpoint(Protocol):
    def get_level(self) -> float: ...
    def set_level(self, level: float) -> None: ...
    def get_mute(self) -> bool: ...
    def set_mute(self, muted: bool) -> None: ...


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _method(obj: ctypes.c_void_p, index: int, *argtypes: Any) -> Callable[..., Any]:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    fn = proto(vtable[index])
    return lambda *args: fn(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj.value:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(obj)  # type: ignore[attr-defined]


class CoreAudioEndpoint:
    """Opens the default render endpoint for one operation. Use as a context manager."""

    def __enter__(self) -> "CoreAudioEndpoint":
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        ole32.CoInitializeEx.restype = ctypes.c_long
        hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        # S_OK/S_FALSE: ours to undo. RPC_E_CHANGED_MODE: the thread already has COM, which is fine to use.
        self._uninit = hr >= 0
        self._enum = ctypes.c_void_p()
        self._device = ctypes.c_void_p()
        self._vol = ctypes.c_void_p()
        try:
            clsid, iid_enum, iid_vol = _guid(CLSID_MMDeviceEnumerator), _guid(IID_IMMDeviceEnumerator), _guid(IID_IAudioEndpointVolume)
            ole32.CoCreateInstance.restype = ctypes.HRESULT  # type: ignore[attr-defined]
            ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid_enum), ctypes.byref(self._enum))
            _method(self._enum, _ENUM_GET_DEFAULT_AUDIO_ENDPOINT, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)(
                E_RENDER, E_CONSOLE, ctypes.byref(self._device))
            _method(self._device, _DEVICE_ACTIVATE, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.byref(iid_vol), CLSCTX_ALL, None, ctypes.byref(self._vol))
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        for obj in (self._vol, self._device, self._enum):
            _release(obj)
        if self._uninit:
            ctypes.windll.ole32.CoUninitialize()  # type: ignore[attr-defined]

    def get_level(self) -> float:
        level = ctypes.c_float()
        _method(self._vol, _VOL_GET_MASTER_SCALAR, ctypes.c_void_p)(ctypes.byref(level))
        return level.value

    def set_level(self, level: float) -> None:
        _method(self._vol, _VOL_SET_MASTER_SCALAR, ctypes.c_float, ctypes.c_void_p)(level, None)

    def get_mute(self) -> bool:
        muted = ctypes.c_int()
        _method(self._vol, _VOL_GET_MUTE, ctypes.c_void_p)(ctypes.byref(muted))
        return bool(muted.value)

    def set_mute(self, muted: bool) -> None:
        _method(self._vol, _VOL_SET_MUTE, ctypes.c_int, ctypes.c_void_p)(1 if muted else 0, None)


# () -> context manager yielding an Endpoint. Tests replace it.
EndpointFactory = Callable[[], Any]


def get_volume(open_endpoint: Optional[EndpointFactory] = None) -> dict:
    """{"level": 0-100, "muted": bool} for the default playback device."""
    with (open_endpoint or CoreAudioEndpoint)() as ep:
        return {"level": round(ep.get_level() * 100), "muted": ep.get_mute()}


def set_volume(level: int, open_endpoint: Optional[EndpointFactory] = None) -> None:
    value = max(0, min(100, int(level)))
    with (open_endpoint or CoreAudioEndpoint)() as ep:
        ep.set_level(value / 100.0)


def set_mute(muted: bool, open_endpoint: Optional[EndpointFactory] = None) -> None:
    with (open_endpoint or CoreAudioEndpoint)() as ep:
        ep.set_mute(bool(muted))
