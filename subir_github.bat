@echo off
cd /d "%~dp0"
echo ============================================
echo   SUBIR QUANTUM MLB A GITHUB (para Render)
echo ============================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git no esta instalado.
    echo Descarga: https://git-scm.com/download/win
    echo Luego vuelve a ejecutar este archivo.
    pause
    exit /b 1
)

if not exist .git (
    echo Inicializando repositorio...
    git init
    git branch -M main
)

echo.
echo IMPORTANTE: odds_api_key.txt NO se sube a GitHub (esta en .gitignore^)
echo En Render pondras ODDS_API_KEY como variable de entorno.
echo.

git add .
git status
echo.
set /p MSG="Mensaje del commit (Enter = actualizacion): "
if "%MSG%"=="" set MSG=Actualizacion Quantum MLB
git commit -m "%MSG%" 2>nul
if errorlevel 1 echo (Sin cambios nuevos o ya commiteado^)

echo.
set /p REPO="URL de tu repo GitHub (ej: https://github.com/TU_USUARIO/quantum-mlb.git): "
if not "%REPO%"=="" (
    git remote remove origin 2>nul
    git remote add origin %REPO%
    git push -u origin main
    echo.
    echo Listo. Ahora ve a Render.com y conecta ese repositorio.
) else (
    echo.
    echo Crea un repo en github.com/new llamado quantum-mlb
    echo Luego ejecuta de nuevo este script con la URL.
)

pause
