@echo off
REM FuckPush: register all three PC-side services as logon scheduled tasks.
REM Run this script AS ADMINISTRATOR once. Idempotent — safe to re-run.

set TASKDIR=C:\Users\omo\fuckpush
set PYW=%TASKDIR%\.venv\Scripts\pythonw.exe
set PCDIR=%TASKDIR%\pc

schtasks /Create /F /TN "FuckPush\subscriber" /TR "\"%PYW%\" \"%PCDIR%\pc_subscriber.py\"" /SC ONLOGON /RL HIGHEST /F
schtasks /Create /F /TN "FuckPush\listener" /TR "\"%PYW%\" \"%PCDIR%\notification_listener.py\"" /SC ONLOGON /RL HIGHEST /F
schtasks /Create /F /TN "FuckPush\triager" /TR "\"%PYW%\" \"%PCDIR%\ai_triager.py\"" /SC ONLOGON /RL HIGHEST /F

echo.
echo Registered. Listing:
schtasks /Query /TN "FuckPush\subscriber" 2>nul | findstr FuckPush
schtasks /Query /TN "FuckPush\listener" 2>nul | findstr FuckPush
schtasks /Query /TN "FuckPush\triager" 2>nul | findstr FuckPush
