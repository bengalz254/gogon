@echo off
rem Start the Up/Down bot. It stays in paper mode unless live trading is
rem switched on in BOTH .env (LIVE_TRADING=true) and config\updown.yaml
rem (execution.allow_live: true).
cd /d "%~dp0"
title Up/Down bot
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat
python -m bot.updown --record
echo.
echo Bot berhenti. Tekan tombol apa saja untuk menutup jendela ini.
pause >nul
