@echo off
echo === Instalando Google Cloud SDK para Windows ===
echo.

REM Crear directorio temporal
if not exist "%TEMP%\gcloud" mkdir "%TEMP%\gcloud"

REM Descargar instalador de Google Cloud SDK
echo Descargando Google Cloud SDK...
powershell -Command "Invoke-WebRequest -Uri 'https://dl.google.com/dlcloudsdk/release/google-cloud-sdk-474.0.0-windows-x86_64-bundled-python.zip' -OutFile '%TEMP%\gcloud\gcloud-sdk.zip'"

REM Descomprimir
echo Descomprimiendo archivos...
powershell -Command "Expand-Archive -Path '%TEMP%\gcloud\gcloud-sdk.zip' -DestinationPath 'C:\' -Force"

REM Agregar al PATH
echo Agregando al PATH...
setx PATH "%PATH%;C:\google-cloud-sdk\bin" /M

REM Iniciar instalación
echo Iniciando instalación...
C:\google-cloud-sdk\install.bat --usage-reporting=false --path-update=true --command-completion=false

echo.
echo === Instalación completada ===
echo.
echo Por favor, cierra esta terminal y abre una nueva para que los cambios surtan efecto.
echo Luego ejecuta: gcloud auth application-default login
echo.
pause
