@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import starlette, uvicorn, sse_starlette, httpx, playwright" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] Some required Python packages may be missing.
    echo If launch fails, run: pip install starlette uvicorn sse-starlette httpx playwright
)

echo Starting OmniSync Downloader (OSDD)...
python "%~dp0app.py"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Application exited with code %errorlevel%.
    pause
)
endlocal
