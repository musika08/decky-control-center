"""Bluetooth devices through WinRT Windows.Devices.Enumeration, via Windows PowerShell 5.1. No admin needed.

- Paired list: DeviceInformation.FindAllAsync over the association endpoints (AEP) of the classic and LE Bluetooth
  protocols, with the system's own "paired" selectors (they do not start an inquiry, so this takes well under a second).
- Search: two DeviceWatchers over the "unpaired" selectors (these do start an inquiry) in a long-lived PowerShell
  helper that reports each device as a JSON line the moment Windows finds it (ScanSession).
- Pairing: DeviceInformationCustomPairing.PairAsync in a long-lived PowerShell helper that speaks JSON lines
  (PairingSession).
- Remove: DeviceInformationPairing.UnpairAsync.

WinRT events (DeviceWatcher.Added, PairingRequested) fire on thread-pool threads. A PowerShell script block cannot run
there (no runspace), and Register-ObjectEvent queues the event and returns at once. So the handlers are built with
System.Linq.Expressions and compiled in memory, and put the event on a ConcurrentQueue that the script drains on its own
thread. Nothing is written to disk and no compiler runs.

Pairing requests are answered inside the handler, on the event thread: an Accept() made later from the script's thread
(through the request's deferral) is ignored by Windows, and the device then fails authentication. "Confirm only" and
"Display PIN" are accepted at once. For "Confirm PIN match" the handler blocks its thread on a per-request
BlockingCollection until the script passes the user's answer (or false on cancel / time limit), then accepts there.
"""
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional, Protocol

from .runner import CREATE_NO_WINDOW, Runner, powershell_argv, run_powershell

SCAN_TIMEOUT = 30  # the search helper stops on its own after this many seconds
PAIR_TIMEOUT = 120  # the helper gives up on its own after this many seconds
PIN_TIMEOUT = 60  # how long the helper waits for Confirm/Cancel on a PIN
KILL_GRACE = 20  # extra seconds before Python kills a helper that did not exit by itself
CLOSE_GRACE = 6  # seconds a replaced helper gets to cancel cleanly before it is killed
UNLOAD_GRACE = 3  # the same on plugin unload (Decky stops the plugin host after 5 s)

# Association endpoint ids as Windows reports them, e.g. Bluetooth#Bluetooth48:45:e6:0b:c7:0e-2c:8d:48:2f:a2:a9.
# Only these are ever put into a script.
_MAC = r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}"
DEVICE_ID = re.compile(rf"Bluetooth(?:LE)?#Bluetooth(?:LE)?{_MAC}-{_MAC}")  # used with fullmatch

PRELUDE = r"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function AsTask($op, [Type]$type) { $asTask.MakeGenericMethod($type).Invoke($null, @($op)) }
# PowerShell cannot call Cancel() on the WinRT operation's COM wrapper, so cancellable waits go through
# AsTask(operation, CancellationToken) and cancel the token instead.
$asTaskCt = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 2 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' -and
  $_.GetParameters()[1].ParameterType -eq [System.Threading.CancellationToken]
} | Select-Object -First 1
function AsTaskCancellable($op, [Type]$type, [System.Threading.CancellationToken]$token) { $asTaskCt.MakeGenericMethod($type).Invoke($null, @($op, $token)) }
function Await($op, [Type]$type) {
  $task = AsTask $op $type
  if (-not $task.Wait(15000)) { throw 'WinRT call timed out' }
  $task.Result
}
[void][Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
[void][Windows.Devices.Bluetooth.BluetoothDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
[void][Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
$DI = [Windows.Devices.Enumeration.DeviceInformation]
$AEP = [Windows.Devices.Enumeration.DeviceInformationKind]::AssociationEndpoint
# PowerShell unrolls the WinRT property map into key/value pairs, so look the key up by walking them.
function Prop($d, [string]$key) { foreach ($kv in $d.Properties) { if ($kv.Key -eq $key) { return $kv.Value } } }
function PropList([string[]]$names) {
  $l = New-Object 'System.Collections.Generic.List[string]'
  foreach ($n in $names) { $l.Add($n) }
  ,$l
}
$props = PropList @('System.Devices.Aep.IsPaired', 'System.Devices.Aep.IsConnected', 'System.Devices.Aep.Category',
  'System.Devices.Aep.Bluetooth.Cod.Major')
function Describe($d) {
  [pscustomobject]@{
    id = $d.Id; name = $d.Name; paired = [bool](Prop $d 'System.Devices.Aep.IsPaired')
    connected = [bool](Prop $d 'System.Devices.Aep.IsConnected')
    category = @(Prop $d 'System.Devices.Aep.Category') -join ','
    major = Prop $d 'System.Devices.Aep.Bluetooth.Cod.Major'
    le = $d.Id.StartsWith('BluetoothLE#')
  }
}
"""

LIST_SCRIPT = PRELUDE + r"""$found = foreach ($sel in @([Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState($true),
                            [Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelectorFromPairingState($true))) {
  $all = Await ($DI::FindAllAsync($sel, $props, $AEP)) ([Windows.Devices.Enumeration.DeviceInformationCollection])
  foreach ($d in $all) { Describe $d }
}
ConvertTo-Json -Compress -InputObject @($found | Where-Object { $_.paired })"""

# Handlers compiled from expression trees: (sender, args) => queue.Enqueue(new object[] { tag, args }).
EXPRESSIONS = r"""$E = [System.Linq.Expressions.Expression]
$queue = New-Object 'System.Collections.Concurrent.ConcurrentQueue[object]'
$enqueue = $queue.GetType().GetMethod('Enqueue')
function New-QueueHandler([Type]$delegateType, [string]$tag) {
  $ps = $delegateType.GetMethod('Invoke').GetParameters()
  $s = $E::Parameter($ps[0].ParameterType, 's')
  $a = $E::Parameter($ps[1].ParameterType, 'a')
  $item = $E::NewArrayInit([object], [System.Linq.Expressions.Expression[]]@($E::Constant($tag, [object]), $E::Convert($a, [object])))
  $body = $E::Call($E::Constant($queue), $enqueue, [System.Linq.Expressions.Expression[]]@($item))
  $E::Lambda($delegateType, $body, [System.Linq.Expressions.ParameterExpression[]]@($s, $a)).Compile()
}
"""

# Long-lived search helper. Two DeviceWatchers over the "unpaired" selectors (these start an inquiry). Writes one JSON
# object per line on stdout as devices come and go: {event: device, id, name, ...} on Added and on an Updated that
# changes the name or paired state, {event: removed, id}, {event: completed, watcher} per EnumerationCompleted, and
# {event: done, reason} last. Stops when both watchers completed, on "stop" or end of input on stdin, or after
# SCAN_TIMEOUT seconds.
SCAN_SCRIPT = r"""function Emit($o) { [Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject $o)); [Console]::Out.Flush() }
$watchers = @(); $reason = 'error'
try {
""" + PRELUDE + EXPRESSIONS + r"""
  $deadline = [DateTime]::UtcNow.AddSeconds(%(timeout)d)
  $tInfo = [Windows.Foundation.TypedEventHandler[Windows.Devices.Enumeration.DeviceWatcher, Windows.Devices.Enumeration.DeviceInformation]]
  $tUpdate = [Windows.Foundation.TypedEventHandler[Windows.Devices.Enumeration.DeviceWatcher, Windows.Devices.Enumeration.DeviceInformationUpdate]]
  $tDone = [Windows.Foundation.TypedEventHandler[Windows.Devices.Enumeration.DeviceWatcher, object]]
  $sources = @(@('classic', [Windows.Devices.Bluetooth.BluetoothDevice]::GetDeviceSelectorFromPairingState($false)),
               @('le', [Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelectorFromPairingState($false)))
  $watchers = @(foreach ($src in $sources) {
    $w = $DI::CreateWatcher($src[1], $props, $AEP)
    [void]$w.add_Added((New-QueueHandler $tInfo 'added'))
    [void]$w.add_Updated((New-QueueHandler $tUpdate 'updated'))
    [void]$w.add_Removed((New-QueueHandler $tUpdate 'removed'))
    [void]$w.add_EnumerationCompleted((New-QueueHandler $tDone ('completed:' + $src[0])))
    $w
  })
  $seen = @{}; $sent = @{}; $completed = @{}
  function Send-Device($d) {
    $o = Describe $d
    $key = '{0}|{1}' -f $o.name, $o.paired
    if ($sent[$d.Id] -ne $key) {
      $sent[$d.Id] = $key
      Emit @{ event = 'device'; id = $o.id; name = $o.name; paired = $o.paired; category = $o.category; major = $o.major; le = $o.le }
    }
  }
  function Drain {
    $item = $null
    while ($queue.TryDequeue([ref]$item)) {
      $tag = [string]$item[0]; $a = $item[1]
      if ($tag -eq 'added') { $seen[$a.Id] = $a; Send-Device $a }
      elseif ($tag -eq 'updated') { if ($seen.ContainsKey($a.Id)) { $seen[$a.Id].Update($a); Send-Device $seen[$a.Id] } }
      elseif ($tag -eq 'removed') {
        if ($seen.ContainsKey($a.Id)) { $seen.Remove($a.Id); $sent.Remove($a.Id); Emit @{ event = 'removed'; id = $a.Id } }
      } else {
        $name = $tag.Substring(10); $completed[$name] = $true; Emit @{ event = 'completed'; watcher = $name }
      }
    }
  }
  foreach ($w in $watchers) { $w.Start() }
  Emit @{ event = 'scanning' }
  $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput())
  $line = $reader.ReadLineAsync()
  while ($true) {
    Drain
    if ($completed.Count -ge $watchers.Count) { $reason = 'completed'; break }
    if ($line.IsCompleted) {
      if ($null -eq $line.Result) { $reason = 'closed'; break }
      if ($line.Result.Trim() -eq 'stop') { $reason = 'stopped'; break }
      $line = $reader.ReadLineAsync()
    }
    if ([DateTime]::UtcNow -gt $deadline) { $reason = 'timeout'; break }
    Start-Sleep -Milliseconds 100
  }
} catch {
  Emit @{ event = 'error'; message = $_.Exception.Message }
} finally {
  foreach ($w in $watchers) { try { $w.Stop() } catch { } }
  Emit @{ event = 'done'; reason = $reason }
}"""

UNPAIR_SCRIPT = PRELUDE + r"""$d = Await ($DI::CreateFromIdAsync('%(id)s', $props, $AEP)) ([Windows.Devices.Enumeration.DeviceInformation])
if (-not $d.Pairing.IsPaired) { 'result:NotPaired'; return }
$r = Await ($d.Pairing.UnpairAsync()) ([Windows.Devices.Enumeration.DeviceUnpairingResult])
'result:' + $r.Status"""

# Pairing handler, compiled from an expression tree. It works for any delegate (sender, args) whose args have PairingKind,
# Pin and Accept(), so the tests can drive it with stub types:
#   (s, a) => {
#     BlockingCollection<bool> box = null; bool decision = false;
#     if (a.PairingKind == ConfirmPinMatch) box = new BlockingCollection<bool>();
#     if (a.PairingKind == ConfirmOnly || a.PairingKind == DisplayPin) a.Accept();
#     queue.Enqueue(new object[] { a.PairingKind, a.Pin, box });
#     if (box != null && box.TryTake(out decision, waitMs) && decision) a.Accept();
#   }
# Other kinds (ProvidePin, ...) return without Accept(), which refuses them.
PAIR_HANDLER = r"""function New-PairingHandler([Type]$delegateType, [int]$waitMs) {
  $ps = $delegateType.GetMethod('Invoke').GetParameters()
  $argsT = $ps[1].ParameterType
  $s = $E::Parameter($ps[0].ParameterType, 's')
  $a = $E::Parameter($argsT, 'a')
  $kindT = $argsT.GetProperty('PairingKind').PropertyType
  $kind = $E::Property($a, 'PairingKind')
  function KindIs([string]$name) { $E::Equal($kind, $E::Constant([Enum]::Parse($kindT, $name), $kindT)) }
  $accept = $E::Call($a, $argsT.GetMethod('Accept', [Type[]]@()))
  $boxT = [System.Collections.Concurrent.BlockingCollection[bool]]
  # $E::New would be read as PowerShell's constructor syntax, so call Expression.New(Type) by reflection.
  $newBox = $E.GetMethod('New', [Type[]]@([Type])).Invoke($null, @($boxT))
  $box = $E::Variable($boxT, 'box')
  $decision = $E::Variable([bool], 'decision')
  $tryTake = $boxT.GetMethod('TryTake', [Type[]]@([bool].MakeByRefType(), [int]))
  $item = $E::NewArrayInit([object], [System.Linq.Expressions.Expression[]]@($E::Convert($kind, [object]),
    $E::Convert($E::Property($a, 'Pin'), [object]), $E::Convert($box, [object])))
  $answered = $E::AndAlso($E::Call($box, $tryTake, [System.Linq.Expressions.Expression[]]@($decision, $E::Constant($waitMs, [int]))), $decision)
  $body = $E::Block([System.Linq.Expressions.ParameterExpression[]]@($box, $decision), [System.Linq.Expressions.Expression[]]@(
    $E::IfThen((KindIs 'ConfirmPinMatch'), $E::Assign($box, $newBox)),
    $E::IfThen($E::OrElse((KindIs 'ConfirmOnly'), (KindIs 'DisplayPin')), $accept),
    $E::Call($E::Constant($queue), $enqueue, [System.Linq.Expressions.Expression[]]@($item)),
    $E::IfThen($E::AndAlso($E::NotEqual($box, $E::Constant($null, $boxT)), $answered), $accept)))
  $E::Lambda($delegateType, $body, [System.Linq.Expressions.ParameterExpression[]]@($s, $a)).Compile()
}
"""

# Long-lived pairing helper. Reads one command per line on stdin (accept, reject, cancel; end of input = cancel) and
# writes one JSON object per line on stdout. It always ends by itself within PAIR_TIMEOUT seconds.
PAIR_SCRIPT = r"""function Emit($o) { [Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject $o)); [Console]::Out.Flush() }
$custom = $null; $token = $null; $op = $null; $pending = $null
try {
""" + PRELUDE + EXPRESSIONS + PAIR_HANDLER + r"""
  $deadline = [DateTime]::UtcNow.AddSeconds(%(timeout)d)
  $d = Await ($DI::CreateFromIdAsync('%(id)s', $props, $AEP)) ([Windows.Devices.Enumeration.DeviceInformation])
  if ($d.Pairing.IsPaired) { Emit @{ event = 'result'; status = 'AlreadyPaired' }; return }
  $tReq = [Windows.Foundation.TypedEventHandler[Windows.Devices.Enumeration.DeviceInformationCustomPairing, Windows.Devices.Enumeration.DevicePairingRequestedEventArgs]]
  # The handler waits a little longer than this script does, so the script's own time limit always answers first.
  $handler = New-PairingHandler $tReq ((%(pin_timeout)d + 10) * 1000)
  $custom = $d.Pairing.Custom
  $token = $custom.add_PairingRequested($handler)
  $kinds = [Windows.Devices.Enumeration.DevicePairingKinds]'ConfirmOnly, DisplayPin, ProvidePin, ConfirmPinMatch'
  $op = $custom.PairAsync($kinds)
  $cts = New-Object System.Threading.CancellationTokenSource
  $task = AsTaskCancellable $op ([Windows.Devices.Enumeration.DevicePairingResult]) $cts.Token
  Emit @{ event = 'pairing' }
  $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput())
  $line = $reader.ReadLineAsync()
  $cancelled = $null
  while (-not $task.IsCompleted) {
    $cmd = ''
    if ($null -ne $line -and $line.IsCompleted) {
      if ($null -eq $line.Result) { $cmd = 'cancel'; $line = $null } else { $cmd = $line.Result.Trim(); $line = $reader.ReadLineAsync() }
    }
    if ($cmd -eq 'cancel' -or [DateTime]::UtcNow -gt $deadline) {
      $cancelled = $(if ($cmd -eq 'cancel') { 'user' } else { 'timeout' })
      # Release a handler that waits for an answer, so no event thread stays blocked.
      if ($pending) { $pending.box.Add($false); $pending = $null }
      $cts.Cancel()
      # Task.Wait would throw on a cancelled task; WaitAny only waits.
      [void][System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]@($task), 5000)
      break
    }
    if ($pending) {
      if ($cmd -eq 'accept') {
        $pending.box.Add($true); $pending = $null; Emit @{ event = 'accepted' }
      } elseif ($cmd -eq 'reject' -or [DateTime]::UtcNow -gt $pending.until) {
        $pending.box.Add($false); $pending = $null
        Emit @{ event = 'rejected'; reason = $(if ($cmd -eq 'reject') { 'user' } else { 'timeout' }) }
      }
    }
    $req = $null
    if ($queue.TryDequeue([ref]$req)) {
      $kind = $req[0].ToString(); $pin = [string]$req[1]
      if ($kind -eq 'ConfirmOnly') {
        Emit @{ event = 'accepted'; kind = $kind }  # already accepted by the handler
      } elseif ($kind -eq 'DisplayPin') {
        # Accepted by the handler; the PIN is typed on the other device (as in Microsoft's custom pairing sample).
        Emit @{ event = 'pin'; kind = $kind; pin = $pin }
      } elseif ($kind -eq 'ConfirmPinMatch') {
        if ($pending) { $pending.box.Add($false) }
        $pending = @{ box = $req[2]; until = [DateTime]::UtcNow.AddSeconds(%(pin_timeout)d) }
        Emit @{ event = 'pin'; kind = $kind; pin = $pin }
      } else {
        Emit @{ event = 'unsupported'; kind = $kind }  # refused by the handler
      }
    }
    Start-Sleep -Milliseconds 100
  }
  # Our own cancel or time limit first, whatever the task ended as (Canceled, or a PairingCanceled result).
  if ($cancelled) { Emit @{ event = 'cancelled'; reason = $cancelled } }
  elseif ($task.IsCanceled) { Emit @{ event = 'cancelled'; reason = 'system' } }
  elseif ($task.IsFaulted) { Emit @{ event = 'error'; message = $task.Exception.InnerException.Message } }
  else {
    $r = $task.Result
    Emit @{ event = 'result'; status = $r.Status.ToString(); protection = $r.ProtectionLevelUsed.ToString() }
  }
} catch {
  Emit @{ event = 'error'; message = $_.Exception.Message }
} finally {
  if ($pending) { try { $pending.box.Add($false) } catch { } }
  if ($null -ne $token) { try { $custom.remove_PairingRequested($token) } catch { } }
}"""


def _check_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        raise ValueError(f"not a Bluetooth device id: {device_id!r}")
    return device_id


def device_type(category: str, major: Any = None) -> str:
    """audio / input / phone / computer / other. The classic Bluetooth major device class decides when it is one of
    these (Bluetooth Assigned Numbers: 1 computer, 2 phone, 4 audio/video, 5 peripheral); otherwise the AEP category
    (e.g. "Audio.Speakers", "Input.Mouse"). Headsets report "Communication.Headset.Bluetooth" as their category, so a
    Communication category is a phone only when it is not a headset or hands-free device."""
    try:
        by_major = {1: "computer", 2: "phone", 4: "audio", 5: "input"}.get(int(major))
    except (TypeError, ValueError):
        by_major = None
    if by_major:
        return by_major
    parts = (category or "").split(",")[0].lower().split(".")
    if parts[0] in ("audio", "input", "phone", "computer"):
        return parts[0]
    if parts[0] == "communication":
        return "audio" if len(parts) > 1 and parts[1] in ("headset", "handsfree") else "phone"
    return "other"


def _device(raw: dict) -> dict:
    kind = device_type(raw.get("category") or "", raw.get("major"))
    return {
        "id": raw.get("id") or "",
        "name": (raw.get("name") or "").strip(),
        "connected": bool(raw.get("connected")),
        "type": kind,
        "le": bool(raw.get("le")),
    }


def _parse_list(out: str) -> list[dict]:
    data = json.loads(out) if out else []
    if isinstance(data, dict):  # a one-item array can still come back as an object
        data = [data]
    devices = [_device(d) for d in data if isinstance(d, dict)]
    return [d for d in devices if d["id"]]


def list_paired(runner: Optional[Runner] = None) -> list[dict]:
    """Paired classic and LE devices: [{id, name, connected, type, le}], sorted connected first, then by name."""
    devices = _parse_list(run_powershell(LIST_SCRIPT, runner))
    for d in devices:
        if not d["name"]:
            d["name"] = "Unnamed device"
    return sorted(devices, key=lambda d: (not d["connected"], d["name"].lower()))


def unpair(device_id: str, runner: Optional[Runner] = None) -> dict:
    """{"ok": bool, "status": DeviceUnpairingResultStatus name}. ok is also true when it was not paired."""
    out = run_powershell(UNPAIR_SCRIPT % {"id": _check_id(device_id)}, runner, timeout=30)
    last = out.splitlines()[-1] if out else ""
    if last.startswith("result:"):
        status = last[len("result:"):]
        return {"ok": status in ("Unpaired", "AlreadyUnpaired", "NotPaired"), "status": status}
    return {"ok": False, "status": last or "no output"}


# Pairing --------------------------------------------------------------------------------------------------------

class HelperProcess(Protocol):
    stdin: Any
    stdout: Any

    def poll(self) -> Optional[int]: ...
    def wait(self, timeout: Optional[float] = None) -> int: ...
    def kill(self) -> None: ...


Spawner = Callable[[list[str]], HelperProcess]


def popen_spawner(argv: list[str]) -> HelperProcess:
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)


# DevicePairingResultStatus names -> plain English.
RESULT_MESSAGES = {
    "Paired": "Paired.",
    "AlreadyPaired": "Already paired.",
    "NotReadyToPair": "The device is not ready to pair. Put it in pairing mode and try again.",
    "NotPaired": "The device did not pair.",
    "ConnectionRejected": "The device refused the connection.",
    "TooManyConnections": "The device has too many connections.",
    "HardwareFailure": "Bluetooth hardware error.",
    "AuthenticationTimeout": "The device did not answer in time.",
    "AuthenticationNotAllowed": "The device does not allow pairing this way.",
    "AuthenticationFailure": "The PIN or confirmation did not match.",
    "NoSupportedProfiles": "Windows does not support this device.",
    "ProtectionLevelCouldNotBeMet": "The device could not pair securely.",
    "AccessDenied": "Windows denied access.",
    "InvalidCeremonyData": "The device sent invalid pairing data.",
    "PairingCanceled": "Pairing was cancelled.",
    "OperationAlreadyInProgress": "Windows is still busy with an earlier pairing. Turn Bluetooth off and on, "
                                  "or restart the device, then try again.",
    "RequiredHandlerNotRegistered": "Pairing needs a step this panel does not support.",
    "RejectedByHandler": "Pairing was cancelled.",
    "RemoteDeviceHasAssociation": "The device is paired with something else. Reset it and try again.",
    "Failed": "Pairing failed.",
}
UNSUPPORTED_MESSAGES = {
    "ProvidePin": "This device needs a PIN typed in here, which this panel does not support yet. "
                  "Use Windows Settings to pair it.",
}


class HelperSession:
    """A long-lived PowerShell helper that writes one JSON object per line on stdout and reads commands on stdin.

    Shared by pairing and search: spawning, a reader thread that passes each event to handle_event(), reaping, a
    backstop timer that kills the helper even when nobody polls, a deadline checked on every poll, and close(grace).
    Subclasses keep their state under self._lock and implement the hooks below."""

    thread_name = "wqs-bt-helper"

    def __init__(self, spawn: Optional[Spawner], clock: Callable[[], float], backstop_after: float) -> None:
        self._spawn = spawn or popen_spawner
        self._clock = clock
        self._lock = threading.Lock()
        self._proc: Optional[HelperProcess] = None
        self._deadline = 0.0
        self._backstop_after = backstop_after
        self._timer: Optional[threading.Timer] = None

    # hooks
    def _finished(self) -> bool:
        raise NotImplementedError

    def handle_event(self, event: dict) -> None:
        raise NotImplementedError

    def _spawn_failed(self, error: Exception) -> None:
        raise NotImplementedError

    def _ended(self) -> None:
        """The helper's output ended before it reported a final event."""
        raise NotImplementedError

    def _overdue(self) -> None:
        """The helper was killed for running past its time limit."""
        raise NotImplementedError

    # lifecycle
    def _launch(self, script: str, limit: float) -> bool:
        """Start the helper; Python gives up on it `limit` seconds from now (on poll) or after the backstop."""
        self._deadline = self._clock() + limit
        try:
            self._proc = self._spawn(powershell_argv(script))
        except Exception as e:  # noqa: BLE001 - reported to the panel
            self._spawn_failed(e)
            return False
        self._timer = threading.Timer(self._backstop_after, self._backstop)
        self._timer.daemon = True
        self._timer.start()
        threading.Thread(target=self._read, args=(self._proc,), name=self.thread_name, daemon=True).start()
        return True

    def _backstop(self) -> None:
        proc = self._proc
        if proc is not None and not self._finished():
            self._kill(proc)
            self._overdue()

    def _check_deadline(self) -> None:
        proc = self._proc
        if proc is not None and self._clock() > self._deadline and not self._finished():
            self._kill(proc)
            self._overdue()

    def _read(self, proc: HelperProcess) -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.handle_event(event)
        except Exception:  # noqa: BLE001 - pipe closed by kill()
            pass
        self._reap(proc)
        if self._timer is not None:
            self._timer.cancel()
        if not self._finished():
            self._ended()

    def _reap(self, proc: HelperProcess) -> None:
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._kill(proc)

    @staticmethod
    def _kill(proc: HelperProcess) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def _send(self, command: str) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            return True
        except Exception:  # noqa: BLE001 - helper already gone
            return False

    def _close(self, command: str, grace: float) -> None:
        """Send `command` so the helper can clean up (cancel the WinRT operation, stop watchers), give it `grace`
        seconds to exit, then kill it."""
        proc = self._proc
        if proc is not None:
            if proc.poll() is None and self._send(command):
                try:
                    proc.wait(timeout=grace)
                except Exception:  # noqa: BLE001 - did not exit in time
                    pass
            self._kill(proc)
        if self._timer is not None:
            self._timer.cancel()


class PairingSession(HelperSession):
    """One pairing attempt, driven by the PowerShell helper. Thread-safe; all methods return snapshot()."""

    thread_name = "wqs-bt-pair"

    def __init__(self, device_id: str, name: str = "", spawn: Optional[Spawner] = None,
                 clock: Callable[[], float] = time.monotonic, timeout: float = PAIR_TIMEOUT,
                 pin_timeout: float = PIN_TIMEOUT, backstop: Optional[float] = None) -> None:
        self.device_id = _check_id(device_id)
        # Kills the helper even when nobody polls; tests pass a short value.
        super().__init__(spawn, clock, timeout + KILL_GRACE if backstop is None else backstop)
        self.name = name or ""
        self._timeout = timeout
        self._pin_timeout = pin_timeout
        # stage: starting -> pairing -> (confirm | show_pin) -> done
        self._state: dict = {"stage": "starting", "pin": None, "kind": None, "ok": None, "message": None,
                             "status": None}
        self._note: Optional[str] = None  # set by "unsupported"/"rejected", kept for the final message

    # lifecycle
    def start(self) -> dict:
        script = PAIR_SCRIPT % {"id": self.device_id, "timeout": int(self._timeout),
                                "pin_timeout": int(self._pin_timeout)}
        self._launch(script, self._timeout + KILL_GRACE)
        return self.snapshot()

    def _finished(self) -> bool:
        with self._lock:
            return self._state["stage"] == "done"

    def _spawn_failed(self, error: Exception) -> None:
        self._finish(False, f"Could not start pairing: {error}")

    def _ended(self) -> None:
        self._finish(False, self._note or "Pairing stopped unexpectedly.")

    def _overdue(self) -> None:
        self._finish(False, "Pairing took too long and was stopped.", status="Timeout")

    def close(self, grace: float = CLOSE_GRACE) -> None:
        """Stop the helper (a new pairing started, or plugin unload): ask it to cancel, so the pairing operation is
        cancelled and its handler removed, give it `grace` seconds to exit, then kill it."""
        self._close("cancel", grace)
        self._finish(False, "Pairing was cancelled.")

    # events from the helper
    def handle_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "pairing":
            self._update(stage="pairing")
        elif kind == "accepted":
            with self._lock:
                if self._state["stage"] == "confirm":
                    self._state.update(stage="pairing", pin=None)
        elif kind == "pin":
            pin_kind = str(event.get("kind") or "")
            stage = "confirm" if pin_kind == "ConfirmPinMatch" else "show_pin"
            self._update(stage=stage, pin=str(event.get("pin") or ""), kind=pin_kind)
        elif kind == "rejected":
            self._note = ("Pairing was cancelled." if event.get("reason") == "user"
                          else "No answer in time, so pairing was cancelled.")
            self._update(stage="pairing", pin=None)
        elif kind == "unsupported":
            k = str(event.get("kind") or "")
            self._note = UNSUPPORTED_MESSAGES.get(k, f"This device asks for a pairing step ({k}) this panel does not "
                                                     "support. Use Windows Settings to pair it.")
        elif kind == "cancelled":
            reason = event.get("reason")
            msg = {"user": "Pairing was cancelled.", "timeout": "Pairing took too long and was cancelled."}.get(
                str(reason), self._note or "Windows cancelled pairing.")
            self._finish(False, msg, status="Cancelled")
        elif kind == "result":
            status = str(event.get("status") or "Failed")
            ok = status in ("Paired", "AlreadyPaired")
            msg = RESULT_MESSAGES.get(status, f"Pairing failed ({status}).")
            if not ok and self._note:
                msg = self._note
            self._finish(ok, msg, status=status)
        elif kind == "error":
            self._finish(False, f"Pairing failed: {event.get('message') or 'unknown error'}", status="Error")

    def _update(self, **changes: Any) -> None:
        with self._lock:
            if self._state["stage"] != "done":
                self._state.update(changes)

    def _finish(self, ok: bool, message: str, status: Optional[str] = None) -> None:
        with self._lock:
            if self._state["stage"] == "done":
                return
            self._state.update(stage="done", ok=ok, message=message, status=status, pin=None)

    # commands from the panel
    def respond(self, accept: bool) -> dict:
        with self._lock:
            waiting = self._state["stage"] == "confirm"
            if waiting:
                self._state.update(stage="pairing", pin=None)
        if waiting:
            self._send("accept" if accept else "reject")
        return self.snapshot()

    def cancel(self) -> dict:
        if not self._finished():
            self._send("cancel")
        return self.snapshot()

    def snapshot(self) -> dict:
        self._check_deadline()
        with self._lock:
            s = dict(self._state)
        return {"active": s["stage"] != "done", "device_id": self.device_id, "name": self.name, **s}


# Search -----------------------------------------------------------------------------------------------------------

class ScanSession(HelperSession):
    """One search, driven by the PowerShell search helper. Devices are kept as the helper reports them, one entry per
    id; snapshot() lists those that have a name and are not paired, in the order they were found (so the list does
    not reorder under the controller's focus). Thread-safe; all methods return snapshot()."""

    thread_name = "wqs-bt-scan"

    def __init__(self, spawn: Optional[Spawner] = None, clock: Callable[[], float] = time.monotonic,
                 timeout: float = SCAN_TIMEOUT, backstop: Optional[float] = None) -> None:
        super().__init__(spawn, clock, timeout + KILL_GRACE if backstop is None else backstop)
        self._timeout = timeout
        self._devices: dict[str, dict] = {}  # id -> {id, name, connected, type, le, paired}
        self._done = False
        self.reason: Optional[str] = None  # completed / stopped / closed / timeout / error / killed / ...
        self.error: Optional[str] = None

    def start(self) -> dict:
        self._launch(SCAN_SCRIPT % {"timeout": int(self._timeout)}, self._timeout + KILL_GRACE)
        return self.snapshot()

    def _finished(self) -> bool:
        with self._lock:
            return self._done

    def _finish(self, reason: str) -> None:
        with self._lock:
            if not self._done:
                self._done = True
                self.reason = reason

    def _spawn_failed(self, error: Exception) -> None:
        self.error = f"Could not start the search: {error}"
        self._finish("error")

    def _ended(self) -> None:
        self._finish("ended")

    def _overdue(self) -> None:
        self._finish("killed")

    def close(self, grace: float = CLOSE_GRACE) -> None:
        """Stop the search (a pairing starts, the Stop button, or plugin unload): ask the helper to stop its watchers,
        give it `grace` seconds to exit, then kill it. The devices found so far are kept."""
        self._close("stop", grace)
        self._finish("stopped")

    def handle_event(self, event: dict) -> None:
        kind = event.get("event")
        with self._lock:
            if self._done:
                return
            if kind == "device":
                d = _device(event)
                if not DEVICE_ID.fullmatch(d["id"]):
                    return
                d["paired"] = bool(event.get("paired"))
                self._devices[d["id"]] = d  # an update keeps the device's place in the order
            elif kind == "removed":
                self._devices.pop(str(event.get("id") or ""), None)
            elif kind == "error":
                self.error = str(event.get("message") or "unknown error")
            elif kind == "done":
                self._done = True
                self.reason = str(event.get("reason") or "completed")

    def snapshot(self) -> dict:
        self._check_deadline()
        with self._lock:
            devices = [{k: v for k, v in d.items() if k != "paired"}
                       for d in self._devices.values() if d["name"] and not d["paired"]]
            return {"active": not self._done, "done": self._done, "devices": devices}
