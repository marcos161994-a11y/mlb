@echo off
cd /d "%~dp0"
schtasks /Create /F /TN "MenteMLB-Manana" /SC DAILY /ST 08:00 /TR "\"%~dp0pasar.bat\""
schtasks /Create /F /TN "MenteMLB-Noche" /SC DAILY /ST 23:30 /TR "\"%~dp0pasar.bat\""
echo.
echo Quedaron dos tareas: 8:00 y 23:30.
echo La PC tiene que estar encendida a esa hora.
echo Cada pasada se guarda en boveda_mental\memoria.jsonl
pause
