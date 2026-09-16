@echo off
title TeleDrive Server - Liquid Glass & MTProto Cloud
color 0b
echo ===================================================================
echo   TeleDrive Cloud Storage Server (Liquid Glass & Telegram MTProto)
echo ===================================================================
echo.
cd /d "%~dp0backend"
echo [1/2] Checking Python virtual environment...
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [ERROR] Python virtualenv not found!
    pause
    exit /b 1
)
echo [2/2] Launching server on port 8000...
start "" "http://127.0.0.1:8000"
"%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
