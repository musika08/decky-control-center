# Windows Quick Settings

A Decky Loader plugin for Windows that puts common system controls in the Quick Access menu:

| Section | Control | How | Admin |
| --- | --- | --- | --- |
| Display | Brightness slider | WMI `root\wmi` `WmiMonitorBrightness` (read) and `WmiMonitorBrightnessMethods.WmiSetBrightness` (write), through Windows PowerShell | No |
| Sound | Volume slider, Mute | Core Audio `IAudioEndpointVolume` of the default playback device, called from Python with `ctypes` (no comtypes/pywin32) | No |
| Wireless | Wi-Fi, Bluetooth on/off | WinRT `Windows.Devices.Radios` through Windows PowerShell 5.1 | No |
| Battery | Limit charging to 80% | ASUS ATKACPI driver (`\\.\ATKACPI`, IOCTL `0x0022240C`, `DEVS` on device `0x00120057`), as in G-Helper | See below |

A section is hidden when its control is not available: brightness on desktops with external monitors only (WMI brightness is for built-in panels), volume without a playback device, a radio the PC does not have, and the battery limit on anything that is not an ASUS laptop with the ATKACPI driver.

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

The backend helpers take injected runners, so the tests never touch the real brightness, volume, radios or ACPI device:

```
python -m pytest -q tests
```
