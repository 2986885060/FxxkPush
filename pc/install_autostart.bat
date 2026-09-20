@echo off
chcp 65001 >nul
REM FxxkPush: register the five PC-side services to start at logon.
REM Uses HKCU\...\Run — schtasks needs elevation here and gets "access denied",
REM while HKCU Run works without admin. Idempotent: safe to re-run.
REM NOTE: the tunnel must be registered too, otherwise the other services have
REM no ntfy to talk to on networks that block the ntfy port.

set "PYW=C:\Users\omo\fuckpush\.venv\Scripts\pythonw.exe"
set "PC=C:\Users\omo\fuckpush\pc"
set "RUN=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

reg add "%RUN%" /v FuckPush_tunnel          /t REG_SZ /d "\"%PYW%\" \"%PC%\ntfy_tunnel.py\""             /f >nul
reg add "%RUN%" /v FuckPush_subscriber      /t REG_SZ /d "\"%PYW%\" \"%PC%\pc_subscriber.py\""           /f >nul
reg add "%RUN%" /v FuckPush_listener        /t REG_SZ /d "\"%PYW%\" \"%PC%\notification_listener.py\""  /f >nul
reg add "%RUN%" /v FuckPush_triager         /t REG_SZ /d "\"%PYW%\" \"%PC%\ai_triager.py\""             /f >nul
reg add "%RUN%" /v FuckPush_wechat_vision   /t REG_SZ /d "\"%PYW%\" \"%PC%\wechat_vision_listener.py\"" /f >nul

echo.
echo 已注册 5 个自启项，当前列表：
reg query "%RUN%" 2>nul | findstr /i fuckpush

echo.
echo 提示：注册只影响下次登录。想立刻启动，请双击 restart_services.bat
pause
