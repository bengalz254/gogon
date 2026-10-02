# Bot DCA Futures — Binance SOL/USDT

Bot DCA (Dollar Cost Averaging) untuk Binance USDⓈ-M futures. Default: **SOL/USDT perpetual,
long dan short sekaligus (hedge mode), margin isolated, leverage 3x, modal 1000 USDT**.

> ⚠️ **Futures bisa menghabiskan modal.** Jalankan paper → demo → live dengan modal kecil.
> Ini bukan saran finansial.

## Cara kerja

```
setiap 10 detik:
  ├─ untuk tiap deal aktif (long / short):
  │    ├─ safety order terisi?  → harga rata-rata turun/naik → pasang ulang TP (+SL)
  │    ├─ TP terisi?            → batalkan semua order sisa, catat profit
  │    ├─ harga tembus SL?      → tutup market, cooldown 60 menit
  │    └─ posisi hilang di Binance (SL exchange / manual / likuidasi) → tutup deal
  └─ sisi tanpa deal: lolos filter risiko + sinyal RSI? → buka deal baru
```

1. **Base order** market $40 (nilai posisi, bukan margin).
2. **6 safety order** dipasang langsung sebagai *limit order* di Binance, makin jauh dan makin besar.
3. **Take profit** limit +1% dari harga rata-rata; dipasang ulang setiap ada SO yang terisi.
4. **Stop loss** di -24% dari entry (di luar SO terakhir yang ada di -19.1%). SL dicek oleh bot
   **dan** dipasang juga sebagai STOP_MARKET di Binance, supaya tetap jalan kalau bot mati.

Karena SO dan TP sudah ada di Binance, deal tetap berjalan walaupun bot offline. Saat bot
hidup lagi, ia membaca `data/dca_state_*.json` lalu menyesuaikan dengan kondisi order di Binance.

## Ladder default (`python scripts/dca_plan.py --price 150`)

| Order | Jarak | USDT | Total USDT | Rata-rata (long) | TP |
|---|---|---|---|---|---|
| base | 0% | 40 | 40 | 150.00 | 151.50 |
| SO1 | 1.50% | 40 | 80 | 148.87 | 150.36 |
| SO2 | 3.45% | 60 | 140 | 147.11 | 148.58 |
| SO3 | 5.99% | 90 | 230 | 144.66 | 146.11 |
| SO4 | 9.28% | 135 | 365 | 141.37 | 142.78 |
| SO5 | 13.56% | 202.5 | 567.5 | 136.95 | 138.32 |
| SO6 | 19.13% | 303.75 | 871.25 | 131.06 | 132.37 |

- Margin per sisi kalau semua SO terisi: **$290**. Long + short: **$581 (58% modal)**, sisanya cadangan.
- Likuidasi long saat penuh di sekitar 88 (−41%), jauh di bawah SL 114 (−24%).
- Kerugian kalau kena SL: sekitar **$113 (long)** / **$95 (short)**. `max_daily_loss_usdt: 100` membuat
  bot berhenti membuka deal baru untuk sisa hari itu (UTC) setelah satu kali SL penuh.

Saat start, bot **menolak jalan** kalau konfigurasinya berbahaya: likuidasi terlalu dekat dengan SL,
SL berada di dalam ladder, atau total margin melebihi 90% modal.

## Filter entry

- `mode: rsi` (default): buka long saat RSI(14) TF 15m ≤ 30, short saat ≥ 70.
- `mode: always`: langsung buka deal baru setelah TP.
- `trend_filter: true`: long hanya di atas EMA200, short hanya di bawahnya.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
python scripts/dca_plan.py --price 150     # cek ladder & kebutuhan margin
python scripts/dca_backtest.py --days 180  # backtest data Binance asli
python scripts/dca_sweep.py --csv data/candles_SOLUSDT_15m_180d.csv  # bandingkan banyak variasi setting
```

### 1. Paper trading (tanpa API key)
`DCA_LIVE_TRADING=false` lalu `python -m dca.main`. Harga diambil dari Binance, order hanya disimulasikan.

### 2. Demo Binance (uang palsu, order sungguhan)
Buat API key di demo.binance.com, isi `BINANCE_API_KEY/SECRET`, set `BINANCE_DEMO=true` dan
`DCA_LIVE_TRADING=true`.

### 3. Live
`BINANCE_DEMO=false`. API key: **hanya izin Futures, jangan pernah aktifkan Withdraw, kunci ke IP**.
Bot akan mengubah akun SOLUSDT ke hedge mode, isolated, dan leverage 3x.
Hedge mode tidak bisa diubah kalau masih ada posisi/order terbuka, jadi tutup dulu semuanya.

## File

```
dca/ladder.py     matematika ladder, harga rata-rata, TP, SL, likuidasi (murni, dites)
dca/deal.py       state satu deal
dca/paper.py      broker simulasi (paper + backtest)
dca/live.py       broker Binance: pasang ladder, sinkron fill, TP/SL, flatten
dca/exchange.py   wrapper ccxt binanceusdm (hedge mode, positionSide)
dca/engine.py     loop per sisi, batas rugi harian, cooldown
dca/signals.py    filter RSI / EMA
dca/main.py       entry point: python -m dca.main
config/dca.yaml   semua parameter
data/dca_trades.csv          log semua event
data/dca_state_<mode>.json   deal aktif (untuk resume)
logs/dca.log
```

## Catatan & batasan

- Kalau sudah ada posisi SOL di Binance yang tidak dilacak bot, bot **tidak** membuka deal di sisi itu.
- PnL di log hanya **estimasi** (sudah termasuk fee); angka yang benar ada di riwayat Binance.
- Paper mode hanya mengecek harga terakhir tiap 10 detik, sehingga lonjakan harga singkat di antara
  pengecekan bisa terlewat. Backtest memakai high/low candle dengan urutan konservatif.
- Kode ini **belum pernah dijalankan ke API Binance asli** (container pengembangan tidak punya akses
  jaringan ke Binance). Logika live dites dengan exchange tiruan. Mulai dari paper dan demo dulu.
- Kelemahan DCA: di pasar yang trending kuat, satu kali SL bisa menghapus puluhan kali TP.
  Selalu cek `max drawdown` dan jumlah SL di hasil backtest.
