@echo off
chcp 65001 >nul
title Rewards Bot — Full Setup
color 0F
echo.
echo  ╔══════════════════════════════════════════════╗
echo  ║     Rewards Bot — Automated Full Setup       ║
echo  ╠══════════════════════════════════════════════╣
echo  ║  This will install everything automatically  ║
echo  ╚══════════════════════════════════════════════╝
echo.

:: ═══════════════════════════════════════════
:: Step 1: Check / Install Python
:: ═══════════════════════════════════════════
echo  [1/4] Checking Python...
python --version >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo         Found Python %%v
    goto :python_ok
)

echo         Python not found. Installing...
echo.

:: Try winget first (Windows 10 1709+, Windows 11)
winget --version >nul 2>&1
if %errorlevel% equ 0 (
    echo         Installing via winget...
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent
    if %errorlevel% equ 0 (
        echo         Installed! Refreshing PATH...
        call refreshenv >nul 2>&1
        set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
        goto :verify_python
    )
)

:: Fallback: download installer from python.org
echo         winget not available, downloading from python.org...
set "PY_URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
set "PY_INSTALLER=%TEMP%\python_installer.exe"

:: Use PowerShell to download
powershell -Command "Write-Host '        Downloading...' ; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' -UseBasicParsing"
if not exist "%PY_INSTALLER%" (
    echo  [ERROR] Download failed. Please install Python manually from python.org
    pause
    exit /b 1
)

echo         Running installer (silent, adds to PATH)...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
if %errorlevel% neq 0 (
    echo  [ERROR] Python installer failed. Try running it manually:
    echo         %PY_INSTALLER%
    pause
    exit /b 1
)

:: Update PATH for this session
set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"

:verify_python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Python installed but not in PATH yet.
    echo         Please close this window and run setup.bat again.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo         Python %%v ready!

:python_ok
echo.

:: ═══════════════════════════════════════════
:: Step 2: Install pip dependencies
:: ═══════════════════════════════════════════
echo  [2/4] Installing dependencies...
python -m pip install --upgrade pip --quiet >nul 2>&1
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo  [ERROR] pip install failed. Check internet connection.
    pause
    exit /b 1
)
echo         All packages installed!
echo.

:: ═══════════════════════════════════════════
:: Step 3: Install Playwright browser
:: ═══════════════════════════════════════════
echo  [3/4] Installing browser (Chromium)...
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo  [ERROR] Playwright browser install failed.
    pause
    exit /b 1
)
echo         Browser ready!
echo.

:: ═══════════════════════════════════════════
:: Step 4: Create directories
:: ═══════════════════════════════════════════
echo  [4/4] Creating directories...
if not exist "config" mkdir config
if not exist "data" mkdir data
if not exist "profiles" mkdir profiles
if not exist "output" mkdir output
echo         Done!
echo.

:: ═══════════════════════════════════════════
:: Complete
:: ═══════════════════════════════════════════
echo  ╔══════════════════════════════════════════════╗
echo  ║           Setup Complete!                    ║
echo  ╠══════════════════════════════════════════════╣
echo  ║                                              ║
echo  ║   Double-click  start.bat  to run the bot   ║
echo  ║                                              ║
echo  ╚══════════════════════════════════════════════╝
echo.
pause
