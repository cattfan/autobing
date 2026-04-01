@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title Rewards Bot - CLI
echo.
echo  ======================================
echo       Rewards Bot -- CLI Mode
echo  ======================================
echo.
python main.py --cli
pause
