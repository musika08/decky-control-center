import asyncio
import json
import queue
import threading
import time

import pytest

import main
from wqs import bluetooth
from wqs.runner import POWERSHELL, PowerShellError, decode_script

SPEAKER = "Bluetooth#Bluetooth48:45:e6:0b:c7:0e-2c:8d:48:2f:a2:a9"
MOUSE = "BluetoothLE#BluetoothLE48:45:e6:0b:c7:0e-f0:b1:6f:50:a4:1c"
NEW = "Bluetooth#Bluetooth48:45:e6:0b:c7:0e-00:11:22:33:44:55"


class FakeRunner:
    def __init__(self, *outputs, code=0, err=""):
        self.outputs = list(outputs)
        self.code, self.err = code, err
        self.scripts, self.timeouts = [], []

    def __call__(self, argv, timeout):
        assert argv[0] == POWERSHELL
        self.scripts.append(decode_script(argv))
        self.timeouts.append(timeout)
        return self.code, self.outputs.pop(0) if self.outputs else "", self.err


# Paired list ------------------------------------------------------------------------------------------------------

def test_list_paired_parses_and_sorts():
    out = json.dumps([
        {"id": SPEAKER, "name": "soundcore Boom Go 3i", "paired": True, "connected": False,
         "category": "Audio.Speakers", "major": 4, "le": False},
        {"id": MOUSE, "name": "BT5.0 Mouse", "paired": True, "connected": True, "category": "Input.Mouse",
         "major": None, "le": True},
    ])
    r = FakeRunner(out)
    assert bluetooth.list_paired(r) == [
        {"id": MOUSE, "name": "BT5.0 Mouse", "connected": True, "type": "input", "le": True},
        {"id": SPEAKER, "name": "soundcore Boom Go 3i", "connected": False, "type": "audio", "le": False},
    ]
    script = r.scripts[0]
    assert "GetDeviceSelectorFromPairingState($true)" in script
    assert "System.Devices.Aep.IsConnected" in script
    for forbidden in ("PairAsync", "UnpairAsync", "CreateWatcher", "SetStateAsync"):
        assert forbidden not in script


def test_list_paired_single_object_empty_and_unnamed():
    one = json.dumps({"id": SPEAKER, "name": "  ", "connected": True, "category": "", "major": 5, "le": False})
    assert bluetooth.list_paired(FakeRunner(one)) == [
        {"id": SPEAKER, "name": "Unnamed device", "connected": True, "type": "input", "le": False}]
    assert bluetooth.list_paired(FakeRunner("[]")) == []
    assert bluetooth.list_paired(FakeRunner("")) == []
    assert bluetooth.list_paired(FakeRunner(json.dumps([{"name": "no id"}]))) == []


@pytest.mark.parametrize("category,major,expected", [
    ("Audio.Headphone", None, "audio"), ("Input.Gaming", None, "input"), ("Phone", None, "phone"),
    ("Communication.Phone", None, "phone"), ("Computer.Laptop", None, "computer"), ("", 4, "audio"),
    ("", 2, "phone"), ("", 31, "other"), ("Other", "x", "other"), ("", None, "other"),
    ("Audio.Speakers,Input.Keyboard", None, "audio"),
])
def test_device_type(category, major, expected):
    assert bluetooth.device_type(category, major) == expected


def test_list_paired_error_raises():
    with pytest.raises(PowerShellError, match="boom"):
        bluetooth.list_paired(FakeRunner("", code=1, err="boom"))


# Search -----------------------------------------------------------------------------------------------------------

def test_scan_filters_nameless_and_paired_dedups_and_sorts():
    out = json.dumps([
        {"id": NEW, "name": "Zeta Headset", "paired": False, "connected": False, "category": "Audio.Headset",
         "major": 4, "le": False},
        {"id": "BluetoothLE#BluetoothLE48:45:e6:0b:c7:0e-aa:bb:cc:dd:ee:ff", "name": "", "paired": False,
         "category": "", "major": None, "le": True},
        {"id": "BluetoothLE#BluetoothLE48:45:e6:0b:c7:0e-aa:bb:cc:dd:ee:01", "name": "alpha pad", "paired": False,
         "category": "Input.Gaming", "major": None, "le": True},
    ])
    r = FakeRunner(out)
    found = bluetooth.scan(12, r)
    assert [d["name"] for d in found] == ["alpha pad", "Zeta Headset"]
    assert found[1]["type"] == "audio" and found[0]["le"] is True
    script = r.scripts[0]
    assert "GetDeviceSelectorFromPairingState($false)" in script
    assert "AddSeconds(12)" in script
    assert "$w.Stop()" in script
    assert "PairAsync" not in script and "UnpairAsync" not in script
    assert r.timeouts == [37]


def test_scan_clamps_seconds_and_handles_empty():
    r = FakeRunner("[]")
    assert bluetooth.scan(999, r) == []
    assert "AddSeconds(60)" in r.scripts[0]
    assert bluetooth.scan(0, FakeRunner("")) == []


# Unpair -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("out,ok", [("result:Unpaired", True), ("result:AlreadyUnpaired", True),
                                    ("result:NotPaired", True), ("result:AccessDenied", False),
                                    ("result:Failed", False), ("", False)])
def test_unpair_results(out, ok):
    r = FakeRunner(out)
    res = bluetooth.unpair(SPEAKER, r)
    assert res["ok"] is ok
    assert f"'{SPEAKER}'" in r.scripts[0] and "UnpairAsync" in r.scripts[0]


@pytest.mark.parametrize("bad", ["", "x", SPEAKER + "'; Remove-Item C:\\", "Bluetooth#Bluetooth' + 'x", None, 5,
                                 "USB#VID_1234", SPEAKER + "\n", " " + SPEAKER])
def test_bad_ids_rejected_without_running(bad):
    r = FakeRunner()
    with pytest.raises(ValueError):
        bluetooth.unpair(bad, r)
    with pytest.raises(ValueError):
        bluetooth.PairingSession(bad, spawn=lambda argv: pytest.fail("spawned"))
    assert r.scripts == []


# Pairing session --------------------------------------------------------------------------------------------------

class FakeStdin:
    def __init__(self, proc):
        self.proc, self.lines, self.closed = proc, [], False

    def write(self, text):
        if self.proc.killed:
            raise OSError("pipe closed")
        self.lines.append(text)
        if self.proc.on_command:
            self.proc.on_command(text.strip())

    def flush(self):
        pass


class FakeProc:
    """Stands in for the PowerShell helper: tests push stdout lines; None ends the stream."""

    def __init__(self, on_command=None, wait_hangs=False):
        self.out = queue.Queue()
        self.stdin = FakeStdin(self)
        self.killed = False
        self.exited = threading.Event()
        self.on_command = on_command
        self.wait_hangs = wait_hangs
        self.argv = None
        self.ended = False
        self.wait_timeouts = []

    @property
    def stdout(self):
        while True:
            line = self.out.get()
            if line is None:
                self.exited.set()
                return
            yield line

    def emit(self, **event):
        self.out.put(json.dumps(event) + "\n")

    def end(self):
        self.ended = True
        self.out.put(None)

    def poll(self):
        if self.wait_hangs and not self.killed:
            return None
        return 0 if self.ended or self.exited.is_set() or self.killed else None

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.wait_hangs and not self.killed:
            raise TimeoutError()
        return 0

    def kill(self):
        self.killed = True
        self.out.put(None)


def spawner(proc):
    def spawn(argv):
        proc.argv = argv
        return proc
    return spawn


def wait_for(session, pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        s = session.snapshot()
        if pred(s):
            return s
        time.sleep(0.01)
    raise AssertionError(f"state never matched: {session.snapshot()}")


def test_pair_script_has_id_timeouts_and_all_kinds():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, "Headset", spawn=spawner(proc), timeout=90, pin_timeout=45)
    assert s.start()["stage"] == "starting"
    script = decode_script(proc.argv)
    assert f"CreateFromIdAsync('{NEW}'" in script
    assert "AddSeconds(90)" in script and "AddSeconds(45)" in script
    assert "'ConfirmOnly, DisplayPin, ProvidePin, ConfirmPinMatch'" in script
    assert "GetDeferral" in script and "remove_PairingRequested" in script
    # A cancelled task must not throw on the wait after $op.Cancel().
    assert "Task]::WaitAny(" in script and ".Wait(5000)" not in script
    proc.end()


def test_confirm_only_auto_accepted_then_paired():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, "Headset", spawn=spawner(proc))
    s.start()
    proc.emit(event="pairing")
    wait_for(s, lambda st: st["stage"] == "pairing")
    proc.emit(event="accepted", kind="ConfirmOnly")
    proc.emit(event="result", status="Paired", protection="Encryption")
    proc.end()
    st = wait_for(s, lambda st: not st["active"])
    assert st["ok"] is True and st["status"] == "Paired" and st["message"] == "Paired."
    assert proc.stdin.lines == []  # nothing needed from the user


def test_confirm_pin_match_accept():
    proc = FakeProc()
    proc.on_command = lambda c: c == "accept" and (proc.emit(event="accepted"),
                                                    proc.emit(event="result", status="Paired"), proc.end())
    s = bluetooth.PairingSession(NEW, "Phone", spawn=spawner(proc))
    s.start()
    proc.emit(event="pairing")
    proc.emit(event="pin", kind="ConfirmPinMatch", pin="123456")
    st = wait_for(s, lambda st: st["stage"] == "confirm")
    assert st["pin"] == "123456" and st["active"] is True and st["name"] == "Phone"
    after = s.respond(True)
    assert after["stage"] in ("pairing", "done") and after["pin"] is None
    st = wait_for(s, lambda st: not st["active"])
    assert st["ok"] is True
    assert proc.stdin.lines == ["accept\n"]


def test_confirm_pin_match_reject_reports_cancel():
    proc = FakeProc()
    proc.on_command = lambda c: c == "reject" and (proc.emit(event="rejected", reason="user"),
                                                    proc.emit(event="result", status="RejectedByHandler"), proc.end())
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="pin", kind="ConfirmPinMatch", pin="000111")
    wait_for(s, lambda st: st["stage"] == "confirm")
    s.respond(False)
    st = wait_for(s, lambda st: not st["active"])
    assert st["ok"] is False and st["message"] == "Pairing was cancelled."
    assert proc.stdin.lines == ["reject\n"]


def test_respond_ignored_when_not_waiting():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="pairing")
    wait_for(s, lambda st: st["stage"] == "pairing")
    s.respond(True)
    assert proc.stdin.lines == []
    proc.end()


def test_display_pin_shown_and_cancel():
    proc = FakeProc()
    proc.on_command = lambda c: c == "cancel" and (proc.emit(event="cancelled", reason="user"), proc.end())
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="pin", kind="DisplayPin", pin="4321")
    st = wait_for(s, lambda st: st["stage"] == "show_pin")
    assert st["pin"] == "4321" and st["kind"] == "DisplayPin"
    s.cancel()
    st = wait_for(s, lambda st: not st["active"])
    assert st["ok"] is False and st["message"] == "Pairing was cancelled." and st["pin"] is None
    assert proc.stdin.lines == ["cancel\n"]


def test_unsupported_provide_pin_gives_clear_message():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="unsupported", kind="ProvidePin")
    proc.emit(event="result", status="RejectedByHandler")
    proc.end()
    st = wait_for(s, lambda st: not st["active"])
    assert st["ok"] is False and "PIN typed in" in st["message"]


def test_unsupported_other_kind():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="unsupported", kind="ProvidePasswordCredential")
    proc.end()  # helper ended without a result
    st = wait_for(s, lambda st: not st["active"])
    assert "ProvidePasswordCredential" in st["message"]


def test_helper_timeout_events():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="pin", kind="ConfirmPinMatch", pin="1")
    proc.emit(event="rejected", reason="timeout")
    proc.emit(event="result", status="AuthenticationFailure")
    proc.end()
    st = wait_for(s, lambda st: not st["active"])
    assert st["message"] == "No answer in time, so pairing was cancelled."

    proc2 = FakeProc()
    s2 = bluetooth.PairingSession(NEW, spawn=spawner(proc2))
    s2.start()
    proc2.emit(event="cancelled", reason="timeout")
    proc2.end()
    assert wait_for(s2, lambda st: not st["active"])["message"] == "Pairing took too long and was cancelled."


def test_result_failure_messages_and_error_event():
    for status, text in (("AuthenticationTimeout", "did not answer"), ("Weird", "Pairing failed (Weird).")):
        proc = FakeProc()
        s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
        s.start()
        proc.emit(event="result", status=status)
        proc.end()
        st = wait_for(s, lambda st: not st["active"])
        assert st["ok"] is False and text in st["message"]
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.out.put("garbage line\n")
    proc.out.put("{not json\n")
    proc.emit(event="error", message="Element not found.")
    proc.end()
    st = wait_for(s, lambda st: not st["active"])
    assert st["message"] == "Pairing failed: Element not found."


def test_already_paired_is_ok():
    proc = FakeProc()
    s = bluetooth.PairingSession(SPEAKER, spawn=spawner(proc))
    s.start()
    proc.emit(event="result", status="AlreadyPaired")
    proc.end()
    assert wait_for(s, lambda st: not st["active"])["ok"] is True


def test_helper_dies_unexpectedly():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.end()
    st = wait_for(s, lambda st: not st["active"])
    assert st["ok"] is False and "unexpectedly" in st["message"]


def test_spawn_failure():
    def spawn(argv):
        raise OSError("no powershell")
    st = bluetooth.PairingSession(NEW, spawn=spawn).start()
    assert st["active"] is False and "no powershell" in st["message"]


def test_python_deadline_kills_stuck_helper():
    now = [1000.0]
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc), clock=lambda: now[0], timeout=30)
    s.start()
    proc.emit(event="pairing")
    wait_for(s, lambda st: st["stage"] == "pairing")
    now[0] += 30 + bluetooth.KILL_GRACE + 1
    st = s.snapshot()
    assert proc.killed is True
    assert st["active"] is False and st["status"] == "Timeout"


def test_reader_kills_helper_that_does_not_exit():
    proc = FakeProc(wait_hangs=True)
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="result", status="Paired")
    proc.end()
    wait_for(s, lambda st: not st["active"])
    end = time.time() + 2
    while not proc.killed and time.time() < end:
        time.sleep(0.01)
    assert proc.killed is True


def test_close_lets_helper_cancel_cleanly():
    proc = FakeProc()
    proc.on_command = lambda c: c == "cancel" and (proc.emit(event="cancelled", reason="user"), proc.end())
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="pairing")
    wait_for(s, lambda st: st["stage"] == "pairing")
    s.close(grace=6)
    assert proc.stdin.lines == ["cancel\n"]
    assert proc.killed is False and 6 in proc.wait_timeouts
    assert wait_for(s, lambda st: not st["active"])["message"] == "Pairing was cancelled."


def test_close_kills_helper_that_ignores_cancel():
    proc = FakeProc(wait_hangs=True)
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    s.close(grace=3)
    assert proc.stdin.lines == ["cancel\n"] and 3 in proc.wait_timeouts and proc.killed is True
    assert s.snapshot()["active"] is False


def test_backstop_timer_kills_without_polling():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc), backstop=0.05)
    s.start()
    proc.emit(event="pairing")
    end = time.time() + 2
    while not proc.killed and time.time() < end:
        time.sleep(0.01)  # no snapshot() calls here
    assert proc.killed is True
    st = s.snapshot()
    assert st["active"] is False and st["status"] == "Timeout"


def test_backstop_timer_cancelled_when_helper_ends():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc), backstop=0.2)
    s.start()
    proc.emit(event="result", status="Paired")
    proc.end()
    wait_for(s, lambda st: not st["active"])
    time.sleep(0.3)
    assert proc.killed is False and s.snapshot()["ok"] is True


def test_close_cancels_and_kills():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="pin", kind="ConfirmPinMatch", pin="9")
    wait_for(s, lambda st: st["stage"] == "confirm")
    s.close()
    assert proc.stdin.lines == ["cancel\n"] and proc.killed
    st = s.snapshot()
    assert st["active"] is False and st["message"] == "Pairing was cancelled."


def test_events_after_done_are_ignored():
    proc = FakeProc()
    s = bluetooth.PairingSession(NEW, spawn=spawner(proc))
    s.start()
    proc.emit(event="cancelled", reason="user")
    proc.emit(event="pin", kind="ConfirmPinMatch", pin="1")
    proc.emit(event="result", status="Paired")
    proc.end()
    st = wait_for(s, lambda st: not st["active"])
    time.sleep(0.05)
    st = s.snapshot()
    assert st["ok"] is False and st["pin"] is None and st["stage"] == "done"


# main.py wiring ---------------------------------------------------------------------------------------------------

@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ON_WINDOWS", True)
    p = main.Plugin()
    p.settings = main.Settings(str(tmp_path / "settings"))
    return p


def run(coro):
    return asyncio.run(coro)


def _boom(*a, **k):
    raise RuntimeError("device error")


def test_main_bluetooth_errors_become_defaults(plugin, monkeypatch):
    monkeypatch.setattr(main.bluetooth, "list_paired", _boom)
    monkeypatch.setattr(main.bluetooth, "scan", _boom)
    monkeypatch.setattr(main.bluetooth, "unpair", _boom)
    assert run(plugin.get_bluetooth_devices()) == {"ok": False, "devices": []}
    assert run(plugin.scan_bluetooth()) == {"ok": False, "devices": []}
    assert run(plugin.unpair_bluetooth(SPEAKER)) == {"ok": False, "status": "failed"}


def test_main_bluetooth_lists(plugin, monkeypatch):
    dev = {"id": SPEAKER, "name": "S", "connected": False, "type": "audio", "le": False}
    monkeypatch.setattr(main.bluetooth, "list_paired", lambda: [dev])
    monkeypatch.setattr(main.bluetooth, "scan", lambda: [])
    assert run(plugin.get_bluetooth_devices()) == {"ok": True, "devices": [dev]}
    assert run(plugin.scan_bluetooth()) == {"ok": True, "devices": []}


def test_main_pairing_flow_and_unload_cleanup(plugin, monkeypatch):
    procs = []

    def spawn(argv):
        procs.append(FakeProc())
        return procs[-1]
    real = bluetooth.PairingSession
    monkeypatch.setattr(main.bluetooth, "PairingSession", lambda i, n: real(i, n, spawn=spawn))
    assert run(plugin.get_pairing())["stage"] == "idle"
    assert run(plugin.respond_pairing(True))["stage"] == "idle"
    assert run(plugin.cancel_pairing())["stage"] == "idle"
    st = run(plugin.start_pairing(NEW, "Headset"))
    assert st["active"] is True and st["device_id"] == NEW
    procs[0].emit(event="pin", kind="ConfirmPinMatch", pin="55")
    end = time.time() + 2
    while run(plugin.get_pairing())["stage"] != "confirm" and time.time() < end:
        time.sleep(0.01)
    assert run(plugin.get_pairing())["pin"] == "55"
    # a second start closes the first helper
    run(plugin.start_pairing(NEW, "Headset"))
    assert procs[0].killed is True and len(procs) == 2
    run(plugin._unload())
    assert procs[1].killed is True and plugin._pairing is None


def test_main_overlapping_starts_leave_one_helper(plugin, monkeypatch):
    procs = []

    def spawn(argv):
        procs.append(FakeProc())
        return procs[-1]
    real = bluetooth.PairingSession
    created = []

    def make(i, n):
        session = real(i, n, spawn=spawn)
        created.append(session)
        return session
    monkeypatch.setattr(main.bluetooth, "PairingSession", make)

    async def both():
        return await asyncio.gather(plugin.start_pairing(NEW, "A"), plugin.start_pairing(MOUSE, "B"),
                                    plugin.start_pairing(NEW, "C"))
    run(both())
    assert len(procs) == 3
    alive = [p for p in procs if not p.killed]
    assert len(alive) == 1
    assert plugin._pairing is created[procs.index(alive[0])]
    run(plugin._unload())
    assert all(p.killed for p in procs) and plugin._pairing is None


def test_main_dismiss_pairing(plugin, monkeypatch):
    proc = FakeProc()
    real = bluetooth.PairingSession
    monkeypatch.setattr(main.bluetooth, "PairingSession", lambda i, n: real(i, n, spawn=spawner(proc)))
    assert run(plugin.dismiss_pairing())["stage"] == "idle"
    run(plugin.start_pairing(NEW, "Headset"))
    # still running: dismiss keeps it
    assert run(plugin.dismiss_pairing())["active"] is True
    assert run(plugin.get_pairing())["active"] is True
    proc.emit(event="result", status="Paired")
    proc.end()
    end = time.time() + 2
    while run(plugin.get_pairing())["active"] and time.time() < end:
        time.sleep(0.01)
    assert run(plugin.get_pairing())["ok"] is True  # shown until dismissed
    assert run(plugin.dismiss_pairing())["stage"] == "idle"
    assert run(plugin.get_pairing())["stage"] == "idle"


def test_main_unload_uses_short_grace(plugin, monkeypatch):
    graces = []

    class Session:
        def close(self, grace):
            graces.append(grace)
    plugin._pairing = Session()
    run(plugin._unload())
    assert graces == [bluetooth.UNLOAD_GRACE] and bluetooth.UNLOAD_GRACE < 5


def test_main_start_pairing_bad_id(plugin):
    st = run(plugin.start_pairing("nope", ""))
    assert st["active"] is False and st["message"] == "Unknown device."


def test_main_bluetooth_not_windows(plugin, monkeypatch):
    monkeypatch.setattr(main, "ON_WINDOWS", False)
    monkeypatch.setattr(main.bluetooth, "PairingSession", lambda *a: pytest.fail("spawned"))
    assert run(plugin.get_bluetooth_devices()) == {"ok": False, "devices": []}
    assert run(plugin.start_pairing(NEW, ""))["active"] is False


def test_main_does_no_bluetooth_io_on_load(plugin, monkeypatch):
    for name in ("list_paired", "scan", "unpair", "PairingSession"):
        monkeypatch.setattr(main.bluetooth, name, lambda *a, _n=name, **k: pytest.fail(f"_main called {_n}"))
    run(plugin._main())
    run(plugin._unload())
