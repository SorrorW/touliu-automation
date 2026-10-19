# 在桌面创建「投流操作台」快捷方式（自定义图标，无控制台黑窗）
# 用法：右键 → 使用 PowerShell 运行；或 powershell -File 安装桌面图标.ps1

$ErrorActionPreference = "Stop"

# 日志路径（投流目录下）
$logFile = Join-Path $PSScriptRoot "desktop_error.log"
function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Out-File -Append -Encoding utf8 -FilePath $logFile
}

try {
    $root   = $PSScriptRoot   # 投流 目录（比 $MyInvocation.MyCommand.Path 更可靠）
    $python = Join-Path $root "runtime\pythonw.exe"
    $icon   = Join-Path $root "webui\assets\touliu.ico"
    $desk   = [Environment]::GetFolderPath("Desktop")
    $lnk    = Join-Path $desk "投流操作台.lnk"

    if (-not (Test-Path $python)) { throw "未找到 $python，便携包不完整" }
    if (-not (Test-Path $icon))   { throw "未找到 $icon，便携包不完整" }

    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $python
    $sc.Arguments = "-m webui.desktop"
    $sc.WorkingDirectory = $root
    $sc.IconLocation = $icon
    $sc.Description = "投流操作台 - 千川投流日常操作"
    $sc.Save()

    Write-Log "快捷方式已创建: $lnk"
    Write-Host "快捷方式已创建: $lnk"
    exit 0
}
catch {
    Write-Log "ERROR: $_"
    Write-Error "创建快捷方式失败: $_"
    exit 1
}
