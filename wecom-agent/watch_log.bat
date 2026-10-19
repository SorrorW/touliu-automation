@echo off
REM watch_log.bat - live-tail today's poll log (double-click to use)
REM Pure ASCII on purpose: cmd reads .bat as GBK, Chinese would mojibake.
cd /d "%~dp0"
powershell -NoProfile -Command "$f = Join-Path '%~dp0logs' ('poll_' + (Get-Date -Format yyyyMMdd) + '.log'); Write-Host ('Watching ' + $f + '  (Ctrl+C to stop)') -ForegroundColor Cyan; Get-Content -Wait -Tail 100 -Encoding UTF8 $f"
