param([Parameter(Mandatory=$true)][string]$Slot)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$work = $PSScriptRoot

# Always leave a trace: a task that fires but dies before Python must be
# distinguishable from a task that never fired (silent exit-1 saga 09-02/09-03).
$launchLog = Join-Path $work 'logs\report_launcher.log'
function Write-LaunchLog([string]$msg) {
  try { Add-Content -LiteralPath $launchLog -Value ("$(Get-Date -Format s) " + $msg) -Encoding UTF8 } catch {}
}
Write-LaunchLog "launch slot=$Slot pid=$PID"

try {
$runtime = Get-ChildItem -LiteralPath $project -Directory |
  ForEach-Object { Join-Path $_.FullName 'runtime\python.exe' } |
  Where-Object { Test-Path -LiteralPath $_ } |
  Select-Object -First 1
if (-not $runtime) { throw "runtime python not found under $project" }

# Windows Task Scheduler can replay a missed task when StartWhenAvailable is enabled
# (or when an old registration still has that flag). Never let a stale daytime slot
# scan at a later hour and block the next report/17:00 closeout. Manual invocations
# remain available by calling the Python entry point directly.
$slotTimes = @{
  'morning' = '08:00'; '10am' = '10:00'; 'noon' = '12:00'; '14pm' = '14:00';
  '16pm' = '16:00'; 'evening' = '17:00'; 'night' = '22:30'
}
if ($slotTimes.ContainsKey($Slot)) {
  $scheduled = [datetime]::Today.Add([timespan]::Parse($slotTimes[$Slot]))
  $lateBy = (Get-Date) - $scheduled
  $grace = if ($Slot -eq 'night') { [timespan]::FromMinutes(30) } else { [timespan]::FromMinutes(20) }
  if ($lateBy -gt $grace) {
    $log = Join-Path $work 'logs\report_launcher.log'
    Add-Content -LiteralPath $log -Value ("$(Get-Date -Format s) skip stale slot=$Slot late_by=$($lateBy.TotalMinutes.ToString('0.0'))m")
    exit 0
  }
}
# Mark this as a scheduler-owned invocation so Python can stop a replayed daytime
# retry before it reaches the next business slot. Manual Python runs are unaffected.
$env:TOULIU_SCHEDULED = '1'
if ($Slot -eq 'evening') {
  & $runtime (Join-Path $work 'evening_closeout.py')
} else {
  & $runtime (Join-Path $work 'daily_report_push.py') --slot $Slot
}
Write-LaunchLog "done slot=$Slot exit=$LASTEXITCODE"
exit $LASTEXITCODE
} catch {
  Write-LaunchLog "FAIL slot=$Slot : $($_.Exception.Message)"
  exit 1
}
