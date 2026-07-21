@echo off
cd /d "%~dp0"
echo ============================================
echo   DESPERTAR NUBE QUANTUM MLB (Render)
echo ============================================
echo.
echo URL panel: https://mlb-1-en7i.onrender.com
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0despertar_nube.ps1" %*
pause
