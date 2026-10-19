@echo off
setlocal EnableExtensions
rem Relative paths avoid hard-coded Chinese checkout names.
set "PS64=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS64%" set "PS64=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS64%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_report_tasks_admin.ps1"
set "RC=%ERRORLEVEL%"
echo.
echo Registration exit code: %RC%
if not "%RC%"=="0" pause
exit /b %RC%