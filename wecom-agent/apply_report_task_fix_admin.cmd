@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply_report_task_fix_admin.ps1"
exit /b %errorlevel%
