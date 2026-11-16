@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ===================================================
echo     OmniSync-Downloader (OSDD) - إيقاف البرنامج
echo ===================================================
echo جاري إنهاء خدمة البرنامج في الخلفية...

:: 1. إيقاف أنيق عبر API الخادم
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/system/shutdown' -Method Post -TimeoutSec 1 | Out-Null } catch {}" >nul 2>&1

:: 2. إنهاء أي عملية تستمع على المنفذ 8765
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

:: 3. التأكد من إغلاق أي عملية بايثون تابعة للتطبيق
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*app.py*' -and $_.CommandLine -like '*sharepoint-downloader*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo.
echo [✓] تم إيقاف البرنامج وإغلاق كافة العمليات في الخلفية بنجاح!
echo.
timeout /t 2 >nul
exit /b 0
