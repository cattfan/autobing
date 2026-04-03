@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
title AutoBing - CLI Mode
echo.
echo  ======================================
echo       AutoBing -- CLI Interactive
echo  ======================================
echo.
python main.py --cli
pause
