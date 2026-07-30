# 在桌面创建「投流操作台」快捷方式（自定义图标，无控制台黑窗）
# 用法：右键 → 使用 PowerShell 运行；或 powershell -File 安装桌面图标.ps1
$ErrorActionPreference = "Stop"

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path   # 投流 目录
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
$icon   = Join-Path $root "webui\assets\touliu.ico"
$desk   = [Environment]::GetFolderPath("Desktop")
$lnk    = Join-Path $desk "投流操作台.lnk"

if (-not (Test-Path $python)) { throw "未找到 $python，请先创建 .venv 并安装依赖" }
if (-not (Test-Path $icon))   { throw "未找到 $icon，请先运行 python -m webui.make_icon" }

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $python
$sc.Arguments = "-m webui.desktop"
$sc.WorkingDirectory = $root
$sc.IconLocation = $icon
$sc.Description = "投流操作台 - 千川投流日常操作"
$sc.Save()

Write-Host "快捷方式已创建: $lnk"
