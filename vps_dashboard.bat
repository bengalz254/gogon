@echo off
rem Open the dashboard of the bot running on your VPS. The VPS dashboard only
rem listens on the server's own 127.0.0.1, so this opens an SSH tunnel to it
rem and shows it at http://127.0.0.1:8767 on this PC.
cd /d "%~dp0"
title Up/Down dashboard (VPS)
set "VPS="
if exist vps_address.txt set /p VPS=<vps_address.txt
if not defined VPS set /p VPS=Alamat VPS (contoh root@203.0.113.5): 
if not defined VPS exit /b
>vps_address.txt echo %VPS%
start "" /min cmd /c "ping -n 9 127.0.0.1 >nul & start http://127.0.0.1:8767"
echo Menyambung ke %VPS% ... dashboard terbuka di browser dalam beberapa detik.
echo Biarkan jendela ini terbuka selama melihat dashboard. Tutup jendela ini untuk berhenti.
ssh -N -L 8767:127.0.0.1:8766 %VPS%
echo.
echo Sambungan tertutup. Kalau alamat VPS salah, hapus file vps_address.txt lalu coba lagi.
pause
