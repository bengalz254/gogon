@echo off
rem Start the read-only monitoring page (http://127.0.0.1:8766).
cd /d "%~dp0"
title Up/Down dashboard
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
python scripts\updown_dashboard.py
echo.
echo Dashboard berhenti. Tekan tombol apa saja untuk menutup jendela ini.
pause >nul
