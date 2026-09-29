@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
REM FxxkPush 一键卸载入口：找可用的 Python 运行 uninstall.py（纯标准库）。
REM 顺序：真实 Python（排除 Store 占位）→ uv → python3；参数原样透传
REM （--yes / --purge / --dry-run）。
pushd "%~dp0"
set "RC=1"

set "PYVER="
for /f "delims=" %%V in ('python --version 2^>^&1') do if not defined PYVER set "PYVER=%%V"
rem 绝不能 echo(!PYVER! —— Store 占位符消息里带 ">"，展开后会被 cmd 当成重定向
if "!PYVER:~0,9!"=="Python 3" goto :with_python

where uv >nul 2>nul
if not errorlevel 1 goto :with_uv

set "PYVER3="
for /f "delims=" %%V in ('python3 --version 2^>^&1') do if not defined PYVER3 set "PYVER3=%%V"
if "!PYVER3:~0,9!"=="Python 3" goto :with_python3

echo [失败] 未找到可用的 Python3。
echo        请安装 Python 3.12+（https://www.python.org）或 uv（https://uv.sh）
goto :end

:with_python
python uninstall.py %*
set "RC=!errorlevel!"
goto :end

:with_uv
uv run --python 3.12 python uninstall.py %*
set "RC=!errorlevel!"
goto :end

:with_python3
python3 uninstall.py %*
set "RC=!errorlevel!"
goto :end

:end
popd
pause
exit /b !RC!
