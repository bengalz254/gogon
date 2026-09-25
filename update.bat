@echo off
rem Download the latest version of the bot from GitHub.
cd /d "%~dp0"
title Update Up/Down bot
git pull
echo.
echo Versi sekarang:
git log --oneline -1
echo.
echo Bot di laptop ini: tutup jendela bot, lalu klik dua kali start_bot lagi.
echo Bot di VPS: file di laptop sudah baru; untuk botnya, masuk ke VPS lalu ketik updown-update.
pause
