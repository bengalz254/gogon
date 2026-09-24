# Bot Polymarket "Up or Down" 5 Menit

Bot ini untuk market Polymarket seperti **"Bitcoin Up or Down – 5 minute"**.
Setiap 5 menit ada market baru. Hasilnya **Up** kalau harga BTC di akhir
window ≥ harga di awal window ("price to beat"), dan **Down** kalau lebih rendah.
Saham yang menang dibayar $1, yang kalah $0.

> ⚠️ **Ini software trading dan bisa bikin rugi uang sungguhan.** Default-nya
> mode paper (simulasi). Jalankan paper minimal beberapa hari dulu. Tidak ada
> jaminan profit, dan ini bukan nasihat keuangan.

## Strateginya: fair value, bukan tebak arah

Kebanyakan bot 5 menit mencoba **menebak arah** candle berikutnya pakai
RSI, MACD, dan sejenisnya. Di horizon 5 menit, hasilnya hampir sama dengan
lempar koin. Karena ada fee, lempar koin berarti rugi pelan-pelan.

Bot ini tidak menebak. Bot ini **menghitung harga wajar** setiap detik:

```
P(Up) = Φ( ln(S / K) / √(σ² · τ + basis²) )
```

| Simbol | Arti |
|---|---|
| `S` | harga BTC sekarang (feed Binance) |
| `K` | harga BTC di awal window (dicatat sendiri oleh bot) |
| `τ` | sisa detik sampai window tutup |
| `σ` | volatilitas per √detik, diestimasi live (EWMA) |
| `basis` | selisih antara feed kita dan oracle Chainlink yang dipakai settlement |

Contoh: sisa 60 detik, BTC sudah naik 0,1% dari harga awal. Model bilang
peluang Up sekitar 90%. Kalau di Polymarket saham Up masih dijual 0,62
(karena harga pasar telat bergerak), berarti ada **edge 28 sen per saham**
sebelum fee. Itu yang dibeli.

### Aturan masuk
1. Hanya trading saat **sisa waktu 240–8 detik**. Menit pertama dilewati
   karena model belum punya informasi (±50/50) dan fee paling mahal di harga 50c.
   Beberapa detik terakhir juga dilewati karena order bisa telat sampai.
2. Edge **setelah fee taker** minimal 4 sen per saham (`min_edge`).
3. **Uji ketahanan volatilitas.** Edge harus tetap ada walaupun volatilitas
   sebenarnya 30% lebih rendah *atau* 30% lebih tinggi dari estimasi. Ini
   membuang "edge palsu" yang cuma muncul karena beda asumsi volatilitas.
   Tanpa aturan ini, bot cenderung membeli saham longshot murah dan rugi
   pelan-pelan (sudah dibuktikan di simulator, lihat di bawah).
4. **Pelindung data salah.** Kalau model dan pasar beda >30 sen, bot
   menganggap datanya sendiri yang salah (feed macet atau strike keliru),
   jadi tidak trading.
5. Harga saham harus di antara 8c dan 92c. Tidak pernah memegang Up dan Down sekaligus.

### Ukuran taruhan
Pakai **Kelly fraksional**: `f* = (p − c) / (1 − c)`, dikali 0,15. Tetap
dibatasi `max_bet_usd`, batas per window, dan sisa jatah rugi harian. Order
bertipe **FAK** (fill-and-kill): ambil likuiditas yang ada sampai harga
limit, sisanya dibatalkan. Tidak ada order yang menggantung.

### Aturan keluar
Posisi biasanya ditahan sampai settlement. Tapi kalau ada yang menawar
(bid) **lebih tinggi dari nilai wajar + 6c** setelah fee, saham dijual ke
mereka. Ini bisa berarti ambil untung, atau potong rugi dengan harga bagus.

### Detail penting: strike dari feed sendiri
Settlement membandingkan Chainlink-akhir dengan Chainlink-awal. Bot
membandingkan Binance-sekarang dengan **Binance-awal**, bukan dengan "price
to beat" dari Polymarket. Dengan begitu, selisih tetap antara Binance dan
Chainlink saling menghapus. Kalau sumbernya dicampur, selisih itu (bisa
puluhan dolar) langsung masuk ke perhitungan dan merusak model. Akibatnya,
bot harus sudah jalan **sebelum** window dibuka. Window yang sedang berjalan
saat bot dinyalakan akan dilewati.

### Manajemen risiko
- Rugi harian maksimal `max_daily_loss_usd`. Setelah itu bot berhenti sampai tengah malam UTC.
- Setelah kalah 4 window berturut-turut, bot istirahat 30 menit.
- Kalau harga feed lebih tua dari 3 detik, bot tidak trading.

## Hasil simulator (jujur)

Simulator (`--simulate`) membuat harga BTC palsu dan market maker palsu yang
harganya **telat `lag` detik**. Rata-rata dari 6 seed, masing-masing 288 window (1 hari), modal $100:

| Kondisi pasar | Rata-rata P&L | Trade | Terburuk |
|---|---|---|---|
| Pasar adil (lag 0 detik) | $0 | 0 window | $0 |
| Harga telat 1 detik | +$461 | 180 window | +$231 |
| Harga telat 2 detik | +$802 | 224 window | +$700 |

**Cara membacanya dengan benar:**
- Di pasar yang adil, bot **tidak trading**. Itu yang diinginkan: tidak ada edge, tidak ada taruhan.
- Semua profit di simulator berasal dari **asumsi** bahwa harga Polymarket telat.
  Angka itu bukan ramalan profit. Di dunia nyata banyak bot lain berlomba
  mengambil harga murah yang sama, jadi fill kamu akan lebih sedikit.
- Jawaban yang sebenarnya hanya didapat dari **paper trading di market asli**.

## Cara pakai

```bash
pip install -r requirements.txt
cp .env.example .env     # biarkan LIVE_TRADING=false

# 1. Cek model dengan data BTC asli (7 hari candle 1 menit dari Binance)
python scripts/updown_calibrate.py --days 7
#    Lihat "skill" (harus positif) dan tabel kalibrasi: "predicted" harus
#    mendekati "actual". Kalau model terlalu yakin, naikkan vol_multiplier.

# 2. Simulator offline (tidak butuh internet)
python -m updown.main --simulate --lag 1

# 3. Paper trading: harga dan order book asli, fill disimulasikan
python -m updown.main

# 4. Dashboard (terminal kedua)
python scripts/dashboard.py
```

### Dashboard radar

```powershell
# terminal 1: bot
python -m updown.main
# terminal 2: dashboard (otomatis membuka http://127.0.0.1:8766)
python scripts/updown_dashboard.py

# coba tampilan tanpa bot / tanpa internet (pakai simulator):
python scripts/updown_dashboard.py --demo
```

Isi dashboard:
- **Radar scope.** Satu putaran sapuan = satu window 5 menit. Titik yang
  bergerak adalah harga BTC: di luar cincin strike artinya di atas harga awal
  (Up, biru), di dalam cincin artinya di bawah (Down, oranye). Jaraknya dari
  cincin dihitung dalam satuan volatilitas. Belah ketupat menandai order yang
  terisi, dan pita hijau di pinggir menandai zona trading. Di tengah ada
  P(Up) dan sisa waktu.
- **Sinyal.** Pita peluang model (dengan uji volatilitas ±30%) dibandingkan
  dengan harga ask Up/Down di Polymarket pada satu sumbu, ditambah bar edge
  setelah fee untuk tiap sisi dan alasan keputusan terakhir bot.
- **Jejak harga** window ini vs strike, **P&L kumulatif**, **riwayat 48
  window**, panel **risiko** (rugi harian, kalah beruntun, eksposur), dan
  **log** setiap order dan settlement beserta alasannya.
- Arahkan kursor ke grafik untuk melihat detailnya. Kalau bot berhenti,
  dashboard akan memberi tahu.

Semua trade (paper maupun live) dicatat di `data/trades.csv` dengan strategi
`updown_5m`, dan langsung muncul di dashboard.

### Sebelum live, wajib cek:
1. **Slug market.** Buka market 5 menit di polymarket.com. Ujung URL-nya
   adalah slug (misalnya `btc-updown-5m-1790243700`). Kalau formatnya
   beda, ubah `slug_template` di `config/updown.yaml`.
2. **Fee.** Cek dokumentasi fee Polymarket untuk market crypto jangka pendek,
   lalu sesuaikan `fees.fee_rate` dan `fees.fee_exponent`.
3. **Binance.** Kalau kamu di AS, pakai `feed.binance_host: https://api.binance.us`.
4. Paper minimal beberapa hari. Lihat win rate dan P&L per window di log.
   Ingat, paper menganggap semua order terisi, jadi hasil live pasti lebih kecil.
5. Mulai live dengan modal kecil (`bankroll_usd`, `max_bet_usd`), lalu set
   `LIVE_TRADING=true` di `.env`.

## Struktur kode

| File | Isi |
|---|---|
| `updown/model.py` | Rumus peluang, fee, Kelly, estimator volatilitas |
| `updown/strategy.py` | Keputusan beli/jual per detik (murni logika, mudah dites) |
| `updown/engine.py` | Siklus window: cari market, catat strike, trading, settlement |
| `updown/markets.py` | Gamma API (cari market) dan CLOB API (order book) |
| `updown/feeds.py` | Feed harga Binance di thread terpisah |
| `updown/broker.py` | Paper fill / order FAK live |
| `updown/risk.py` | Batas rugi harian dan cooldown |
| `updown/sim.py` | Simulator offline |
| `scripts/updown_calibrate.py` | Uji kalibrasi model dengan data historis |
| `updown/state.py` | Snapshot status bot untuk dashboard |
| `scripts/updown_dashboard.py` + `.html` | Dashboard radar (lokal, offline) |
| `config/updown.yaml` | Semua parameter |
