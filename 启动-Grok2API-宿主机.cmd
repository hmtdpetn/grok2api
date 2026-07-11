@echo off
title Grok2API Launcher (Host)

echo ============================================
echo   Grok2API Anti-Ban Launcher
echo ============================================
echo.

REM Check Docker
where docker >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker not found. Please install Docker Desktop first.
    pause
    exit /b 1
)

docker ps >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker Engine not running.
    echo Start Docker Desktop and try again.
    pause
    exit /b 1
)

REM Ensure containers are running
echo [*] Starting containers...
docker start warp-proxy privoxy flaresolverr grok2api >nul 2>&1

REM Check if grok2api is already healthy
curl -s -o nul http://localhost:8000/health 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [OK] grok2api is already running!
    goto :open
)

REM Wait loop
echo [*] Waiting for grok2api to be ready...
set /a COUNT=0
:loop
timeout /t 2 /nobreak >nul
set /a COUNT+=1
echo   ... waiting (%COUNT%/60)

curl -s -o nul http://localhost:8000/health 2>nul
if %ERRORLEVEL% EQU 0 goto :ready
if %COUNT% LSS 60 goto :loop

echo [!] Timeout - opening browser anyway...
goto :open

:ready
echo [OK] grok2api is healthy!

:open
echo [*] Opening admin panel...
start http://localhost:8000/admin/login
echo.
echo ============================================
echo   You can close this window.
echo ============================================
pause >nul
