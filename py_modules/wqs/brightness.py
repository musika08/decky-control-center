"""Built-in display brightness through WMI (root\\wmi WmiMonitorBrightness), via Windows PowerShell. No admin needed.

External monitors usually have no WMI brightness (that needs DDC/CI), so "no instance" means unsupported.
"""
from typing import Optional

from .runner import Runner, run_powershell

GET_SCRIPT = """$ErrorActionPreference = 'Stop'
$b = @(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness -ErrorAction SilentlyContinue | Where-Object { $_.Active })
if ($b.Count -eq 0) { 'none' } else { [int]$b[0].CurrentBrightness }"""

SET_SCRIPT = """$ErrorActionPreference = 'Stop'
$m = @(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods -ErrorAction SilentlyContinue | Where-Object { $_.Active })
if ($m.Count -eq 0) { 'none' } else {
  $m | Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{ Timeout = [uint32]0; Brightness = [byte]%d } | Out-Null
  'ok'
}"""


def get_brightness(runner: Optional[Runner] = None) -> Optional[int]:
    """Current brightness 0-100, or None when no display exposes WMI brightness."""
    out = run_powershell(GET_SCRIPT, runner)
    if out == "none":
        return None
    return int(out.splitlines()[-1])


def set_brightness(value: int, runner: Optional[Runner] = None) -> bool:
    """Set brightness (clamped to 0-100). False when no display exposes WMI brightness."""
    level = max(0, min(100, int(value)))
    return run_powershell(SET_SCRIPT % level, runner) == "ok"
