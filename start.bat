@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Bilibili Downloader
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PIP=.venv\Scripts\pip.exe"

if not exist "%VENV_PY%" (
    echo [1/3] Creating virtual environment...

    set "PYTHON="

    REM 1) 优先使用 PATH 中的 python / py 启动器
    where python >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=python"
    ) else (
        where py >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON=py"
        )
    )

    REM 2) PATH 中都没有时，探测本机常见绝对路径兜底
    if not defined PYTHON (
        for %%P in (
            "D:\RJ\Python\python.exe"
            "C:\Users\mmmmm\.workbuddy\binaries\python\versions\3.13.12\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
            "C:\Python314\python.exe"
            "C:\Python313\python.exe"
            "C:\Python312\python.exe"
        ) do (
            if not defined PYTHON (
                if exist %%P set "PYTHON=%%P"
            )
        )
    )

    if not defined PYTHON (
        echo [ERROR] 未找到 Python 3.10+。请从 https://www.python.org/downloads/ 安装，
        echo         或将 python 加入 PATH 后重新运行本脚本。
        pause
        exit /b 1
    )

    echo   使用 Python: !PYTHON!
    "!PYTHON!" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在。
)

echo [2/3] 安装依赖...
"%VENV_PIP%" install -r requirements.txt -q 2>nul
if errorlevel 1 (
    echo [WARN] 静默安装失败，改用详细输出重试...
    "%VENV_PIP%" install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败，请检查网络。
        pause
        exit /b 1
    )
)

echo [3/3] 启动服务...
echo.
echo   浏览器: http://localhost:8000
echo   下载目录: %~dp0downloads
echo.
echo   按 Ctrl+C 停止。
echo ============================================

start "" cmd /c "timeout /t 2 >nul && start http://localhost:8000"
"%VENV_PY%" server.py

pause
