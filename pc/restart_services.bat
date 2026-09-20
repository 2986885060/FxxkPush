@echo off
REM FuckPush PC services: manual restart helper (double-click or run in cmd).
REM NOTE: venv pythonw.exe is a shim that spawns the real uv interpreter -
REM each service legitimately shows as TWO processes (shim + worker).

set PYW=C:\Users\omo\fuckpush\.venv\Scripts\pythonw.exe
set PC=C:\Users\omo\fuckpush\pc

echo Restarting FuckPush services...
taskkill /F /IM pythonw.exe 2>nul
timeout /t 2 /nobreak >nul

start "" "%PYW%" "%PC%\pc_subscriber.py"
start "" "%PYW%" "%PC%\notification_listener.py"
start "" "%PYW%" "%PC%\ai_triager.py"

echo Done. Verify with: tasklist ^| findstr pythonw  (expect 6 entries = 3x2)
