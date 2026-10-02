# Bot EMA Cross untuk Hyperliquid (`hlbot`)

Bot trading perpetual di [Hyperliquid](https://app.hyperliquid.xyz) dengan
aturan sederhana:

| Aturan | Implementasi |
|---|---|
| **Wajib backtest** | Setiap kali bot dijalankan, backtest otomatis jalan dulu dengan parameter yang sama. Kalau backtest gagal → bot tidak start. Di mode LIVE, kalau hasil backtest rugi → bot menolak trading live. |
| Candle 30 menit harus sudah **ditutup** | Candle yang masih berjalan selalu dibuang. Sinyal dibaca beberapa detik setelah candle tutup. |
| EMA 9 menyilang **ke atas** EMA 21 | Tutup SHORT (kalau ada), lalu buka **LONG**. |
| EMA 9 menyilang **ke bawah** EMA 21 | Tutup LONG (kalau ada), lalu buka **SHORT**. |
| Tidak ada silang | Bot **diam**. |
| Leverage | **10x** (isolated) |
| TP 5%, trailing 0,5% **dari margin** | Setelah profit mencapai **5% dari margin** (di 10x = gerak harga 0,5%), trailing stop aktif **0,5% dari margin** (gerak harga 0,05%) di belakang harga terbaik. |

> ⚠️ **Ini software trading dengan leverage. Bisa rugi uang sungguhan.**
> Di 10x, harga yang bergerak melawan posisi sejauh jarak likuidasi =
> **likuidasi** (seluruh margin posisi hilang). Jaraknya tergantung max
> leverage coin: ±9% untuk BTC (max 40x), tapi hanya **±5%** untuk coin yang
> max leverage-nya 10x. Backtest menampilkan jarak ini untuk coin yang dipakai. Strategi ini **tidak punya stop-loss** (sesuai aturan) —
> posisi rugi hanya ditutup oleh silang EMA berlawanan. Jalankan di mode
> paper / testnet dulu. Bukan saran keuangan.

## Cara kerja TP + trailing (mode `trailing`, default)

Persentase dihitung **dari margin (ROE)** (`pct_basis: margin`). Di 10x,
persen margin ÷ 10 = persen gerak harga:

| | dari margin | gerak harga (10x) |
|---|---|---|
| Take profit | 5% | 0,5% |
| Trailing | 0,5% | 0,05% |

Contoh LONG, entry $100, margin $100 (posisi $1000):

1. Harga $100,49 → belum apa-apa (profit < 5% margin).
2. Harga sentuh **$100,50** (+5% margin = +$5) → trailing aktif, stop di
   $100,50 × 0,9995 ≈ **$100,45**.
3. Harga naik ke $101,00 → stop ikut naik ke ≈ **$100,95**.
4. Harga turun ke $100,94 → **posisi ditutup** di ±$100,95.

Begitu TP tersentuh, profit minimal ±4,5% dari margin (sebelum fee), dan bisa
lebih besar kalau tren berlanjut. Setelah keluar, bot **diam** sampai ada
silang EMA berikutnya.

Kalau ingin persen dihitung dari gerak harga (5% harga = 50% margin di 10x),
ubah ke `pct_basis: price`.

Alternatif `exit_mode: fixed`: TP pasti di +5%, plus trailing stop 0,5% dari
harga terbaik sejak entry (jadi juga berfungsi sebagai stop-loss ketat).
Bandingkan dengan backtest: `python -m hlbot.backtest --exit-mode fixed`.

### ⚠️ Fee vs TP dari margin

Fee taker Hyperliquid 0,045% per eksekusi dihitung dari **nilai posisi**,
bukan dari margin. Di 10x, buka + tutup = 0,09% × 10 = **0,9% dari margin**,
ditambah slippage (±0,4% margin). Jadi trade yang menang dengan profit
minimal 4,5% margin bersih sekitar **+3,2% margin**. Sementara itu trade yang rugi tidak
dibatasi (tanpa stop-loss, hanya ditutup oleh silang EMA berlawanan) dan bisa
-20% margin atau lebih. Pastikan hasil backtest memang positif sebelum live.

## Instalasi

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 1. Backtest (WAJIB)

```bash
# Ambil ~100 hari candle 30m ZEC (coin di config) dari Hyperliquid lalu backtest:
python -m hlbot.backtest

# Coin lain / periode lain:
python -m hlbot.backtest --coin BTC --days 60
python -m hlbot.backtest --coin SOL --save-candles data/sol_30m.csv

# Bandingkan mode exit / coba stop-loss 30% margin (= 3% harga di 10x) tanpa mengubah config:
python -m hlbot.backtest --exit-mode fixed
python -m hlbot.backtest --stop-loss 0.3

# Hitung persen dari gerak harga, bukan margin:
python -m hlbot.backtest --pct-basis price

# Coba setelan lain tanpa mengubah config (TP 20% margin, trailing 5%, leverage 5x):
python -m hlbot.backtest --tp 0.2 --trailing 0.05 --leverage 5

# Bandingkan banyak kombinasi TP / trailing / SL (persen dari margin) sekaligus:
python -m hlbot.backtest --sweep
python -m hlbot.backtest --sweep --leverage 5

# Timeframe lain (data CSV harus timeframe yang sama):
python -m hlbot.backtest --csv data/zec_binance_1h.csv --interval 1h --sweep
```

Hyperliquid hanya menyediakan **5000 candle terakhir** (±104 hari untuk 30m).
Untuk backtest lebih panjang, unduh data kline dari
[data.binance.vision](https://data.binance.vision) (mis.
`data/futures/um/monthly/klines/ZECUSDT/30m/`), gabungkan CSV-nya, lalu:

```bash
python -m hlbot.backtest --csv data/ZECUSDT-30m-2025.csv
```

Output: ringkasan di terminal + `data/hl_backtest_<COIN>_30m_trades.csv`
(setiap trade) dan `..._report.json`.

Yang disimulasikan backtest:
- Sinyal dibaca saat candle **tutup**, eksekusi di **open candle berikutnya**
  (sama seperti bot live).
- TP/trailing dicek di dalam setiap candle. Candle 30m hanya punya
  open/high/low/close, padahal trailing 0,05% harga bisa kena oleh gerakan
  kecil dalam hitungan detik. Karena itu default `intrabar: conservative`:
  begitu trailing aktif, backtest menganggap harga langsung berbalik dan exit
  di profit minimum yang terkunci (TP − trailing). Mode `--intrabar ohlc`
  mengikuti jalur open→low→high→close / open→high→low→close dan **terlalu
  optimis** untuk trailing seketat ini; pakai hanya untuk perbandingan.
- Fee taker 0,045% + slippage tiap entry/exit.
- Likuidasi isolated margin (kehilangan seluruh margin posisi). Maintenance
  margin dihitung otomatis dari max leverage coin di Hyperliquid
  (`maintenance_margin_rate: auto`), jadi jarak likuidasi sesuai coin.
- **Tidak** termasuk funding rate.

Yang perlu dilihat: `Net PnL`, `Max drawdown`, `Likuidasi`, `ROE terburuk`,
dan `Max adverse` (gerak harga terburuk melawan posisi — kalau mendekati
jarak likuidasi di baris `Likuidasi`, artinya hampir terlikuidasi).

## 2. Paper trading (simulasi, tanpa wallet)

```bash
python -m hlbot.main
```

`.env` default `HL_LIVE_TRADING=false` → order hanya disimulasikan memakai
harga live Hyperliquid. Log di `logs/hlbot.log`, riwayat trade di
`data/hl_trades.csv`.

Saat pertama start, bot **tidak** langsung masuk berdasarkan candle yang
sudah lewat — bot menunggu candle 30m berikutnya tutup.

## 3. Live trading

1. Buat **API wallet** di <https://app.hyperliquid.xyz/API> (API wallet bisa
   trading tapi **tidak bisa withdraw** — lebih aman daripada private key
   utama).
2. Isi `.env`:
   ```
   HL_LIVE_TRADING=true
   HL_NETWORK=mainnet          # atau testnet untuk uji coba dulu
   HL_SECRET_KEY=0x...         # private key API wallet
   HL_ACCOUNT_ADDRESS=0x...    # alamat akun UTAMA (yang pegang USDC)
   ```
   **Jangan pernah commit `.env` atau membagikan private key.**
3. Atur ukuran di `config/hyperliquid.yaml` → `trade.margin_usd`
   (default $100 margin × 10x = posisi $1000). Minimal nilai posisi $10.
4. `python -m hlbot.main`

Di mode live:
- Entry/exit memakai order market (IOC, slippage maks `max_slippage`).
- Begitu trailing aktif, bot memasang **stop-market reduce-only di
  exchange** dan menggesernya mengikuti harga, jadi posisi tetap terlindungi
  kalau bot mati. (Sebelum profit 5% margin, tidak ada stop di exchange karena memang tidak
  ada stop-loss.)
- State disimpan di `data/hl_state_<COIN>_live.json`; kalau bot restart,
  posisi yang terbuka dilanjutkan.
- `Ctrl+C` menghentikan bot **tanpa** menutup posisi yang terbuka.

## Konfigurasi (`config/hyperliquid.yaml`)

| Key | Default | Arti |
|---|---|---|
| `strategy.coin` | `ZEC` | Perp yang ditradingkan |
| `strategy.interval` | `30m` | Timeframe candle |
| `strategy.ema_fast` / `ema_slow` | `9` / `21` | Periode EMA |
| `trade.leverage` | `10` | Leverage |
| `trade.margin_mode` | `isolated` | `isolated` atau `cross` |
| `trade.margin_usd` | `100` | Margin per posisi (USD) → posisi $1000 di 10x |
| `trade.take_profit_pct` | `0.05` | 5% |
| `trade.trailing_pct` | `0.005` | 0,5% |
| `trade.pct_basis` | `margin` | Persen dihitung dari `margin` (ROE) atau `price` |
| `trade.exit_mode` | `trailing` | `trailing` atau `fixed` |
| `trade.stop_loss_pct` | `null` (kosong) | Stop-loss 30% margin **disiapkan tapi tidak aktif**. Isi `0.3` untuk mengaktifkan (= harga 3% melawan posisi di 10x) |
| `trade.poll_seconds` | `2` | Interval cek harga untuk TP/trailing |
| `backtest.days` | `100` | Panjang data backtest |
| `backtest.initial_equity_usd` | `1000` | Modal awal simulasi |
| `backtest.maintenance_margin_rate` | `auto` | Dari max leverage coin, untuk estimasi likuidasi |
| `backtest.intrabar` | `conservative` | `conservative` atau `ohlc` |
| `backtest.block_live_if_unprofitable` | `true` | Tolak live bila backtest rugi |

## Tes

```bash
python -m pytest tests/test_hl_*.py
```

Tes memakai data sintetis tanpa koneksi internet: EMA, deteksi silang, candle
yang belum tutup diabaikan, TP/trailing, likuidasi, backtest, loop live
(buka/tutup/balik posisi, restart), dan broker Hyperliquid (SDK di-mock).

## Struktur

```
hlbot/
  indicators.py   # EMA
  strategy.py     # deteksi silang EMA, filter candle yang sudah tutup
  position.py     # TP + trailing stop (dipakai backtest & live)
  backtest.py     # backtester + CLI
  data.py         # API candle Hyperliquid, import/export CSV
  broker.py       # PaperBroker & HyperliquidBroker (hyperliquid-python-sdk)
  config.py       # baca .env + config/hyperliquid.yaml
  main.py         # loop bot: wajib backtest -> tunggu candle tutup -> eksekusi
config/hyperliquid.yaml
```
