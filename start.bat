@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
start "" wscript.exe "%~dp0start.vbs"
exit /b 0
