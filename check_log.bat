@echo off
rem Show the bot's recent connection events from logs\bot.log and copy them
rem to the clipboard, ready to paste into a support chat.
cd /d "%~dp0"
title Up/Down log check
powershell -NoProfile -Command "$log = 'logs\bot.log'; if (-not (Test-Path $log)) { 'logs\bot.log belum ada: jalankan start_bot.bat dulu.'; exit }; $out = @(); $out += Select-String -Path $log -Pattern 'Strategies enabled|clob feed (connected|disconnected)|Event loop' | Select-Object -Last 15 | ForEach-Object { $_.Line }; $s = Select-String -Path $log -Pattern 'stuck here' -Context 0,14 | Select-Object -Last 1; if ($s) { $out += ''; $out += 'Terakhir tertahan di:'; $out += $s.Line; $out += $s.Context.PostContext }; $out; try { $out | Set-Clipboard; ''; 'Sudah disalin ke clipboard: tinggal tempel (Ctrl+V) ke chat.' } catch { ''; 'Blok teks di atas dengan mouse, lalu klik kanan untuk menyalin.' }"
echo.
pause
