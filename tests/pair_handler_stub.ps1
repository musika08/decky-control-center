Add-Type -TypeDefinition @'
using System.Threading;
using System.Threading.Tasks;
public enum StubKinds : uint { None = 0, ConfirmOnly = 1, DisplayPin = 2, ProvidePin = 4, ConfirmPinMatch = 8 }
public class StubArgs {
  public StubKinds PairingKind { get; set; }
  public string Pin { get; set; }
  public int Accepted;
  public int AcceptThread = -1;
  public void Accept() { Interlocked.Increment(ref Accepted); AcceptThread = Thread.CurrentThread.ManagedThreadId; }
  public void Accept(string pin) { throw new System.InvalidOperationException("wrong overload"); }
}
public delegate void StubHandler(object sender, StubArgs args);
public static class StubFire {
  public static Task Fire(StubHandler h, StubArgs a) { return Task.Run(() => h(null, a)); }
}
'@
$me = [System.Threading.Thread]::CurrentThread.ManagedThreadId
$results = [ordered]@{}
function Run([string]$name, [string]$kind, [string]$pin, $answer, [int]$waitMs) {
  $h = New-PairingHandler ([StubHandler]) $waitMs
  $a = New-Object StubArgs
  $a.PairingKind = [StubKinds]$kind; $a.Pin = $pin
  $t = [StubFire]::Fire($h, $a)
  $item = $null
  $until = [DateTime]::UtcNow.AddSeconds(5)
  while (-not $queue.TryDequeue([ref]$item) -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 10 }
  $blocked = $null
  if ($null -ne $answer) {
    $blocked = -not $t.Wait(300)   # the handler must still be waiting for the answer
    $acceptedWhileBlocked = $a.Accepted
    $item[2].Add([bool]$answer)
  }
  $done = $t.Wait(5000)
  $results[$name] = [ordered]@{ kind = $item[0].ToString(); pin = [string]$item[1]; hasBox = ($null -ne $item[2])
    blocked = $blocked; acceptedWhileBlocked = $acceptedWhileBlocked; done = $done; accepted = $a.Accepted
    otherThread = ($a.AcceptThread -ne -1 -and $a.AcceptThread -ne $me) }
}
Run 'confirm_only' 'ConfirmOnly' '' $null 60000
Run 'display_pin' 'DisplayPin' '4321' $null 60000
Run 'provide_pin' 'ProvidePin' '' $null 60000
Run 'match_accept' 'ConfirmPinMatch' '123456' $true 60000
Run 'match_reject' 'ConfirmPinMatch' '123456' $false 60000
Run 'match_timeout' 'ConfirmPinMatch' '7' $null 200
ConvertTo-Json -Compress -Depth 4 -InputObject $results
