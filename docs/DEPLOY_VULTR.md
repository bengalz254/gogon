# Memindahkan Bot ke Server Vultr

Di server, bot jalan 24/7 tanpa VPN, otomatis restart kalau crash atau server
reboot, dan PC kamu boleh dimatikan. Semua perintah di bawah diketik di
**PowerShell PC kamu** (bagian 1) lalu di **terminal server** (bagian 2 dst).

## 0. Server yang disarankan
- OS: **Ubuntu 24.04**
- Paket termurah sudah cukup (1 vCPU, 1 GB RAM).
- Lokasi: **jangan Amerika Serikat** (Binance memblokir IP AS). Eropa
  (London/Amsterdam/Frankfurt) biasanya paling dekat ke server Polymarket.
- Sebelum live trading: pastikan trading di Polymarket diizinkan untuk
  lokasi server dan untuk kamu sendiri menurut syarat & ketentuan Polymarket.

## 1. Masuk ke server
Di PowerShell PC kamu (IP dan password root ada di halaman server di Vultr):
```powershell
ssh root@IP_SERVER
```
Ketik `yes` saat pertama kali, lalu masukkan password.

## 2. Ambil kode bot
```bash
git clone https://github.com/bengalz254/gogon.git /opt/gogon
cd /opt/gogon
git checkout claude/greeting-rlix6b
```
Kalau repo-nya private, git akan meminta username dan password: isi username
GitHub kamu, dan untuk password pakai **Personal Access Token** (GitHub →
Settings → Developer settings → Fine-grained tokens → akses *Contents: Read*
ke repo `gogon`). Password GitHub biasa tidak bisa.

## 3. Cek koneksi dari lokasi server ini
```bash
bash deploy/check.sh
```
Semua harus **OK**. Kalau Binance **FAIL**, lokasi server diblokir: hapus
server, buat baru di lokasi lain.

## 4. Install (satu perintah)
```bash
sudo bash deploy/install.sh
```
Skrip ini memasang Python dan dependensi, membuat user khusus `gogon`,
menyalakan sinkronisasi jam, memasang bot dan dashboard sebagai service
otomatis, dan menutup semua port kecuali SSH. Bot berjalan di **mode paper**.

Pengaturan (`config/updown.yaml`) ikut dari repo: 7 coin, bankroll $1000,
rugi harian maks $250, maks $5 per order.

## 5. Perintah sehari-hari (di server)
| Keperluan | Perintah |
|---|---|
| Laporan P&L per coin + status service | `sudo bash /opt/gogon/deploy/report.sh` |
| Laporan 24 jam terakhir | `sudo bash /opt/gogon/deploy/report.sh --hours 24` |
| Lihat log langsung | `journalctl -u gogon-bot -f` (keluar: Ctrl+C) |
| Update ke versi terbaru | `sudo bash /opt/gogon/deploy/update.sh` |
| Stop / start bot | `sudo systemctl stop gogon-bot` / `sudo systemctl start gogon-bot` |
| Keluar dari server | `exit` (bot tetap jalan) |

## 6. Buka dashboard dari PC
Dashboard tidak dibuka ke internet (aman). Buka lewat terowongan SSH:
di PowerShell PC kamu
```powershell
ssh -L 8766:127.0.0.1:8766 root@IP_SERVER
```
Biarkan jendela itu terbuka, lalu buka **http://127.0.0.1:8766** di browser.
Tutup jendelanya kalau sudah selesai; bot di server tetap jalan.

## 7. Jangan lupa matikan bot di PC
Setelah server jalan, stop bot di PC (`Ctrl+C`) supaya tidak ada dua bot.
Riwayat trade di PC (`data/trades.csv`) tidak ikut pindah; server mulai
dari nol, jadi penilaian ~300 window dihitung dari data server.

## Live trading (nanti, setelah paper terbukti)
Edit `/opt/gogon/.env` di server (`sudo nano /opt/gogon/.env`), isi
`POLY_PRIVATE_KEY`, `POLY_FUNDER_ADDRESS`, `POLY_SIGNATURE_TYPE`, set
`LIVE_TRADING=true`, lalu `sudo systemctl restart gogon-bot`. Private key
hanya ada di file itu (hak akses 600, hanya user `gogon` yang bisa baca);
jangan pernah dikirim ke siapa pun, termasuk ke chat ini.
