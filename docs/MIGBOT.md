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

## Pasang di VPS (±15 menit)

Bot ini jalan berdampingan dengan bot lain di VPS yang sama (nama service dan port
dashboard-nya sendiri: `migbot`, port 8780).

### 1. Ambil kode (folder baru, tidak mengganggu bot lain)

```bash
cd ~
git clone -b claude/kind-mayer-gr7tjq https://github.com/bengalz254/gogon.git migbot
cd ~/migbot
```

### 2. Jalankan skrip instalasi

```bash
bash deploy/setup_migbot.sh
```

Skrip ini memasang Python dan paket, menjalankan tes otomatis, membuat `.env`,
mengecek koneksi ke semua sumber data (`python -m migbot check`), lalu memasang dua
service yang otomatis hidup lagi setelah crash atau VPS restart:

* `migbot`: bot-nya;
* `migbot-dashboard`: dashboard di `127.0.0.1:8780` (atau port kosong berikutnya).

✅ **Cek:** di akhir muncul `SELESAI. Bot migbot jalan 24/7`.

### 3. Isi `.env` (disarankan)

```bash
nano ~/migbot/.env
```

| Isian | Kegunaan |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alert beli/jual + ringkasan harian ke HP (cara buat: @BotFather → `/newbot`) |
| `SOLANA_RPC_URL` | **Sangat disarankan.** RPC publik Solana sering membatasi request, sehingga cek holder/dev bisa dilewati. Daftar gratis di [helius.dev](https://www.helius.dev), lalu isi `https://mainnet.helius-rpc.com/?api-key=KEY_KAMU` |
| `PUMPPORTAL_API_KEY`, `JUPITER_API_KEY` | Opsional. Hanya kalau PumpPortal/Jupiter meminta key |

Setelah mengubah `.env`: `sudo systemctl restart migbot`, lalu tes Telegram:

```bash
cd ~/migbot && venv/bin/python -m migbot check --telegram-test
```

---

## Dashboard

**Dari PC (PowerShell)**, jalankan perintah tunnel yang dicetak skrip (biasanya):

```powershell
ssh -N -L 8780:127.0.0.1:8780 root@IP-VPS
```

Biarkan jendelanya terbuka, lalu buka <http://127.0.0.1:8780>.

**Dari HP (Tailscale sudah terpasang di VPS):**

```bash
tailscale serve --bg --https=8453 8780
```

Lalu buka alamat `https://nama-vps.xxxx.ts.net:8453/` dari HP (Tailscale menyala).
Jangan pakai `tailscale funnel` (itu membuka dashboard ke seluruh internet).

Isi dashboard:

* **P&L paper**: saldo + nilai posisi − modal awal (5 SOL simulasi).
* **Token migrated yang sedang dipantau**: status (dipantau / dibeli / ditolak /
  tanpa data), market cap, likuiditas, volume 5 menit, rasio beli, dan hasil setiap
  filter (klik untuk melihat semua cek ✓/✕).
* **Riset: dibeli vs ditolak**: bagian terpenting (lihat di bawah).
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
