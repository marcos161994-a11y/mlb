@echo off
cd /d "%~dp0"
py -3 noche.py
if errorlevel 1 python noche.py
echo.
echo El resumen quedo en salida\resumen.txt
pause
