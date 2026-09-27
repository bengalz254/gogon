# Panduan Bot Grid Tokocrypto

Panduan ini membawa kamu dari nol sampai bot grid jalan 24 jam di VPS, dalam
**mode paper**: bot membaca harga asli Tokocrypto, tapi semua order hanya
simulasi. Tidak perlu API key, tidak ada uang sungguhan.

- **Tahap A — Siapkan server** (sekali saja, ±30 menit)
- **Tahap B — Atur grid** (±15 menit)
- **Tahap C — Jalankan paper 2–4 minggu, lalu evaluasi**

Setiap langkah punya bagian **✅ Cek**. Jangan lanjut sebelum hasilnya sesuai.

> ⚠️ Ini software trading. Grid **bisa rugi** kalau harga turun terus, dan
> kalah dari "beli lalu tahan" kalau harga naik terus. Tidak ada strategi
> yang pasti untung.

## Cara kerja singkat

Bot membagi sebuah range harga menjadi beberapa garis (level). Di setiap
celah antar garis, bot **membeli di garis bawah** lalu **menjual di garis
atasnya**. Setiap kali harga turun lalu naik melewati satu celah, bot
mendapat satu "putaran" untung.

Dengan pengaturan bawaan (BTC/IDR, range ±10%, 15 level):

| | |
|---|---|
| Jarak antar level | ±1,44% |
| Biaya + pajak satu putaran (beli lalu jual) | ±0,47% |
| Untung bersih per putaran | ±0,97% dari nilai order (±Rp 900 per order Rp 100.000) |
| Modal terpakai kalau semua order beli terisi | ±Rp 1,3 juta (dari modal paper Rp 2 juta) |

- **Harga naik-turun di dalam range:** bot untung dari setiap putaran.
- **Harga naik terus:** bot tidak memegang koin, jadi untungnya kecil.
  Membeli lalu menahan koin akan lebih untung.
- **Harga turun terus:** semua order beli terisi dan bot memegang koin yang
  nilainya turun. Kalau harga ditutup 10% di bawah garis terbawah,
  stop-loss menjual semuanya dan grid berhenti.

**Yang perlu disiapkan:** VPS Linux (Ubuntu 22.04/24.04, 1 GB RAM cukup)
dan, sebaiknya, Telegram di HP. Akun Tokocrypto **belum** diperlukan.

---

## Tahap A — Siapkan server

### Langkah 1 — Masuk ke VPS dan perbarui

```bash
ssh root@ALAMAT_IP_VPS
sudo apt update && sudo apt upgrade -y
sudo reboot
```

Tunggu ±1 menit, lalu masuk lagi dengan `ssh`.

✅ **Cek:** setelah masuk lagi, tidak ada tulisan `*** System restart required ***`.

### Langkah 2 — Cek koneksi ke Tokocrypto

```bash
curl -s -o /dev/null -w "Tokocrypto: %{http_code}\n" https://www.tokocrypto.com/open/v1/common/time
curl -s -o /dev/null -w "Binance: %{http_code}\n" https://api.binance.com/api/v3/ping
```

✅ **Cek:** `Tokocrypto: 200`. Hasil `Binance: 200` hanya perlu untuk
pasangan yang datanya diambil dari Binance (Langkah 7 menunjukkan
pasangan mana saja). Kalau keluar `451` atau `403`, lihat bagian
[Kalau ada masalah](#kalau-ada-masalah).

### Langkah 3 — Pasang git dan Docker

```bash
sudo apt install -y git
curl -fsSL https://get.docker.com | sudo sh
```

✅ **Cek:** `sudo docker --version` dan `sudo docker compose version`
menampilkan nomor versi.

### Langkah 4 — Ambil kode bot

```bash
cd ~
git clone -b claude/crypto-strategy-24h-1qaqdg https://github.com/bengalz254/gogon.git
cd gogon
```

Kalau folder `gogon` sudah ada dari sebelumnya: `cd ~/gogon && git pull`.

✅ **Cek:** `ls` menampilkan `spot`, `config`, `docker-compose.yml`, `PANDUAN.md`.

### Langkah 5 — File `.env` dan Telegram

```bash
cp .env.example .env
chmod 600 .env
```

Mode paper tidak butuh isian apa pun di `.env`, kecuali Telegram
(sangat disarankan):

1. Di Telegram, buka **@BotFather**, kirim `/newbot`, ikuti petunjuknya,
   lalu salin **token** yang diberikan.
2. Buka bot barumu dan kirim pesan apa saja (misalnya "halo").
3. Di browser, buka `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (ganti `<TOKEN>`). Cari `"chat":{"id":` dan salin angkanya.
4. `nano .env`, isi dua baris ini, simpan (`Ctrl+O`, `Enter`, `Ctrl+X`):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```

### Langkah 6 — Siapkan folder dan bangun bot

```bash
mkdir -p data logs && sudo chown -R 1000:1000 data logs
sudo docker compose build
```

✅ **Cek:** perintah terakhir selesai tanpa `ERROR`. Build pertama bisa
makan waktu beberapa menit.

---

## Tahap B — Atur grid

### Langkah 7 — Pilih pasangan

```bash
sudo docker compose run --rm spot python scripts/spot_check.py --symbols IDR
```

Hasilnya daftar pasangan IDR di Tokocrypto, beserta sumber datanya.
Mulailah dengan **BTC/IDR** (paling ramai). Hindari koin kecil yang sepi:
harganya bisa melompat dan order grid jarang terisi.

Kenapa IDR? Beli pakai rupiah tidak kena pajak, jadi hanya penjualan
yang kena PPh 0,21%. Di pasangan kripto-ke-kripto (misalnya BTC/USDT),
biaya bursanya lebih besar. Selain itu, menukar kripto dengan kripto
dihitung sebagai penjualan, jadi kedua sisi bisa kena pajak.

### Langkah 8 — Atur `config/spot.yaml`

```bash
nano config/spot.yaml
```

| Pengaturan | Artinya | Bawaan |
|---|---|---|
| `symbol` | Pasangan yang ditradingkan | `BTC/IDR` |
| `grid.range_pct` | Range otomatis: sekian % di bawah dan di atas harga saat pertama jalan | `10` |
| `grid.levels` | Jumlah garis. Makin banyak = jarak makin rapat, transaksi makin sering tapi untung per putaran makin kecil | `15` |
| `grid.order_value` | Rupiah per order beli | `100000` |
| `risk.paper_balance` | Modal paper | `2000000` |
| `risk.stop_loss_pct` | Jual semua kalau harga ditutup sekian % di bawah garis terbawah (0 = mati) | `10` |
| `costs.*` | Biaya dan pajak dalam persen | lihat di bawah |

Tulis angka tanpa titik ribuan (`2000000`, bukan `2.000.000`).

**Cek biaya terbaru** di halaman
[Informasi Biaya Transaksi Tokocrypto](https://support.tokocrypto.com/hc/en-us/articles/360004044591-Tokocrypto-Transaction-Fee-Details),
lalu samakan `maker_fee_pct` (fee maker pasangan IDR) dan `exchange_fee_pct`
(biaya bursa/kliring). Nilai bawaannya 0,10% dan 0,03%, ditambah pajak
penjualan 0,21%. Kalau biayanya lebih tinggi dari itu, hasil paper
terlihat lebih bagus daripada kenyataannya.

### Langkah 9 — Cek semuanya

```bash
sudo docker compose run --rm spot python scripts/spot_check.py --telegram-test
```

Skrip ini mengecek koneksi dan pasangan, lalu menampilkan rencana grid:
level-levelnya, jarak antar level, untung bersih per putaran, modal
terpakai, dan harga stop-loss.

✅ **Cek:** baris terakhir **✅ Siap**, dan pesan tes masuk ke Telegram.
Kalau ada baris `MASALAH`, perbaiki sesuai pesannya (lihat juga
[Kalau ada masalah](#kalau-ada-masalah)), lalu jalankan cek ini lagi.

---

## Tahap C — Mode paper 2–4 minggu

### Langkah 10 — Nyalakan

```bash
sudo docker compose up -d
sudo docker compose logs -f spot
```

`logs -f` menampilkan log langsung. Tekan `Ctrl+C` untuk keluar; bot tetap
jalan di belakang dan otomatis menyala lagi kalau VPS di-restart.

✅ **Cek:**
- Telegram menerima **🟢 Bot grid mulai (PAPER)** berisi range, jumlah
  slot, dan untung bersih per putaran.
- Setelah ±3 menit, `sudo docker compose ps` menampilkan `(healthy)`.

### Langkah 11 — Pantau

- **Telegram:** 🟢 setiap pembelian, 💰 setiap penjualan (plus untung
  bersihnya), 📊 ringkasan setiap 24 jam, ⬆️/⬇️ saat harga keluar range,
  ⛔ stop-loss, dan ⚠️ kalau ada gangguan.
- **Status lengkap:**

  ```bash
  sudo docker compose exec spot python scripts/spot_status.py
  ```

- **Semua transaksi:** `data/spot/trades.csv`.

Order grid baru terisi kalau harga bergerak melewati level (±1,4%). Saat
pasar tenang, wajar kalau seharian tidak ada transaksi.

### Langkah 12 — Evaluasi

Setelah 2–4 minggu, jalankan `spot_status.py` dan periksa:

- [ ] Ringkasan harian datang rutin, dan tidak ada ⚠️ yang berulang.
- [ ] Berapa putaran selesai, dan berapa untung terealisasinya.
- [ ] **Nilai akun** dibandingkan baris **Pembanding** (kalau modal
      dibelikan BTC sejak awal). Grid unggul saat harga naik-turun, dan
      kalah saat harga naik terus.
- [ ] Pernah kena stop-loss? Kalau ya, kenapa?
- [ ] Kamu paham kenapa bot melakukan setiap transaksinya.

Tentang simulasi:
- Simulasinya sengaja **pesimis soal terisinya order**: order hanya
  dianggap terisi kalau harga benar-benar melewati levelnya.
- Simulasi **tidak** memperhitungkan gangguan bursa, perubahan biaya,
  atau emosi saat uang sungguhan dipertaruhkan.

### Langkah 13 — Sesudahnya

Mode live **belum ada** di bot ini, dan itu sengaja. Kalau hasil paper
masuk akal, minta saya (Claude) menambahkan mode live. Syaratnya:
- API key Tokocrypto dengan izin trading saja: **tanpa** izin penarikan,
  dan dikunci ke IP VPS;
- tes dengan satu order kecil dulu;
- modal yang siap hilang.

---

## Rutinitas dan keadaan darurat

| Situasi | Yang dilakukan |
|---|---|
| Lihat kondisi grid | `sudo docker compose exec spot python scripts/spot_status.py` |
| Mengubah `config/spot.yaml` | `sudo docker compose restart spot`. Kalau pengaturan `grid` diubah saat bot masih memegang koin, bot menolak jalan: kembalikan pengaturannya, atau reset grid. |
| Reset grid (mulai baru di harga sekarang) | `sudo docker compose stop spot`, lalu `sudo docker compose run --rm spot python -m spot.main --reset`, lalu `sudo docker compose up -d` |
| **⛔ Stop-loss** | Grid berhenti dan hanya menunggu. Pahami dulu penyebabnya, baru reset grid. |
| **⬆️ Harga di atas range** lama | Bot hanya menunggu. Kalau berhari-hari tidak kembali, reset grid supaya range mengikuti harga. |
| Update versi bot | `git pull`, lalu `sudo docker compose up -d --build` |
| Hentikan bot | `sudo docker compose down` |

Kalau `git pull` menolak karena kamu mengubah `config/spot.yaml`:
`git stash && git pull && git stash pop`.

## Kalau ada masalah

| Gejala | Penyebab dan solusi |
|---|---|
| `451` atau `403` di Langkah 2 / `spot_check.py` | Server diblokir berdasarkan lokasinya. Pilih pasangan yang datanya dari Tokocrypto (lihat Langkah 7), atau pakai VPS berlokasi di Indonesia, tempat kamu memang berada. Jangan pakai VPN/VPS negara lain untuk mengakali blokir. |
| `Pasangan ... tidak ada di Tokocrypto` | Cek nama pasangan di daftar Langkah 7, lalu perbaiki `symbol`. |
| `... terlalu rapat` | Biaya memakan untung. Kurangi `grid.levels` atau besarkan `grid.range_pct`. |
| `Modal kurang` | Kecilkan `grid.order_value` atau `grid.levels`, atau naikkan `risk.paper_balance`. |
| `grid.order_value terlalu kecil` | Order di bawah minimal Tokocrypto. Naikkan `grid.order_value`. |
| `... grid lama masih memegang koin` | Kembalikan pengaturan `grid` yang lama, atau reset grid. |
| `Config salah: ...` | Pesannya menunjukkan pengaturan mana yang salah. Perbaiki, lalu `sudo docker compose restart spot`. |
| `env file ... .env not found` | `cp .env.example .env` (Langkah 5). |
| `PermissionError` pada `data/` atau `logs/` | `sudo chown -R 1000:1000 data logs` |
| `(unhealthy)` di `docker compose ps` | Lihat `sudo docker compose logs --tail 100 spot`. |
| Tes Telegram gagal | Periksa token dan chat id di `.env`, pastikan kamu sudah mengirim pesan ke bot. |

Penjelasan teknis (dalam bahasa Inggris) ada di [README.md](README.md).
Panduan bot Polymarket lama disimpan di
[PANDUAN_POLYMARKET.md](PANDUAN_POLYMARKET.md) sebagai arsip; bot itu tidak
bisa dipakai dari Indonesia.
