$t = Get-ScheduledTask -TaskName wecom_poll
Write-Output ("LogonType: " + $t.Principal.LogonType)
Write-Output ("UserId: " + $t.Principal.UserId)
Write-Output ("Interval: " + $t.Triggers[0].Repetition.Interval)
Write-Output ("Duration: " + $t.Triggers[0].Repetition.Duration)
Write-Output ("State: " + $t.State)
Write-Output ("Action: " + $t.Actions[0].Execute + " " + $t.Actions[0].Arguments)
