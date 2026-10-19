$py = 'D:\Documents\SR_Project\计划投流\投流\runtime\pythonw.exe'
$work = 'D:\Documents\SR_Project\计划投流\投流'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
$agent = 'D:\Documents\SR_Project\计划投流\wecom-agent'

# 清理旧的17:00/17:10并发暂停任务，防止与日报抢占浏览器。
# 2026-09-04：wecom_report_noon 一并摘除——12:00 播报并入 noon_report_resume
# 串行链（先播报再恢复老池），避免两个 12:00 任务并发抢浏览器。
foreach ($legacy in @('wecom_daily_report','wecom_evening_new_check','wecom_old_pool_pause','wecom_report_noon')) {
  Unregister-ScheduledTask -TaskName $legacy -Confirm:$false -ErrorAction SilentlyContinue
}

# pythonw.exe 运行时不创建命令行窗口；执行记录由 plan_power 写入日志文件。
# 23:00 完成分类后仅暂停老计划池，随后放行夜间建计划。
$closeAction = New-ScheduledTaskAction -Execute $py -Argument '-m 巨量千川.plan_power close-old-pool --force' -WorkingDirectory $work
$closeTrigger = New-ScheduledTaskTrigger -Daily -At '23:00'
Register-ScheduledTask -TaskName 'wecom_plan_close' -Action $closeAction -Trigger $closeTrigger -Settings $settings -Description '千川计划 23:00 分类并暂停老计划池' -Force | Out-Null

# 2026-09-04 用户定「12点先播报，然后开启老计划」：12:00 改为单任务串行链
# （noon_report_resume.py：先跑 noon 播报，完成后再恢复老计划池），
# 取代原先 12:00 并发的 wecom_report_noon + 本任务。只恢复前一晚分类后
# 实际暂停的老计划池；重点、潜力、新计划和人工白名单均不在此任务范围内。
$noonChain = Join-Path $agent 'noon_report_resume.py'
$oldPoolResumeAction = New-ScheduledTaskAction -Execute $py -Argument ('"' + $noonChain + '"') -WorkingDirectory $agent
$oldPoolResumeTrigger = New-ScheduledTaskTrigger -Daily -At '12:00'
Register-ScheduledTask -TaskName 'wecom_old_pool_enable' -Action $oldPoolResumeAction -Trigger $oldPoolResumeTrigger -Settings $settings -Description '千川计划 12:00先播报再恢复老计划池（串行链）' -Force | Out-Null

# 老计划池暂停不再独立设17:10任务，由17:00收口在日报和新计划暂停完成后串行执行。

# 2026-08-23 策略：新计划 8:00 启动。夜间建成的新计划发布后已「暂停待发」挂入队列，
# 08:00 恢复新计划/潜力计划（老池仍由 12:00 的 wecom_old_pool_enable 恢复——resume_time 分流）。
$morningResumeAction = New-ScheduledTaskAction -Execute $py -Argument '-m 巨量千川.plan_power resume-old-pool --force' -WorkingDirectory $work
$morningResumeTrigger = New-ScheduledTaskTrigger -Daily -At '08:00'
Register-ScheduledTask -TaskName 'wecom_morning_resume' -Action $morningResumeAction -Trigger $morningResumeTrigger -Settings $settings -Description '千川计划 新计划/潜力计划08:00恢复（2026-08-23策略）' -Force | Out-Null

# 12:20 熔断：避开 12:00 老池恢复，检查 08:00-12:20 的新计划 ROI<4.5。
# 2026-09-01 起：不达标新计划不暂停，改写 10.3/300 转日常继续投放到 17:10；
# 串行追加潜力降级闸（订单<4 或 ROI<6 转日常）。达标新计划升到ROI7.3。
# 2026-09-03 起：再串行重点盘中闸（白板③，0-12 ROI<7.3 降潜力 7.3/300 继续投放）。
$noonCheckAction = New-ScheduledTaskAction -Execute $py -Argument '-m 巨量千川.plan_power pause-new-low-roi --threshold 4.5 --force' -WorkingDirectory $work
$noonPotentialAction = New-ScheduledTaskAction -Execute $py -Argument '-m 巨量千川.plan_power pause-potential-low-roi --threshold 6 --scan-slot 12:00 --force' -WorkingDirectory $work
$noonKeyAction = New-ScheduledTaskAction -Execute $py -Argument '-m 巨量千川.plan_power demote-key-low-roi --threshold 7.3 --scan-slot 12:00 --force' -WorkingDirectory $work
$noonCheckTrigger = New-ScheduledTaskTrigger -Daily -At '12:20'
Register-ScheduledTask -TaskName 'wecom_noon_new_check' -Action @($noonCheckAction, $noonPotentialAction, $noonKeyAction) -Trigger $noonCheckTrigger -Settings $settings -Description '千川计划 12:20新计划不达标转日常+潜力降级闸+重点盘中闸（避开12:00老池恢复）' -Force | Out-Null

# 17:00由统一收口入口执行：先完成日报，再执行新计划暂停，最后执行老池暂停。
$closeout = Join-Path $agent 'evening_closeout.py'
$closeoutAction = New-ScheduledTaskAction -Execute $py -Argument ('"' + $closeout + '"') -WorkingDirectory $agent
$closeoutTrigger = New-ScheduledTaskTrigger -Daily -At '17:00'
Register-ScheduledTask -TaskName 'wecom_evening_closeout' -Action $closeoutAction -Trigger $closeoutTrigger -Settings $settings -Description '17:00先完成四群日报，再串行暂停新计划和老计划池' -Force | Out-Null

Get-ScheduledTask -TaskName 'wecom_plan_close' | ForEach-Object {
  $info = $_ | Get-ScheduledTaskInfo
  Write-Output ($_.TaskName + ' | ' + $_.State + ' | Next: ' + $info.NextRunTime)
}
Get-ScheduledTask -TaskName 'wecom_old_pool_enable' | ForEach-Object {
  $info = $_ | Get-ScheduledTaskInfo
  Write-Output ($_.TaskName + ' | ' + $_.State + ' | Next: ' + $info.NextRunTime)
}
Get-ScheduledTask -TaskName 'wecom_morning_resume' | ForEach-Object {
  $info = $_ | Get-ScheduledTaskInfo
  Write-Output ($_.TaskName + ' | ' + $_.State + ' | Next: ' + $info.NextRunTime)
}
Get-ScheduledTask -TaskName 'wecom_noon_new_check' | ForEach-Object {
  $info = $_ | Get-ScheduledTaskInfo
  Write-Output ($_.TaskName + ' | ' + $_.State + ' | Next: ' + $info.NextRunTime)
}

Get-ScheduledTask -TaskName 'wecom_evening_closeout' | ForEach-Object {
  $info = $_ | Get-ScheduledTaskInfo
  Write-Output ($_.TaskName + ' | ' + $_.State + ' | Next: ' + $info.NextRunTime)
}
