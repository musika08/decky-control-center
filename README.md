# Windows Quick Settings

A Decky Loader plugin for Windows that puts common system controls in the Quick Access menu:

| Section | Control | How | Admin |
| --- | --- | --- | --- |
| Display | Brightness slider | WMI `root\wmi` `WmiMonitorBrightness` (read) and `WmiMonitorBrightnessMethods.WmiSetBrightness` (write), through Windows PowerShell | No |
| Sound | Volume slider, Mute | Core Audio `IAudioEndpointVolume` of the default playback device, called from Python with `ctypes` (no comtypes/pywin32) | No |
| Wireless | Wi-Fi, Bluetooth on/off | WinRT `Windows.Devices.Radios` through Windows PowerShell 5.1 | No |
| Bluetooth devices | Paired list, Search, Pair, Remove | WinRT `Windows.Devices.Enumeration` (association endpoints, `DeviceWatcher`, `DeviceInformationCustomPairing`, `UnpairAsync`) through Windows PowerShell 5.1 | No |
| Battery | Limit charging to 80% | ASUS ATKACPI driver (`\\.\ATKACPI`, IOCTL `0x0022240C`, `DEVS` on device `0x00120057`), as in G-Helper | See below |

A section is hidden when its control is not available: brightness on desktops with external monitors only (WMI brightness is for built-in panels), volume without a playback device, a radio the PC does not have, and the battery limit on anything that is not an ASUS laptop with the ATKACPI driver.

## Bluetooth devices

Shown when the PC has a Bluetooth radio. When the radio is off, the section only says "Bluetooth is off".

- **Paired devices** (classic and LE) are read each time the panel opens and after every action, with name, connected or not, and a type icon (audio, input, phone, computer, other) from the endpoint's `System.Devices.Aep.Category`, falling back to the classic major device class. The query uses the system's "paired" selectors, which do not start an inquiry, so it takes well under a second.
- **Search for devices** watches for unpaired classic and LE devices for 12 seconds. Devices without a name are not listed.
- **Pair**: tap a found device. Pairing runs in the panel, never through a Windows dialog (those would open behind Big Picture):
  - "Confirm only" requests are accepted automatically.
  - "Confirm PIN match": the panel shows the PIN with "PIN matches, pair" and "Cancel". Without an answer in 60 seconds, the request is refused.
  - "Display PIN": the panel shows the PIN to type on the device (keyboards), with "Cancel".
  - Devices that need a PIN typed in on this side, or a user name and password, are not supported yet; pairing stops with a message saying so.
  - A pairing attempt ends by itself after 120 seconds.
- **Remove**: tap a paired device, then confirm.
- **Connect / Disconnect** are not offered. Windows has no public API to connect or disconnect a paired classic audio device (A2DP/HFP). `BluetoothSetServiceState` works by removing and reinstalling the device's drivers, so it is not used. An LE device stays connected only while some program holds it open. Paired devices reconnect by themselves when they are turned on; classic audio devices say so in their status line.

How pairing works: WinRT raises `PairingRequested` on a thread-pool thread, where a PowerShell script block cannot run, and `Register-ObjectEvent` returns before the pairing deferral can be taken. So the pairing runs in a PowerShell helper process whose event handlers are compiled in memory from `System.Linq.Expressions` (no compiler, no file on disk). The handler takes the deferral and queues the request; the helper answers it and talks to the plugin in JSON lines over stdin/stdout (`accept`, `reject`, `cancel`; closing stdin also cancels). A timer in the plugin kills a helper that has not finished 20 seconds after its own time limit. When a new pairing starts, the running helper is asked to cancel and killed if it has not exited within 6 seconds (3 seconds on plugin unload). Calls that start, cancel or dismiss a pairing run one at a time. The result of a finished pairing stays in the panel until you press OK.

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

The backend helpers take injected runners (and the pairing session an injected helper process), so the tests never touch the real brightness, volume, radios, Bluetooth devices or ACPI device:

```
python -m pytest -q tests
```
