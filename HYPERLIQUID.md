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
| TP 2%, trailing 0,5% | Setelah harga bergerak **+2%** searah posisi, trailing stop aktif **0,5%** di belakang harga terbaik. Posisi ditutup saat harga balik 0,5% dari puncak. |

> ⚠️ **Ini software trading dengan leverage. Bisa rugi uang sungguhan.**
> Di 10x, gerak harga ~9% melawan posisi = **likuidasi** (seluruh margin
> posisi hilang). Strategi ini **tidak punya stop-loss** (sesuai aturan) —
> posisi rugi hanya ditutup oleh silang EMA berlawanan. Jalankan di mode
> paper / testnet dulu. Bukan saran keuangan.

## Cara kerja TP + trailing (mode `trailing`, default)

Contoh LONG, entry $100:

1. Harga naik ke $101,9 → belum apa-apa (belum +2%).
2. Harga sentuh **$102** (+2%) → trailing aktif, stop di $102 × 0,995 = **$101,49**.
3. Harga naik ke $105 → stop ikut naik ke $105 × 0,995 = **$104,475**.
4. Harga turun ke $104,4 → **posisi ditutup** di ±$104,475.

Jadi profit minimal ±1,5% harga (≈ 15% ROE di 10x) begitu TP tersentuh, dan
bisa lebih besar kalau tren berlanjut. Setelah keluar, bot **diam** sampai ada
silang EMA berikutnya.

Persentase adalah **gerak harga**, bukan ROE. Di 10x: 2% harga ≈ 20% ROE.

Alternatif `exit_mode: fixed`: TP pasti di +2%, plus trailing stop 0,5% dari
harga terbaik sejak entry (jadi juga berfungsi sebagai stop-loss ketat).
Bandingkan keduanya dengan backtest: `python -m hlbot.backtest --exit-mode fixed`.

## Instalasi

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 1. Backtest (WAJIB)

```bash
# Ambil ~100 hari candle 30m BTC dari Hyperliquid lalu backtest:
python -m hlbot.backtest

# Coin lain / periode lain:
python -m hlbot.backtest --coin ETH --days 60
python -m hlbot.backtest --coin SOL --save-candles data/sol_30m.csv

# Bandingkan mode exit / tambah stop-loss opsional 3%:
python -m hlbot.backtest --exit-mode fixed
python -m hlbot.backtest --stop-loss 0.03
```

Hyperliquid hanya menyediakan **5000 candle terakhir** (±104 hari untuk 30m).
Untuk backtest lebih panjang, unduh data kline dari
[data.binance.vision](https://data.binance.vision) (mis.
`data/futures/um/monthly/klines/BTCUSDT/30m/`), gabungkan CSV-nya, lalu:

```bash
python -m hlbot.backtest --csv data/BTCUSDT-30m-2025.csv
```

Output: ringkasan di terminal + `data/hl_backtest_<COIN>_30m_trades.csv`
(setiap trade) dan `..._report.json`.

Yang disimulasikan backtest:
- Sinyal dibaca saat candle **tutup**, eksekusi di **open candle berikutnya**
  (sama seperti bot live).
- TP/trailing dicek di dalam setiap candle dengan asumsi urutan harga
  open→low→high→close (candle hijau) atau open→high→low→close (candle merah).
- Fee taker 0,045% + slippage tiap entry/exit.
- Likuidasi isolated margin (kehilangan seluruh margin posisi).
- **Tidak** termasuk funding rate.

Yang perlu dilihat: `Net PnL`, `Max drawdown`, `Likuidasi`, `ROE terburuk`,
dan `Max adverse` (gerak harga terburuk melawan posisi — kalau mendekati 9%,
artinya hampir likuidasi).

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
   (default $20 margin × 10x = posisi $200). Minimal nilai posisi $10.
4. `python -m hlbot.main`

Di mode live:
- Entry/exit memakai order market (IOC, slippage maks `max_slippage`).
- Begitu trailing aktif, bot memasang **stop-market reduce-only di
  exchange** dan menggesernya mengikuti harga, jadi posisi tetap terlindungi
  kalau bot mati. (Sebelum +2%, tidak ada stop di exchange karena memang tidak
  ada stop-loss.)
- State disimpan di `data/hl_state_<COIN>_live.json`; kalau bot restart,
  posisi yang terbuka dilanjutkan.
- `Ctrl+C` menghentikan bot **tanpa** menutup posisi yang terbuka.

## Konfigurasi (`config/hyperliquid.yaml`)

| Key | Default | Arti |
|---|---|---|
| `strategy.coin` | `BTC` | Perp yang ditradingkan |
| `strategy.interval` | `30m` | Timeframe candle |
| `strategy.ema_fast` / `ema_slow` | `9` / `21` | Periode EMA |
| `trade.leverage` | `10` | Leverage |
| `trade.margin_mode` | `isolated` | `isolated` atau `cross` |
| `trade.margin_usd` | `20` | Margin per posisi (USD) |
| `trade.take_profit_pct` | `0.02` | 2% |
| `trade.trailing_pct` | `0.005` | 0,5% |
| `trade.exit_mode` | `trailing` | `trailing` atau `fixed` |
| `trade.stop_loss_pct` | `null` | Stop-loss opsional (mis. `0.03`) |
| `backtest.days` | `100` | Panjang data backtest |
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
