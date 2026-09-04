@echo off
title Lechange Capture mitmproxy :8080
cd /d "%~dp0"

echo ============================================
echo  Lechange App Capture Start
echo  Log: API\capture\flow_live_*.jsonl
echo  Proxy: Set phone WiFi proxy to this PC IP:8080
echo  Cert: Open http://mitm.it on phone browser
echo ============================================
echo.

:: Show local IPv4 addresses
echo  Local IPv4 Address(es):
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=* delims= " %%j in ("%%i") do echo    %%j
)
echo.

:: Check required files
if not exist "API\capture\capture_addon_live.py" (
    echo [ERROR] Script not found: API\capture\capture_addon_live.py
    pause
    exit /b 1
)

:: Check mitmdump
where mitmdump >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] mitmdump not found. Install with: pip install mitmproxy
    pause
    exit /b 1
)

echo Starting mitmproxy...
echo Press Ctrl+C to stop.
echo.

mitmdump -s API\capture\capture_addon_live.py --listen-port 8080 --set ssl_insecure=true

echo.
echo Capture stopped.
pause