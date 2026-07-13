@echo off
cd /d "%~dp0"
echo Cerrando servidor viejo en puerto 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
echo Instalando dependencias...
if exist requirements.txt (python -m pip install -q -r requirements.txt) else (echo requirements.txt no encontrado, saltando...)
echo.
echo Sincronizando fecha del experimento...
python sincronizar_fecha.py
echo ========================================
echo   INICIANDO QUANTUM MLB
echo ========================================
echo.
echo Iniciando servidor Python...
python servidor_mlb.py
