@echo off
chcp 65001 >nul
title Rewards Bot
echo.
echo  ======================================
echo       Rewards Bot -- Starting...
echo  ======================================
echo.
echo  Dashboard: http://localhost:8080
echo  (Browser will open automatically)
echo.
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8080"
python main.py --web
pause
