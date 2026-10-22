import json
import subprocess

import pytest

from wqs import brightness, radios
from wqs.runner import POWERSHELL, PowerShellError, decode_script, run_powershell


class FakeRunner:
    def __init__(self, *outputs, code=0, err=""):
        self.outputs = list(outputs)
        self.code = code
        self.err = err
        self.scripts = []
        self.timeouts = []

    def __call__(self, argv, timeout):
        assert argv[0] == POWERSHELL
        assert "-NoProfile" in argv and "-NonInteractive" in argv
        self.scripts.append(decode_script(argv))
        self.timeouts.append(timeout)
        return self.code, self.outputs.pop(0) if self.outputs else "", self.err


def test_run_powershell_returns_trimmed_stdout_and_passes_timeout():
    r = FakeRunner("  hello\r\n")
    assert run_powershell("Write-Output hello", r, timeout=3) == "hello"
    assert r.timeouts == [3]
    assert r.scripts[0].endswith("Write-Output hello")
    assert "OutputEncoding" in r.scripts[0]


def test_run_powershell_raises_on_failure_with_stderr():
    with pytest.raises(PowerShellError, match="boom"):
        run_powershell("x", FakeRunner("", code=1, err="boom\n"))


def test_run_powershell_timeout_is_powershell_error():
    def runner(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    with pytest.raises(PowerShellError, match="timed out"):
        run_powershell("x", runner, timeout=1)


# brightness
def test_get_brightness_parses_value():
    r = FakeRunner("75")
    assert brightness.get_brightness(r) == 75
    assert "WmiMonitorBrightness" in r.scripts[0] and "root/wmi" in r.scripts[0]
    assert "WmiSetBrightness" not in r.scripts[0]


def test_get_brightness_none_when_no_wmi_display():
    assert brightness.get_brightness(FakeRunner("none")) is None


@pytest.mark.parametrize("value,expected", [(42, 42), (-5, 0), (250, 100), (55.7, 55)])
def test_set_brightness_clamps_and_calls_wmisetbrightness(value, expected):
    r = FakeRunner("ok")
    assert brightness.set_brightness(value, r) is True
    script = r.scripts[0]
    assert "WmiMonitorBrightnessMethods" in script and "WmiSetBrightness" in script
    assert f"Brightness = [byte]{expected} " in script


def test_set_brightness_false_without_display():
    assert brightness.set_brightness(50, FakeRunner("none")) is False


# radios
def test_get_radios_maps_winrt_kinds():
    out = json.dumps([
        {"kind": "WiFi", "state": "On", "name": "Wi-Fi"},
        {"kind": "Bluetooth", "state": "Off", "name": "Bluetooth"},
        {"kind": "MobileBroadband", "state": "On", "name": "Cellular"},
    ])
    r = FakeRunner(out)
    assert radios.get_radios(r) == {
        "wifi": {"present": True, "on": True, "disabled": False},
        "bluetooth": {"present": True, "on": False, "disabled": False},
    }
    assert "GetRadiosAsync" in r.scripts[0]
    assert "SetStateAsync" not in r.scripts[0]


def test_get_radios_single_object_and_missing_kind():
    out = json.dumps({"kind": "Bluetooth", "state": "Disabled", "name": "BT"})
    assert radios.get_radios(FakeRunner(out)) == {
        "wifi": {"present": False, "on": False},
        "bluetooth": {"present": True, "on": False, "disabled": True},
    }


def test_get_radios_empty():
    assert radios.get_radios(FakeRunner("[]"))["wifi"] == {"present": False, "on": False}


@pytest.mark.parametrize("kind,on,winrt,state", [("wifi", True, "WiFi", "On"), ("bluetooth", False, "Bluetooth", "Off")])
def test_set_radio_script_targets_kind_and_state(kind, on, winrt, state):
    r = FakeRunner("result:Allowed")
    assert radios.set_radio(kind, on, r) == {"ok": True, "detail": "Allowed"}
    script = r.scripts[0]
    assert f"$_.Kind.ToString() -eq '{winrt}'" in script
    assert f"[Windows.Devices.Radios.RadioState]::{state}" in script
    assert "RequestAccessAsync" in script


def test_set_radio_reports_denied_and_access():
    assert radios.set_radio("wifi", True, FakeRunner("result:Allowed,DeniedBySystem")) == \
        {"ok": False, "detail": "Allowed,DeniedBySystem"}
    assert radios.set_radio("wifi", True, FakeRunner("access:DeniedByUser")) == {"ok": False, "detail": "access:DeniedByUser"}
    assert radios.set_radio("bluetooth", True, FakeRunner("none")) == {"ok": False, "detail": "none"}


def test_set_radio_rejects_unknown_kind_without_running():
    r = FakeRunner()
    with pytest.raises(ValueError):
        radios.set_radio("airplane'; Remove-Item x", True, r)
    assert r.scripts == []
