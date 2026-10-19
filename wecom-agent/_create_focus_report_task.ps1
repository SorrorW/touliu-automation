$ErrorActionPreference = 'Stop'

# 潜力/重点计划 30 分钟独立监控播报（plan_focus_report.py）。
# 任务动作只保存 ASCII 的 powershell.exe + EncodedCommand，避免中文路径乱码
# （同 _create_report_slots.ps1 的教训：08:00 曾返回 0x80070002）。
$root = (Get-ChildItem -LiteralPath 'D:\Documents\SR_Project' -Directory |
  Where-Object { Test-Path (Join-Path $_.FullName 'wecom-agent\plan_focus_report.py') } |
  Select-Object -First 1 -ExpandProperty FullName)
if (-not $root) { throw '投流 checkout not found' }
$runtime = (Get-ChildItem -LiteralPath $root -Directory |
  ForEach-Object { Join-Path $_.FullName 'runtime\python.exe' } |
  Where-Object { Test-Path $_ } | Select-Object -First 1)
if (-not $runtime) { throw 'runtime python not found' }
$py = $runtime
$script = Join-Path $root 'wecom-agent\plan_focus_report.py'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("& '$py' '$script'"))
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -EncodedCommand ' + $encoded)

# 每 30 分钟重复一次，从 08:15 起 = 每小时 :15/:45（2026-09-01 用户定的预算监控时点）。
# 注册任务时若当天已过 08:15，改从下一个 :15/:45 开始，避免任务被顺延到次日。
# 脚本内部自带播报档 ±12 分钟/忙标/夜间/最小间隔守卫，触发即查。
$now = Get-Date
$first = $now.Date.AddHours(8).AddMinutes(15)
while ($first -le $now) { $first = $first.AddMinutes(30) }
$trigger = New-ScheduledTaskTrigger -Once -At $first `
  -RepetitionInterval (New-TimeSpan -Minutes 30) `
  -RepetitionDuration (New-TimeSpan -Days 365)
Register-ScheduledTask -TaskName 'wecom_plan_focus_report' -Action $action -Trigger $trigger -Settings $settings -Description '潜力/重点计划预算+ROI独立监控播报（每小时:15/:45，自动避开整点播报档±12分钟）' -Force | Out-Null

Get-ScheduledTask -TaskName 'wecom_plan_focus_report' | ForEach-Object {
  $info = $_ | Get-ScheduledTaskInfo
  Write-Output "$($_.TaskName) | $($_.State) | Next: $($info.NextRunTime)"
}
