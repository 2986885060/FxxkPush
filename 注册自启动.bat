@echo off
chcp 65001 >nul
REM FxxkPush 单文件自启动注册器：检测当前目录定位项目根，把 6 个组件写入
REM HKCU Run（无需管理员）。幂等，可反复双击。
REM
REM 定位策略：从「运行本脚本时的当前目录」向上最多找 4 级，标记是
REM pc\services\ai_triager.py + .venv\Scripts\pythonw.exe 同时存在；
REM 找不到再回退到本脚本所在目录。以后挪动项目目录后重跑一次即可。

setlocal EnableDelayedExpansion

set "MARKER=pc\services\ai_triager.py"
set "ROOT="

rem ---- 1) 从当前工作目录向上探测 ----
set "DIR=%CD%"
for /l %%N in (1,1,4) do (
    if exist "!DIR!\%MARKER%" if exist "!DIR!\.venv\Scripts\pythonw.exe" (
        set "ROOT=!DIR!"
        goto :found
    )
    for %%I in ("!DIR!\..") do set "DIR=%%~fI"
)

rem ---- 2) 回退：本脚本所在目录 ----
for %%I in ("%~dp0.") do set "DIR=%%~fI"
if exist "!DIR!\%MARKER%" if exist "!DIR!\.venv\Scripts\pythonw.exe" set "ROOT=!DIR!"

:found
if not defined ROOT (
    echo [失败] 当前目录向上 4 级、脚本所在目录都不是 FxxkPush 项目根。
    echo        缺少标记文件: %MARKER% 或 .venv\Scripts\pythonw.exe
    echo        请在项目目录内运行本脚本，或把本文件放在项目根目录。
    pause
    exit /b 1
)

for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYW=%ROOT%\.venv\Scripts\pythonw.exe"
set "PC=%ROOT%\pc"
set "RUN=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

echo 项目根: %ROOT%
echo.

reg add "%RUN%" /v FuckPush_tunnel          /t REG_SZ /d "\"%PYW%\" \"%PC%\services\ntfy_tunnel.py\""             /f >nul
reg add "%RUN%" /v FuckPush_subscriber      /t REG_SZ /d "\"%PYW%\" \"%PC%\services\pc_subscriber.py\""           /f >nul
reg add "%RUN%" /v FuckPush_listener        /t REG_SZ /d "\"%PYW%\" \"%PC%\services\notification_listener.py\""    /f >nul
reg add "%RUN%" /v FuckPush_triager         /t REG_SZ /d "\"%PYW%\" \"%PC%\services\ai_triager.py\""               /f >nul
reg add "%RUN%" /v FuckPush_wechat_vision   /t REG_SZ /d "\"%PYW%\" \"%PC%\services\vision_listener.py\""          /f >nul
reg add "%RUN%" /v FuckPush_watchdog        /t REG_SZ /d "\"%PYW%\" \"%PC%\services\watchdog.py\""                 /f >nul

echo 已写入 6 个自启项，当前注册表内容：
reg query "%RUN%" 2>nul | findstr /i fuckpush

echo.
set /a OK=0
if exist "%PYW%" set /a OK+=1
for %%S in (ntfy_tunnel pc_subscriber notification_listener ai_triager vision_listener watchdog) do (
    if exist "%PC%\services\%%S.py" set /a OK+=1
)
echo 校验: !OK!/7 个路径有效（解释器 + 6 个服务脚本）

echo.
echo 提示：注册只影响下次登录；想立刻启动请运行 restart_services.bat（或
echo       pc\bootstrap\start_services.py）。以后挪动项目目录后重跑本脚本即可。
pause
