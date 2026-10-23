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

from wqs import asus_acpi, bluetooth, brightness, radios, volume, wifi  # noqa: E402

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
        self._scan: Optional[bluetooth.ScanSession] = None
        self._pairing: Optional[bluetooth.PairingSession] = None
        # Decky runs every call as its own task: one lock per helper kind so overlapping calls cannot orphan a helper.
        # Lock order: _pair_lock before _scan_lock, never the other way round.
        self._scan_lock = asyncio.Lock()
        self._pair_lock = asyncio.Lock()
        # Wi-Fi: one connect / disconnect / forget at a time; the search is only re-reading Windows' list.
        self._wifi_lock = asyncio.Lock()
        self._wifi_scan = wifi.ScanState()

    async def _main(self) -> None:
        decky.logger.info("Windows Quick Settings started")

    async def _unload(self) -> None:
        # Never leave a helper behind. Both at once: Decky stops the plugin host after 5 seconds.
        await asyncio.gather(self._unload_pairing(), self._close_scan(bluetooth.UNLOAD_GRACE))

    async def _unload_pairing(self) -> None:
        async with self._pair_lock:
            session, self._pairing = self._pairing, None
            if session is not None:
                await asyncio.to_thread(session.close, bluetooth.UNLOAD_GRACE)

    async def _close_scan(self, grace: float) -> None:
        async with self._scan_lock:
            await self._close_scan_locked(grace)

    async def _close_scan_locked(self, grace: float) -> None:
        """Stop a running search and wait (up to `grace`) until its helper has exited. Its result stays readable.
        The caller holds _scan_lock. Closing a finished search does nothing."""
        if self._scan is not None:
            await asyncio.to_thread(self._scan.close, grace)

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

    @staticmethod
    def _idle_scan() -> dict:
        return {"active": False, "done": False, "devices": []}

    async def start_scan(self) -> dict:
        """Starts a search in the background; the panel then polls get_scan. A search that is still running is kept
        and returned instead of starting a second one. No search while a pairing runs (they share the radio)."""
        if not ON_WINDOWS:
            return {**self._idle_scan(), "done": True}
        async with self._scan_lock:
            # start_pairing sets _pairing while it holds _scan_lock, so this check cannot race with it.
            pairing = self._pairing
            if pairing is not None and (await asyncio.to_thread(pairing.snapshot))["active"]:
                return {**self._idle_scan(), "done": True}
            if self._scan is not None:
                state = await asyncio.to_thread(self._scan.snapshot)
                if state["active"]:
                    return state
            session = bluetooth.ScanSession()
            self._scan = session
            decky.logger.info("Bluetooth search started")
            return await asyncio.to_thread(session.start)

    async def get_scan(self) -> dict:
        session = self._scan
        if session is None:
            return self._idle_scan()
        return await asyncio.to_thread(session.snapshot)

    async def stop_scan(self) -> dict:
        """The Stop button (and the panel before it pairs): returns once the helper has stopped its watchers."""
        await self._close_scan(bluetooth.CLOSE_GRACE)
        return await self.get_scan()

    async def unpair_bluetooth(self, device_id: str) -> dict:
        """Only called from the Remove confirmation in the panel."""
        res = await self._guarded("unpair_bluetooth", lambda: bluetooth.unpair(device_id),
                                  {"ok": False, "status": "failed"})
        decky.logger.info(f"Unpair {device_id!r}: {res}")
        return res

    def _idle_pairing(self) -> dict:
        return {"active": False, "stage": "idle", "device_id": None, "name": "", "pin": None, "kind": None,
                "ok": None, "message": None, "status": None}

    async def start_pairing(self, device_id: str, name: str = "") -> dict:
        """Starts pairing in the background; the panel then polls get_pairing."""
        if not ON_WINDOWS:
            return {**self._idle_pairing(), "stage": "done", "ok": False, "message": "Not Windows."}
        async with self._pair_lock:
            old, self._pairing = self._pairing, None
            if old is not None:
                await asyncio.to_thread(old.close, bluetooth.CLOSE_GRACE)
            try:
                session = bluetooth.PairingSession(device_id, name)
            except ValueError:
                return {**self._idle_pairing(), "stage": "done", "ok": False, "message": "Unknown device."}
            async with self._scan_lock:
                # A running search and a pairing fight for the radio: stop the search first.
                await self._close_scan_locked(bluetooth.CLOSE_GRACE)
                self._pairing = session
                decky.logger.info(f"Pairing {device_id!r} ({name!r})")
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
        async with self._pair_lock:
            if self._pairing is None:
                return self._idle_pairing()
            return await asyncio.to_thread(self._pairing.cancel)

    async def dismiss_pairing(self) -> dict:
        """The OK on a finished pairing: forget it, so it does not show again. A running pairing is kept."""
        async with self._pair_lock:
            session = self._pairing
            if session is None:
                return self._idle_pairing()
            state = await asyncio.to_thread(session.snapshot)
            if state["active"]:
                return state
            self._pairing = None
            return self._idle_pairing()

    # Wi-Fi networks
    @staticmethod
    def _wifi_failed() -> dict:
        return {"ok": False, "present": False, "interface": None, "state": None, "connected": None, "profiles": [],
                "location_off": False}

    async def get_wifi(self) -> dict:
        return await self._guarded("get_wifi", wifi.get_status, self._wifi_failed())

    @staticmethod
    def _wifi_scan_failed() -> dict:
        return {"active": False, "location_off": False, "error": "The request failed.", "networks": []}

    async def start_wifi_scan(self) -> dict:
        """Opens the search; the panel then polls get_wifi_scan every few seconds until stop_wifi_scan."""
        self._wifi_scan.start()
        return await self.get_wifi_scan()

    async def get_wifi_scan(self) -> dict:
        return await self._guarded("get_wifi_scan", self._wifi_scan.refresh, self._wifi_scan_failed())

    async def stop_wifi_scan(self) -> dict:
        self._wifi_scan.stop()
        return self._wifi_scan.snapshot()

    async def connect_wifi(self, ssid: str, password: str = "") -> dict:
        """Only called from the panel's Connect. The password is never logged or returned."""
        async with self._wifi_lock:
            res = await self._guarded("connect_wifi", lambda: wifi.connect(ssid, password),
                                      {"ok": False, "status": "failed", "message": "Could not connect: the request "
                                       "failed.", "ssid": ssid})
        decky.logger.info(f"Wi-Fi connect {ssid!r}: {res.get('status')}")
        return res

    async def disconnect_wifi(self) -> dict:
        async with self._wifi_lock:
            res = await self._guarded("disconnect_wifi", wifi.disconnect,
                                      {"ok": False, "message": "The request failed."})
        decky.logger.info(f"Wi-Fi disconnect: {res.get('ok')}")
        return res

    async def forget_wifi(self, profile: str) -> dict:
        """Only called from the Forget confirmation in the panel."""
        async with self._wifi_lock:
            res = await self._guarded("forget_wifi", lambda: wifi.forget(profile),
                                      {"ok": False, "message": "The request failed."})
        decky.logger.info(f"Wi-Fi forget {profile!r}: {res.get('ok')}")
        return res

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
