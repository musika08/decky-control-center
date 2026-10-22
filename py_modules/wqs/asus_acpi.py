"""ASUS battery charge limit through the ATKACPI driver (\\\\.\\ATKACPI), the interface G-Helper uses (AsusACPI.cs).

A method call is DeviceIoControl(IOCTL 0x0022240C) with the buffer
    [method id: u32][argument length: u32][device id: u32][value: u32]   (little endian)
and the first u32 of the 16-byte reply is the result.
  DSTS ("DSTS", 0x53545344): read a device status. Read-only.
  DEVS ("DEVS", 0x53564544): set a device value. A write.
Battery charge limit is device 0x00120057 (ASUS_WMI_DEVID_RSOC in Linux asus-wmi). DSTS reports it present when bit
0x00010000 is set, and 0xFFFFFFFE means the BIOS does not know the method. The current limit cannot be read back
reliably, so callers remember the last value they set.

This file is stdlib-only and runs standalone for the read-only probe (it never calls DEVS):
    python -I asus_acpi.py --probe
"""
import ctypes
import json
import struct
import sys
from typing import Callable, Optional

DEVICE_PATH = r"\\.\ATKACPI"
IOCTL_ATK_ACPI = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544
DEVID_BATTERY_LIMIT = 0x00120057
PRESENCE_BIT = 0x00010000
UNSUPPORTED = 0xFFFFFFFE
MIN_LIMIT, MAX_LIMIT = 20, 100

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ_WRITE = 0x3
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5

# (ioctl code, input bytes) -> output bytes. Tests replace it; the real one is AtkAcpiDevice.ioctl.
Transport = Callable[[int, bytes], bytes]


def build_call(method: int, device: int, value: int = 0) -> bytes:
    args = struct.pack("<II", device, value & 0xFFFFFFFF)
    return struct.pack("<II", method, len(args)) + args


def parse_result(out: bytes) -> int:
    return struct.unpack_from("<I", out, 0)[0]


def dsts(transport: Transport, device: int) -> int:
    return parse_result(transport(IOCTL_ATK_ACPI, build_call(DSTS, device)))


def devs(transport: Transport, device: int, value: int) -> int:
    return parse_result(transport(IOCTL_ATK_ACPI, build_call(DEVS, device, value)))


def is_present(status: int) -> bool:
    return status != UNSUPPORTED and bool(status & PRESENCE_BIT)


class AtkAcpiDevice:
    """Real transport. Opened like G-Helper (read/write access); only the IOCTLs callers choose are sent."""

    def __init__(self) -> None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_uint32, ctypes.c_void_p]
        k.DeviceIoControl.restype = ctypes.c_int
        k.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                      ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k = k
        handle = k.CreateFileW(DEVICE_PATH, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        if handle is None or handle == INVALID_HANDLE:
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise OSError(err, ctypes.FormatError(err).strip())  # type: ignore[attr-defined]
        self._handle = handle

    def ioctl(self, code: int, data: bytes) -> bytes:
        inbuf = ctypes.create_string_buffer(data, len(data))
        outbuf = ctypes.create_string_buffer(16)
        returned = ctypes.c_uint32()
        if not self._k.DeviceIoControl(self._handle, code, inbuf, len(data), outbuf, 16, ctypes.byref(returned), None):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise OSError(err, ctypes.FormatError(err).strip())  # type: ignore[attr-defined]
        return outbuf.raw

    def close(self) -> None:
        if self._handle:
            self._k.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "AtkAcpiDevice":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# () -> context manager with an .ioctl(code, data) method. Tests replace it.
DeviceOpener = Callable[[], AtkAcpiDevice]


def probe(open_device: Optional[DeviceOpener] = None) -> dict:
    """Read-only: DSTS for the charge-limit device. Never raises.

    {"supported": bool, "status": "0x...." | None, "error": str | None, "reason": "ok" | "no_driver" |
     "access_denied" | "not_present" | "error"}
    """
    opener = open_device or AtkAcpiDevice
    try:
        with opener() as dev:
            status = dsts(dev.ioctl, DEVID_BATTERY_LIMIT)
    except OSError as e:
        code = e.errno if isinstance(e.errno, int) else (e.args[0] if e.args and isinstance(e.args[0], int) else None)
        reason = {ERROR_FILE_NOT_FOUND: "no_driver", ERROR_PATH_NOT_FOUND: "no_driver",
                  ERROR_ACCESS_DENIED: "access_denied"}.get(code or -1, "error")
        return {"supported": False, "status": None, "error": str(e), "reason": reason}
    except Exception as e:
        return {"supported": False, "status": None, "error": repr(e), "reason": "error"}
    present = is_present(status)
    return {"supported": present, "status": f"0x{status:08X}", "error": None, "reason": "ok" if present else "not_present"}


def set_charge_limit(limit: int, open_device: Optional[DeviceOpener] = None) -> dict:
    """WRITE: DEVS(0x00120057, limit). Only call on an explicit user action. Returns {"ok": bool, "result": "0x...."}."""
    limit = int(limit)
    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ValueError(f"charge limit must be {MIN_LIMIT}-{MAX_LIMIT}, got {limit}")
    with (open_device or AtkAcpiDevice)() as dev:
        result = devs(dev.ioctl, DEVID_BATTERY_LIMIT, limit)
    # The firmware answers 1 when it accepted the value (G-Helper and Linux asus-wmi both treat 1 as success).
    return {"ok": result == 1, "result": f"0x{result:08X}"}


def main(argv: list[str]) -> int:
    if argv[1:] != ["--probe"]:
        print("usage: asus_acpi.py --probe   (read-only: DSTS for the battery charge limit device)", file=sys.stderr)
        return 2
    print(json.dumps(probe(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
