@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 首次运行自动创建桌面图标（已存在则跳过）
if not exist "%USERPROFILE%\Desktop\投流操作台.lnk" (
  powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0安装桌面图标.ps1" >nul 2>&1
)
start "" "%~dp0runtime\pythonw.exe" -m webui.desktop
