# Panduan Langkah demi Langkah

Panduan ini membawa kamu dari nol sampai bot jalan 24 jam di server (VPS),
dalam tiga tahap:

- **Tahap A — Siapkan server** (sekali saja, ±30 menit)
- **Tahap B — Mode paper** (1–2 minggu, tanpa uang sungguhan)
- **Tahap C — Live dengan modal kecil**

Setiap langkah punya bagian **✅ Cek** — jangan lanjut sebelum hasilnya sesuai.

> ⚠️ Ini software trading. Bisa rugi. Tidak ada strategi yang pasti untung.
> Mulai dari mode paper, lalu live dengan uang yang siap hilang.

**Yang perlu disiapkan:** VPS Linux (Ubuntu 22.04/24.04, 1 vCPU / 1 GB RAM
cukup, sekitar $5/bulan), aplikasi Telegram di HP, dan — baru di Tahap C —
akun Polymarket khusus untuk bot.

---

## Tahap A — Siapkan server

### Langkah 1 — Masuk ke VPS dan cek wilayah

Dari komputer/HP (aplikasi terminal atau Termux), masuk ke VPS:

```bash
ssh root@ALAMAT_IP_VPS
```

(Sebagian penyedia VPS memakai user `ubuntu`, bukan `root` — pakai user
yang diberikan penyedia VPS-mu, di sini dan di perintah `ssh` berikutnya.)

Cek apakah Polymarket menerima koneksi dari server ini:

```bash
curl -s https://polymarket.com/api/geoblock
```

✅ **Cek:** hasilnya berisi `"blocked":false`.

> ⚠️ Aturan Polymarket juga berlaku untuk **tempat tinggalmu**, bukan cuma
> lokasi server. Pastikan kamu boleh memakai Polymarket dari negaramu.
> Memakai VPS untuk mengakali pembatasan melanggar aturan Polymarket dan
> dananya bisa dibekukan.

### Langkah 2 — Pasang git dan Docker

```bash
sudo apt update && sudo apt install -y git
curl -fsSL https://get.docker.com | sudo sh
```

✅ **Cek:** kedua perintah ini menampilkan nomor versi:

```bash
sudo docker --version
sudo docker compose version
```

### Langkah 3 — Ambil kode bot

```bash
cd ~
git clone -b claude/crypto-strategy-24h-1qaqdg https://github.com/bengalz254/gogon.git
cd gogon
```

`-b` langsung mengambil branch yang berisi versi bot ini (branch bawaan
repo adalah versi lama). Repo-nya publik, jadi tidak perlu login.

✅ **Cek:** `ls` menampilkan `bot`, `config`, `docker-compose.yml`, `PANDUAN.md`.

### Langkah 4 — Buat file pengaturan rahasia (`.env`)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Untuk sekarang cukup pastikan ada baris `LIVE_TRADING=false`. Private key
**belum** diperlukan. Simpan dengan `Ctrl+O`, `Enter`, lalu keluar `Ctrl+X`.

✅ **Cek:** `grep LIVE_TRADING .env` menampilkan `LIVE_TRADING=false`.

### Langkah 5 — Pasang notifikasi Telegram

1. Di Telegram, buka **@BotFather**, kirim `/newbot`, ikuti petunjuknya,
   lalu salin **token** yang diberikan.
2. Buka bot barumu dan kirim pesan apa saja (misalnya "halo").
3. Di browser, buka `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (ganti `<TOKEN>`). Cari `"chat":{"id":` dan salin angkanya.
4. Masukkan keduanya ke `.env` (`nano .env`):

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```

5. Siapkan folder data, bangun bot, lalu tes:

   ```bash
   mkdir -p data logs && sudo chown -R 1000:1000 data logs
   sudo docker compose build
   sudo docker compose run --rm bot python scripts/check_setup.py --telegram-test
   ```

✅ **Cek:** pesan *"Tes notifikasi dari bot gogon"* masuk ke Telegram-mu,
dan hasil perintah terakhir berisi `[OK] Fetched sampling markets` serta
`All checks passed.`

---

## Tahap B — Mode paper (1–2 minggu, tanpa uang)

Di mode paper bot memakai data pasar asli tapi semua order hanya simulasi.

### Langkah 6 — Nyalakan market making

```bash
nano config/settings.yaml
```

Cari bagian `market_maker:` dan ubah `enabled: false` menjadi
`enabled: true`. Biarkan pengaturan lain seperti bawaan.

✅ **Cek:** `grep -A1 "market_maker:" config/settings.yaml` menampilkan `enabled: true`.

### Langkah 7 — Jalankan 24 jam

```bash
sudo docker compose up -d
sudo docker compose logs -f
```

`logs -f` menampilkan log langsung; tekan `Ctrl+C` untuk keluar — bot tetap
jalan di belakang, dan otomatis menyala lagi kalau VPS di-restart.

✅ **Cek (dalam ±5 menit):**
- Telegram menerima **🟢 Bot mulai (PAPER)** lalu **📊 Market making di … market**.
  Kalau yang datang *"Tidak ada market reward yang cocok"*, ukuran minimum
  reward di market yang ada belum muat di `risk.max_position_usd` (bawaan
  $25) — bot tetap jalan dan mencoba lagi setiap 30 menit.
- `sudo docker compose ps` menampilkan `(healthy)`.

### Langkah 8 — Pantau

- **Telegram:** setiap 6 jam ada pesan **💓 Bot masih jalan**. Kalau pesan
  ini berhenti datang, bot atau servernya mati.
- **Status singkat:**

  ```bash
  cat data/status.json
  ```

  Perhatikan `websocket_live` (sebaiknya `true`), `market_maker.fills_today`
  dan `realized_pnl_today_usd`.
- **Dashboard** (dari komputer): buka koneksi dengan terowongan, lalu
  jalankan dashboard di sesi yang sama:

  ```bash
  ssh -L 8765:127.0.0.1:8765 root@ALAMAT_IP_VPS
  python3 ~/gogon/scripts/dashboard.py
  ```

  Buka `http://127.0.0.1:8765` di browser komputermu. Jangan buka port
  dashboard ke internet.

### Langkah 9 — Evaluasi sebelum live

Setelah 1–2 minggu, lanjut ke Tahap C hanya kalau:

- [ ] Pesan 💓 datang rutin, tidak ada alert error yang berulang.
- [ ] `websocket_live` hampir selalu `true`.
- [ ] Market making mendapat fill dan P&L-nya tidak terus-menerus negatif.
- [ ] Batas rugi harian (⛔) jarang atau tidak pernah kena.
- [ ] Kamu paham kenapa bot melakukan tiap trade-nya.

Arbitrase biasanya jarang atau tidak pernah dapat peluang — itu normal
setelah Polymarket menarik fee. Ingat juga: mode paper **tidak** menghitung
liquidity rewards dan rebate, dan pasar asli lebih "kejam" daripada simulasi.

---

## Tahap C — Live dengan modal kecil

### Langkah 10 — Buat akun Polymarket khusus bot

Daftar akun Polymarket **baru** (email berbeda) khusus untuk bot, lalu
deposit kecil, misalnya $50–100 (otomatis jadi pUSD).

> ⚠️ Akun khusus itu wajib: saat mulai dan berhenti, bot membatalkan
> **semua** order di akunnya — termasuk order yang kamu pasang manual.

### Langkah 11 — Ambil private key dan alamat dompet

- **Akun email (login pakai email):** buka
  `https://reveal.magic.link/polymarket` saat sedang login ke Polymarket
  untuk mengekspor private key. Tipe signature: `1`.
- **Akun dompet browser (MetaMask dll.):** ekspor private key dari dompet
  tersebut. Tipe signature: `2`.
- **Alamat dana (funder):** alamat dompet Polymarket-mu yang tertera di
  profil/halaman deposit Polymarket — **bukan** alamat MetaMask.

> ⚠️ Siapa pun yang memegang private key bisa mengambil semua dana. Jangan
> pernah kirim ke siapa pun (termasuk "support"), jangan simpan di chat,
> jangan commit ke git. Polymarket tidak akan pernah memintanya.

### Langkah 12 — Isi `.env` dan kecilkan batas risiko

```bash
nano .env
```

```
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
POLY_SIGNATURE_TYPE=1        # 1 = akun email, 2 = dompet browser
LIVE_TRADING=true
```

Lalu kecilkan batas untuk minggu pertama (`nano config/settings.yaml`,
bagian `risk:`), misalnya:

```yaml
risk:
  max_position_usd: 15
  max_total_exposure_usd: 50
  max_daily_loss_usd: 10
```

Kalau nanti Telegram bilang *"Tidak ada market reward yang cocok"*, artinya
ukuran minimum reward di market-market itu tidak muat di `max_position_usd`.
Naikkan sedikit, atau jalan dengan arbitrase saja.

### Langkah 13 — Cek sebelum menyalakan live

```bash
sudo docker compose down
sudo docker compose run --rm bot python scripts/check_setup.py
```

✅ **Cek:** muncul `[OK] Connected and authenticated` dan
`[OK] Trading balance: … pUSD` dengan saldo lebih dari 0. Kalau gagal,
periksa lagi private key, alamat funder dan tipe signature.

### Langkah 14 — Nyalakan live

```bash
sudo docker compose up -d --force-recreate
sudo docker compose logs -f
```

✅ **Cek:** Telegram menerima **🟢 Bot mulai (LIVE)**; di website Polymarket
(akun bot) muncul order terbuka dari market making.

### Langkah 15 — Pantau ketat 3 hari pertama

- Cocokkan fill di Telegram dengan riwayat di Polymarket.
- Alert **⚠️ Posisi bot beda dengan Polymarket** harus dicek.
- Lihat halaman **Rewards** Polymarket beberapa hari kemudian untuk tahu
  berapa reward yang benar-benar didapat.

Kalau hasilnya masuk akal, naikkan batas pelan-pelan — bukan sekaligus.

---

## Rutinitas dan keadaan darurat

| Situasi | Yang dilakukan |
|---|---|
| Setiap hari | Baca Telegram; sesekali `cat data/status.json`. |
| **🔁 set lengkap bisa di-merge** | Di Polymarket pilih posisi itu → *Merge*. Lalu jalankan tiga perintah di bawah tabel ini. |
| **🏁 Market sudah selesai** | Di Polymarket tekan *Redeem* untuk mengklaim pUSD. |
| **🚨 Arbitrase hanya terisi sebagian** | Cek posisinya di Polymarket, putuskan jual atau tahan. Kalau dijual manual: `sudo docker compose run --rm bot python scripts/positions.py --live close <token_id> --price <harga>` (bot dihentikan dulu). |
| **⛔ Batas rugi harian tercapai** | Bot berhenti membuka posisi sendiri. Cari tahu penyebabnya sebelum mengubah batas. |
| **Darurat — hentikan semuanya** | `sudo docker compose down` (bot membatalkan semua order-nya), lalu pastikan di Polymarket tidak ada order terbuka yang tersisa. |
| Mengubah `config/settings.yaml` | `sudo docker compose restart` |
| Mengubah `.env` | `sudo docker compose up -d --force-recreate` |
| Update versi bot | `git pull` lalu `sudo docker compose up -d --build` |

Mencatat merge yang sudah dilakukan di Polymarket:

```bash
sudo docker compose down
sudo docker compose run --rm bot python scripts/positions.py --live merge <market_id>
sudo docker compose up -d
```

Melihat posisi yang dicatat bot (boleh kapan saja):

```bash
sudo docker compose run --rm bot python scripts/positions.py --live list
```

Kalau `git pull` menolak karena kamu mengubah `config/settings.yaml`:
`git stash && git pull && git stash pop`.

## Kalau ada masalah

| Gejala | Penyebab dan solusi |
|---|---|
| `PermissionError` pada `data/` atau `logs/` | `sudo chown -R 1000:1000 data logs` |
| `Could not fetch market data` | Server tidak bisa menghubungi Polymarket. Ulangi cek wilayah di Langkah 1. |
| Tes Telegram gagal | Periksa `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`, pastikan kamu sudah mengirim pesan ke bot. |
| `(unhealthy)` di `docker compose ps` | Lihat `sudo docker compose logs --tail 100`. |
| `websocket_live: false` terus | Server tidak bisa membuka WebSocket; bot tetap jalan lewat REST tapi market making berhenti. |
| Tidak pernah ada trade arbitrase | Normal: dengan fee sekarang peluangnya jarang. |
| `positions.py` bilang bot masih jalan | Hentikan dulu dengan `sudo docker compose down`. |

Penjelasan teknis lengkap (strategi, fee, risiko) ada di [README.md](README.md).
