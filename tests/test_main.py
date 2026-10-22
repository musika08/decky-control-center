import asyncio

import pytest

import main
from wqs import asus_acpi


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ON_WINDOWS", True)
    p = main.Plugin()
    p.settings = main.Settings(str(tmp_path / "settings"))
    return p


def run(coro):
    return asyncio.run(coro)


def test_errors_become_unsupported_state(plugin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(main.brightness, "get_brightness", boom)
    monkeypatch.setattr(main.volume, "get_volume", boom)
    monkeypatch.setattr(main.radios, "get_radios", boom)
    assert run(plugin.get_brightness()) == {"supported": False, "value": None}
    assert run(plugin.get_volume())["supported"] is False
    assert run(plugin.get_radios())["wifi"]["present"] is False


def test_not_windows_does_nothing(plugin, monkeypatch):
    monkeypatch.setattr(main, "ON_WINDOWS", False)
    monkeypatch.setattr(main.volume, "set_volume", lambda *a: pytest.fail("called"))
    assert run(plugin.set_volume(10)) is False


def test_brightness_coalesces_to_latest(plugin, monkeypatch):
    applied = []

    def slow_set(v):
        import time
        time.sleep(0.05)
        applied.append(v)
        return True
    monkeypatch.setattr(main.brightness, "set_brightness", slow_set)

    async def drag():
        return await asyncio.gather(*(plugin.set_brightness(v) for v in (10, 20, 30, 40)))
    assert run(drag()) == [True, True, True, True]
    assert applied[0] == 10 and applied[-1] == 40 and len(applied) == 2


def test_battery_probe_once_and_write_only_when_supported(plugin, monkeypatch):
    probes = []
    monkeypatch.setattr(main.asus_acpi, "probe",
                        lambda: probes.append(1) or {"supported": False, "status": None, "error": "e", "reason": "no_driver"})
    monkeypatch.setattr(main.asus_acpi, "set_charge_limit", lambda limit: pytest.fail("must not write"))
    assert run(plugin.get_battery())["supported"] is False
    run(plugin.get_battery())
    assert probes == [1]
    assert run(plugin.set_battery_limit(80)) == {"ok": False, "result": None}


def test_battery_limit_set_and_remembered(plugin, monkeypatch):
    dev_writes = []
    monkeypatch.setattr(main.asus_acpi, "probe",
                        lambda: {"supported": True, "status": "0x00010000", "error": None, "reason": "ok"})
    monkeypatch.setattr(main.asus_acpi, "set_charge_limit",
                        lambda limit: dev_writes.append(limit) or {"ok": True, "result": "0x00000001"})
    assert run(plugin.get_battery())["limit"] is None
    assert run(plugin.set_battery_limit(80))["ok"] is True
    assert dev_writes == [80]
    assert run(plugin.get_battery())["limit"] == 80


def test_battery_failed_write_not_remembered(plugin, monkeypatch):
    monkeypatch.setattr(main.asus_acpi, "probe",
                        lambda: {"supported": True, "status": "0x00010000", "error": None, "reason": "ok"})
    monkeypatch.setattr(main.asus_acpi, "set_charge_limit", lambda limit: {"ok": False, "result": "0x00000000"})
    run(plugin.get_battery())
    assert run(plugin.set_battery_limit(80))["ok"] is False
    assert run(plugin.get_battery())["limit"] is None


def test_battery_probe_default_shape_matches_real_probe():
    # main.py's fallback must have the same keys as a real probe result.
    real = asus_acpi.probe(lambda: (_ for _ in ()).throw(OSError(2, "x")))
    assert set(real) == {"supported", "status", "error", "reason"}
