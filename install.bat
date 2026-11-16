@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

title OmniSync-Downloader (OSDD) - Auto Installer
cd /d "%~dp0"

echo ======================================================================
echo           OmniSync-Downloader (OSDD) - Automatic Installer
echo ======================================================================
echo.

:: 1. Check Python installation
echo [*] Checking Python 3 installation...
set "PY_CMD="

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PY_CMD=py -3"
    )
)

if "%PY_CMD%"=="" (
    echo [!] Python 3.9+ was not found on this system.
    echo [*] Downloading official Python 3.11.9 (64-bit) installer...
    
    set "INSTALLER_PATH=%TEMP%\python-3.11.9-amd64.exe"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe', '%INSTALLER_PATH%')"
    
    if not exist "%INSTALLER_PATH%" (
        echo [ERROR] Failed to download Python installer automatically.
        echo Please manually download and install Python 3.10+ from: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    
    echo [*] Installing Python 3.11 silently and adding to PATH...
    start /wait "" "%INSTALLER_PATH%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0 SimpleInstall=1
    del "%INSTALLER_PATH%" >nul 2>&1
    
    :: Refresh PATH for the current process
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
    for /f "tokens=*" %%p in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')"') do set "PATH=%%p;!PATH!"
    
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PY_CMD=python"
        echo [OK] Python installed successfully.
    ) else (
        echo [ERROR] Python installation completed but could not be initialized in this window.
        echo Please restart your computer or command prompt, then run install.bat again.
        pause
        exit /b 1
    )
) else (
    echo [OK] Python is already installed and detected.
)

echo.
echo ======================================================================
echo [*] Step 1 of 3: Upgrading pip...
echo ======================================================================
%PY_CMD% -m pip install --upgrade pip

echo.
echo ======================================================================
echo [*] Step 2 of 3: Installing required Python packages (requirements.txt)...
echo ======================================================================
%PY_CMD% -m pip install -r "%~dp0requirements.txt"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install one or more Python packages.
    pause
    exit /b %errorlevel%
)

echo.
echo ======================================================================
echo [*] Step 3 of 3: Installing Playwright Chromium browser driver...
echo ======================================================================
%PY_CMD% -m playwright install chromium
if %errorlevel% neq 0 (
    echo [WARNING] Playwright Chromium installation encountered a warning, continuing...
)

echo.
echo ======================================================================
echo  OmniSync-Downloader (OSDD) Setup Completed!
echo ======================================================================
echo.
echo  The application is ready to use!
echo  All dependencies and browser drivers have been successfully installed.
echo.
echo  You can now launch the application by double-clicking:
echo  --> start.bat
echo.
echo ======================================================================

:: Display Native Windows Info Popup Dialog
set "VBS_POPUP=%TEMP%\osdd_setup_ready.vbs"
(
    echo Set objShell = CreateObject^("WScript.Shell"^)
    echo MsgBox "The application is ready to use!" ^& vbCrLf ^& vbCrLf ^& "All dependencies and drivers have been installed successfully." ^& vbCrLf ^& vbCrLf ^& "Please run 'start.bat' to launch OmniSync-Downloader.", vbInformation + vbOKOnly, "OmniSync-Downloader (OSDD)"
) > "%VBS_POPUP%"

cscript //nologo "%VBS_POPUP%"
del "%VBS_POPUP%" >nul 2>&1

pause
exit /b 0
