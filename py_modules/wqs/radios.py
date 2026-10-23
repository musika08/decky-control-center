"""Wi-Fi and Bluetooth radios through WinRT Windows.Devices.Radios, via Windows PowerShell 5.1. No admin needed.

This switches the radio itself (like the Windows Quick Settings tiles); it does not touch airplane mode or adapters.
"""
import json
from typing import Optional

from .runner import Runner, run_powershell

KINDS = {"wifi": "WiFi", "bluetooth": "Bluetooth"}

PRELUDE = r"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($op, [Type]$type) {
  $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
  if (-not $task.Wait(10000)) { throw 'WinRT call timed out' }
  $task.Result
}
[void][Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType = WindowsRuntime]
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
"""

GET_SCRIPT = PRELUDE + r"""$list = @($radios | ForEach-Object { [pscustomobject]@{ kind = $_.Kind.ToString(); state = $_.State.ToString(); name = $_.Name } })
ConvertTo-Json -Compress -InputObject $list"""

SET_SCRIPT = PRELUDE + r"""$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
if ($access.ToString() -ne 'Allowed') { 'access:' + $access; return }
$targets = @($radios | Where-Object { $_.Kind.ToString() -eq '%(kind)s' })
if ($targets.Count -eq 0) { 'none'; return }
$results = foreach ($r in $targets) {
  (Await ($r.SetStateAsync([Windows.Devices.Radios.RadioState]::%(state)s)) ([Windows.Devices.Radios.RadioAccessStatus])).ToString()
}
'result:' + (@($results) -join ',')"""


def _radio_state(radios: list[dict], winrt_kind: str) -> dict:
    mine = [r for r in radios if r.get("kind") == winrt_kind]
    if not mine:
        return {"present": False, "on": False}
    # Disabled = turned off in Device Manager; it cannot be switched from here.
    return {"present": True, "on": any(r.get("state") == "On" for r in mine),
            "disabled": all(r.get("state") == "Disabled" for r in mine)}


def get_radios(runner: Optional[Runner] = None) -> dict:
    """{"wifi": {present, on, disabled}, "bluetooth": {...}}"""
    out = run_powershell(GET_SCRIPT, runner)
    data = json.loads(out) if out else []
    if isinstance(data, dict):  # a one-item array can still come back as an object
        data = [data]
    return {key: _radio_state(data, kind) for key, kind in KINDS.items()}


def set_radio(kind: str, on: bool, runner: Optional[Runner] = None) -> dict:
    """Switch every radio of `kind` ("wifi" or "bluetooth"). Returns {"ok": bool, "detail": str}."""
    if kind not in KINDS:
        raise ValueError(f"unknown radio kind {kind!r}")
    out = run_powershell(SET_SCRIPT % {"kind": KINDS[kind], "state": "On" if on else "Off"}, runner)
    last = out.splitlines()[-1] if out else ""
    if last.startswith("result:"):
        results = last[len("result:"):].split(",")
        return {"ok": all(r == "Allowed" for r in results), "detail": last[len("result:"):]}
    return {"ok": False, "detail": last or "no output"}
