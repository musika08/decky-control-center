"""Wi-Fi networks through Windows' built-in `netsh wlan`. No admin needed.

- Status: `netsh wlan show interfaces` (current network, signal) and `netsh wlan show profiles` (saved networks).
- Search: `netsh wlan show networks mode=bssid`. It lists what Windows found in its own background scans; netsh has no
  command to start a scan, so the panel just reads the list again every few seconds while the search is open.
- Connect to a saved network: `netsh wlan connect name=<profile>`.
- Connect to a new network: write a WLAN profile XML to a temporary file in a private folder, `netsh wlan add profile
  filename=... user=current`, delete the file (always, in a finally block), then connect. The password is only ever
  written to that file; it is never logged, never put on a command line and never returned.
- Disconnect: `netsh wlan disconnect`. Forget: `netsh wlan delete profile name=<profile>`.

netsh is run directly (full path, argv list, no shell, no window, with a timeout) through an injectable runner, so tests
use fake output.

Parsing: netsh's labels are translated on non-English Windows. The parsers use the layout (indentation, "key : value"
lines, the dashed section underlines, "SSID n" block headers) plus the English keys where they exist, and fall back to
values that are not translated (signal "NN%", authentication names such as "WPA2-Personal"). Status words such as
"connected" are only understood in English; elsewhere "connected" is inferred from the signal line, which netsh shows
only while connected. SSIDs and profile names are taken verbatim after the first " : " (netsh pads no spaces after them),
so leading and trailing spaces, quotes, colons and emoji survive.

netsh treats double quotes in its arguments specially (a value that starts with one loses it, some layouts are misread
as other parameters), so a name with a double quote is never put on a netsh command line: a new profile for such an SSID
gets a profile name with the quotes replaced and is connected by that name alone (the profile XML carries the exact SSID,
also as hex), and a saved profile whose name has a double quote can only be connected or forgotten in Windows Settings.
"""
import os
import re
import subprocess
import tempfile
import time
from typing import Callable, Optional
from xml.sax.saxutils import escape

from .runner import CREATE_NO_WINDOW, Runner

NETSH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")
TIMEOUT = 15.0  # per netsh call
CONNECT_WAIT = 15.0  # how long a connect waits for Windows to report "connected"
POLL_INTERVAL = 1.0
SCAN_IDLE = 15.0  # a search nobody has polled for this long counts as closed

LOCATION_MESSAGE = "Turn on Location in Windows Settings to search for networks."
WRONG_PASSWORD_MESSAGE = "Could not connect. Check the password."
SETTINGS_MESSAGE = "Connect from Windows Settings."

# Authentication values netsh prints (not translated). Lower case.
KNOWN_AUTH = ("open", "shared", "wpa-personal", "wpa2-personal", "wpa3-personal", "wpa-enterprise", "wpa2-enterprise",
              "wpa3-enterprise", "wpa3-enterprise 192 bits", "owe", "unknown")


class NetshError(RuntimeError):
    pass


# Running netsh ------------------------------------------------------------------------------------------------------

def _oem_codepage() -> str:
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not Windows
        return "utf-8"


def decode_output(data: bytes) -> str:
    """netsh writes UTF-8 to a pipe on current Windows 11 builds; older builds use the console (OEM) code page."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode(_oem_codepage(), "replace")
        except LookupError:
            text = data.decode("utf-8", "replace")
    return text.lstrip("\ufeff")


def netsh_runner(argv: list[str], timeout: float) -> "tuple[int, str, str]":
    p = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    return p.returncode, decode_output(p.stdout), decode_output(p.stderr)


def run_netsh(args: list[str], runner: Optional[Runner] = None, timeout: float = TIMEOUT) -> "tuple[int, str]":
    """(returncode, stdout + stderr). Raises NetshError on a timeout or when netsh cannot start."""
    run = runner or netsh_runner
    try:
        code, out, err = run([NETSH, *args], timeout)
    except subprocess.TimeoutExpired as e:
        raise NetshError(f"netsh timed out after {timeout:g}s") from e
    except OSError as e:
        raise NetshError(f"could not run netsh: {e}") from e
    return code, (out or "") + (err or "")


def safe_arg(value: str) -> bool:
    """True when `value` can be passed to netsh as name=/ssid=/interface= and arrive unchanged (see module doc).
    "*" is refused because `delete profile name=*` deletes every profile."""
    return (isinstance(value, str) and value != "" and '"' not in value and value.strip() != "*"
            and not any(ord(c) < 32 for c in value))


def location_blocked(code: int, text: str) -> bool:
    """netsh refuses WLAN commands when Windows Location is off for desktop apps. Its message ("Network shell commands
    need location permission to access WLAN information. Turn on Location services ...") always carries the Settings
    URI ms-settings:privacy-location, which is not translated."""
    if "privacy-location" in text:
        return True
    return code != 0 and "location" in text.lower()


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# Parsing ------------------------------------------------------------------------------------------------------------

_KV = re.compile(r"^(?P<indent>[ \t]*)(?P<key>\S.*?) :(?: (?P<value>.*))?$")


def _lines(text: str) -> list[str]:
    return [line.rstrip("\r") for line in text.split("\n")]


def _kv(line: str) -> Optional["tuple[int, str, str]"]:
    """(indent, key, value) for a "key : value" line. The value is kept exactly (SSIDs may have spaces at either end)."""
    m = _KV.match(line)
    if not m:
        return None
    return len(m.group("indent").expandtabs(4)), m.group("key").strip(), m.group("value") or ""


_SIGNAL = re.compile(r"(\d{1,3})\s*%")


def _signal(value: str) -> Optional[int]:
    m = _SIGNAL.fullmatch(value.strip())
    return min(int(m.group(1)), 100) if m else None


def classify(auth: str, encryption: str = "") -> str:
    """open / wpa2 / wpa3 / wpa2wpa3 / enterprise / unsupported."""
    a = (auth or "").strip().lower()
    e = (encryption or "").strip().lower()
    if "enterprise" in a or "802.1x" in a or "192" in a:
        return "enterprise"
    if a == "open":
        return "open" if e in ("", "none") else "unsupported"  # WEP
    if "wpa3" in a and "personal" in a:
        return "wpa2wpa3" if ("wpa2" in a or "transition" in a) else "wpa3"
    if a == "wpa2-personal":
        return "wpa2"
    return "unsupported"


def parse_networks(text: str) -> list[dict]:
    """`show networks mode=bssid` -> [{ssid, signal, auth, encryption, kind, secured, supported}], one per SSID (the
    strongest), hidden networks (no name) left out, in netsh's order."""
    blocks: list[dict] = []
    cur: Optional[dict] = None
    for line in _lines(text):
        kv = _kv(line)
        if kv is None:
            continue
        indent, key, value = kv
        if indent == 0:
            # "SSID 1 : name" starts a block; "Interface name : Wi-Fi" does not.
            cur = {"ssid": value, "top": [], "keys": {}, "signal": None} if re.fullmatch(r"\S+ \d+", key) else None
            if cur is not None:
                blocks.append(cur)
            continue
        if cur is None:
            continue
        sig = _signal(value)
        if sig is not None:
            cur["signal"] = sig if cur["signal"] is None else max(cur["signal"], sig)
        if indent <= 4 and not re.fullmatch(r"\S+ \d+", key):  # network-level line, not "BSSID 1" or its details
            cur["top"].append(value.strip())
            cur["keys"].setdefault(key, value.strip())
    best: dict[str, dict] = {}
    order: list[str] = []
    for b in blocks:
        ssid = b["ssid"]
        if ssid == "":
            continue  # hidden network
        top = b["top"]
        auth = b["keys"].get("Authentication")
        if auth is None:
            auth = next((v for v in top if v.lower() in KNOWN_AUTH), top[1] if len(top) > 1 else "")
        enc = b["keys"].get("Encryption")
        if enc is None:
            enc = top[2] if len(top) > 2 else ""
        kind = classify(auth, enc)
        net = {"ssid": ssid, "signal": b["signal"] or 0, "auth": auth, "encryption": enc, "kind": kind,
               "secured": kind != "open", "supported": kind in ("open", "wpa2", "wpa3", "wpa2wpa3")}
        if ssid not in best:
            order.append(ssid)
            best[ssid] = net
        elif net["signal"] > best[ssid]["signal"]:
            best[ssid] = net
    return [best[s] for s in order]


def parse_profiles(text: str) -> list[dict]:
    """`show profiles` -> [{name, readonly}]. Each interface lists a group policy section (read only) and then a user
    section, each under a dashed underline; the names are the values of the "key : name" lines."""
    profiles: list[dict] = []
    seen: set[str] = set()
    sections = 0
    for line in _lines(text):
        if re.fullmatch(r"\s*-{3,}\s*", line):
            sections += 1
            continue
        if not line.startswith((" ", "\t")):
            continue
        kv = _kv(line)
        if kv is None or sections == 0:
            continue
        name = kv[2]
        if name and name not in seen:
            seen.add(name)
            profiles.append({"name": name, "readonly": sections % 2 == 1})
    return profiles


STATES = ("connected", "disconnected", "connecting", "associating", "authenticating", "disconnecting", "discovering",
          "ad hoc network formed", "not ready")


def parse_interfaces(text: str) -> list[dict]:
    """`show interfaces` -> [{name, state, ssid, signal, profile, auth, connected}], one per Wi-Fi adapter."""
    ifaces: list[dict] = []
    cur: Optional[dict] = None
    for line in _lines(text):
        if not line.strip():
            cur = None
            continue
        kv = _kv(line)
        if kv is None or kv[0] == 0:
            continue
        _, key, value = kv
        if cur is None:
            cur = {"values": [], "keys": {}}
            ifaces.append(cur)
        cur["values"].append(value)
        cur["keys"].setdefault(key, value)
    result = []
    for raw in ifaces:
        keys, values = raw["keys"], raw["values"]
        state = (keys.get("State") or "").strip().lower()
        signal = next((s for s in (_signal(v) for v in values) if s is not None), None)
        ssid = keys.get("SSID")
        profile = keys.get("Profile")
        if state in STATES:
            connected = state == "connected"
        else:  # translated output: netsh shows the signal only while connected
            connected = signal is not None
        result.append({
            "name": keys.get("Name", values[0] if values else ""),
            "state": state if state in STATES else ("connected" if connected else "unknown"),
            "ssid": ssid if connected else None,
            "signal": signal if connected else None,
            "profile": profile if connected else None,
            "auth": (keys.get("Authentication") or "").strip() if connected else None,
            "connected": connected,
        })
    return result


# Status -------------------------------------------------------------------------------------------------------------

def read_interfaces(runner: Optional[Runner] = None) -> dict:
    """{"location_off": bool, "interfaces": [...], "error": str|None}"""
    code, text = run_netsh(["wlan", "show", "interfaces"], runner)
    if location_blocked(code, text):
        return {"location_off": True, "interfaces": [], "error": None}
    ifaces = parse_interfaces(text)
    return {"location_off": False, "interfaces": ifaces,
            "error": None if code == 0 or ifaces else (_first_line(text) or f"netsh exited with {code}")}


def read_profiles(runner: Optional[Runner] = None) -> dict:
    """{"location_off": bool, "profiles": [...], "error": str|None}"""
    code, text = run_netsh(["wlan", "show", "profiles"], runner)
    if location_blocked(code, text):
        return {"location_off": True, "profiles": [], "error": None}
    if code != 0:
        return {"location_off": False, "profiles": [], "error": _first_line(text) or f"netsh exited with {code}"}
    return {"location_off": False, "profiles": parse_profiles(text), "error": None}


def get_status(runner: Optional[Runner] = None) -> dict:
    """{ok, present, interface, connected: {ssid, signal, profile, auth}|None, profiles: [{name, readonly}],
    location_off}. Saved networks are listed even when Location blocks the rest."""
    ifs = read_interfaces(runner)
    prof = read_profiles(runner)
    iface = ifs["interfaces"][0] if ifs["interfaces"] else None
    current = None
    for i in ifs["interfaces"]:
        if i["connected"]:
            current = {"ssid": i["ssid"] or i["profile"] or "", "signal": i["signal"] or 0,
                       "profile": i["profile"], "auth": i["auth"]}
            break
    return {
        "ok": ifs["error"] is None and prof["error"] is None,
        "present": iface is not None or ifs["location_off"],
        "interface": iface["name"] if iface else None,
        "state": iface["state"] if iface else None,
        "connected": current,
        "profiles": prof["profiles"],
        "location_off": ifs["location_off"] or prof["location_off"],
    }


def scan(runner: Optional[Runner] = None) -> dict:
    """{"location_off": bool, "error": str|None, "networks": [...]}, strongest first within netsh's order."""
    code, text = run_netsh(["wlan", "show", "networks", "mode=bssid"], runner)
    if location_blocked(code, text):
        return {"location_off": True, "error": None, "networks": []}
    nets = parse_networks(text)
    if code != 0 and not nets:
        return {"location_off": False, "error": _first_line(text) or f"netsh exited with {code}", "networks": []}
    return {"location_off": False, "error": None, "networks": nets}


class ScanState:
    """The panel's open search. netsh cannot start a scan, so a search is only "read the list Windows keeps, again and
    again"; this keeps the order networks were first seen in (so the list does not jump under the controller's focus)
    and goes inactive when nobody has polled it for SCAN_IDLE seconds (the panel was closed without Stop)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, idle: float = SCAN_IDLE) -> None:
        self._clock = clock
        self._idle = idle
        self._active = False
        self._last_poll = 0.0
        self._order: list[str] = []
        self._last: dict = {"location_off": False, "error": None, "networks": []}

    def active(self) -> bool:
        return self._active and self._clock() - self._last_poll <= self._idle

    def start(self) -> None:
        self._active = True
        self._last_poll = self._clock()
        self._order = []
        self._last = {"location_off": False, "error": None, "networks": []}

    def stop(self) -> None:
        self._active = False

    def refresh(self, runner: Optional[Runner] = None) -> dict:
        """Read the list again when the search is open; returns snapshot()."""
        if not self.active():
            self._active = False
            return self.snapshot()
        self._last_poll = self._clock()
        res = scan(runner)
        nets = {n["ssid"]: n for n in res["networks"]}
        self._order = [s for s in self._order if s in nets] + [s for s in nets if s not in self._order]
        self._last = {**res, "networks": [nets[s] for s in self._order]}
        return self.snapshot()

    def snapshot(self) -> dict:
        return {"active": self.active(), **self._last}


# Profiles -----------------------------------------------------------------------------------------------------------

AUTH_XML = {
    # kind -> (authentication, encryption, transitionMode)
    "open": ("open", "none", False),
    "wpa2": ("WPA2PSK", "AES", False),
    "wpa3": ("WPA3SAE", "AES", False),
    "wpa2wpa3": ("WPA3SAE", "AES", True),
}

_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _xml(text: str) -> str:
    return escape(_XML_INVALID.sub("", text), {'"': "&quot;", "'": "&apos;"})


def profile_name_for(ssid: str) -> str:
    """The profile name for a new network: the SSID, with double quotes replaced (see module doc)."""
    name = ssid.replace('"', "'")
    return "Wi-Fi network" if name.strip() in ("", "*") else name


def valid_password(password: str) -> bool:
    """WPA passphrase: 8 to 63 characters, or a 64-digit hex key."""
    if re.fullmatch(r"[0-9A-Fa-f]{64}", password or ""):
        return True
    return 8 <= len(password or "") <= 63 and not any(ord(c) < 32 for c in password)


def build_profile_xml(ssid: str, kind: str, password: str = "", name: Optional[str] = None) -> str:
    """WLAN profile XML (WLAN_profile schema v1) for an open, WPA2-Personal, WPA3-Personal (SAE) or WPA2/WPA3
    transition network. The SSID is given both as hex (exact bytes, any characters) and as an escaped name."""
    if kind not in AUTH_XML:
        raise ValueError(f"unsupported network kind {kind!r}")
    auth, enc, transition = AUTH_XML[kind]
    ssid_hex = ssid.encode("utf-8").hex().upper()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">',
        f"\t<name>{_xml(name if name is not None else profile_name_for(ssid))}</name>",
        "\t<SSIDConfig>",
        "\t\t<SSID>",
        f"\t\t\t<hex>{ssid_hex}</hex>",
        f"\t\t\t<name>{_xml(ssid)}</name>",
        "\t\t</SSID>",
        "\t</SSIDConfig>",
        "\t<connectionType>ESS</connectionType>",
        "\t<connectionMode>auto</connectionMode>",
        "\t<MSM>",
        "\t\t<security>",
        "\t\t\t<authEncryption>",
        f"\t\t\t\t<authentication>{auth}</authentication>",
        f"\t\t\t\t<encryption>{enc}</encryption>",
        "\t\t\t\t<useOneX>false</useOneX>",
    ]
    if transition:
        lines.append('\t\t\t\t<transitionMode xmlns="http://www.microsoft.com/networking/WLAN/profile/v4">true'
                     '</transitionMode>')
    lines.append("\t\t\t</authEncryption>")
    if kind != "open":
        key_type = "networkKey" if re.fullmatch(r"[0-9A-Fa-f]{64}", password) else "passPhrase"
        lines += [
            "\t\t\t<sharedKey>",
            f"\t\t\t\t<keyType>{key_type}</keyType>",
            "\t\t\t\t<protected>false</protected>",
            f"\t\t\t\t<keyMaterial>{_xml(password)}</keyMaterial>",
            "\t\t\t</sharedKey>",
        ]
    lines += ["\t\t</security>", "\t</MSM>", "</WLANProfile>", ""]
    return "\n".join(lines)


def private_dir_base() -> str:
    """The running user's own local app data folder (only that user and SYSTEM can read it)."""
    return os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()


def add_profile(xml: str, interface: Optional[str], runner: Optional[Runner] = None,
                tmp_base: Optional[str] = None) -> "tuple[bool, str]":
    """Write `xml` to a file in a new randomly named private folder, `netsh wlan add profile`, and always delete the
    file and the folder again. Returns (ok, netsh's first line)."""
    folder = tempfile.mkdtemp(prefix="wqs-wifi-", dir=tmp_base or private_dir_base())
    path = os.path.join(folder, "profile.xml")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(xml.encode("utf-8"))
        args = ["wlan", "add", "profile", f"filename={path}", "user=current"]
        if interface and safe_arg(interface):
            args.append(f"interface={interface}")
        code, text = run_netsh(args, runner)
        return code == 0, _first_line(text)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.rmdir(folder)
        except OSError:
            pass


def _iface_args(interface: Optional[str]) -> list[str]:
    return [f"interface={interface}"] if interface and safe_arg(interface) else []


def delete_profile(name: str, interface: Optional[str] = None, runner: Optional[Runner] = None) -> "tuple[bool, str]":
    if not safe_arg(name):
        raise ValueError("profile name cannot be passed to netsh")
    code, text = run_netsh(["wlan", "delete", "profile", f"name={name}", *_iface_args(interface)], runner)
    return code == 0, _first_line(text)


def forget(profile: str, runner: Optional[Runner] = None) -> dict:
    """Delete a saved (user) profile. {"ok", "message"}."""
    prof = read_profiles(runner)
    match = next((p for p in prof["profiles"] if p["name"] == profile), None)
    if match is None:
        return {"ok": False, "message": "That network is not saved."}
    if match["readonly"]:
        return {"ok": False, "message": "This network is set by your organization and cannot be forgotten here."}
    if not safe_arg(profile):
        return {"ok": False, "message": "This name cannot be handled here. Forget it in Windows Settings."}
    ok, line = delete_profile(profile, None, runner)
    return {"ok": ok, "message": "Forgotten." if ok else (line or "Windows could not forget it.")}


def disconnect(runner: Optional[Runner] = None) -> dict:
    ifs = read_interfaces(runner)
    iface = next((i for i in ifs["interfaces"] if i["connected"]), None)
    args = ["wlan", "disconnect"]
    if iface is not None:
        args += _iface_args(iface["name"])
    code, text = run_netsh(args, runner)
    return {"ok": code == 0, "message": "Disconnected." if code == 0 else (_first_line(text) or "Could not disconnect.")}


# Connect ------------------------------------------------------------------------------------------------------------

def _result(ok: bool, status: str, message: str, ssid: str) -> dict:
    return {"ok": ok, "status": status, "message": message, "ssid": ssid}


def _is_target(iface: dict, ssid: str, profile: str) -> bool:
    return iface["connected"] and (iface["ssid"] == ssid or iface["profile"] == profile)


def connect(ssid: str, password: str = "", runner: Optional[Runner] = None,
            clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
            wait: float = CONNECT_WAIT, tmp_base: Optional[str] = None) -> dict:
    """Connect to `ssid`: with its saved profile when there is one (a saved profile is matched by name), otherwise by
    creating a profile from the search result (asking for a password when the network is secured).

    Returns {ok, status, message, ssid}; status is connected / needs_password / invalid_password / wrong_password /
    out_of_range / unsupported / location_off / timeout / failed / invalid. The password never appears in it."""
    if not isinstance(ssid, str) or ssid == "" or any(ord(c) < 32 for c in ssid):
        return _result(False, "invalid", "Not a Wi-Fi network name.", str(ssid or ""))
    password = password or ""

    ifs = read_interfaces(runner)
    iface = ifs["interfaces"][0] if ifs["interfaces"] else None
    if iface is None and not ifs["location_off"]:
        return _result(False, "failed", "No Wi-Fi adapter found.", ssid)
    iface_name = iface["name"] if iface else None
    if any(_is_target(i, ssid, ssid) for i in ifs["interfaces"]):
        return _result(True, "connected", f"Connected to {ssid}.", ssid)

    prof = read_profiles(runner)
    saved = next((p for p in prof["profiles"] if p["name"] == ssid), None)
    created: Optional[str] = None
    secured = False
    if saved is not None:
        profile = saved["name"]
        if not safe_arg(profile):
            return _result(False, "unsupported", f"This name cannot be handled here. {SETTINGS_MESSAGE}", ssid)
    else:
        found = scan(runner)
        if found["location_off"]:
            return _result(False, "location_off", LOCATION_MESSAGE, ssid)
        net = next((n for n in found["networks"] if n["ssid"] == ssid), None)
        if net is None:
            return _result(False, "out_of_range", f"{ssid} is out of range.", ssid)
        if not net["supported"]:
            what = "a sign-in (802.1X / Enterprise)" if net["kind"] == "enterprise" else "a kind of security"
            return _result(False, "unsupported", f"This network uses {what} this panel does not support. "
                                                 f"{SETTINGS_MESSAGE}", ssid)
        secured = net["secured"]
        if secured and not password:
            return _result(False, "needs_password", f"Enter the password for {ssid}.", ssid)
        if secured and not valid_password(password):
            return _result(False, "invalid_password", "The password must be 8 to 63 characters.", ssid)
        profile = profile_name_for(ssid)
        if any(p["name"] == profile for p in prof["profiles"]):
            return _result(False, "failed", f"A saved network named {profile} already exists. Forget it first.", ssid)
        ok, line = add_profile(build_profile_xml(ssid, net["kind"], password if secured else "", profile),
                               iface_name, runner, tmp_base)
        if not ok:
            return _result(False, "failed", f"Windows did not accept the network: {line or 'unknown error'}", ssid)
        created = profile

    try:
        res = _connect_and_wait(ssid, profile, iface_name, runner, clock, sleep, wait)
    except Exception:
        if created is not None:
            _cleanup(created, iface_name, runner)
        raise
    if not res["ok"] and created is not None:
        # A new profile that never connected is removed again, so a wrong password does not stay saved.
        _cleanup(created, iface_name, runner)
        if secured and res["status"] in ("timeout", "failed"):
            return _result(False, "wrong_password", WRONG_PASSWORD_MESSAGE, ssid)
    return res


def _cleanup(profile: str, interface: Optional[str], runner: Optional[Runner]) -> None:
    try:
        if safe_arg(profile):
            delete_profile(profile, interface, runner)
    except Exception:  # noqa: BLE001 - best effort
        pass


def _connect_and_wait(ssid: str, profile: str, interface: Optional[str], runner: Optional[Runner],
                      clock: Callable[[], float], sleep: Callable[[float], None], wait: float) -> dict:
    args = ["wlan", "connect", f"name={profile}"]
    if safe_arg(ssid):
        args.append(f"ssid={ssid}")
    args += _iface_args(interface)
    code, text = run_netsh(args, runner)
    if location_blocked(code, text):
        return _result(False, "location_off", LOCATION_MESSAGE, ssid)
    if code != 0:
        line = _first_line(text)
        if "not available" in line.lower():
            return _result(False, "out_of_range", f"{ssid} is out of range.", ssid)
        return _result(False, "failed", f"Could not connect: {line or 'unknown error'}", ssid)

    deadline = clock() + wait
    tried = False  # Windows reported an attempt (associating / authenticating)
    dropped = 0  # disconnected polls after an attempt
    while True:
        sleep(POLL_INTERVAL)
        try:
            ifs = read_interfaces(runner)
        except NetshError:
            ifs = {"location_off": False, "interfaces": []}
        if ifs["location_off"]:
            # Without Location, Windows does not tell netsh which network it is on.
            return _result(True, "requested", f"Connecting to {ssid}. Turn on Location in Windows Settings to see "
                                              "the result here.", ssid)
        mine = [i for i in ifs["interfaces"] if interface is None or i["name"] == interface] or ifs["interfaces"]
        if any(_is_target(i, ssid, profile) for i in mine):
            return _result(True, "connected", f"Connected to {ssid}.", ssid)
        states = {i["state"] for i in mine}
        if states & {"associating", "authenticating", "connecting"}:
            tried, dropped = True, 0
        elif tried and states & {"disconnected"}:
            dropped += 1
            if dropped >= 2:  # Windows gave up (a wrong password ends like this)
                return _result(False, "failed", f"Could not connect to {ssid}.", ssid)
        if clock() >= deadline:
            return _result(False, "timeout", f"Could not connect to {ssid}. Check that it is in range.", ssid)
