@echo off
rem wecom-agent poll entry (schtasks every 15 min)
rem unset local proxy: wecom API needs direct connection
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=
rem %~dp0 = this bat's own dir (avoid CJK chars in file: cmd reads .bat as GBK)
cd /d "%~dp0"
if not exist logs mkdir logs
rem python: portable package = sibling dir containing runtime\python.exe
set PY=
for /d %%D in ("%~dp0..\*") do if exist "%%D\runtime\python.exe" set PY=%%~fD\runtime\python.exe
rem dev machine fallback
if not defined PY set PY=C:\Users\fengsheng\AppData\Local\Programs\Python\Python312\python.exe
rem step logs: logs\poll_YYYYMMDD.log; stdout here catches process-level crashes only
"%PY%" poll_and_run.py 1>>logs\poll_stdout.log 2>&1
