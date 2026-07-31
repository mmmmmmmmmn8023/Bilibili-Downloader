@echo off
chcp 65001 >nul 2>&1
title Bilibili Downloader
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PIP=.venv\Scripts\pip.exe"

if not exist "%VENV_PY%" (
    echo [1/3] Creating virtual environment...
    python --version >nul 2>&1
    if errorlevel 1 (
        py --version >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
            pause
            exit /b 1
        )
        set PYTHON=py
    ) else (
        set PYTHON=python
    )
    %PYTHON% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Virtual environment exists.
)

echo [2/3] Installing dependencies...
"%VENV_PIP%" install -r requirements.txt -q 2>nul
if errorlevel 1 (
    echo [WARN] Retry with verbose output...
    "%VENV_PIP%" install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your network.
        pause
        exit /b 1
    )
)

echo [3/3] Starting server...
echo.
echo   Browser: http://localhost:8000
echo   Downloads: %~dp0downloads
echo.
echo   Press Ctrl+C to stop.
echo ============================================

start "" cmd /c "timeout /t 2 >nul && start http://localhost:8000"
"%VENV_PY%" server.py

pause
