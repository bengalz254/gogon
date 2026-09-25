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
rem Open the browser only once the tunnel is up, i.e. after the password was accepted.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 300; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', 8767); $c.Close(); Start-Process 'http://127.0.0.1:8767'; break } catch { Start-Sleep -Seconds 1 } }"
echo Menyambung ke %VPS% ...
echo Masukkan password VPS kalau diminta. Sesudah itu jendela ini memang kosong: artinya tersambung,
echo dan dashboard terbuka sendiri di browser (alamatnya http://127.0.0.1:8767).
echo Biarkan jendela ini terbuka selama melihat dashboard. Tutup jendela ini untuk berhenti.
echo.
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 8767:127.0.0.1:8766 %VPS%
echo.
echo Sambungan tertutup. Kalau password atau alamat VPS salah, hapus file vps_address.txt lalu coba lagi.
pause
