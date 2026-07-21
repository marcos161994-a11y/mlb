@echo off
cd /d "%~dp0"
echo ============================================
echo   SUBIR MEMORIA LOCAL A LA NUBE (Render)
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0subir_memoria_nube.ps1" %*
pause
