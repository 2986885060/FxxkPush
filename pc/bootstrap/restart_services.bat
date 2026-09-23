@echo off
chcp 65001 >nul
REM FxxkPush: restart all PC services.
REM
REM The real logic is in pc\bootstrap\start_services.py — it does the ordering and the
REM health gate this script used to fake with `timeout /t 3`:
REM   kill ours -> start tunnel -> poll /v1/health until 200 -> only then
REM   start the rest -> verify every script has shim+worker = 2 processes.
REM If the tunnel never goes green, nothing else starts (no pointless retry
REM storm against a tunnel that is not up).
REM
REM Uses python.exe (console) so you can read each step; logs also go to
REM pc\logs\start_services.log via pclog.

set "PY=C:\Users\omo\fuckpush\.venv\Scripts\python.exe"
set "PC=C:\Users\omo\fuckpush\pc"

"%PY%" "%PC%\bootstrap\start_services.py"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo All services up. Manual check: tasklist ^| findstr pythonw  ^(expect 12 = 6x2^)
) else (
    echo start_services.py exited with code %RC% - see pc\logs\start_services.log
)
pause
exit /b %RC%
