import asyncio
import json
import os
import sys
from typing import Any, Callable, Optional, TypeVar

import decky  # type: ignore

# The loader appends py_modules to sys.path; older Windows builds did not, so make sure.
_PY_MODULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "py_modules")
if _PY_MODULES not in sys.path:
    sys.path.append(_PY_MODULES)

from wqs import asus_acpi, bluetooth, brightness, radios, volume  # noqa: E402

ON_WINDOWS = sys.platform == "win32"
T = TypeVar("T")


class Settings:
    """Tiny JSON store in the plugin's settings folder (the plugin folder itself may be read-only)."""

    def __init__(self, directory: str) -> None:
        self.path = os.path.join(directory, "settings.json")

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set(self, key: str, value: Any) -> None:
        data = self.load()
        data[key] = value
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)


class Plugin:
    def __init__(self) -> None:
        self.settings = Settings(decky.DECKY_PLUGIN_SETTINGS_DIR)
        self._battery: Optional[dict] = None
        # One PowerShell at a time per control, so a dragged slider cannot pile up processes.
        self._brightness_target: Optional[int] = None
        self._brightness_busy = False
        self._radio_lock = asyncio.Lock()
        self._bt_scan_lock = asyncio.Lock()
        self._pairing: Optional[bluetooth.PairingSession] = None

    async def _main(self) -> None:
        decky.logger.info("Windows Quick Settings started")

    async def _unload(self) -> None:
        # Never leave a pairing helper behind.
        if self._pairing is not None:
            await asyncio.to_thread(self._pairing.close)
            self._pairing = None

    async def _guarded(self, name: str, fn: Callable[[], T], default: T) -> T:
        if not ON_WINDOWS:
            return default
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            decky.logger.error(f"{name} failed: {e!r}")
            return default

    # Brightness
    async def get_brightness(self) -> dict:
        value = await self._guarded("get_brightness", brightness.get_brightness, None)
        return {"supported": value is not None, "value": value}

    async def set_brightness(self, value: int) -> bool:
        # Each set is a PowerShell run of a few seconds; while one runs, later values only replace the target,
        # and the running call applies the newest one when it finishes.
        self._brightness_target = int(value)
        if self._brightness_busy:
            return True
        self._brightness_busy = True
        try:
            ok = False
            while self._brightness_target is not None:
                target, self._brightness_target = self._brightness_target, None
                ok = await self._guarded("set_brightness", lambda: brightness.set_brightness(target), False)
            return ok
        finally:
            self._brightness_busy = False

    # Volume
    async def get_volume(self) -> dict:
        state = await self._guarded("get_volume", volume.get_volume, None)
        if state is None:
            return {"supported": False, "level": 0, "muted": False}
        return {"supported": True, **state}

    async def set_volume(self, level: int) -> bool:
        return await self._guarded("set_volume", lambda: volume.set_volume(level) or True, False)

    async def set_mute(self, muted: bool) -> bool:
        return await self._guarded("set_mute", lambda: volume.set_mute(muted) or True, False)

    # Wi-Fi / Bluetooth
    async def get_radios(self) -> dict:
        off = {"present": False, "on": False, "disabled": False}
        async with self._radio_lock:
            return await self._guarded("get_radios", radios.get_radios, {"wifi": off, "bluetooth": off})

    async def set_radio(self, kind: str, on: bool) -> dict:
        async with self._radio_lock:
            return await self._guarded("set_radio", lambda: radios.set_radio(kind, on),
                                       {"ok": False, "detail": "failed"})

    # Bluetooth devices
    async def get_bluetooth_devices(self) -> dict:
        devices = await self._guarded("get_bluetooth_devices", bluetooth.list_paired, None)
        return {"ok": devices is not None, "devices": devices or []}

    async def scan_bluetooth(self) -> dict:
        """Takes about bluetooth.SCAN_SECONDS. A second call while one runs waits for it and scans again."""
        async with self._bt_scan_lock:
            devices = await self._guarded("scan_bluetooth", bluetooth.scan, None)
        return {"ok": devices is not None, "devices": devices or []}

    async def unpair_bluetooth(self, device_id: str) -> dict:
        """Only called from the Remove confirmation in the panel."""
        res = await self._guarded("unpair_bluetooth", lambda: bluetooth.unpair(device_id),
                                  {"ok": False, "status": "failed"})
        decky.logger.info(f"Unpair {device_id}: {res}")
        return res

    def _idle_pairing(self) -> dict:
        return {"active": False, "stage": "idle", "device_id": None, "name": "", "pin": None, "kind": None,
                "ok": None, "message": None, "status": None}

    async def start_pairing(self, device_id: str, name: str = "") -> dict:
        """Starts pairing in the background; the panel then polls get_pairing."""
        if not ON_WINDOWS:
            return {**self._idle_pairing(), "stage": "done", "ok": False, "message": "Not Windows."}
        old, self._pairing = self._pairing, None
        if old is not None:
            await asyncio.to_thread(old.close)
        try:
            session = bluetooth.PairingSession(device_id, name)
        except ValueError:
            return {**self._idle_pairing(), "stage": "done", "ok": False, "message": "Unknown device."}
        self._pairing = session
        decky.logger.info(f"Pairing {device_id} ({name})")
        return await asyncio.to_thread(session.start)

    async def get_pairing(self) -> dict:
        if self._pairing is None:
            return self._idle_pairing()
        return await asyncio.to_thread(self._pairing.snapshot)

    async def respond_pairing(self, accept: bool) -> dict:
        """The user's answer to "Does this PIN match?"."""
        if self._pairing is None:
            return self._idle_pairing()
        return await asyncio.to_thread(self._pairing.respond, bool(accept))

    async def cancel_pairing(self) -> dict:
        if self._pairing is None:
            return self._idle_pairing()
        return await asyncio.to_thread(self._pairing.cancel)

    # ASUS battery charge limit
    async def get_battery(self) -> dict:
        if self._battery is None:
            # Read-only DSTS, once per plugin start.
            self._battery = await self._guarded("battery probe", asus_acpi.probe,
                                                {"supported": False, "status": None, "error": "not Windows",
                                                 "reason": "error"})
            decky.logger.info(f"Battery charge limit probe: {self._battery}")
        return {**self._battery, "limit": self.settings.load().get("battery_limit")}

    async def set_battery_limit(self, limit: int) -> dict:
        """Only called from the user's toggle in the panel."""
        if not (self._battery or {}).get("supported"):
            return {"ok": False, "result": None}
        res = await self._guarded("set_battery_limit", lambda: asus_acpi.set_charge_limit(limit),
                                  {"ok": False, "result": None})
        decky.logger.info(f"Battery charge limit set to {limit}: {res}")
        if res.get("ok"):
            try:
                self.settings.set("battery_limit", int(limit))
            except Exception as e:
                decky.logger.error(f"Could not save the battery charge limit: {e!r}")
        return res
