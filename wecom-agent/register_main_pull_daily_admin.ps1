# 注册「主号自动拉取达人日报」计划任务：每日 09:10 / 13:30 / 17:30 抓取+落库+播报
# 播报含千川操作日志佐证（哪个账号什么时候拉取/删除了达人）
# 用法：右键「以管理员身份运行 PowerShell」执行本脚本
$ErrorActionPreference = 'Stop'
$py = 'D:\Documents\SR_Project\计划投流\投流\runtime\pythonw.exe'
$work = 'D:\Documents\SR_Project\计划投流\wecom-agent'
$action = New-ScheduledTaskAction -Execute $py -Argument 'main_pull_daily.py' -WorkingDirectory $work
$triggers = @(
  (New-ScheduledTaskTrigger -Daily -At '09:10'),
  (New-ScheduledTaskTrigger -Daily -At '13:30'),
  (New-ScheduledTaskTrigger -Daily -At '17:30')
)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'wecom_main_pull_daily' -Action $action -Trigger $triggers `
  -Settings $settings -Description '主号四大主品自动拉取达人日报：09:10/13:30/17:30 抓取→落库→diff+操作日志→汇总群播报' -Force
Write-Host "已注册 wecom_main_pull_daily（每日 09:10 / 13:30 / 17:30）"
Get-ScheduledTask -TaskName 'wecom_main_pull_daily' | Select-Object TaskName, State
