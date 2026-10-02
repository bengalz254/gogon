# swarmbot — bot jual beli mood dotswarm.fun (mode PAPER)

Bot ini memantau mood token Solana seperti di https://dotswarm.fun/moods, dan
**membeli secara simulasi** token yang mood-nya **shocked**, **happy**, atau **calm**.
Setiap posisi dijual saat harga **naik 15% (take profit)** atau **turun 10% (stop loss)**.

> ⚠️ **Mode paper saja.** Tidak ada wallet, private key, atau uang sungguhan. Tujuannya
> membuktikan dulu dengan data apakah aturan ini untung. Token launch Solana sangat
> berisiko; tidak ada bot yang menjamin untung. Jangan pernah kirim private key ke chat.

## Dari mana bot tahu mood-nya?

dotswarm.fun tidak punya API publik; halamannya diisi oleh skrip. Tapi di
https://dotswarm.fun/how mereka menjelaskan aturannya dan sumber datanya (Jupiter
Tokens API). Bot ini memakai **data yang sama dan aturan yang sama**, dicek berurutan,
aturan pertama yang cocok menang:

| Mood | Aturan | Dibeli? |
|---|---|---|
| shocked | naik 30%+ dalam 5 menit | ✅ |
| happy | naik 20%+ dalam 1 jam | ✅ |
| focused | sebagian besar trader 1 jam terakhir net pembeli | — |
| graduated | pindah dari bonding curve ke pool dalam 24 jam | — |
| newborn | pool pertama dibuka < 1 jam lalu | — |
| calm | trading stabil: minimal 2 trader/jam, gerak 5m ≤ 5%, gerak 1j ≤ 10% | ✅ |
| suspicious | ditandai mencurigakan oleh Jupiter | — |
| stressed | turun 20%+ dalam 1 jam | — |
| asleep | tidak ada transaksi 1 jam | — |
| rekt | turun 60% dalam 1 jam / 80% dalam sehari | — |

Situs tidak memberi angka untuk "calm", jadi batasnya kita tentukan sendiri (bisa
diubah di `config/swarmbot.yaml`). Karena itu hasil bot bisa sedikit beda dari layar
dotswarm.

Filter keamanan tambahan (bisa diubah): likuiditas minimal $20.000, market cap antara
$50.000 dan $20 juta (koin besar seperti SOL/BTC dilewati), token yang ditandai
mencurigakan dilewati, paling banyak 5 posisi sekaligus, $10 per posisi dari saldo
simulasi $100, dan token yang baru dijual tidak dibeli lagi selama 60 menit.
Simulasi memotong perkiraan fee + selip 1% saat beli dan 1% saat jual, jadi TP 15%
menghasilkan sekitar +13,9% bersih dan SL 10% sekitar −10,9%.

## Pasang di VPS (copy-paste)

**1 (di PC).** Buka PowerShell dan masuk ke VPS:

```powershell
ssh root@IP-VPS
```

**2.** Ambil kode ke folder baru `~/swarmbot`:

```bash
cd ~
git clone -b claude/swarm-mood-bot https://github.com/bengalz254/gogon.git swarmbot
cd ~/swarmbot
```

✅ Tidak ada tulisan `fatal`. Kalau muncul `already exists`, jalankan `cd ~/swarmbot && git pull`.

**3.** Pasang dan nyalakan (1–3 menit):

```bash
bash deploy/setup_swarmbot.sh
```

✅ Di tahap `4/5` ada baris `[OK] ...` dan daftar token, lalu paling bawah
`SELESAI. swarmbot jalan 24/7`.
❌ Kalau ada tulisan merah `GAGAL`, salin semua tulisan di layar dan kirim ke Claude.

Bot ini terpisah dari migbot (folder, service, dan data sendiri), jadi aman jalan di
VPS yang sama.

## Dashboard pantau

Skrip pasang juga menyalakan dashboard (service `swarmbot-dashboard`, biasanya port
8790). Isinya: nilai total, kas, hasil untung/rugi, grafik hasil, posisi terbuka dengan
harga terkini, hasil per mood, mood semua token, kandidat yang akan dibeli, dan
transaksi terakhir. Halaman memperbarui sendiri tiap 5 detik. Dashboard hanya
membaca data; tidak bisa membeli atau menjual.

Dashboard hanya terbuka di dalam VPS (aman). Cara membukanya dari PC:

1. Buka **PowerShell baru** di PC, lalu ketik (ganti IP-VPS):
   ```powershell
   ssh -N -L 8790:127.0.0.1:8790 root@IP-VPS
   ```
   Masukkan password. Kalau terlihat diam saja, itu normal. **Biarkan jendela itu terbuka.**
2. Buka browser di PC: http://127.0.0.1:8790

Kalau skrip pasang menulis port lain (misalnya 8791), pakai angka itu di kedua tempat.

## Sehari-hari

```bash
journalctl -u swarmbot -f                         # log langsung; Ctrl+C keluar, bot tetap jalan
cd ~/swarmbot && venv/bin/python -m swarmbot report   # saldo, untung/rugi, per mood
cd ~/swarmbot && venv/bin/python -m swarmbot moods    # token yang cocok sekarang
systemctl stop swarmbot      # matikan
systemctl start swarmbot     # nyalakan
cd ~/swarmbot && git pull && bash deploy/setup_swarmbot.sh   # update
```

Mengubah angka (TP, SL, besar posisi, dll): `nano ~/swarmbot/config/swarmbot.yaml`,
simpan, lalu `systemctl restart swarmbot`.

Mulai ulang simulasi dari nol: `systemctl stop swarmbot && rm -rf ~/swarmbot/data/swarmbot && systemctl start swarmbot`.

## Kalau Jupiter menolak (HTTP 401/403/429)

Buat API key gratis di https://portal.jup.ag, lalu `nano ~/swarmbot/.env` dan isi
`JUPITER_API_KEY=...`, kemudian `systemctl restart swarmbot`. Bot otomatis pindah ke
`api.jup.ag`.

## Uang sungguhan

Belum dibuat, sengaja. Biarkan mode paper jalan beberapa hari, lihat `report`. Kalau
hasilnya bagus dan kamu mau lanjut, bilang ke Claude; private key nanti hanya ditaruh
di file `.env` di VPS, tidak pernah di chat.
