@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" "%~dp0pc\services\vision_listener.py" park
