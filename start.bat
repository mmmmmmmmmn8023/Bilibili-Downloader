@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Bilibili Downloader
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PIP=.venv\Scripts\pip.exe"
set "PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PY_VERSION=3.13.17"
set "PY_BOOT_DIR=%LOCALAPPDATA%\BiliDownloader\python"

REM 端口：与 server.py 的解析顺序一致（BILI_DL_PORT > config.json 的 port > 8000）
set "OPEN_PORT=%BILI_DL_PORT%"
if not defined OPEN_PORT set "OPEN_PORT=8000"

if not exist "%VENV_PY%" (
    echo [1/3] Creating virtual environment...

    set "PYTHON="

    REM 1) PATH 中的 python / py：必须能真正运行且 >= 3.10
    REM    （Windows 应用商店的 python.exe 只是占位符，会在这里被筛掉）
    if not defined PYTHON (
        where python >nul 2>&1
        if not errorlevel 1 (
            python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
            if not errorlevel 1 set "PYTHON=python"
        )
    )
    if not defined PYTHON (
        where py >nul 2>&1
        if not errorlevel 1 (
            py -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
            if not errorlevel 1 set "PYTHON=py"
        )
    )

    REM 2) 常见安装位置兜底（含本脚本上次自动安装的目录）
    if not defined PYTHON (
        for %%P in (
            "%PY_BOOT_DIR%\python.exe"
            "D:\tool\python\python.exe"
            "D:\RJ\Python\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
            "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
            "C:\Python314\python.exe"
            "C:\Python313\python.exe"
            "C:\Python312\python.exe"
        ) do (
            if not defined PYTHON if exist %%P (
                %%P -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
                if not errorlevel 1 set "PYTHON=%%P"
            )
        )
    )

    REM 3) 注册表里的安装路径（覆盖装到自定义目录、没进 PATH 的情况）
    if not defined PYTHON call :probe_registry

    REM 4) 一个都没有：自动下载官方安装包，静默装到 %LOCALAPPDATA%（免管理员）
    if not defined PYTHON (
        call :bootstrap_python
        if exist "%PY_BOOT_DIR%\python.exe" set "PYTHON=%PY_BOOT_DIR%\python.exe"
        if not defined PYTHON call :probe_registry
    )

    if not defined PYTHON (
        echo [ERROR] 未找到可用的 Python 3.10+，自动安装也没成功。
        echo         请手动安装：https://www.python.org/downloads/
        echo         安装时勾选 "Add python.exe to PATH"，然后重新运行本脚本。
        pause
        exit /b 1
    )

    echo   使用 Python: !PYTHON!
    "!PYTHON!" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] 创建虚拟环境失败：!PYTHON! -m venv .venv
        echo         可先手动执行上面这条命令查看完整报错。
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在。
)

echo [2/3] 安装依赖...
"%VENV_PIP%" install -r requirements.txt -q -i %PIP_INDEX%
if errorlevel 1 (
    echo [WARN] 镜像安装失败，改用详细输出重试...
    "%VENV_PIP%" install -r requirements.txt -i %PIP_INDEX%
    if errorlevel 1 (
        echo [WARN] 镜像仍失败，尝试官方源（可能需要代理）...
        "%VENV_PIP%" install -r requirements.txt
        if errorlevel 1 (
            echo [ERROR] 依赖安装失败，请检查网络或代理设置。
            pause
            exit /b 1
        )
    )
)

echo [3/3] 启动服务...
echo.
echo   浏览器: http://localhost:%OPEN_PORT%
echo   下载目录: %~dp0downloads
echo.
echo   按 Ctrl+C 停止。
echo ============================================

start "" cmd /c "timeout /t 2 >nul && start http://localhost:%OPEN_PORT%"
"%VENV_PY%" server.py

pause
exit /b 0

REM ============================================================
REM 从注册表探测 Python 安装路径（HKCU 优先，其次 HKLM）
REM 覆盖「装在自定义目录 / 没加进 PATH」的情况；只认 >= 3.10。
REM ============================================================
:probe_registry
for %%R in (HKCU HKLM) do (
    for %%V in (3.14 3.13 3.12 3.11 3.10) do (
        if not defined PYTHON (
            for /f "tokens=2,*" %%A in ('reg query "%%R\Software\Python\PythonCore\%%V\InstallPath" /ve 2^>nul ^| findstr /i "REG_SZ"') do (
                if not defined PYTHON (
                    set "PYC=%%B"
                    if "!PYC:~-1!"=="\" set "PYC=!PYC:~0,-1!"
                    if exist "!PYC!\python.exe" (
                        "!PYC!\python.exe" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
                        if not errorlevel 1 set "PYTHON=!PYC!\python.exe"
                    )
                )
            )
        )
    )
)
exit /b 0

REM ============================================================
REM 自动安装 Python（只有整台机器都找不到可用 Python 时才执行）
REM 只装到当前用户目录，不需要管理员权限；安装包按 华为云 -> 阿里云
REM -> npmmirror -> 官方 的顺序自动换源。
REM ============================================================
:bootstrap_python
echo.
echo [INFO] 没有找到可用的 Python 3.10+。
echo        正在自动下载并安装 Python %PY_VERSION% 到：
echo        %PY_BOOT_DIR%
echo        （仅首次，约 30MB，只装到当前用户，无需管理员权限）
echo        不想自动安装请按 Ctrl+C 退出，手动安装后重新运行本脚本。
echo.
timeout /t 5 /nobreak >nul 2>&1

set "BOOT_EXE=%TEMP%\bili_dl_python_%PY_VERSION%.exe"
set "BOOT_OK="
if exist "%BOOT_EXE%" del /f /q "%BOOT_EXE%" >nul 2>&1

for %%U in (
    "https://mirrors.huaweicloud.com/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
    "https://mirrors.aliyun.com/python-release/windows/python-%PY_VERSION%-amd64.exe"
    "https://registry.npmmirror.com/-/binary/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
    "https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
) do (
    if not defined BOOT_OK (
        echo   下载: %%~U
        powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -Uri '%%~U' -OutFile '%BOOT_EXE%' -UseBasicParsing -TimeoutSec 300 -ErrorAction Stop; exit 0}catch{if(Test-Path '%BOOT_EXE%'){Remove-Item '%BOOT_EXE%' -Force};exit 1}"
        if not errorlevel 1 if exist "%BOOT_EXE%" set "BOOT_OK=1"
    )
)

if not defined BOOT_OK (
    echo [ERROR] Python 安装包下载失败（网络受限？）。
    exit /b 1
)

echo   安装中（静默，请稍候）...
start /wait "" "%BOOT_EXE%" /quiet InstallAllUsers=0 TargetDir="%PY_BOOT_DIR%" PrependPath=0 Shortcuts=0 Include_test=0 Include_launcher=0 Include_doc=0
del /f /q "%BOOT_EXE%" >nul 2>&1

if exist "%PY_BOOT_DIR%\python.exe" (
    echo   安装完成: %PY_BOOT_DIR%\python.exe
    exit /b 0
)

REM 安装器发现机器上已有同版本 Python 时会进入「维护模式」而不写 TargetDir，
REM 这时到注册表里把那个已存在的解释器找出来继续用。
echo   [WARN] 目标目录里没有生成 python.exe，检查是否复用了已安装的 Python...
call :probe_registry
exit /b 0
