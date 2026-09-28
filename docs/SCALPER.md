# Bot Scalping Binance Futures (USDⓈ-M Perpetual)

Bot scalping otomatis untuk Binance USDⓈ-M perpetual futures (BTCUSDT,
ETHUSDT, SOLUSDT, dll.), lengkap dengan backtester, paper trading, testnet,
dashboard lokal, dan notifikasi Telegram.

> ⚠️ **Baca ini dulu.** Futures dengan leverage bisa menghabiskan modal dengan
> cepat. Tidak ada bot — dari Grok, dari saya, atau dari siapa pun — yang bisa
> *menjamin* profit. Hasil bagus beberapa hari/minggu hampir selalu bisa
> dijelaskan oleh keberuntungan; butuh ratusan trade untuk tahu apakah sebuah
> strategi benar-benar punya keunggulan (*edge*). Yang bisa dibuat "10/10"
> adalah **cara bot ini dibangun**: disiplin, aman saat terjadi masalah, dan
> jujur soal performanya. Ikuti urutan **backtest → paper → testnet → live
> dengan modal kecil**, jangan dilompati.

---

## Apa yang membuat bot ini berbeda

| Masalah umum bot scalping | Cara bot ini menanganinya |
|---|---|
| Stop-loss hanya ada di memori bot. Kalau bot crash/internet putus, posisi tanpa pengaman. | Stop-loss dipasang **di exchange** (STOP_MARKET `closePosition`) begitu entry terisi. Kalau stop gagal dipasang, posisi **langsung ditutup**. |
| Ukuran posisi berdasarkan leverage ("pakai 20x!"). | Ukuran posisi berdasarkan **risiko**: tiap trade maksimal rugi `risk_per_trade_pct` (default 0,5%) dari saldo, **fee & slippage sudah dihitung**. Leverage hanya menentukan margin yang terpakai. |
| Fee memakan semua profit. | **Filter biaya**: setup dengan stop terlalu sempit dibanding biaya round-trip dilewati. |
| Backtest terlalu indah (masuk di harga close, anggap TP kena duluan, lupa fee). | Backtest **jujur**: entry di open candle berikutnya + slippage; jika satu candle menyentuh stop *dan* target, dianggap **stop duluan**; fee taker/maker dan funding dihitung; hasil dipisah **in-sample vs out-of-sample**. |
| Kode backtest berbeda dengan kode live. | Strategi, sizing, manajemen posisi, dan simulasi fill **memakai kode yang sama** di backtest, paper, dan live. |
| Timeout jaringan → order terkirim dua kali → posisi dobel. | Setiap order punya client-id unik; saat timeout bot **mencari order itu dulu**, tidak asal kirim ulang. |
| Bot restart → lupa posisi yang sedang terbuka. | State disimpan **atomik** setelah setiap perubahan. Saat start, bot **merekonsiliasi** dengan exchange: posisi yang sudah tertutup dibukukan, stop yang hilang dipasang ulang, order basi dibersihkan, posisi tak dikenal dilindungi. |
| Tidak ada rem. | **Circuit breaker**: batas rugi harian, batas drawdown (bot berhenti total), jeda setelah rugi beruntun, batas jumlah trade per hari, batas posisi terbuka. |
| Dua instance bot jalan bersamaan tanpa sadar. | **Kunci instance**: bot menolak jalan dua kali di mode yang sama. |
| Tidak ada tes. | **120+ tes otomatis**, termasuk exchange Binance palsu yang memverifikasi signature dan mensimulasikan stop kena, crash, timeout, dan API lama/baru. |

---

## Matematika fee: kenapa kebanyakan bot scalping rugi

Biaya satu kali buka + tutup posisi (round-trip) di Binance Futures (VIP0):

```
fee taker masuk 0,05% + fee taker keluar 0,05% + slippage ~0,02% x 2 ≈ 0,14%
```

Di timeframe **1 menit**, pergerakan BTC per candle biasanya cuma 0,05–0,1%.
Kalau stop-loss 0,15% dan target 0,25%, biaya 0,14% itu memakan **hampir
seluruh keuntungan** dan memperbesar kerugian. Butuh win rate di atas ~65%
hanya untuk impas — hampir mustahil konsisten.

Karena itu bot ini:

* default di **timeframe 5m** (masih scalping, trade biasanya selesai dalam
  menit sampai ~2 jam), dengan filter tren dari **1h**;
* menolak setup yang stop-nya kurang dari **3x biaya round-trip**
  (`min_sl_cost_ratio`). Di pasar yang sepi, bot memang *sengaja* jarang
  trading — itu fitur, bukan bug;
* memakai **order limit (maker, fee 0,02%)** untuk take-profit.

---

## Strategi

### `trend_pullback` (default)

Hanya trading **searah tren besar**, dan hanya setelah harga **pullback**
lalu **melanjutkan tren**:

1. Tren 1h naik: close > EMA50 dan EMA20 > EMA50 (hanya candle 1h yang sudah
   close, tanpa "mengintip masa depan").
2. Tren 5m searah (EMA20 > EMA50), ADX ≥ 18 (tren cukup kuat).
3. Dalam 6 candle terakhir harga menyentuh area EMA20 dan RSI sempat turun ≤ 45.
4. Candle pemicu: bullish, close di atas EMA20 **dan** di atas high candle
   sebelumnya, RSI kembali ≥ 50.
5. Volatilitas wajar dan candle pemicu bukan spike berita.

Stop di bawah swing low terakhir (0,8–2,5 ATR). Target 1,5R. Setelah profit
1R stop pindah ke **breakeven + biaya**. Posisi ditutup paksa setelah
24 candle (2 jam). Short adalah kebalikannya.

### `range_reversion` (opsional)

Untuk pasar **sideways** (ADX rendah): setelah candle close di luar Bollinger
Band dengan RSI ekstrem, entry saat harga **kembali masuk** ke dalam band,
target di garis tengah. Strategi ini rugi saat pasar trending — backtest dulu
di simbol pilihanmu.

---

## Langkah demi langkah

### 0. Instalasi

Butuh Python 3.10 atau lebih baru (3.11/3.12 disarankan).

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements-scalper.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Semua pengaturan strategi & risiko ada di `config/scalper.yaml` (lengkap
dengan penjelasan). API key hanya di `.env`.

### 1. Backtest (wajib)

```bash
python -m scalper backtest --days 180
```

Mengunduh candle dari Binance (disimpan di `data/scalper/klines/`), lalu
menampilkan hasil per simbol + laporan HTML di `data/scalper/backtests/…/report.html`.

Opsi berguna: `--symbols BTCUSDT SOLUSDT`, `--timeframe 15m`,
`--strategy range_reversion`, `--balance 500`. Kalau tidak bisa akses API
Binance, unduh file CSV manual dari
[data.binance.vision](https://data.binance.vision/?prefix=data/futures/um/monthly/klines/)
lalu: `python -m scalper backtest --csv BTCUSDT-5m-2025-08.csv --symbols BTCUSDT`.

**Cara membaca hasil:**

* **Trades** di bawah ~100: sampel terlalu kecil untuk menyimpulkan apa pun.
* **Profit factor** (total untung ÷ total rugi): < 1 rugi, 1–1,2 tipis sekali,
  > 1,3 lumayan, > 2 di scalping patut dicurigai (overfit / data terlalu pendek).
* **OUT-OF-SAMPLE** = 30% data terakhir. Ini ujian yang paling jujur. Kalau
  out-of-sample rugi, **jangan jalankan live**, sebagus apa pun kolom lainnya.
* **Max drawdown**: bayangkan kerugian itu benar-benar terjadi di akunmu. Masih
  sanggup? Kalau tidak, turunkan `risk_per_trade_pct`.
* Baris **Verdict** memberi interpretasi konservatif secara otomatis.

Mau mencoba variasi parameter?

```bash
python -m scalper optimize --days 180
```

Menguji 36 kombinasi pada 70% data awal, lalu menunjukkan hasil kombinasi
terbaik pada 30% data yang **tidak pernah dilihat**. Kalau hasil terbaik
in-sample ambruk di out-of-sample, itu *overfitting* — pilih setting yang bagus
di **kedua** sisi dan yang tetangganya juga bagus.

### 2. Paper trading (minimal 1–2 minggu)

Tidak perlu API key. Harga live, eksekusi simulasi.

```bash
python -m scalper check          # cek konfigurasi & koneksi
python -m scalper run            # SCALPER_MODE=paper (default)
python -m scalper dashboard      # di terminal lain: http://127.0.0.1:8766
```

Bandingkan hasil paper dengan backtest. Kalau jauh berbeda, cari tahu kenapa
sebelum lanjut.

Ingin melihat tampilan dashboard sebelum ada trade? `python -m scalper dashboard --demo`
mengisinya dengan trade dari logika bot yang asli di atas **harga acak**. Di
pasar acak tidak ada strategi yang punya edge, jadi hasil demo hanyalah noise
dikurangi fee. Dalam 10 simulasi acak 3 minggu, hasilnya berkisar dari +42
sampai −85 USDT — satu di antaranya "untung" murni karena beruntung. Itulah
sebabnya hasil beberapa minggu tidak bisa dipakai untuk menilai bot mana pun.

### 3. Testnet (uang mainan, order sungguhan)

1. Buat akun & API key (HMAC) di <https://testnet.binancefuture.com>.
   Kalau situs itu mengarahkan ke **Binance Demo Trading**, buat API key di
   Demo Trading dan tambahkan `BINANCE_TESTNET_FAPI_URL=https://demo-fapi.binance.com`
   ke `.env`.
2. Isi `BINANCE_TESTNET_API_KEY` dan `BINANCE_TESTNET_API_SECRET` di `.env`
   (key testnet, **bukan** key akun Binance asli).
3. Jalankan berurutan:

   ```bash
   python -m scalper check --mode testnet      # koneksi, key, mode akun
   python -m scalper selftest                  # uji siklus order lengkap (~30 detik)
   python -m scalper run --mode testnet        # jalankan bot
   ```

`selftest` membuka posisi **sekecil mungkin**, lalu menguji semua yang
dibutuhkan bot: stop-loss diterima exchange, take-profit reduce-only, stop bisa
dipindah (seperti breakeven), posisi bisa ditutup, tidak ada order tertinggal,
dan data fill bisa dibaca untuk menghitung P&L. Ia selalu membersihkan posisinya
sendiri, bahkan kalau ada langkah yang gagal, dan menolak berjalan di akun live.
Tanpa ini, bisa berjam-jam sampai sinyal pertama muncul sebelum kamu tahu
apakah eksekusinya bekerja.

Setelah bot jalan, cek di UI testnet: entry terisi, stop-loss & take-profit
muncul di tab *Open Orders*, stop pindah ke breakeven, dsb. Catatan: harga
testnet terpisah dari pasar asli dan sering aneh, jadi **jangan menilai
profitabilitas dari testnet**.

### 4. Live dengan modal kecil

1. Buat **sub-account** khusus bot (disarankan) dan isi saldo kecil di
   dompet USDⓈ-M Futures.
2. Buat API key: centang **Enable Futures** saja. **Jangan pernah** mengaktifkan
   *Enable Withdrawals*. Batasi ke **IP server**-mu.
3. Isi `BINANCE_API_KEY` & `BINANCE_API_SECRET`, set `SCALPER_MODE=live`.
4. `python -m scalper check` — akan **gagal** kalau key bisa withdraw.
5. `python -m scalper run` (ada hitung mundur 10 detik; Ctrl+C untuk batal).

Akun harus **One-way Mode** (Binance Futures → ⚙ Preferences → Position Mode).
**Jangan trading manual** di simbol yang sama pada akun yang sama — bot
membersihkan order di simbol itu sebelum entry, dan akan "mengadopsi" posisi
yang tidak ia kenal. Karena itu sub-account sangat disarankan.

---

## Perintah

| Perintah | Fungsi |
|---|---|
| `python -m scalper run [--mode paper\|testnet\|live]` | Menjalankan bot |
| `python -m scalper check` | Cek konfigurasi, koneksi, izin API key, mode akun, dan preview ukuran posisi |
| `python -m scalper selftest` | **Testnet saja**: buka-lindungi-tutup satu posisi kecil untuk memastikan API order Binance bekerja |
| `python -m scalper backtest` | Backtest (`--days`, `--symbols`, `--timeframe`, `--strategy`, `--csv`, `--balance`) |
| `python -m scalper optimize` | Grid parameter kecil + validasi out-of-sample |
| `python -m scalper download` | Hanya mengunduh/menyegarkan cache candle |
| `python -m scalper status` | Posisi terbuka, P&L hari ini, statistik jurnal |
| `python -m scalper dashboard` | Dashboard web lokal (hanya-baca) |
| `python -m scalper dashboard --demo` | Pratinjau dashboard dengan trade demo di atas **harga acak** (bukan hasil trading, bukan perkiraan profit; disimpan terpisah) |
| `python -m scalper reset-risk` | Menghapus status "halted" setelah drawdown limit (setelah kamu evaluasi penyebabnya) |

Menghentikan bot: **Ctrl+C**. Posisi yang masih terbuka tetap dilindungi
stop-loss & take-profit di exchange (atau set `execution.flatten_on_exit: true`
agar semua posisi ditutup saat bot berhenti).

File penting:

* `data/scalper/trades_<mode>.csv` — jurnal setiap trade yang sudah tertutup
  (entry, exit, alasan, fee, P&L bersih, R).
* `data/scalper/state_<mode>.json` — state bot (jangan diedit saat bot jalan).
* `logs/scalper.log` — log lengkap.

---

## Pengaturan risiko terpenting (`config/scalper.yaml`)

| Pengaturan | Default | Arti |
|---|---|---|
| `risk.risk_per_trade_pct` | 0.5 | Rugi maksimal per trade (% saldo), termasuk fee. 0,25–1% itu wajar. |
| `risk.max_open_positions` | 2 | BTC/ETH/SOL bergerak bersama; banyak posisi = satu taruhan besar. |
| `risk.max_daily_loss_pct` | 3.0 | Rugi harian tercapai → tidak ada entry baru sampai 00:00 UTC (07:00 WIB). |
| `risk.max_drawdown_pct` | 15.0 | Turun 15% dari puncak → bot **berhenti total** sampai `reset-risk`. |
| `risk.max_consecutive_losses` | 3 | Rugi 3x beruntun → jeda `loss_cooldown_minutes`. |
| `risk.min_sl_cost_ratio` | 3.0 | Stop minimal 3x biaya round-trip. |
| `execution.leverage` | 5 | Leverage **tidak** memperbesar risiko per trade (itu diatur di atas); leverage tinggi hanya mendekatkan likuidasi. |
| `management.breakeven_at_r` | 1.0 | Stop pindah ke breakeven + biaya setelah profit 1R. |
| `management.max_bars_in_trade` | 24 | Tutup posisi setelah 24 candle (2 jam di 5m). |

Setiap nama pengaturan yang salah ketik akan **ditolak** saat start, jadi
tidak ada setting yang diam-diam diabaikan.

---

## Notifikasi Telegram

1. Chat [@BotFather](https://t.me/BotFather) → `/newbot` → salin token.
2. Kirim pesan apa saja ke bot barumu, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` dan salin `chat.id`.
3. Isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di `.env`, lalu set
   `notify.telegram_enabled: true`.

Kamu akan menerima pesan saat bot start/stop, entry, exit (dengan P&L),
peringatan (stop hilang, limit harian), dan heartbeat tiap jam.

---

## Menjalankan 24/7 (VPS)

Bot scalping harus jalan terus. Pakai VPS Linux kecil (1 vCPU / 1 GB cukup)
di region yang tidak diblokir Binance, dengan sinkronisasi jam aktif
(`timedatectl set-ntp true`). Contoh service systemd
(`/etc/systemd/system/scalper.service`):

```ini
[Unit]
Description=Binance scalper
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/gogon
ExecStart=/home/ubuntu/gogon/venv/bin/python -m scalper run --yes
Restart=on-failure
RestartSec=30
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now scalper` lalu `journalctl -u scalper -f` untuk log.
Kalau bot restart, ia melanjutkan dari state terakhir dan merekonsiliasi
dengan exchange.

---

## Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| `Hedge Mode` saat start | Ubah ke One-way Mode di pengaturan Futures (harus tanpa posisi/order), atau set `execution.auto_one_way_mode: true`. |
| Error `-2015` / `-1022` / `-2014` | Key/secret salah, Futures belum dicentang di API key, IP tidak di-whitelist, atau key testnet dipakai di mode live (dan sebaliknya). |
| Error `-1021` (timestamp) | Jam komputer tidak sinkron. Bot sudah mengoreksi otomatis; aktifkan sinkronisasi waktu otomatis di OS. |
| HTTP 451 / "restricted location" | Binance memblokir wilayah server/VPS-mu. Pakai VPS di region lain. |
| "stop-loss could not be placed" | Bot menutup posisi demi keamanan. Kalau berulang, coba `execution.conditional_order_api: legacy` (atau `algo`), lalu ulangi `python -m scalper selftest`. |
| Bot tidak pernah entry | Normal di pasar sepi: lihat `python -m scalper check` (baris "fee filter") dan log `signal skipped`. Jangan buru-buru melonggarkan filter — backtest dulu. |
| `below exchange minimum` | Saldo terlalu kecil untuk minimum notional simbol itu (BTCUSDT biasanya 100 USDT) pada risk yang dipakai. Tambah saldo atau pilih simbol lain. |
| `Another scalper instance is already running` | Ada bot lain yang jalan di mode yang sama. Hentikan dulu. |

---

## Menambahkan strategi sendiri (misalnya strategi dari bot Grok)

1. Buat file baru di `scalper/strategies/`, turunkan dari `Strategy`
   (lihat `trend_pullback.py` sebagai contoh). Implementasikan `_on_candle`
   (dipanggil setiap candle **close**) yang mengembalikan `Signal` berisi arah,
   stop-loss, dan target — atau `None`.
2. Tambahkan dataclass parameternya di `scalper/config.py` dan daftarkan di
   `build_strategy` (`scalper/strategies/__init__.py`).
3. Backtest dengan `--strategy nama_strategi`. Sizing, filter biaya,
   breakeven, circuit breaker, dan eksekusi otomatis ikut berlaku.

---

## Batasan yang perlu kamu tahu

* Kode ini dikembangkan dan diuji terhadap **simulasi** API Binance (120+ tes),
  bukan akun Binance sungguhan — lingkungan pengembangannya tidak punya akses
  ke server Binance. Karena itu **testnet adalah langkah wajib** sebelum live.
* Order kondisional (stop-loss) memakai **Algo Order API** Binance
  (`/fapi/v1/algoOrder`) dengan fallback otomatis ke endpoint lama. Kalau
  Binance mengubah API lagi, bot akan gagal memasang stop dan **menutup
  posisi** (aman), dan kamu akan melihatnya di log/Telegram.
* Hanya One-way Mode; hanya kontrak perpetual USDⓈ-M.
* Backtest memakai candle, bukan order book: slippage dan eksekusi hanya
  perkiraan. Funding dihitung sebagai biaya konstan.
* Hasil masa lalu tidak menjamin hasil masa depan. Pasar berubah; strategi
  yang bagus tahun lalu bisa rugi tahun ini. Evaluasi ulang secara berkala.
