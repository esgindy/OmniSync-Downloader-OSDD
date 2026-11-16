@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please run install.bat to automatically install Python and all requirements.
    pause
    exit /b 1
)

python -c "import starlette, uvicorn, sse_starlette, httpx, playwright, yt_dlp" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] Some required Python packages are missing.
    echo Please run install.bat to install all required dependencies.
    echo Attempting to launch install.bat now...
    call "%~dp0install.bat"
)

echo Starting OmniSync Downloader (OSDD)...
python "%~dp0app.py"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Application exited with code %errorlevel%.
    pause
)
endlocal
