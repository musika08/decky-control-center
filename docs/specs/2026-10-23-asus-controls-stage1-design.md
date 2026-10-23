# ASUS controls in Windows Quick Settings, Stage 1: design

Date: 2026-10-23. Status: approved in conversation, awaiting owner review of this written spec.

## 1. Purpose

Bring the ROG Ally's hardware controls into the Decky panel (Ctrl+2 / Quick Access, Decky tab, Windows Quick Settings
plugin), so the owner can tune performance from Steam Big Picture without Armoury Crate. The controls are based on
G-Helper (https://github.com/seerge/g-helper, GPL-3.0, by seerge and contributors). The plugin was relicensed to
GPL-3.0 on 2026-10-23 for this (commit 5078bc5).

Owner decisions (2026-10-23):
- Features wanted, in three stages:
  - Stage 1 (this spec): performance mode, power limits (TDP), FPS limit, temperature/fan/power readout, fan curves.
  - Stage 2: Auto TDP, CPU temperature limit, AMD undervolting (needs the PawnIO driver; decide then).
  - Stage 3: controller mode, button remap, deadzones, vibration, Xbox controller off, stick LEDs, refresh rate.
- The plugin replaces Armoury Crate SE on the Ally (G-Helper does the same).
- Hardware access through a helper program built from G-Helper's own C# code (approach A).
- Each stage is designed, built, reviewed and tested on the Ally before the next.

Devices: ROG Ally family only. G-Helper treats any model containing "RC7" as an Ally (`AppConfig.IsAlly`); the
owner's device reports SystemProductName "ROG Xbox Ally X RC73XA_RC73XA". On other devices the ASUS section is hidden.

## 2. Architecture

```
Decky panel (src/index.tsx, ASUS section)
   | callable methods (main.py)
Python backend (py_modules/wqs/asus_controls.py)
   | one long-lived child process, JSON lines over stdin/stdout
asus-helper.exe (helper/, C#, built with the in-box csc.exe like the dashboard)
   | DeviceIoControl on \\.\ATKACPI (G-Helper AsusACPI.cs)
   | AMD ADL (G-Helper AmdAdl2.cs / AmdGpuControl.cs) for the FPS limit
   | service control for Armoury Crate (G-Helper AsusService.cs service list)
```

- **asus-helper.exe** lives in the plugin folder (`bin\asus-helper.exe`). Source: G-Helper files copied with their
  copyright headers kept and a "Modified for Windows Quick Settings" note, plus our own `HelperMain.cs` message loop.
  Built by a `helper\build.ps1` (csc 4.8, C# 5 features only; G-Helper's newer C# syntax is rewritten where needed).
- **Protocol:** one JSON object per line. Request `{"id":n,"cmd":"name","args":{...}}`, reply
  `{"id":n,"ok":bool,"message":"","data":...}`. Fixed command list only: `hello`, `status`, `sensors`, `set_mode`,
  `set_power`, `get_fans`, `set_fans`, `reset_fans`, `get_fps_limit`, `set_fps_limit`, `armoury_state`,
  `armoury_stop`, `armoury_restore`. Unknown commands and unknown args are refused.
- **The helper validates everything itself** (section 3); the Python side validates too, but the helper never trusts it.
- **Battery charge limit stays as it is** (Python `asus_acpi.py`, already verified on the Ally); the helper does not
  touch it.
- **Process:** started by the plugin on load, restarted on exit with backoff (1 s, 2 s, 4 s, max 5 tries per 10 min);
  after that the section shows "ASUS controls unavailable" with the last error. Stopped on plugin unload.

## 3. Features and limits

| Feature | Hardware call (from G-Helper) | Limits enforced by the helper |
|---|---|---|
| Performance mode | DEVS PerformanceMode 0x00120075: Balanced 0, Turbo 1, Silent 2 (Manual = Balanced + custom limits) | only those values |
| Power limits | SPL PPT_APUA3 0x001200A3, sPPT PPT_APUA0 0x001200A0, fPPT PPT_APUC1 0x001200C1 | 5..50 W each (G-Helper Ally MaxTotal 50, default 30); fPPT >= sPPT >= SPL, corrected before apply |
| Fan curve | SetFanCurve/GetFanCurve, CPU fan (G-Helper Fans/AsusACPI) | 8 points; temperatures strictly rising; fan % never decreasing; the per-point minimums G-Helper's fan editor enforces, copied with its code; presets: Armoury default (read back from the device at first run and stored), Quiet, Cool |
| FPS limit | AMD ADL FRTC via G-Helper AmdGpuControl Get/SetFPSLimit | 0 (off) or one of 30, 40, 45, 50, 60, 75, 90, 120 |
| Sensors | DSTS Temp_CPU 0x00120094, CPU_Fan 0x00110013; package power only if the copied G-Helper code reads it (otherwise the readout shows temp and fan only) | read-only, every 2 s while the panel is open, stopped when closed |

Exact byte layouts and conversions are taken from the G-Helper source at the commit copied (recorded in
`helper/GHELPER_COMMIT`), never re-derived from memory.

## 4. Panel (Quick Settings, new "ASUS" section)

- Mode: Silent / Balanced / Turbo / Manual. Manual shows three power sliders (Sustained, Slow boost, Fast boost) with
  watt labels.
- FPS limit: Off, 30, 40, 45, 50, 60, 75, 90, 120 (G-Helper's steps up to 120).
- Fans: preset picker plus "Edit curve" (8 points, D-pad friendly steppers per point, not drag-only).
- Live line: CPU temp, fan RPM, package watts (only while the section is visible).
- Switch "Use these ASUS controls instead of Armoury Crate" (hidden when Armoury Crate is not installed).
- "Reset to Armoury Crate defaults": Balanced, factory limits, stored default fan curve, FPS limit off.
- Every control works with the controller (Decky's focus system) and shows success/failure inline.

## 5. Saving and reapplying

- Settings in the plugin's settings file (Decky settings dir): mode, three limits, fan curve + preset, FPS limit,
  Armoury replacement on/off, and the recorded Armoury service start types.
- Reapplied: on plugin load, after resume from sleep, and on AC plug/unplug (both reset power limits on the Ally).
- Applying is idempotent and logged; a failed reapply shows a warning in the section, keeps the stored values.

## 6. Replacing Armoury Crate

- Off by default. Turning it on: record each listed service's start type and state, stop the services in G-Helper's
  list (`AsusService.cs` services + servicesAC, AC ones set to Manual, others Disabled exactly as G-Helper does), then
  apply our stored settings.
- Turning it off: restore each service's recorded start type and start the ones that were running.
- The Windows dashboard (steam-home-app) shows on its Armoury tile: "ASUS controls in Quick Settings are managing the
  device" while this is on, and hides the "Armoury Crate after Steam" toggle. (Small dashboard change, separate commit.)
- Decky runs elevated, so service control and the ATKACPI device need no extra prompt.

## 7. Errors

- Driver unreachable / call refused: inline message, previous value kept, nothing else changed.
- Helper crash: automatic restart (section 2); requests in flight return "ASUS helper restarted, try again".
- Not an Ally: section hidden; helper not started.
- Armoury service stop/start failure: switch returns to its previous state, message lists the failing service.

## 8. Testing

- C# helper: unit tests of validation and protocol with a fake ACPI/ADL/service layer (no hardware), run by
  `helper\build.ps1 -Tests`.
- Python: pytest for the client (request ids, restarts, backoff, settings save/reapply, AC/resume triggers) with a
  fake helper process.
- Frontend: build check plus the existing test style.
- Hardware probe on the Ally: `asus-helper.exe --probe` reads (never writes) mode, limits, fan curve, FPS limit and
  sensors and prints them; run before any write is enabled.
- Owner live checks: each mode, Manual limits (verify with the sensor watts under load), FPS limit in a game, a fan
  curve, sleep/wake and AC plug reapply, Armoury replacement on and off (services restored).

## 9. Licensing

- Plugin: GPL-3.0-only (LICENSE from gnu.org gpl-3.0.txt).
- Copied G-Helper files keep their headers; THIRD-PARTY-NOTICES in the plugin lists "Includes code from G-Helper by
  seerge and contributors (GPL-3.0)" with the repo URL and the copied commit.
- The plugin is not named G-Helper and does not imply endorsement.

## 10. Out of scope (Stage 1)

Auto TDP, CPU temperature limit, undervolting, controller/remap/deadzones/vibration, LEDs, refresh rate, battery
limit changes, non-Ally devices.
