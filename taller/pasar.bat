@echo off
cd /d "%~dp0"
py -3 mente_pc.py
if errorlevel 1 python mente_pc.py
