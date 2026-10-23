import asyncio
import logging
import os
import subprocess
import xml.etree.ElementTree as ET

import pytest

import main
from wqs import wifi

NS = {"w": "http://www.microsoft.com/networking/WLAN/profile/v1"}
PASSWORD = "s3cret pass<&>\"'word"

# The message netsh prints on this PC (from wlancfg.dll.mui) when Windows Location is off.
LOCATION_OFF = (
    "Network shell commands need location permission to access WLAN information. Turn on Location services on the "
    "Location page in Privacy & security settings.\r\n\r\nHere is the URI for the Location page in the Settings app: "
    "ms-settings:privacy-location\r\nTo open the Location page in the Settings app, run the following command:\r\n"
    "start ms-settings:privacy-location\r\n"
)

# Odd names: quotes, spaces at both ends, colons (also " : "), emoji, XML characters.
ODD = ["  spaced out  ", "it's \"quoted\"", "a : b:c", "café \U0001F600", "<Tom & Jerry>"]


def networks_output(nets):
    """nets: [(ssid, auth, encryption, [signals])] in netsh's English layout (trailing spaces as on a real PC)."""
    out = [" \r", "Interface name : Wi-Fi \r", f"There are {len(nets)} networks currently visible. \r", "\r"]
    for n, (ssid, auth, enc, signals) in enumerate(nets, 1):
        out.append(f"SSID {n} : {ssid}\r")
        out.append("    Network type            : Infrastructure\r")
        out.append(f"    Authentication          : {auth}\r")
        out.append(f"    Encryption              : {enc} \r")
        for b, sig in enumerate(signals, 1):
            out.append(f"    BSSID {b}                 : c8:d3:a3:6a:cf:{b:02x}\r")
            out.append(f"         Signal             : {sig}%  \r")
            out.append("         Radio type         : 802.11ax\r")
            out.append("         Channel            : 36 \r")
            out.append("         Basic rates (Mbps) : 6 12 24\r")
        out.append("\r")
    return "\n".join(out) + "\n"


def interfaces_output(state="disconnected", ssid=None, signal=None, profile=None, auth="WPA2-Personal", name="Wi-Fi"):
    out = ["\r", "There is 1 interface on the system: \r", "\r",
           f"    Name                   : {name}\r",
           "    Description            : MediaTek Wi-Fi 6E MT7922 (RZ616) 160MHz Wireless LAN Card\r",
           "    GUID                   : cb37081f-a312-41c5-8545-ad0a0d03d41f\r",
           "    Physical address       : 48:45:e6:0b:c7:0d\r",
           "    Interface type         : Primary\r",
           f"    State                  : {state}\r"]
    if state == "connected":
        out += [f"    SSID                   : {ssid}\r",
                "    AP BSSID               : c8:d3:a3:6a:cf:3e\r",
                "    Band                   : 5 GHz\r",
                "    Network type           : Infrastructure\r",
                f"    Authentication         : {auth}\r",
                "    Cipher                 : CCMP\r",
                "    Connection mode        : Profile\r",
                "    Receive rate (Mbps)    : 866.7\r",
                f"    Signal                 : {signal}% \r",
                f"    Profile                : {profile if profile is not None else ssid}\r"]
    out += ["    Radio status           : Hardware On\r", "                             Software On\r", "\r"]
    return "\n".join(out) + "\n"


def profiles_output(user=(), group=()):
    out = ["\r", "Profiles on interface Wi-Fi:\r", "\r", "Group policy profiles (read only)\r",
           "---------------------------------\r"]
    out += [f"    All User Profile     : {g}\r" for g in group] or ["    <None>\r"]
    out += ["\r", "User profiles\r", "-------------\r"]
    out += [f"    All User Profile     : {u}\r" for u in user] or ["    <None>\r"]
    return "\n".join(out + ["\r"]) + "\n"


class FakeNetsh:
    """Answers netsh calls by their first three arguments. A value may be a string, a (code, text) pair, a list of
    those (used in turn, the last one repeats) or a callable(argv) -> (code, text)."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[list[str]] = []
        self.files: dict[str, str] = {}  # profile XML seen by "add profile", by path

    def __call__(self, argv, timeout):
        assert argv[0] == wifi.NETSH
        assert timeout > 0
        self.calls.append(list(argv))
        args = argv[1:]
        key = "_".join(args[1:3]) if args[:1] == ["wlan"] else "other"
        if key == "add_profile":
            path = next(a[len("filename="):] for a in args if a.startswith("filename="))
            with open(path, encoding="utf-8") as f:
                self.files[path] = f.read()
        ans = self.answers.get(key, (0, ""))
        if isinstance(ans, list):
            ans = ans.pop(0) if len(ans) > 1 else ans[0]
        if callable(ans):
            ans = ans(argv)
        if isinstance(ans, str):
            ans = (0, ans)
        return ans[0], ans[1], ""

    def commands(self, key):
        return [c for c in self.calls if "_".join(c[2:4]) == key]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def connect(fake, ssid, password="", tmp_path=None, wait=15.0):
    clock = Clock()
    return wifi.connect(ssid, password, fake, clock=clock, sleep=clock.sleep, wait=wait,
                        tmp_base=str(tmp_path) if tmp_path else None)


# Running netsh ------------------------------------------------------------------------------------------------------

def test_netsh_runner_uses_full_path_argv_no_window_and_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, "café".encode("utf-8"), b"")
    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    assert wifi.run_netsh(["wlan", "show", "interfaces"], timeout=7) == (0, "café")
    assert seen["argv"] == [wifi.NETSH, "wlan", "show", "interfaces"]
    assert os.path.isabs(wifi.NETSH) and wifi.NETSH.lower().endswith(os.path.join("system32", "netsh.exe"))
    assert seen["kw"]["timeout"] == 7 and "shell" not in seen["kw"]
    if os.name == "nt":
        assert seen["kw"]["creationflags"] == 0x08000000


def test_run_netsh_timeout_is_netsh_error():
    def runner(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    with pytest.raises(wifi.NetshError, match="timed out"):
        wifi.run_netsh(["wlan", "show", "networks"], runner, timeout=1)


def test_decode_output_utf8_then_code_page(monkeypatch):
    assert wifi.decode_output("﻿\U0001F600".encode("utf-8")) == "\U0001F600"
    monkeypatch.setattr(wifi, "_oem_codepage", lambda: "cp437")
    assert wifi.decode_output("café".encode("cp437")) == "café"


# Parsing ------------------------------------------------------------------------------------------------------------

def test_parse_networks_multiple_secured_open_signal():
    text = networks_output([
        ("Home", "WPA2-Personal", "CCMP", [40, 81]),
        ("Cafe Free", "Open", "None", [55]),
        ("New Router", "WPA3-Personal", "GCMP", [30]),
        ("Office", "WPA2-Enterprise", "CCMP", [70]),
        ("Old", "Open", "WEP", [20]),
        ("", "WPA2-Personal", "CCMP", [90]),  # hidden
    ])
    nets = wifi.parse_networks(text)
    assert [(n["ssid"], n["signal"], n["kind"], n["secured"], n["supported"]) for n in nets] == [
        ("Home", 81, "wpa2", True, True),
        ("Cafe Free", 55, "open", False, True),
        ("New Router", 30, "wpa3", True, True),
        ("Office", 70, "enterprise", True, False),
        ("Old", 20, "unsupported", True, False),
    ]


def test_parse_networks_odd_names_round_trip():
    nets = wifi.parse_networks(networks_output([(s, "WPA2-Personal", "CCMP", [50]) for s in ODD]))
    assert [n["ssid"] for n in nets] == ODD


def test_parse_networks_duplicates_keep_strongest():
    text = networks_output([("Twin", "WPA2-Personal", "CCMP", [30]), ("Twin", "WPA3-Personal", "CCMP", [77])])
    nets = wifi.parse_networks(text)
    assert len(nets) == 1 and nets[0]["signal"] == 77 and nets[0]["kind"] == "wpa3"


def test_parse_networks_transition_and_unknown():
    assert wifi.classify("WPA2/WPA3-Personal") == "wpa2wpa3"
    assert wifi.classify("WPA3-Personal Transition") == "wpa2wpa3"
    assert wifi.classify("WPA3-Enterprise 192 Bits") == "enterprise"
    assert wifi.classify("OWE") == "unsupported"


def test_parse_networks_translated_labels_use_layout():
    text = ("\r\nSchnittstellenname : WLAN \r\nEs sind 2 Netzwerke sichtbar. \r\n\r\n"
            "SSID 1 : Zuhause\r\n    Netzwerktyp             : Infrastruktur\r\n"
            "    Authentifizierung       : WPA2-Personal\r\n    Verschlüsselung         : CCMP \r\n"
            "    BSSID 1                 : c8:d3:a3:6a:cf:3e\r\n         Signal             : 64%  \r\n\r\n"
            "SSID 2 : Offen\r\n    Netzwerktyp             : Infrastruktur\r\n"
            "    Authentifizierung       : Open\r\n    Verschlüsselung         : None \r\n"
            "    BSSID 1                 : c8:d3:a3:6a:cf:3f\r\n         Signal             : 12%  \r\n")
    nets = wifi.parse_networks(text)
    assert [(n["ssid"], n["kind"], n["signal"]) for n in nets] == [("Zuhause", "wpa2", 64), ("Offen", "open", 12)]


def test_parse_profiles_sections_and_odd_names():
    text = profiles_output(user=["Musika", *ODD], group=["Corp"])
    assert wifi.parse_profiles(text) == [{"name": "Corp", "readonly": True}] + [
        {"name": n, "readonly": False} for n in ["Musika", *ODD]]
    assert wifi.parse_profiles(profiles_output()) == []


def test_parse_interfaces_connected_and_disconnected():
    [i] = wifi.parse_interfaces(interfaces_output("connected", " my : net ", 87, profile=" my : net "))
    assert i == {"name": "Wi-Fi", "state": "connected", "ssid": " my : net ", "signal": 87, "profile": " my : net ",
                 "auth": "WPA2-Personal", "connected": True}
    [d] = wifi.parse_interfaces(interfaces_output("disconnected"))
    assert d["connected"] is False and d["ssid"] is None and d["state"] == "disconnected"
    assert wifi.parse_interfaces("There is no wireless interface on the system.\r\n") == []


def test_parse_interfaces_translated_infers_connected_from_signal():
    text = interfaces_output("connected", "Zuhause", 70).replace("State ", "Status").replace(": connected", ": verbunden")
    [i] = wifi.parse_interfaces(text)
    assert i["connected"] is True and i["state"] == "connected" and i["signal"] == 70 and i["ssid"] == "Zuhause"


# Status and search --------------------------------------------------------------------------------------------------

def test_get_status_lists_current_and_saved():
    fake = FakeNetsh(show_interfaces=interfaces_output("connected", "Home", 66),
                     show_profiles=profiles_output(user=["Home", "Cafe"]))
    s = wifi.get_status(fake)
    assert s["ok"] and s["present"] and s["interface"] == "Wi-Fi" and not s["location_off"]
    assert s["connected"] == {"ssid": "Home", "signal": 66, "profile": "Home", "auth": "WPA2-Personal"}
    assert [p["name"] for p in s["profiles"]] == ["Home", "Cafe"]


def test_location_off_message_and_saved_networks_still_listed():
    fake = FakeNetsh(show_interfaces=(1, LOCATION_OFF), show_networks=(1, LOCATION_OFF),
                     show_profiles=profiles_output(user=["Home"]))
    assert wifi.scan(fake) == {"location_off": True, "error": None, "networks": []}
    s = wifi.get_status(fake)
    assert s["location_off"] is True and [p["name"] for p in s["profiles"]] == ["Home"]
    assert wifi.LOCATION_MESSAGE == "Turn on Location in Windows Settings to search for networks."
    # Plain "location" in an error also counts; a network named "location" in a normal list does not.
    assert wifi.location_blocked(1, "Location services are off") is True
    assert wifi.scan(FakeNetsh(show_networks=networks_output([("location", "Open", "None", [5])])))["networks"]


def test_scan_state_keeps_order_and_expires_when_not_polled():
    lists = [networks_output([("A", "Open", "None", [10]), ("B", "Open", "None", [90])]),
             networks_output([("C", "Open", "None", [99]), ("B", "Open", "None", [50])])]
    fake = FakeNetsh(show_networks=lists)
    clock = Clock()
    s = wifi.ScanState(clock, idle=15)
    assert s.refresh(fake)["active"] is False and fake.calls == []  # not started: no netsh run
    s.start()
    assert [n["ssid"] for n in s.refresh(fake)["networks"]] == ["A", "B"]
    assert [n["ssid"] for n in s.refresh(fake)["networks"]] == ["B", "C"]  # A gone, C appended
    clock.t += 16
    assert s.refresh(fake)["active"] is False
    s.start()
    s.stop()
    assert s.snapshot()["active"] is False


# Profile XML --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,auth,enc,transition", [
    ("open", "open", "none", False), ("wpa2", "WPA2PSK", "AES", False), ("wpa3", "WPA3SAE", "AES", False),
    ("wpa2wpa3", "WPA3SAE", "AES", True)])
@pytest.mark.parametrize("ssid", ["Home", *ODD])
def test_profile_xml_each_kind_with_escaping(kind, auth, enc, transition, ssid):
    password = "" if kind == "open" else PASSWORD
    root = ET.fromstring(wifi.build_profile_xml(ssid, kind, password).encode("utf-8"))
    assert root.findtext("w:name", namespaces=NS) == wifi.profile_name_for(ssid)
    assert root.findtext("w:SSIDConfig/w:SSID/w:name", namespaces=NS) == ssid
    assert bytes.fromhex(root.findtext("w:SSIDConfig/w:SSID/w:hex", namespaces=NS)).decode("utf-8") == ssid
    ae = root.find("w:MSM/w:security/w:authEncryption", NS)
    assert ae.findtext("w:authentication", namespaces=NS) == auth
    assert ae.findtext("w:encryption", namespaces=NS) == enc
    assert ae.findtext("w:useOneX", namespaces=NS) == "false"
    tm = ae.find("{http://www.microsoft.com/networking/WLAN/profile/v4}transitionMode")
    assert (tm is not None and tm.text == "true") is transition
    key = root.find("w:MSM/w:security/w:sharedKey", NS)
    if kind == "open":
        assert key is None
    else:
        assert key.findtext("w:keyType", namespaces=NS) == "passPhrase"
        assert key.findtext("w:protected", namespaces=NS) == "false"
        assert key.findtext("w:keyMaterial", namespaces=NS) == PASSWORD


def test_profile_xml_hex_key_and_quote_profile_name():
    root = ET.fromstring(wifi.build_profile_xml('say "hi"', "wpa2", "ab" * 32).encode("utf-8"))
    assert root.findtext("w:MSM/w:security/w:sharedKey/w:keyType", namespaces=NS) == "networkKey"
    assert root.findtext("w:name", namespaces=NS) == "say 'hi'"
    with pytest.raises(ValueError):
        wifi.build_profile_xml("x", "enterprise", "")


# Connect ------------------------------------------------------------------------------------------------------------

def test_connect_saved_network_directly():
    fake = FakeNetsh(show_interfaces=[interfaces_output(), interfaces_output("authenticating"),
                                      interfaces_output("connected", "Home", 72)],
                     show_profiles=profiles_output(user=["Home"]))
    res = connect(fake, "Home")
    assert res == {"ok": True, "status": "connected", "message": "Connected to Home.", "ssid": "Home"}
    assert fake.commands("add_profile") == [] and fake.commands("show_networks") == []
    [c] = [c for c in fake.calls if c[1:3] == ["wlan", "connect"]]
    assert c[3:] == ["name=Home", "ssid=Home", "interface=Wi-Fi"]


def test_connect_new_secured_network_success(tmp_path):
    ssid = "café \U0001F600 : x "
    fake = FakeNetsh(show_interfaces=[interfaces_output(), interfaces_output("associating"),
                                      interfaces_output("connected", ssid, 90)],
                     show_profiles=profiles_output(user=["Other"]),
                     show_networks=networks_output([(ssid, "WPA2-Personal", "CCMP", [90])]))
    res = connect(fake, ssid, PASSWORD, tmp_path)
    assert res["ok"] is True and res["status"] == "connected"
    [add] = [c for c in fake.calls if c[1:4] == ["wlan", "add", "profile"]]
    assert "user=current" in add
    [path] = fake.files
    assert os.path.dirname(os.path.dirname(path)) == str(tmp_path)
    xml = ET.fromstring(fake.files[path].encode("utf-8"))
    assert xml.findtext("w:MSM/w:security/w:sharedKey/w:keyMaterial", namespaces=NS) == PASSWORD
    assert not os.path.exists(path) and os.listdir(tmp_path) == []  # file and its folder deleted
    [c] = [c for c in fake.calls if c[1:3] == ["wlan", "connect"]]
    assert c[3:5] == [f"name={ssid}", f"ssid={ssid}"]
    assert fake.commands("delete_profile") == []
    _assert_password_nowhere(fake, res)


def test_connect_open_network_needs_no_password(tmp_path):
    fake = FakeNetsh(show_interfaces=[interfaces_output(), interfaces_output("connected", "Cafe", 50)],
                     show_networks=networks_output([("Cafe", "Open", "None", [50])]),
                     show_profiles=profiles_output())
    assert connect(fake, "Cafe", "", tmp_path)["status"] == "connected"
    [xml] = fake.files.values()
    assert "<authentication>open</authentication>" in xml and "sharedKey" not in xml


def test_connect_quote_ssid_uses_safe_profile_name_and_no_ssid_arg(tmp_path):
    ssid = '"Free" Wi-Fi'
    fake = FakeNetsh(show_interfaces=[interfaces_output(), interfaces_output("connected", ssid, 50, "'Free' Wi-Fi")],
                     show_networks=networks_output([(ssid, "Open", "None", [50])]), show_profiles=profiles_output())
    assert connect(fake, ssid, "", tmp_path)["status"] == "connected"
    [c] = [c for c in fake.calls if c[1:3] == ["wlan", "connect"]]
    assert c[3:] == ["name='Free' Wi-Fi", "interface=Wi-Fi"]
    assert not any('"' in a for call in fake.calls for a in call[1:] if not a.startswith("filename="))


def test_wrong_password_reports_and_removes_new_profile(tmp_path):
    fake = FakeNetsh(show_interfaces=[interfaces_output(), interfaces_output("authenticating"),
                                      interfaces_output("disconnected"), interfaces_output("disconnected")],
                     show_networks=networks_output([("Home", "WPA3-Personal", "CCMP", [70])]),
                     show_profiles=profiles_output())
    res = connect(fake, "Home", "wrongpassword", tmp_path)
    assert res == {"ok": False, "status": "wrong_password", "message": "Could not connect. Check the password.",
                   "ssid": "Home"}
    [d] = [c for c in fake.calls if c[1:4] == ["wlan", "delete", "profile"]]
    assert d[4:] == ["name=Home", "interface=Wi-Fi"]
    assert os.listdir(tmp_path) == []


def test_connect_timeout_saved_network_keeps_profile():
    fake = FakeNetsh(show_interfaces=interfaces_output(), show_profiles=profiles_output(user=["Home"]))
    res = connect(fake, "Home", wait=5)
    assert res["ok"] is False and res["status"] == "timeout"
    assert fake.commands("delete_profile") == []
    assert len([c for c in fake.calls if c[1:4] == ["wlan", "show", "interfaces"]]) <= 7


def test_connect_timeout_new_network_cleans_up(tmp_path):
    fake = FakeNetsh(show_interfaces=interfaces_output(), show_profiles=profiles_output(),
                     show_networks=networks_output([("Home", "WPA2-Personal", "CCMP", [70])]))
    assert connect(fake, "Home", "password1", tmp_path, wait=3)["status"] == "wrong_password"
    assert len([c for c in fake.calls if c[1:4] == ["wlan", "delete", "profile"]]) == 1


def test_temp_file_deleted_even_when_add_profile_fails(tmp_path):
    def boom(argv):
        raise subprocess.TimeoutExpired(argv, 15)
    fake = FakeNetsh(show_interfaces=interfaces_output(), show_profiles=profiles_output(),
                     show_networks=networks_output([("Home", "WPA2-Personal", "CCMP", [70])]), add_profile=boom)
    with pytest.raises(wifi.NetshError):
        connect(fake, "Home", PASSWORD, tmp_path)
    assert fake.files and os.listdir(tmp_path) == []
    fake2 = FakeNetsh(show_interfaces=interfaces_output(), show_profiles=profiles_output(),
                      show_networks=networks_output([("Home", "WPA2-Personal", "CCMP", [70])]),
                      add_profile=(1, "The profile is corrupted."))
    res = connect(fake2, "Home", PASSWORD, tmp_path)
    assert res["status"] == "failed" and "corrupted" in res["message"]
    assert os.listdir(tmp_path) == []
    _assert_password_nowhere(fake2, res)


def test_connect_refusals():
    nets = networks_output([("Corp", "WPA2-Enterprise", "CCMP", [80]), ("Home", "WPA2-Personal", "CCMP", [70])])
    fake = FakeNetsh(show_interfaces=interfaces_output(), show_profiles=profiles_output(), show_networks=nets)
    res = connect(fake, "Corp", PASSWORD)
    assert res["status"] == "unsupported" and "Connect from Windows Settings" in res["message"]
    assert connect(fake, "Home")["status"] == "needs_password"
    assert connect(fake, "Home", "short")["status"] == "invalid_password"
    assert connect(fake, "Gone", PASSWORD)["status"] == "out_of_range"
    assert connect(fake, "")["status"] == "invalid"
    assert fake.commands("add_profile") == [] and not any(c[1:3] == ["wlan", "connect"] for c in fake.calls)
    _assert_password_nowhere(fake, res)


def test_connect_new_network_with_location_off():
    fake = FakeNetsh(show_interfaces=(1, LOCATION_OFF), show_networks=(1, LOCATION_OFF),
                     show_profiles=profiles_output())
    res = connect(fake, "Home", PASSWORD)
    assert res["status"] == "location_off" and res["message"] == wifi.LOCATION_MESSAGE


def test_connect_saved_network_with_location_off_reports_request_sent():
    fake = FakeNetsh(show_interfaces=(1, LOCATION_OFF), show_profiles=profiles_output(user=["Home"]))
    res = connect(fake, "Home")
    assert res["status"] == "requested" and "Location" in res["message"]
    [c] = [c for c in fake.calls if c[1:3] == ["wlan", "connect"]]
    assert c[3:] == ["name=Home", "ssid=Home"]


def test_connect_already_connected_does_nothing():
    fake = FakeNetsh(show_interfaces=interfaces_output("connected", "Home", 80))
    assert connect(fake, "Home")["status"] == "connected"
    assert [c[1:4] for c in fake.calls] == [["wlan", "show", "interfaces"]]


def _assert_password_nowhere(fake, res):
    assert all(PASSWORD not in a and "wrongpassword" not in a for c in fake.calls for a in c)
    assert PASSWORD not in repr(res)


# Disconnect / forget ------------------------------------------------------------------------------------------------

def test_disconnect():
    fake = FakeNetsh(show_interfaces=interfaces_output("connected", "Home", 80))
    assert wifi.disconnect(fake)["ok"] is True
    assert fake.calls[-1][1:] == ["wlan", "disconnect", "interface=Wi-Fi"]


def test_forget_guards():
    fake = FakeNetsh(show_profiles=profiles_output(user=["Home", "*", 'say "hi"'], group=["Corp"]))
    assert wifi.forget("Home", fake)["ok"] is True
    assert fake.calls[-1][1:] == ["wlan", "delete", "profile", "name=Home"]
    for name in ("*", 'say "hi"', "Corp", "Missing"):
        assert wifi.forget(name, fake)["ok"] is False
    assert len([c for c in fake.calls if c[1:3] == ["wlan", "delete"]]) == 1
    with pytest.raises(ValueError):
        wifi.delete_profile("*")


# main.py ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ON_WINDOWS", True)
    p = main.Plugin()
    p.settings = main.Settings(str(tmp_path / "settings"))
    return p


def test_main_connect_logs_without_password(plugin, monkeypatch, caplog):
    monkeypatch.setattr(main.wifi, "connect",
                        lambda ssid, password: {"ok": True, "status": "connected", "message": "x", "ssid": ssid})
    with caplog.at_level(logging.DEBUG):
        res = asyncio.run(plugin.connect_wifi("Home", PASSWORD))
    assert res["status"] == "connected"
    assert "Home" in caplog.text and PASSWORD not in caplog.text


def test_main_wifi_calls_are_guarded(plugin, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("netsh broke")
    for name in ("get_status", "connect", "disconnect", "forget", "scan"):
        monkeypatch.setattr(main.wifi, name, boom)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(plugin.get_wifi())["ok"] is False
        assert asyncio.run(plugin.connect_wifi("Home", PASSWORD))["ok"] is False
        assert asyncio.run(plugin.disconnect_wifi())["ok"] is False
        assert asyncio.run(plugin.forget_wifi("Home"))["ok"] is False
        assert asyncio.run(plugin.start_wifi_scan())["error"]
    assert asyncio.run(plugin.stop_wifi_scan())["active"] is False
    assert PASSWORD not in caplog.text


def test_main_wifi_not_windows_returns_defaults(plugin, monkeypatch):
    monkeypatch.setattr(main, "ON_WINDOWS", False)
    monkeypatch.setattr(main.wifi, "connect", lambda *a: pytest.fail("called"))
    assert asyncio.run(plugin.connect_wifi("Home", "password1"))["ok"] is False
