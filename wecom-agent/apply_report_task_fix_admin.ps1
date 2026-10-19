$ErrorActionPreference = 'Stop'

# Resolve everything relative to this script. Do not embed the Chinese checkout path:
# PowerShell (x86) can mojibake a hard-coded path before Task Scheduler is reached.
$agent = $PSScriptRoot
$register = Join-Path $agent 'register_report_tasks_admin.ps1'
$log = Join-Path (Join-Path $agent 'logs') 'register_report_tasks_20260902.log'
if (-not (Test-Path -LiteralPath $register)) { throw "register script not found beside wrapper: $register" }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# Use the native 64-bit Windows PowerShell host. Sysnative is required when the
# caller is the x86 PowerShell shown in the user's screenshot.
$ps64 = if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
  [IO.Path]::Combine($env:windir, 'Sysnative', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
} else {
  [IO.Path]::Combine($env:windir, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}
if (-not (Test-Path -LiteralPath $ps64)) { throw "64-bit Windows PowerShell not found: $ps64" }

if (-not $isAdmin) {
  # Pass the already-resolved script path as an argument object; do not build a
  # command string and do not reconstruct the non-ASCII directory name.
  $child = Start-Process -FilePath $ps64 -Verb RunAs -WorkingDirectory $agent `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath) `
    -Wait -PassThru
  exit $child.ExitCode
}

Push-Location -LiteralPath $agent
try {
  & $ps64 -NoProfile -ExecutionPolicy Bypass -File $register 2>&1 |
    Tee-Object -FilePath $log
  $rc = $LASTEXITCODE
} finally {
  Pop-Location
}
if ($rc -ne 0) { throw "task registration failed; see $log" }
Write-Host "Task registration completed. Details: $log"
