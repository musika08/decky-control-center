# Control Center

Formerly "Windows Quick Settings". The plugin folder (`windows-quick-settings`) and internal ids are unchanged.

A Decky Loader plugin for Windows that puts common system controls in the Quick Access menu:

| Section | Control | How | Admin |
| --- | --- | --- | --- |
| Display | Brightness slider | WMI `root\wmi` `WmiMonitorBrightness` (read) and `WmiMonitorBrightnessMethods.WmiSetBrightness` (write), through Windows PowerShell | No |
| Sound | Volume slider, Mute | Core Audio `IAudioEndpointVolume` of the default playback device, called from Python with `ctypes` (no comtypes/pywin32) | No |
| Wireless | Wi-Fi, Bluetooth on/off | WinRT `Windows.Devices.Radios` through Windows PowerShell 5.1 | No |
| Bluetooth devices | Paired list, Search, Pair, Remove | WinRT `Windows.Devices.Enumeration` (association endpoints, `DeviceWatcher`, `DeviceInformationCustomPairing`, `UnpairAsync`) through Windows PowerShell 5.1 | No |
| Wi-Fi networks | Current network, Disconnect, saved networks (Connect, Forget), Search, Connect with password | Windows' built-in `netsh wlan` (`show interfaces/profiles/networks`, `add profile`, `connect`, `disconnect`, `delete profile`) | No |
| Battery | Limit charging to 80% | ASUS ATKACPI driver (`\\.\ATKACPI`, IOCTL `0x0022240C`, `DEVS` on device `0x00120057`), as in G-Helper | See below |

A section is hidden when its control is not available: brightness on desktops with external monitors only (WMI brightness is for built-in panels), volume without a playback device, a radio the PC does not have, and the battery limit on anything that is not an ASUS laptop with the ATKACPI driver.

## Bluetooth devices

Shown when the PC has a Bluetooth radio. When the radio is off, the section only says "Bluetooth is off".

- **Paired devices** (classic and LE) are read each time the panel opens and after every action, with name, connected or not, and a type icon (audio, input, phone, computer, other) from the classic Bluetooth major device class when it is computer, phone, audio/video or peripheral, otherwise from the endpoint's `System.Devices.Aep.Category` (headsets report `Communication.Headset.*` and count as audio). The query uses the system's "paired" selectors, which do not start an inquiry, so it takes well under a second.
- **Search for devices** watches for unpaired classic and LE devices, and each device shows up in the panel as soon as Windows finds it (the panel polls twice a second), in the order found. Devices without a name are not listed until a name arrives; devices that are already paired are not listed. The search stops by itself after 30 seconds, when Windows reports both enumerations complete, or with **Stop**. Tapping a found device stops the search, then pairs. Only one search runs at a time (pressing Search while one runs keeps it), none starts while a pairing runs, and starting a pairing stops a search first, since an inquiry and a pairing fight for the radio.
- **Pair**: tap a found device. Pairing runs in the panel, never through a Windows dialog (those would open behind Big Picture):
  - "Confirm only" requests are accepted automatically.
  - "Confirm PIN match": the panel shows the PIN with "PIN matches, pair" and "Cancel". Without an answer in 60 seconds, the request is refused.
  - "Display PIN": the panel shows the PIN to type on the device (keyboards), with "Cancel".
  - Devices that need a PIN typed in on this side, or a user name and password, are not supported yet; pairing stops with a message saying so.
  - A pairing attempt ends by itself after 120 seconds.
  - "Windows is still busy with an earlier pairing": a pairing that did not finish can leave Windows' pairing service stuck, and every new attempt fails at once. Turning Bluetooth off and on may clear it; if not, restart the PC.
- **Remove**: tap a paired device, then confirm.
- **Connect / Disconnect** are not offered. Windows has no public API to connect or disconnect a paired classic audio device (A2DP/HFP). `BluetoothSetServiceState` works by removing and reinstalling the device's drivers, so it is not used. An LE device stays connected only while some program holds it open. Paired devices reconnect by themselves when they are turned on; classic audio devices say so in their status line.

How pairing works: WinRT raises `PairingRequested` on a thread-pool thread, where a PowerShell script block cannot run, and `Register-ObjectEvent` returns before the request can be answered. So the pairing runs in a PowerShell helper process whose event handler is compiled in memory from `System.Linq.Expressions` (no compiler, no file on disk). The handler answers the request itself, on the event thread: Windows ignores an `Accept()` made later from another thread through the request's deferral, and the device then fails authentication. "Confirm only" and "Display PIN" are accepted at once. For "Confirm PIN match" the handler blocks its thread on a per-request `BlockingCollection<bool>` until the helper passes the user's answer, then accepts (or not); on cancel or the 60-second limit the helper passes "no", and the handler gives up by itself 10 seconds later in any case. Other kinds are refused. The helper talks to the plugin in JSON lines over stdin/stdout (`accept`, `reject`, `cancel`; closing stdin also cancels). A timer in the plugin kills a helper that has not finished 20 seconds after its own time limit. When a new pairing starts, the running helper is asked to cancel and killed if it has not exited within 6 seconds (3 seconds on plugin unload). Calls that start, cancel or dismiss a pairing run one at a time. The result of a finished pairing stays in the panel until you press OK.

How search works: the same kind of helper process. Two `DeviceWatcher`s over the unpaired selectors put their `Added`, `Updated`, `Removed` and `EnumerationCompleted` events on a queue through handlers compiled from expression trees; the helper writes one JSON line per device (again when an update changes its name or paired state), per removal and per completed enumeration, then `done`. It stops its watchers on `stop` or end of input on stdin, after 30 seconds, or when both enumerations completed. The plugin keeps one entry per device id. Stopping waits for the helper to exit (it takes well under a second) and kills it after 6 seconds (3 on plugin unload); the same backstop timer as for pairing kills a helper that runs 20 seconds past its limit.

## Wi-Fi networks

Shown when the PC has a Wi-Fi radio. When the radio is off, the section only says "Wi-Fi is off". Everything goes through `%SystemRoot%\System32\netsh.exe`, started directly (argument list, no shell, no window, 15-second time limit per call).

- **Current network** with its signal, and **Disconnect**.
- **Saved networks** (Windows WLAN profiles): tap one for **Connect** or **Forget** (Forget asks first). Profiles set by group policy can be connected but not forgotten.
- **Search for networks** lists nearby networks with signal bars and a lock when secured, and reads the list again every 3 seconds until **Stop search** or the panel closes (the plugin also drops a search nobody has polled for 15 seconds). netsh has no command to start a scan, so the list is the one Windows keeps from its own background scans. Networks keep the order they were first seen in. Networks with the same name show once, with the strongest signal.
- **Connect**: a saved network connects with its profile. An open network connects without a password. A secured network asks for the password (a text field, so the Steam on-screen keyboard works, with **Show password**). The plugin then writes a WLAN profile (WPA2-Personal, WPA3-Personal (SAE), WPA2/WPA3 transition, or open; the SSID also as hex, so any characters work) to a file in a new randomly named folder under the user's `%LOCALAPPDATA%`, runs `netsh wlan add profile filename=... user=current`, and deletes the file and folder in every case. It runs `netsh wlan connect` and waits up to 15 seconds for Windows to report the network as connected. The result shows "Connected", "Could not connect. Check the password." (the new profile is deleted again, so a wrong password does not stay saved), out of range, or the failure reason. The password is never logged, never put on a command line and never returned to the panel.
- **Not supported**: hidden networks; 802.1X / Enterprise networks ("Connect from Windows Settings"); WEP, WPA (version 1) and Enhanced Open (OWE) networks.
- **Location**: on current Windows 11, netsh may only read Wi-Fi information when Location is on for desktop apps. When it refuses (its message points to `ms-settings:privacy-location`), the panel says "Turn on Location in Windows Settings to search for networks"; saved networks can still be connected and forgotten.

Parsing: netsh's labels are translated on non-English Windows. The parser uses the layout ("key : value" lines, indentation, the dashed section underlines, the "SSID n" headers) plus the English keys, and falls back to values that are not translated (signal "NN%", authentication names such as "WPA2-Personal"). Status words ("connected", "authenticating") are read in English only; elsewhere "connected" is taken from the signal line, which netsh shows only while connected. Names are read verbatim, so spaces at either end, quotes, colons and emoji survive. netsh's output is read as UTF-8 (what current Windows 11 writes to a pipe), else in the console code page.

netsh mangles some double quotes in its arguments, so a name with a double quote never goes on a netsh command line: a new network with one gets a profile name with `'` instead and is connected by that name, and a saved profile with one must be connected or forgotten in Windows Settings. A profile named `*` cannot be forgotten here (`delete profile name=*` would delete every profile).

## Battery charge limit

The plugin checks once per start, read-only, whether the firmware has the charge-limit device: it sends `DSTS` for `0x00120057` and treats it as present when bit `0x00010000` is set (and the answer is not `0xFFFFFFFE`, "unknown method"). Only then does the toggle appear. A write (`DEVS`) happens only when you flip the toggle: on sets 80%, off sets 100%.

The firmware does not report the current limit, so the plugin remembers the last value it set. Many ASUS models reset the limit to 100% after a restart; set it again after a restart if needed.

Check a machine by hand, read-only (prints JSON; `reason` is `ok`, `not_present`, `no_driver`, `access_denied` or `error`):

```
python -I py_modules\wqs\asus_acpi.py --probe
```

The plugin does not set the `root` flag. If the probe reports `access_denied` without administrator rights but works with them, add `"root"` to `flags` in `plugin.json`; in Decky's installed mode the plugin then runs with administrator rights.

## Build

The frontend is built with the packages from a Decky Loader checkout; nothing is installed. Link them, then build:

```
New-Item -ItemType Junction -Path node_modules -Target <decky-loader>\frontend\node_modules
npm run typecheck
npm run build
```

This writes `dist/index.js`. A plugin folder for Decky contains `plugin.json`, `package.json`, `main.py`, `py_modules` and `dist`.

## Tests

The backend helpers take injected runners (and the pairing session an injected helper process), so the tests never touch the real brightness, volume, radios, Wi-Fi, Bluetooth devices or ACPI device (the Wi-Fi tests answer netsh calls with recorded-style output):

```
python -m pytest -q tests
```

On Windows, one test also compiles the real pairing handler against stub types (`tests/pair_handler_stub.ps1`, built with `Add-Type`) and fires it on thread-pool threads, and another parses (without running) the search and pairing helper scripts with PowerShell's parser; neither touches Bluetooth.
