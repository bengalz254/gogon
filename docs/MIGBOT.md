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
| Masuk secepat mungkin (bersaing dengan sniper) | **Tidak membeli saat launch.** Bot menunggu harga *dip* (turun jauh dari puncaknya lalu mulai naik lagi), paling cepat 5 menit setelah migrasi |
| Bergantung penuh pada API tidak resmi GMGN (sering diblokir Cloudflare) | Data utama dari sumber publik yang stabil; **GMGN hanya tambahan**. Kalau GMGN memblokir, bot tetap jalan |
| Tidak ada cara menilai apakah filternya bekerja | **Setiap token migrasi dipantau 2 jam, dibeli atau tidak.** Laporan membandingkan token yang dibeli dengan yang ditolak |

## Cara kerja

```
1. Deteksi migrasi   PumpPortal (WebSocket, real-time). GeckoTerminal dimatikan: pool
                     PumpSwap baru di sana kebanyakan pool sampah untuk token lama
2. Pantau            harga, likuiditas, volume, jumlah transaksi dari DexScreener (tiap 10 detik)
3. Tunggu dip        paling cepat 5 menit setelah migrasi, sampai harga ≥ 30% di bawah
                     puncaknya lalu naik lagi ≥ 10% dari dasar (boleh sampai menit ke-60)
4. Filter pasar      likuiditas, market cap, volume 5m, transaksi 5m, rasio beli, harga vs awal
5. Filter keamanan   (on-chain lewat Solana RPC) mint/freeze authority, ekstensi Token-2022
                     berbahaya, top 10 holder, holder terbesar, % dev, jumlah holder;
                     RugCheck; GMGN kalau bisa
6. Beli (paper)      harga dari quote Jupiter (fee pool + price impact asli) + selip + priority fee
7. Jual (paper)      stop loss, stop impas setelah untung, take profit bertingkat, trailing
                     stop, batas waktu, likuiditas anjlok
8. Catat             setiap token: harga 1, 3, 5, 10, 15, 30, 60, 120 menit setelah migrasi,
                     plus pergerakan harganya tiap 10 detik (data/migbot/paths.jsonl.gz) untuk
                     menguji aturan beli/jual lain pada token yang sama
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

**Langkah 5 (di Telegram).** Buat bot Telegram khusus migbot. Paling mudah di PC
(Telegram Desktop atau web.telegram.org), supaya tokennya bisa langsung disalin ke
PowerShell.

1. Cari **@BotFather** (centang biru), tekan **Start**.
2. Kirim `/newbot`.
3. Ketik nama bebas, misalnya `Migbot Alert`.
4. Ketik username yang berakhiran `bot`, misalnya `migbot_namakamu_bot` (kalau sudah
   dipakai orang, coba nama lain).
5. BotFather membalas dengan token seperti `123456789:AAH...`. Salin token itu.
6. Buka bot barumu (link `t.me/...` di pesan BotFather), lalu tekan **Start**.

✅ **Cek:** token sudah tersalin. **Jangan kirim token ini ke siapa pun**, termasuk ke Claude.

**Langkah 6 (di browser).** Daftar RPC Solana gratis di <https://www.helius.dev>
(Sign up), lalu di dashboard Helius salin URL RPC **mainnet**, bentuknya
`https://mainnet.helius-rpc.com/?api-key=...`.

✅ **Cek:** kamu punya URL yang diawali `https://mainnet.helius-rpc.com/?api-key=`.
Tanpa ini bot tetap jalan, tapi cek holder/dev sering dilewati karena RPC publik
membatasi request, dan jumlah holder tidak bisa dihitung sama sekali.

**Langkah 7.** Isi keduanya dengan panduan otomatis (tidak perlu mengedit file):

```bash
cd ~/migbot && git pull
venv/bin/python -m migbot setup
```

Setup menanyakan dua hal. Saat menempel (klik kanan di PowerShell) **tulisannya memang
tidak muncul**; langsung tekan Enter.

1. Token Telegram → ✅ `✓ Token benar ... bot @nama_botmu`. Lalu setup memintamu
   mengirim `halo` ke bot itu dan menekan Enter → ✅ `✓ Chat: ... Pesan tes terkirim`,
   dan Telegram menerima `✅ Telegram tersambung ke migbot`.
2. URL RPC dari Helius → ✅ `✓ RPC berfungsi: bisa membaca 20 holder terbesar`.

✅ **Cek:** baris terakhir `✅ Disimpan ke .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SOLANA_RPC_URL`.
❌ Ada tanda `✕`: baca pesannya (setup memberi tahu apa yang salah), lalu jalankan
`venv/bin/python -m migbot setup` lagi. Yang sudah benar tinggal dilewati dengan Enter
kosong.

**Langkah 8.** Mulai ulang dengan data bersih (data sebelum RPC terpasang belum punya
cek holder), lalu tes semuanya. Ketik satu per satu:

```bash
sudo systemctl stop migbot
cd ~/migbot && venv/bin/python -m migbot reset --yes
sudo systemctl start migbot
venv/bin/python -m migbot check --telegram-test
```

✅ **Cek:**
- `reset` menulis `Data lama dipindah ke ...` atau `Tidak ada data untuk direset.`;
- Telegram menerima `[migbot] Tes notifikasi ... berhasil ✅` dan `🟢 Bot migrated mulai (PAPER)`;
- ada `[OK] Solana RPC` tanpa tulisan "sebagian gagal";
- ada `[OK] Holder ... holder; filter: minimal 200`;
- baris terakhir `✅ Siap.`

❌ `[!] Telegram gagal`: jalankan lagi `venv/bin/python -m migbot setup` (Langkah 7).

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
(bot **tetap** jalan). Token itu baru dibeli (`PAPER BUY`) kalau harganya dip dalam 60
menit setelah migrasi; kalau tidak, ditolak (alasannya terlihat di dashboard, misalnya
`dip: baru 12% di bawah puncak`).

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

* **P&L paper**: saldo + nilai posisi − modal awal (10 SOL simulasi).
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

Laporan menampilkan kenaikan harga **dari titik yang sama untuk semua token** (5 menit
setelah migrasi) untuk tiga kelompok:

| Kelompok | Artinya |
|---|---|
| `semua` | Semua token migrasi. Ini gambaran "beli acak" |
| `lolos filter` | Token yang lolos semua filter (termasuk yang tidak jadi dibeli karena batas risiko, misalnya sudah 6 posisi terbuka) |
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
* **Mode beli saat dip:** token yang dibeli memang sudah turun dulu sebelum dibeli, jadi
  tabel kelompok di atas tidak adil untuk menilai filternya. Nilai dari **P&L** dan
  **Diagnosa posisi**.
* **Diagnosa posisi** menjawab *di mana* ruginya:
  * `tidak pernah naik 10%` banyak → harga langsung turun setelah dibeli: masalahnya
    **waktu beli**;
  * `sempat +20% lalu rugi` banyak → posisi sempat untung tapi tidak diamankan:
    masalahnya **cara jual**.
* **Posisi terakhir** menampilkan per posisi: berapa menit setelah migrasi dibeli,
  naik tertinggi setelah dibeli (`puncak`), hasil akhir, dan cara keluarnya.
* Data lama setelah `reset` tetap bisa dilihat:
  `venv/bin/python -m migbot report --dir data/migbot/archive/<tanggal-jam>`.
* `reset --simpan-lama` hanya mengosongkan dashboard: P&L, grafik, hitungan hari ini
  dan daftar transaksi mulai dari nol, saldo kembali ke modal awal. Token lama yang
  sedang diikuti (dan `long_samples.csv.gz`) tidak disentuh, jadi `backtest --lama`
  tetap memakai semua datanya. `reset` biasa juga mengarsipkan uji token lama.
* **Alasan ditolak terbanyak** memberi tahu filter mana yang paling sering menolak.
  Kalau satu risiko RugCheck menolak hampir semua token, lihat daftarnya di laporan
  dan pertimbangkan `safety.rugcheck.ignore_risks`.

Harga paper sudah memperhitungkan fee pool dan price impact (quote Jupiter), selip
tambahan 1%, dan priority fee + tip 0.001 SOL per transaksi (2% pulang-pergi untuk
posisi 0.1 SOL). Transaksi sungguhan bisa
tetap lebih buruk (telat, MEV), jadi hasil paper adalah batas atas, bukan jaminan.

### Token mana yang hancur? (`migbot analyze`)

```bash
cd ~/migbot && venv/bin/python -m migbot analyze
```

Kebanyakan token migrated hancur (turun 80%+) dalam 1–2 jam, sering dalam sekali jual
besar oleh orang dalam, dan tidak ada stop loss yang bisa lolos dari itu. Satu-satunya
jalan adalah **tidak membeli** token seperti itu. Karena itu bot mencatat data SETIAP
token di awal jendela beli (jumlah holder, berapa dompet memegang ≥ 1% supply, porsi 50
dompet terbesar, dev, RugCheck, mcap, likuiditas, volume, transaksi), lalu `analyze`
membagi token per ukuran menjadi tiga kelompok (rendah/sedang/tinggi) dan menunjukkan
berapa persen yang hancur di tiap kelompok.

* **Paling membedakan** di bagian atas adalah ukuran yang selisih hancurnya paling besar
  antar kelompok. Itu kandidat filter.
* Di bawah 150 token, selisih 10–15 poin masih bisa kebetulan.
* Kalau tidak ada ukuran yang membedakan (semua kelompok hancur sama seringnya), berarti
  data publik tidak cukup untuk menghindari token yang akan di-dump, dan strategi ini
  sebaiknya tidak dipakai dengan uang sungguhan.
* Data lama: `venv/bin/python -m migbot analyze --dir data/migbot/archive/<tanggal-jam>`
  (data sebelum versi ini hanya punya data holder untuk token yang lolos filter pasar).
* Token yang hanya ditemukan GeckoTerminal tidak dihitung: itu pool sampah untuk token
  lama, bukan migrasi.

### Aturan beli mana yang untung? (`migbot backtest`)

```bash
cd ~/migbot && venv/bin/python -m migbot backtest
```

Bot mencatat harga, likuiditas, volume, dan transaksi setiap token tiap 10 detik
(`data/migbot/paths.jsonl.gz`). `backtest` mencoba beberapa aturan beli pada SEMUA token
itu sekaligus, menjual dengan aturan keluar bot (kolom `ketat`) dan aturan yang lebih
longgar (kolom `longgar`), lalu memotong biaya di setiap beli dan jual:

| Aturan | Beli kalau |
|---|---|
| `acak menit 5` | semua migrasi asli, di menit ke-5 (pembanding) |
| `ramai menit 5` | 1000+ transaksi dalam 5 menit (trending saat launch) |
| `sepi menit 5` | 10–300 transaksi dalam 5 menit |
| `sepi+tersebar` | sepi, 50 dompet terbesar ≤ 25% supply, paling banyak 1 dompet ≥ 1% |
| `aturan bot` | aturan bot sekarang dari config (dip + filter + cek holder) |
| `bertahan 30m` | menit ke-30 harga masih ≥ separuh harga menit ke-5 |
| `trending 30-60m` | menit 30–60 ramai lagi (300+ transaksi) dan dekat harga tertingginya |

Angka di tabel = rata-rata untung/rugi per beli; dalam kurung = berapa persen yang
untung. Aturan yang rata-ratanya positif dengan 100+ beli baru layak diuji langsung (paper).
Kalau semua negatif, aturan-aturan itu tidak bisa untung di pasar ini.

**Hasil uji 1.693 migrasi (30 Sep – 2 Okt 2026): semua aturan rugi 18–48% per beli.**
Karena itu bot sekarang dikirim dalam **mode riset** (`trading.enabled: false`): token
tetap dipantau, difilter, dan dicatat, tapi tidak ada yang dibeli.

### Meme coin lama (`migbot backtest --lama`)

Setiap migrasi asli yang masih punya likuiditas setelah 2 jam pemantauan diikuti terus
sampai 7 hari: harga, likuiditas, mcap, volume dan transaksi 1 jam, tiap 10 menit
(`data/migbot/long_samples.csv.gz`, bagian `long_tracking` di config). Token yang 3 kali
berturut-turut likuiditasnya di bawah $1.000 dianggap mati dan berhenti diikuti. Saat
versi ini pertama jalan, token dari `tokens.csv` yang umurnya di bawah 7 hari ikut diikuti.

```bash
cd ~/migbot && venv/bin/python -m migbot backtest --lama
```

* **Masih bisa diperdagangkan setelah 6 jam / 1 / 2 / 3 hari**: berapa persen token yang
  likuiditasnya masih ≥ $10k dan mcap ≥ $30k.
* Aturan beli: `hidup hari 1/2/3` (masih hidup di umur itu), `naik di hari 1` (lebih tinggi
  dari umur 12 jam), `ramai di hari 1` (volume 1 jam ≥ $10k), `dip hari 1-3` (≥ 30% di bawah
  puncak lalu naik 10%).
* Dua cara jual: `1 hari` (SL −30%, +50% jual separuh, trailing 30%, maks 24 jam) dan
  `3 hari` (SL −50%, +100% jual separuh, trailing 40%, maks 72 jam).
* Token yang mati saat dipegang dihitung terjual di harga nol. Pembelian yang datanya belum
  cukup panjang tidak dihitung (ditulis sebagai "belum selesai").
* Butuh 2–3 hari data sebelum ada hasil, dan 4–5 hari untuk aturan hari ke-3.

---

## Pengaturan penting (`config/migbot.yaml`)

Semua pengaturan ada penjelasannya di file itu. Yang paling sering diubah:

| Pengaturan | Bawaan | Arti |
|---|---|---|
| `entry.delay_seconds` | 300 | Tidak pernah beli di 5 menit pertama setelah migrasi |
| `entry.window_seconds` | 3600 | Lewat 60 menit tanpa dip + lolos filter = ditolak |
| `entry.dip_pct` | 30 | Beli hanya kalau harga ≥ 30% di bawah harga tertinggi sejak migrasi. 0 = cara lama (beli begitu filter lolos) |
| `entry.dip_bounce_pct` | 10 | …dan sudah naik lagi ≥ 10% dari titik terendahnya (jatuhnya berhenti). 0 = tidak menunggu |
| `filters.max_drop_from_first_pct` | 50 | Tolak token yang sudah turun > 50% dari harga pertamanya (sekarat) |
| `filters.min_liquidity_usd` | 10000 | Likuiditas pool minimal |
| `filters.min_market_cap_usd` / `max_market_cap_usd` | 40k / 1.5M | Rentang market cap |
| `filters.min_volume_5m_usd`, `min_txns_5m`, `min_buy_ratio_5m` | 5000, 60, 0.5 | Token harus ramai dan lebih banyak yang beli |
| `filters.max_top10_pct`, `max_top_holder_pct`, `max_dev_hold_pct` | 30, 10, 5 | Konsentrasi holder (100 = mati) |
| `filters.min_holders` | 200 | Jumlah wallet pemegang minimal, dihitung lewat RPC (butuh Helius; pool tidak dihitung). 0 = mati |
| `filters.min_smart_buys`, `max_sniper_count` | 0 | Filter GMGN (0 = mati; hanya berlaku kalau GMGN bisa diakses) |
| `trading.buy_sol` | 0.1 | Ukuran beli simulasi per token |
| `trading.paper_balance_sol` | 10 | Modal simulasi. Perubahan baru berlaku setelah `python -m migbot reset` (atau `reset --simpan-lama`) |
| `trading.max_open_positions` | 6 | Posisi terbuka bersamaan |
| `trading.max_buys_per_day` | 100 | Beli maksimal per hari (UTC) |
| `trading.max_daily_loss_sol` | 2 | Rugi hari ini sampai segini → berhenti beli sampai 00:00 UTC (08:00 WITA) |
| `exits.stop_loss_pct` | 25 | Jual semua kalau turun 25% dari harga beli |
| `exits.breakeven_after_pct` | 20 | Setelah pernah +20%, jual semua kalau harga kembali ke harga beli ("stop impas"). 0 = mati |
| `exits.take_profit` | `[[40, 0.5]]` | Naik 40% → jual 50% posisi awal |
| `exits.trailing_start_pct` / `trailing_pct` | 40 / 25 | Setelah pernah +40%, jual semua kalau turun 25% dari puncak |
| `exits.max_hold_minutes` | 45 | Jual semua setelah 45 menit |

Nama pengaturan yang salah ketik akan ditolak saat bot start (lihat
`journalctl -u migbot`). Setelah mengubah config: `sudo systemctl restart migbot`.

**Soal GMGN:** GMGN tidak punya API publik resmi dan sering memblokir request dari
server. Bot memakai data GMGN (smart money, sniper) hanya kalau bisa diakses,
tanpa mencoba mengakali blokirnya. Kalau diblokir, dashboard menulis "diblokir oleh
GMGN" dan filter khusus GMGN dilewati; filter lainnya tetap berjalan. Jumlah holder
tidak bergantung pada GMGN: bot menghitungnya sendiri lewat RPC Helius
(`getTokenAccounts`), dan angka GMGN hanya dipakai kalau RPC tidak bisa menghitung.

---

## Perintah sehari-hari (di VPS)

| Perintah | Fungsi |
|---|---|
| `sudo journalctl -u migbot -f` | Log langsung (Ctrl+C = keluar, bot tetap jalan) |
| `cd ~/migbot && venv/bin/python -m migbot report` | Laporan riset + P&L |
| `cd ~/migbot && venv/bin/python -m migbot analyze` | Token mana yang hancur, dan apa yang membedakannya |
| `cd ~/migbot && venv/bin/python -m migbot backtest` | Uji aturan beli pada catatan harga semua token |
| `cd ~/migbot && venv/bin/python -m migbot backtest --lama` | Uji aturan beli untuk token umur 1–3 hari |
| `cd ~/migbot && venv/bin/python -m migbot check` | Cek koneksi semua sumber data |
| `cd ~/migbot && venv/bin/python -m migbot setup` | Isi/ganti token Telegram dan RPC (langsung dites), lalu `sudo systemctl restart migbot` |
| `sudo systemctl stop migbot` / `start migbot` | Hentikan / nyalakan bot |
| `sudo systemctl disable --now migbot migbot-dashboard` | Matikan total (tidak hidup lagi saat reboot) |
| `sudo systemctl stop migbot && cd ~/migbot && venv/bin/python -m migbot reset && sudo systemctl start migbot` | Mulai dari nol, termasuk uji token lama: data lama dipindah ke `data/migbot/archive/`, tidak dihapus |
| `cd ~/migbot && venv/bin/python -m migbot status` | Cek bot yang sedang jalan: hidup atau tidak, sumber data, data riset bertambah, peringatan di log, disk dan RAM |
| `sudo systemctl stop migbot && cd ~/migbot && venv/bin/python -m migbot reset --simpan-lama && sudo systemctl start migbot` | Kosongkan dashboard saja (P&L, grafik, hitungan, saldo kembali ke modal awal); token lama tetap diikuti sampai 7 hari |
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
