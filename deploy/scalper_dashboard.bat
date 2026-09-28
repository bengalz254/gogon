@echo off
rem Buka dashboard scalper yang jalan di VPS, langsung dari PC Windows.
rem Klik dua kali: file ini membuat terowongan SSH ke VPS lalu membuka browser.
rem Alamat VPS ditanyakan sekali saja, lalu disimpan di %APPDATA%\scalper-vps.txt.
rem Password TIDAK pernah disimpan.
rem Kalau setup_vps.sh memakai port lain (lihat baris "ssh -N -L ..." di akhir
rem hasilnya), ganti angka PORT di bawah.
setlocal
set "PORT=8777"
set "CFG=%APPDATA%\scalper-vps.txt"
if "%~1"=="wait" goto wait

set "VPS="
if exist "%CFG%" set /p VPS=<"%CFG%"
if defined VPS goto connect
echo Alamat VPS belum tersimpan.
set /p "VPS=Ketik IP VPS-mu lalu Enter: "
if not defined VPS goto end
echo %VPS%| findstr /l "@" >nul || set "VPS=root@%VPS%"
>"%CFG%" echo %VPS%

:connect
title Scalper Dashboard - biarkan jendela ini terbuka
echo Menghubungkan ke %VPS% ...
echo Masukkan password VPS kalau diminta (tidak tampil saat diketik), lalu tekan Enter.
echo Browser terbuka sendiri. Tutup jendela ini kalau sudah selesai melihat dashboard.
echo.
start "" /min "%~f0" wait
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L %PORT%:127.0.0.1:%PORT% %VPS%
echo.
echo Terowongan tertutup: koneksi putus, password salah, atau jendela dashboard lain masih terbuka.
choice /c yn /m "Ganti alamat VPS yang tersimpan (%VPS%)"
if errorlevel 2 goto end
del "%CFG%" >nul 2>&1
echo Alamat dihapus. Klik dua kali file ini lagi untuk mengisi alamat baru.
pause
goto end

:wait
rem Jendela kecil pembantu: tunggu sampai terowongan siap, lalu buka browser.
for /l %%i in (1,1,180) do (
  curl -s -o nul http://127.0.0.1:%PORT%/ && (start "" http://127.0.0.1:%PORT%/ & exit)
  timeout /t 1 /nobreak >nul
)
exit

:end
endlocal
