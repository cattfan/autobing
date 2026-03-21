@echo off
chcp 65001 >nul
title Rewards Bot — Setup
echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Rewards Bot — Automated Setup      ║
echo  ╚══════════════════════════════════════╝
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Install Python 3.10+ from python.org
    echo  Make sure "Add Python to PATH" is checked during installation.
    pause
    exit /b 1
)

echo  [1/3] Installing Python dependencies...
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo  [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)

echo  [2/3] Installing Playwright browser...
playwright install chromium
if %errorlevel% neq 0 (
    echo  [ERROR] Playwright install failed.
    pause
    exit /b 1
)

echo  [3/3] Creating directories...
if not exist "config" mkdir config
if not exist "data" mkdir data
if not exist "profiles" mkdir profiles
if not exist "output" mkdir output

:: Copy example config if no accounts exist
if not exist "config\accounts.json.enc" (
    if not exist "config\accounts.json" (
        echo  [INFO] No accounts found. You can add them via the dashboard.
    )
)

echo.
echo  ╔══════════════════════════════════════╗
echo  ║         Setup Complete!              ║
echo  ╠══════════════════════════════════════╣
echo  ║  Run:  python main.py               ║
echo  ║  Web:  python main.py --web         ║
echo  ║  Auto: python main.py --auto        ║
echo  ╚══════════════════════════════════════╝
echo.
pause
