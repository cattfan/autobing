@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title Rewards Bot - Web Dashboard
echo.
echo  ======================================
echo       Rewards Bot -- Web Dashboard
echo  ======================================
echo.
echo  Dashboard: http://localhost:23900
echo  (Browser will open automatically)
echo.
python main.py
pause
