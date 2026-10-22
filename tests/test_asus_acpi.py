import struct

import pytest

from wqs import asus_acpi


class FakeDevice:
    """Records every IOCTL; answers DSTS with `status` and DEVS with `devs_result`."""

    def __init__(self, status=0x00010050, devs_result=1, open_error=None):
        self.status, self.devs_result, self.open_error = status, devs_result, open_error
        self.calls = []
        self.closed = False

    def __call__(self):  # the opener
        if self.open_error:
            raise self.open_error
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True

    def ioctl(self, code, data):
        self.calls.append((code, data))
        method = struct.unpack_from("<I", data)[0]
        value = self.status if method == asus_acpi.DSTS else self.devs_result
        return struct.pack("<I", value) + bytes(12)


def test_buffer_layout_matches_g_helper():
    assert asus_acpi.build_call(asus_acpi.DSTS, 0x00120057) == bytes.fromhex("44535453" "08000000" "57001200" "00000000")
    assert asus_acpi.build_call(asus_acpi.DEVS, 0x00120057, 80) == bytes.fromhex("44455653" "08000000" "57001200" "50000000")
    assert asus_acpi.IOCTL_ATK_ACPI == 0x0022240C


def test_probe_is_read_only_and_reports_present():
    dev = FakeDevice(status=0x00010064)
    res = asus_acpi.probe(dev)
    assert res == {"supported": True, "status": "0x00010064", "error": None, "reason": "ok"}
    assert len(dev.calls) == 1
    code, data = dev.calls[0]
    assert code == asus_acpi.IOCTL_ATK_ACPI
    assert struct.unpack_from("<I", data)[0] == asus_acpi.DSTS
    assert dev.closed


@pytest.mark.parametrize("status", [0x00000000, 0x00000050, asus_acpi.UNSUPPORTED])
def test_probe_not_present(status):
    res = asus_acpi.probe(FakeDevice(status=status))
    assert res["supported"] is False and res["reason"] == "not_present"


@pytest.mark.parametrize("err,reason", [(2, "no_driver"), (3, "no_driver"), (5, "access_denied"), (31, "error")])
def test_probe_open_errors(err, reason):
    res = asus_acpi.probe(FakeDevice(open_error=OSError(err, "msg")))
    assert res["supported"] is False and res["reason"] == reason and res["status"] is None


def test_probe_never_raises():
    class Broken(FakeDevice):
        def ioctl(self, code, data):
            raise RuntimeError("bad")
    assert asus_acpi.probe(Broken())["reason"] == "error"


def test_set_charge_limit_sends_devs():
    dev = FakeDevice(devs_result=1)
    assert asus_acpi.set_charge_limit(80, dev) == {"ok": True, "result": "0x00000001"}
    (code, data), = dev.calls
    assert data == asus_acpi.build_call(asus_acpi.DEVS, asus_acpi.DEVID_BATTERY_LIMIT, 80)


def test_set_charge_limit_reports_firmware_refusal():
    assert asus_acpi.set_charge_limit(100, FakeDevice(devs_result=0))["ok"] is False


@pytest.mark.parametrize("bad", [0, 19, 101, -80])
def test_set_charge_limit_validates_before_opening(bad):
    dev = FakeDevice(open_error=AssertionError("must not open"))
    with pytest.raises(ValueError):
        asus_acpi.set_charge_limit(bad, dev)


def test_cli_requires_probe_flag(capsys):
    assert asus_acpi.main(["asus_acpi.py"]) == 2
    assert asus_acpi.main(["asus_acpi.py", "--set", "80"]) == 2
