# Bot EMA Cross untuk Hyperliquid (`hlbot`)

Bot trading perpetual di [Hyperliquid](https://app.hyperliquid.xyz). Setelan
saat ini (`config/hyperliquid.yaml`):

| Aturan | Implementasi |
|---|---|
| **Wajib backtest** | Setiap kali bot dijalankan, backtest otomatis jalan dulu dengan parameter yang sama. Kalau backtest gagal → bot tidak start. Di mode LIVE, kalau hasil backtest rugi → bot menolak trading live. |
| Pair | **SOL** (SOLUSDT) |
| Candle **4 jam** harus sudah **ditutup** | Candle yang masih berjalan selalu dibuang. Sinyal dibaca beberapa detik setelah candle tutup. |
| EMA 9 menyilang **ke atas** EMA 21 | Tutup SHORT (kalau ada), lalu buka **LONG**. |
| EMA 9 menyilang **ke bawah** EMA 21 | Tutup LONG (kalau ada), lalu buka **SHORT**. |
| Tidak ada silang | Bot **diam** (posisi yang ada tetap dipegang). |
| Leverage | **5x** isolated, margin **$100** per posisi (posisi $500) |
| Take profit | **Tidak ada.** Posisi hanya ditutup oleh silang EMA berlawanan atau stop-loss. |
| Stop-loss | **30% dari margin** = harga bergerak 6% melawan posisi (rugi ±$30). |

> ⚠️ **Ini software trading dengan leverage. Bisa rugi uang sungguhan.**
> Setelan di atas dipilih dari backtest **ZEC**, belum teruji di **SOL** —
> jalankan uji SOL di bawah sebelum paper/live. Funding rate tidak dihitung
> di backtest; karena bot hampir selalu memegang posisi, funding bisa
> mengurangi hasil. Bukan saran keuangan.

## Kenapa setelan ini (hasil uji ZEC)

Semua tes memakai margin $100, modal simulasi $1000, fee 0,045% + slippage.
Syarat lolos: **untung di ketiga periode** dengan setelan yang sama persis.

| Setelan | Jul 2024 – Jun 2025 (sideways) | Jul 2025 – Mei 2026 | Data terbaru Hyperliquid | Lolos |
|---|---|---|---|---|
| 30m, TP 5% + trailing 0,5% (setelan awal) | 23 bulan, 5x: -$1317 | — | 10x: -$226 (100 hari) | ❌ |
| 30m, TP 50% 10x + SL 30% / TP 30% 5x (terbaik di 100 hari terakhir) | 23 bulan: -$760 / +$25 | — | +$751 / +$391 (100 hari) | ❌ |
| 15m, 5x, tanpa TP, tanpa SL | -$1320 | +$664 | +$156 (50 hari) | ❌ |
| 1h, 5x, tanpa TP, SL 30% | -$151 | +$1595 | +$352 (120 hari) | ❌ |
| **4h, 5x, tanpa TP, SL 30%** | **+$54** | **+$1703** | **+$521** (120 hari) | ✅ |

Pelajaran:
- **TP kecil merusak strategi ini.** Win rate memang tinggi, tapi profit per
  trade kecil dan habis dimakan fee, sementara kerugian saat sinyal berbalik
  besar. Strategi EMA cross butuh membiarkan tren berjalan.
- **Timeframe kecil = terlalu banyak trade.** Di 15m/30m fee 2 tahun mencapai
  $650–1300 dan sinyal palsu di pasar sideways menghabiskan modal.
- **Tanpa SL, ZEC sering terlikuidasi** (sampai 8 kali di 4h). SL 30% margin
  mencegahnya.
- Di periode sideways, 4h pun hanya **impas** (+$54, drawdown ±34%).
  Profit datang dari periode tren. Siapkan mental untuk masa datar/minus
  yang panjang.

## Take profit + trailing (opsional, saat ini MATI)

Isi `trade.take_profit_pct` (mis. `0.5` = 50% margin) untuk mengaktifkan:
setelah profit mencapai TP, trailing stop `trailing_pct` mengikuti harga
terbaik. Persen dihitung dari margin (`pct_basis: margin`): persen margin ÷
leverage = persen gerak harga. Ingat fee taker 0,045% dihitung dari **nilai
posisi**: buka + tutup = 0,09% × leverage dari margin. Di uji ZEC semua
variasi TP lebih buruk daripada tanpa TP.

## Instalasi

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 1. Backtest (WAJIB)

```bash
# Ambil ±800 hari candle 4h SOL (setelan di config) dari Hyperliquid lalu backtest:
python -m hlbot.backtest

# Coin lain / periode lain:
python -m hlbot.backtest --coin BTC --days 120

# Tanpa SL / dengan TP 50% margin, tanpa mengubah config (0 = mati):
python -m hlbot.backtest --stop-loss 0
python -m hlbot.backtest --tp 0.5 --trailing 0.005

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

Hyperliquid hanya menyediakan **5000 candle terakhir** (±833 hari di 4h,
±104 hari di 30m). Output: ringkasan di terminal +
`data/hl_backtest_<COIN>_<TF>_trades.csv` (setiap trade) dan `..._report.json`.

### Uji coin baru di data panjang (Binance)

Wajib dilakukan setiap ganti coin. Contoh SOL 4h, dibagi 3 periode
(2022 – Jun 2024, Jul 2024 – Jun 2025, Jul 2025 – Mei 2026) ditambah 120 hari
terakhir di Hyperliquid. Setelan yang layak harus **untung di semua periode**.
Jalankan dari folder `hlbot` dengan venv aktif (`unzip` dan `wget` harus
terpasang):

```bash
C=SOLUSDT; TF=4h
mkdir -p data/binance && cd data/binance
for m in 2022-{01..12} 2023-{01..12} 2024-{01..12} 2025-{01..12} 2026-{01..05}; do
  wget -q https://data.binance.vision/data/futures/um/monthly/klines/$C/$TF/$C-$TF-$m.zip && unzip -o -q $C-$TF-$m.zip
done
cat $C-$TF-2022-*.csv $C-$TF-2023-*.csv $C-$TF-2024-0[1-6].csv > ../${C}_${TF}_p0.csv
cat $C-$TF-2024-0[7-9].csv $C-$TF-2024-1*.csv $C-$TF-2025-0[1-6].csv > ../${C}_${TF}_p1.csv
cat $C-$TF-2025-0[7-9].csv $C-$TF-2025-1*.csv $C-$TF-2026-*.csv > ../${C}_${TF}_p2.csv
cd ../..
F='BACKTEST|Jumlah|Net PnL|Profit factor|Max drawdown|Likuidasi|Biaya'
for p in p0 p1 p2; do python -m hlbot.backtest --csv data/${C}_${TF}_$p.csv | grep -E "$F"; done
python -m hlbot.backtest --days 120 | grep -E "$F"
```

Kalau `C` berbeda dari `strategy.coin` di config, tambahkan `--coin <NAMA>`
(dan `--interval` kalau `TF` berbeda) di setiap perintah `hlbot.backtest`.

Yang disimulasikan backtest:
- Sinyal dibaca saat candle **tutup**, eksekusi di **open candle berikutnya**
  (sama seperti bot live).
- Stop-loss (dan TP/trailing bila aktif) dicek di dalam setiap candle. Candle hanya punya
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
sudah lewat — bot menunggu candle 4h berikutnya tutup (candle 4h Hyperliquid
tutup tiap 00:00, 04:00, 08:00, ... UTC = 07:00, 11:00, 15:00, ... WIB). Jadi
bisa beberapa hari sebelum trade pertama, karena harus menunggu silang EMA.

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
   (default $100 margin × 5x = posisi $500). Minimal nilai posisi $10.
4. `python -m hlbot.main`

Di mode live:
- Entry/exit memakai order market (IOC, slippage maks `max_slippage`).
- Begitu posisi dibuka, bot memasang **stop-market reduce-only di exchange**
  di harga stop-loss (dan menggesernya mengikuti harga bila trailing aktif),
  jadi posisi tetap terlindungi kalau bot mati. Silang EMA berlawanan tetap
  butuh bot yang menyala.
- State disimpan di `data/hl_state_<COIN>_live.json`; kalau bot restart,
  posisi yang terbuka dilanjutkan.
- `Ctrl+C` menghentikan bot **tanpa** menutup posisi yang terbuka.

## Konfigurasi (`config/hyperliquid.yaml`)

| Key | Default | Arti |
|---|---|---|
| `strategy.coin` | `SOL` | Perp yang ditradingkan |
| `strategy.interval` | `4h` | Timeframe candle |
| `strategy.ema_fast` / `ema_slow` | `9` / `21` | Periode EMA |
| `trade.leverage` | `5` | Leverage |
| `trade.margin_mode` | `isolated` | `isolated` atau `cross` |
| `trade.margin_usd` | `100` | Margin per posisi (USD) → posisi $500 di 5x |
| `trade.take_profit_pct` | `null` (mati) | TP dari margin, mis. `0.5` = 50% |
| `trade.trailing_pct` | `0.005` | Trailing setelah TP (hanya bila TP diisi) |
| `trade.pct_basis` | `margin` | Persen dihitung dari `margin` (ROE) atau `price` |
| `trade.exit_mode` | `trailing` | `trailing` atau `fixed` |
| `trade.stop_loss_pct` | `0.3` | Stop-loss 30% margin (= harga 6% melawan posisi di 5x). `null` = mati |
| `trade.poll_seconds` | `2` | Interval cek harga untuk SL (dan TP/trailing) |
| `backtest.days` | `800` | Panjang data backtest dari Hyperliquid |
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
