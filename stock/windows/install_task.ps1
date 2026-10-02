# Registers a Windows scheduled task that runs run_local.ps1 every weekday morning.
#
#   powershell -ExecutionPolicy Bypass -File stock\windows\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File stock\windows\install_task.ps1 -Time 07:45
#
# If the PC is off or asleep at that time, the task runs as soon as it is back on.
# Remove it with:  Unregister-ScheduledTask -TaskName LedgerStockPicker -Confirm:$false

param([string]$Time = "08:30")

$name = "LedgerStockPicker"
$script = Join-Path $PSScriptRoot "run_local.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily stock picker: scrape PTT/Dcard from this PC and push to GitHub" -Force | Out-Null

Write-Host "Task '$name' registered: weekdays at $Time (runs late if the PC was off)."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName $name"
Write-Host "Log file: $(Join-Path $PSScriptRoot 'run.log')"
