# Bot Meme Coin Migrated (pump.fun → PumpSwap) — mode PAPER

Bot ini khusus untuk meme coin Solana yang **sudah migrasi**: token pump.fun yang
bonding curve-nya penuh lalu pindah ke pool PumpSwap (kolom *Migrated* di GMGN).
Bot memantau setiap migrasi, menunggu beberapa menit, memeriksa token dengan filter
ala GMGN, lalu **membeli secara simulasi (paper)** token yang lolos.

> ⚠️ **Mode paper saja.** Tidak ada wallet, private key, atau uang sungguhan di bot
> ini. Itu sengaja. Tujuan tahap ini adalah membuktikan dulu, dengan data, apakah
> filternya benar-benar memilih token yang lebih bagus. Meme coin sangat berisiko;
> tidak ada bot yang bisa menjamin untung.

---

## Bedanya dengan bot pump.fun / gmgn yang lama

| Bot lama | Bot ini |
|---|---|
| Beli token di bonding curve (sebelum migrasi) atau copy wallet | **Hanya token yang sudah migrasi**, dengan likuiditas pool sungguhan |
| Masuk secepat mungkin (bersaing dengan sniper) | **Sengaja menunggu 3 menit** setelah migrasi supaya dump awal dev/sniper lewat dulu |
| Bergantung penuh pada API tidak resmi GMGN (sering diblokir Cloudflare) | Data utama dari sumber publik yang stabil; **GMGN hanya tambahan**. Kalau GMGN memblokir, bot tetap jalan |
| Tidak ada cara menilai apakah filternya bekerja | **Setiap token migrasi dipantau 2 jam, dibeli atau tidak.** Laporan membandingkan token yang dibeli dengan yang ditolak |

## Cara kerja

```
1. Deteksi migrasi   PumpPortal (WebSocket, real-time) + GeckoTerminal (cadangan)
2. Pantau            harga, likuiditas, volume, jumlah transaksi dari DexScreener (tiap 10 detik)
3. Tunggu            3 menit setelah migrasi (entry.delay_seconds)
4. Filter pasar      likuiditas, market cap, volume 5m, transaksi 5m, rasio beli, harga vs awal
5. Filter keamanan   (on-chain lewat Solana RPC) mint/freeze authority, ekstensi Token-2022
                     berbahaya, top 10 holder, holder terbesar, % dev; RugCheck; GMGN kalau bisa
6. Beli (paper)      harga dari quote Jupiter (fee pool + price impact asli) + selip + priority fee
7. Jual (paper)      stop loss, take profit bertingkat, trailing stop, batas waktu, likuiditas anjlok
8. Catat             setiap token: harga 1, 3, 5, 10, 15, 30, 60, 120 menit setelah migrasi
```

Holder dihitung **hanya dari wallet biasa**: vault pool, akun program, dan alamat burn
tidak ikut dihitung, jadi likuiditas di pool tidak dianggap sebagai "whale".

Setiap alert Telegram dan setiap token di dashboard punya **link ke GMGN**, jadi kamu
bisa langsung membuka token itu di GMGN dari HP.

---

## Mulai dari sini (langkah demi langkah)

Semua perintah diketik di VPS, kecuali yang ditandai **(di PC)** atau **(di browser)**.
Setiap langkah punya **✅ Cek**. Jangan lanjut sebelum hasilnya sesuai; kalau ada
**❌**, ikuti petunjuknya atau kirim tulisan di layar ke Claude.

Bot ini ringan dan bisa jalan di VPS yang sama dengan bot lain (service-nya
`migbot`, dashboard-nya port 8780, folder-nya `~/migbot`).

### Tahap A — Masuk ke VPS

**Langkah 1 (di PC).** Buka PowerShell, lalu:

```powershell
ssh root@IP-VPS
```

Ganti `IP-VPS` dengan IP server Vultr-mu. Ketik password (hurufnya tidak muncul, itu
normal), lalu Enter.

✅ **Cek:** muncul `root@...:~#`.
❌ `Connection timed out`: IP salah atau VPS mati. Cek di dashboard Vultr.

### Tahap B — Pasang bot

**Langkah 2.** Ambil kode ke folder baru `~/migbot`:

```bash
cd ~
git clone -b claude/kind-mayer-gr7tjq https://github.com/bengalz254/gogon.git migbot
```

✅ **Cek:** tidak ada tulisan `fatal`.
❌ `destination path 'migbot' already exists`: folder sudah ada. Jalankan
`cd ~/migbot && git pull`, lalu lanjut ke Langkah 3.

**Langkah 3.** Masuk ke folder:

```bash
cd ~/migbot
ls
```

✅ **Cek:** terlihat `migbot`, `config`, `deploy`, `docs`, `tests`.

**Langkah 4.** Jalankan skrip instalasi (2–5 menit, jangan ditutup):

```bash
bash deploy/setup_migbot.sh
```

✅ **Cek:**
- tahap `2/5` diakhiri `... passed` (tes otomatis lolos);
- tahap `4/5` punya `[OK] PumpPortal` **atau** `[OK] GeckoTerminal`, dan
  `[OK] DexScreener`. Baris `[!] GMGN ... diblokir` dan `[!] Telegram belum diisi`
  itu normal;
- paling bawah: `SELESAI. Bot migbot jalan 24/7 di VPS ini (mode PAPER).` beserta
  perintah tunnel dashboard. Catat nomor port-nya (biasanya 8780).

❌ Berhenti dengan tulisan merah `GAGAL: ...`: salin semua tulisan dari `==> 4/5`
sampai bawah dan kirim ke Claude. Jangan lanjut dulu.

### Tahap C — Telegram dan RPC (disarankan)

**Langkah 5.** Cari token Telegram dari bot lain yang sudah pernah dipakai:

```bash
grep -H TELEGRAM ~/*/.env
```

✅ **Cek:** muncul baris seperti `/root/scalper-bot/.env:TELEGRAM_BOT_TOKEN=123456:ABC...`
dan `...TELEGRAM_CHAT_ID=...`. Catat kedua nilainya. Bot Telegram yang sama boleh
dipakai; pesan bot ini diawali `[migbot]`. Abaikan baris `/root/migbot/.env` yang
masih kosong.
❌ Tidak ada yang terisi: buat bot baru. Di Telegram buka **@BotFather**, kirim
`/newbot`, ikuti petunjuknya, salin token. Kirim "halo" ke bot barumu, buka
`https://api.telegram.org/bot<TOKEN>/getUpdates` di browser, lalu salin angka
setelah `"chat":{"id":`.

**Langkah 6 (di browser).** Daftar RPC Solana gratis di <https://www.helius.dev>
(Sign up), lalu di dashboard Helius salin URL RPC **mainnet**, bentuknya
`https://mainnet.helius-rpc.com/?api-key=...`.

✅ **Cek:** kamu punya URL yang diawali `https://mainnet.helius-rpc.com/?api-key=`.
Tanpa ini bot tetap jalan, tapi cek holder/dev sering dilewati karena RPC publik
membatasi request.

**Langkah 7.** Masukkan ke `.env`:

```bash
nano ~/migbot/.env
```

Tekan panah ↓ sampai bagian `migbot` di bawah. Isi setelah tanda `=` (tanpa spasi;
di PowerShell, klik kanan = tempel):

```
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Simpan: `Ctrl+O`, Enter, lalu `Ctrl+X`.

✅ **Cek:** `grep -E "SOLANA_RPC_URL|TELEGRAM" ~/migbot/.env` menampilkan ketiga baris
itu beserta isinya.

**Langkah 8.** Restart bot dan tes semuanya:

```bash
sudo systemctl restart migbot
cd ~/migbot && venv/bin/python -m migbot check --telegram-test
```

✅ **Cek:**
- Telegram menerima `[migbot] Tes notifikasi ... berhasil ✅` dan `🟢 Bot migrated mulai (PAPER)`;
- ada `[OK] Solana RPC` tanpa tulisan "sebagian gagal";
- baris terakhir `✅ Siap.`

❌ `[!] Telegram gagal`: token atau chat id salah ketik. Ulangi Langkah 7.

### Tahap D — Pastikan bot jalan

**Langkah 9.** Lihat log:

```bash
sudo journalctl -u migbot -n 20 --no-pager
```

✅ **Cek:** ada `migbot 1.0.0 started (PAPER)` dan
`PumpPortal connected, subscribing to migrations`.

**Langkah 10.** Tunggu migrasi pertama (biasanya 5–30 menit, tergantung ramai pasar):

```bash
sudo journalctl -u migbot -f
```

✅ **Cek:** muncul `Migration ... via pumpportal`. Tekan `Ctrl+C` untuk keluar dari log
(bot **tetap** jalan). Sekitar 3 menit setelah migrasi, token itu dibeli
(`PAPER BUY`) atau ditolak (alasannya terlihat di dashboard).

### Tahap E — Buka dashboard

**Langkah 11 (di PC).** Buka jendela PowerShell **baru** (jendela SSH yang lama biarkan
saja), lalu:

```powershell
ssh -N -L 8780:127.0.0.1:8780 root@IP-VPS
```

Pakai nomor port dari Langkah 4 kalau bukan 8780. Masukkan password. Jendelanya
terlihat diam: itu normal, biarkan terbuka. Lalu buka <http://127.0.0.1:8780> di browser.

✅ **Cek:** judul **Migrated Meme Bot** dan tulisan hijau **✓ bot jalan**.
❌ `Address already in use`: port itu di PC dipakai program lain. Pakai
`ssh -N -L 8790:127.0.0.1:8780 root@IP-VPS`, lalu buka <http://127.0.0.1:8790>.

**Langkah 12 (opsional, dashboard di HP lewat Tailscale).** Di VPS:

```bash
tailscale status
```

- Muncul daftar perangkat: jalankan `sudo tailscale serve --bg --https=8453 8780`.
  ✅ **Cek:** muncul alamat `https://....ts.net:8453/`; buka di HP dengan aplikasi
  Tailscale menyala.
- `command not found`: pasang dulu dengan
  `curl -fsSL https://tailscale.com/install.sh | sh` lalu
  `sudo tailscale up --accept-dns=false`, buka link login yang muncul (akun Tailscale
  yang sama dengan di HP), lalu jalankan perintah `tailscale serve` di atas.

Jangan pakai `tailscale funnel` (itu membuka dashboard ke seluruh internet).

### Tahap F — Setelah itu

**Langkah 13.** Biarkan jalan minimal 3–7 hari **tanpa mengubah pengaturan**. Pantau
dashboard dan Telegram (ringkasan harian jam 08:00 WITA / 07:00 WIB).

**Langkah 14.** Ambil laporan dan kirim ke Claude:

```bash
cd ~/migbot && venv/bin/python -m migbot report
```

Dari laporan itu kelihatan apakah baris **lolos filter** lebih bagus dari **ditolak**.

| Perlu | Perintah |
|---|---|
| Hentikan bot | `sudo systemctl stop migbot` |
| Nyalakan lagi | `sudo systemctl start migbot` |
| Update bot | `cd ~/migbot && git pull && bash deploy/setup_migbot.sh` |

---

## Dashboard

Isi dashboard:

* **P&L paper**: saldo + nilai posisi − modal awal (5 SOL simulasi).
* **Token migrated yang sedang dipantau**: status (dipantau / dibeli / lolos, tak
  dibeli / ditolak / tanpa data), market cap, likuiditas, volume 5 menit, rasio beli, dan hasil setiap
  filter (klik untuk melihat semua cek ✓/✕).
* **Riset: lolos filter vs ditolak**: bagian terpenting (lihat di bawah).
* **Sumber data**: status PumpPortal, GeckoTerminal, DexScreener, RPC, RugCheck,
  GMGN, Jupiter, dan Telegram.

---

## Cara menilai hasilnya (baca ini sebelum memikirkan uang sungguhan)

```bash
cd ~/migbot && venv/bin/python -m migbot report
```

Laporan menampilkan kenaikan harga **dari titik beli** (3 menit setelah migrasi) untuk
tiga kelompok:

| Kelompok | Artinya |
|---|---|
| `semua` | Semua token migrasi. Ini gambaran "beli acak" |
| `lolos filter` | Token yang lolos semua filter (termasuk yang tidak jadi dibeli karena batas risiko, misalnya sudah 3 posisi terbuka) |
| `dibeli` | Token yang benar-benar dibeli (paper) |
| `ditolak` | Token yang tidak lolos |

**Filter hanya berguna kalau baris `lolos filter` jelas lebih bagus dari `ditolak` dan
`semua`**, terutama di kolom 30m/60m, dan P&L paper positif **setelah fee**.

* Di bawah 30 posisi yang selesai, hasil apa pun masih bisa kebetulan. Tunggu
  100+ posisi sebelum menyimpulkan.
* Kalau `lolos filter` tidak lebih bagus dari `ditolak`, filternya tidak memilih apa-apa.
  Mengubah angka filter lalu melihat hasil minggu depan boleh saja, tapi hati-hati:
  mencoba banyak setting sampai ada yang "untung" di data lama biasanya gagal di
  data baru.
* Kolom `pernah 2x` dan `pernah −50%` menunjukkan seberapa sering token sempat
  naik 2x atau turun setengah dalam 2 jam.
* **Alasan ditolak terbanyak** memberi tahu filter mana yang paling sering menolak.
  Kalau satu risiko RugCheck menolak hampir semua token, lihat daftarnya di laporan
  dan pertimbangkan `safety.rugcheck.ignore_risks`.

Harga paper sudah memperhitungkan fee pool dan price impact (quote Jupiter), selip
tambahan 1%, dan priority fee + tip 0.001 SOL per transaksi (2% pulang-pergi untuk
posisi 0.1 SOL). Transaksi sungguhan bisa
tetap lebih buruk (telat, MEV), jadi hasil paper adalah batas atas, bukan jaminan.

---

## Pengaturan penting (`config/migbot.yaml`)

Semua pengaturan ada penjelasannya di file itu. Yang paling sering diubah:

| Pengaturan | Bawaan | Arti |
|---|---|---|
| `entry.delay_seconds` | 180 | Tunggu 3 menit setelah migrasi sebelum boleh beli |
| `entry.window_seconds` | 900 | Lewat 15 menit tanpa lolos filter = ditolak |
| `filters.min_liquidity_usd` | 10000 | Likuiditas pool minimal |
| `filters.min_market_cap_usd` / `max_market_cap_usd` | 40k / 1.5M | Rentang market cap |
| `filters.min_volume_5m_usd`, `min_txns_5m`, `min_buy_ratio_5m` | 5000, 60, 0.5 | Token harus ramai dan lebih banyak yang beli |
| `filters.max_top10_pct`, `max_top_holder_pct`, `max_dev_hold_pct` | 30, 10, 5 | Konsentrasi holder (100 = mati) |
| `filters.min_holders`, `min_smart_buys`, `max_sniper_count` | 0 | Filter GMGN (0 = mati; hanya berlaku kalau GMGN bisa diakses) |
| `trading.buy_sol` | 0.1 | Ukuran beli simulasi per token |
| `trading.max_open_positions` | 3 | Posisi terbuka bersamaan |
| `trading.max_daily_loss_sol` | 0.5 | Rugi hari ini sampai segini → berhenti beli sampai 00:00 UTC (08:00 WITA) |
| `exits.stop_loss_pct` | 35 | Jual semua kalau turun 35% dari harga beli |
| `exits.take_profit` | `[[100, 0.5]]` | Naik 100% → jual 50% posisi awal |
| `exits.trailing_start_pct` / `trailing_pct` | 50 / 30 | Setelah pernah +50%, jual semua kalau turun 30% dari puncak |
| `exits.max_hold_minutes` | 60 | Jual semua setelah 60 menit |

Nama pengaturan yang salah ketik akan ditolak saat bot start (lihat
`journalctl -u migbot`). Setelah mengubah config: `sudo systemctl restart migbot`.

**Soal GMGN:** GMGN tidak punya API publik resmi dan sering memblokir request dari
server. Bot memakai data GMGN (holder, smart money, sniper) hanya kalau bisa diakses,
tanpa mencoba mengakali blokirnya. Kalau diblokir, dashboard menulis "diblokir oleh
GMGN" dan filter khusus GMGN dilewati; filter lainnya tetap berjalan.

---

## Perintah sehari-hari (di VPS)

| Perintah | Fungsi |
|---|---|
| `sudo journalctl -u migbot -f` | Log langsung (Ctrl+C = keluar, bot tetap jalan) |
| `cd ~/migbot && venv/bin/python -m migbot report` | Laporan riset + P&L |
| `cd ~/migbot && venv/bin/python -m migbot check` | Cek koneksi semua sumber data |
| `sudo systemctl stop migbot` / `start migbot` | Hentikan / nyalakan bot |
| `sudo systemctl disable --now migbot migbot-dashboard` | Matikan total (tidak hidup lagi saat reboot) |
| `sudo systemctl stop migbot && cd ~/migbot && venv/bin/python -m migbot reset && sudo systemctl start migbot` | Mulai dari nol: data lama dipindah ke `data/migbot/archive/`, tidak dihapus |
| `cd ~/migbot && git pull && bash deploy/setup_migbot.sh` | Update bot |

File data (`data/migbot/`): `tokens.csv` (riset, satu baris per token migrasi),
`trades.csv` (semua beli/jual paper), `state.json` (posisi dan saldo; jangan diedit
saat bot jalan), `status.json` (untuk dashboard).

---

## Kalau ada masalah

| Gejala | Penyebab & solusi |
|---|---|
| Dashboard: PumpPortal "gagal/terputus" | Koneksi WebSocket putus; bot menyambung ulang sendiri. Selama itu migrasi tetap ditemukan lewat GeckoTerminal (telat ±1 menit) |
| Tidak ada migrasi berjam-jam | Cek `migbot check`. Kalau PumpPortal dan GeckoTerminal OK, pasar memang sedang sepi |
| Solana RPC banyak gagal / "HTTP 429" | RPC publik membatasi request. Isi `SOLANA_RPC_URL` dengan RPC gratis Helius, lalu restart |
| Semua token ditolak dengan alasan yang sama | Lihat "Alasan ditolak terbanyak". Mungkin filter itu terlalu ketat untuk pasar sekarang; ubah pelan-pelan dan catat tanggal perubahannya |
| GMGN "diblokir" | Normal untuk server VPS. Bot tetap jalan tanpa filter khusus GMGN |
| Jupiter gagal | Harga simulasi memakai estimasi fee 1.25% + selip 3% (lebih pesimis). Kalau Jupiter meminta API key, isi `JUPITER_API_KEY` |
| `Config salah` / service berhenti dengan exit code 2 | Ada pengaturan yang salah ketik di `config/migbot.yaml`; pesannya menyebut yang mana |
| Port 8780 dipakai program lain | Skrip instalasi memilih port kosong berikutnya dan mencetak perintah tunnel yang benar |

---

## Langkah berikutnya

Mode live (beli sungguhan lewat wallet) **belum ada, dan itu disengaja**. Minta
ditambahkan hanya kalau setelah **100+ posisi paper** baris `lolos filter` jelas lebih bagus
dari `ditolak` dan P&L paper positif setelah fee. Kalau nanti ditambahkan, syaratnya:
wallet khusus bot dengan saldo kecil yang siap hilang, dan uji dengan satu transaksi
kecil dulu.
