@echo off
rem Download the latest version of the bot from GitHub.
cd /d "%~dp0"
title Update Up/Down bot
git pull
echo.
echo Versi sekarang:
git log --oneline -1
echo.
echo Kalau bot sedang jalan, tutup dulu jendela bot lalu buka lagi start_bot.bat.
pause
