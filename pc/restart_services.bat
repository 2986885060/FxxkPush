@echo off
REM FxxkPush PC services: manual restart helper (double-click or run in cmd).
REM NOTE: venv pythonw.exe is a shim that spawns the real uv interpreter -
REM each service legitimately shows as TWO processes (shim + worker).
REM ORDER MATTERS: the ntfy_tunnel must come up first, the others need it.

set PYW=C:\Users\omo\fuckpush\.venv\Scripts\pythonw.exe
set PC=C:\Users\omo\fuckpush\pc

echo Restarting FxxkPush services...
taskkill /F /IM pythonw.exe 2>nul
timeout /t 2 /nobreak >nul

start "" "%PYW%" "%PC%\ntfy_tunnel.py"
timeout /t 3 /nobreak >nul
start "" "%PYW%" "%PC%\pc_subscriber.py"
start "" "%PYW%" "%PC%\notification_listener.py"
start "" "%PYW%" "%PC%\ai_triager.py"
start "" "%PYW%" "%PC%\wechat_vision_listener.py"

echo Done. Verify with: tasklist ^| findstr pythonw  (expect 10 entries = 5x2)
