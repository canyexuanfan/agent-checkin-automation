# WorkBuddy 每日积分签到 —— Windows 一键安装（计划任务每天 09:05）
# 用法：右键「使用 PowerShell 运行」，或
#      powershell -ExecutionPolicy Bypass -File install_windows.ps1
#      powershell -ExecutionPolicy Bypass -File install_windows.ps1 -Remove
param([switch]$Remove, [string]$Time = "09:05")

$ErrorActionPreference = "Stop"
$TaskName = "WorkBuddy每日积分签到"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Dir "wb_checkin.py"
$Vbs = Join-Path $Dir "run_silent.vbs"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "已卸载计划任务：$TaskName"
    exit 0
}

$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $Py) { throw "找不到 python，请先装 Python 3" }
if (-not (Test-Path $Script)) { throw "找不到 $Script" }

$Auth = Join-Path $env:LOCALAPPDATA "CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info"
if (-not (Test-Path $Auth)) { "提醒：没找到凭据文件，请先打开一次 WorkBuddy 客户端登录" }

# 生成静默启动器（wscript //B 不弹黑框；此文件必须纯 ASCII）
$vbsBody = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$Dir"
sh.Run """$Py"" ""$Script"" --verbose", 0, False
"@
[System.IO.File]::WriteAllText($Vbs, $vbsBody, [System.Text.Encoding]::ASCII)

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`"" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "每天 $Time 自动领取 WorkBuddy 积分。脚本 $Script，日志 checkin.log" | Out-Null

"已安装计划任务：$TaskName（每天 $Time）"
"--- 立即试跑一次验证 ---"
& $Py $Script --verbose
