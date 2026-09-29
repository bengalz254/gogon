#!/usr/bin/env bash
# Pasang bot scalper di VPS Linux (Ubuntu/Debian) supaya jalan 24/7.
#
#   cd ~/scalper-bot
#   bash deploy/setup_vps.sh                          # bot scalping (config/scalper.yaml)
#   bash deploy/setup_vps.sh config/trend.yaml paper  # bot lain dari config lain, mode paper
#
# Argumen (opsional): 1) file konfigurasi, 2) mode paper|testnet|live (tanpa
# argumen kedua: SCALPER_MODE dari .env). Config selain scalper.yaml dipasang
# sebagai service tersendiri, misalnya config/trend.yaml -> scalper-trend dan
# scalper-trend-dashboard, jadi bisa jalan berdampingan dengan bot scalping.
#
# Yang dilakukan:
#   1. memasang Python 3 + venv lewat apt (minta password sudo kalau perlu)
#   2. membuat virtualenv dan memasang paket bot
#   3. membuat .env kalau belum ada (key diketik di VPS, secret tidak tampil)
#   4. menjalankan "python -m scalper check"; berhenti kalau ada [FAIL]
#   5. memasang dan menyalakan dua service systemd:
#        scalper            bot-nya; hidup lagi otomatis setelah crash / reboot
#        scalper-dashboard  dashboard di 127.0.0.1:8777 (atau port kosong berikutnya kalau
#                           dipakai program lain), dibuka dari PC lewat SSH tunnel
#
# Aman dijalankan ulang, misalnya untuk update:  git pull && bash deploy/setup_vps.sh
# .env dan data trading (data/scalper/) tidak pernah diubah oleh skrip ini.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
UNIT_DIR="${SCALPER_UNIT_DIR:-/etc/systemd/system}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mGAGAL: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  fail "jalankan TANPA sudo:  bash deploy/setup_vps.sh   (skrip meminta sudo sendiri kalau perlu)"
fi
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "perintah sudo tidak ada; login sebagai root atau pasang sudo dulu"
  SUDO="sudo"
fi

cd "$APP_DIR"
[ -f requirements-scalper.txt ] && [ -d scalper ] || fail "folder bot tidak lengkap: $APP_DIR"
case "$APP_DIR" in *" "*) fail "nama folder mengandung spasi ($APP_DIR); pindahkan ke folder tanpa spasi" ;; esac
command -v systemctl >/dev/null 2>&1 || fail "systemd tidak ditemukan. Skrip ini untuk VPS Ubuntu/Debian."

CONFIG="${1:-config/scalper.yaml}"
RUN_MODE="${2:-}"
[ -f "$CONFIG" ] || fail "file konfigurasi $CONFIG tidak ditemukan"
case "$CONFIG" in *" "*) fail "nama file konfigurasi tidak boleh mengandung spasi" ;; esac
case "$RUN_MODE" in ""|paper|testnet|live) ;; *) fail "mode harus paper, testnet atau live (bukan '$RUN_MODE')" ;; esac
if [ "$CONFIG" = "config/scalper.yaml" ]; then
  BOT="scalper"
  CFG_ARG=""
else
  BOT="scalper-$(basename "$CONFIG" .yaml)"
  CFG_ARG="--config $CONFIG"
fi
DASH="$BOT-dashboard"
MODE_ARG=""
if [ -n "$RUN_MODE" ]; then MODE_ARG="--mode $RUN_MODE"; fi

# Jangan pernah menimpa service milik program lain dengan nama yang sama.
for unit in "$BOT" "$DASH"; do
  f="$UNIT_DIR/$unit.service"
  if [ -f "$f" ] && ! grep -qxF "WorkingDirectory=$APP_DIR" "$f"; then
    fail "$f sudah ada dan menunjuk ke folder lain. Tidak ditimpa; periksa dulu isinya (cat $f)."
  fi
done

# ---------------------------------------------------------------------------
say "1/5 Python"
apt_install() {
  $SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 \
    apt-get -o DPkg::Lock::Timeout=300 install -y -qq "$@" >/dev/null
}
if command -v apt-get >/dev/null 2>&1 && ! venv/bin/python -m pip --version >/dev/null 2>&1; then
  $SUDO apt-get -o DPkg::Lock::Timeout=300 update -qq || echo "(apt-get update melaporkan error; lanjut)"
  apt_install python3 python3-venv python3-pip git
fi
find_python() {
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ] && command -v apt-get >/dev/null 2>&1; then
  echo "python3 bawaan terlalu lama (butuh 3.9+); memasang python3.9..."
  apt_install python3.9 python3.9-venv || true
  PY="$(find_python || true)"
fi
[ -n "$PY" ] || fail "butuh Python 3.9 atau lebih baru (Ubuntu 22.04/24.04 sudah punya)."
echo "Memakai $("$PY" --version)"

# ---------------------------------------------------------------------------
say "2/5 Virtualenv & paket"
if ! venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf venv
  "$PY" -m venv venv || fail "venv gagal dibuat. Coba: sudo apt-get install python3-venv"
fi
venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip
venv/bin/python -m pip install -q --disable-pip-version-check -r requirements-scalper.txt
echo "Paket terpasang."

# ---------------------------------------------------------------------------
say "3/5 File .env (API key)"
if [ -f .env ]; then
  echo ".env sudah ada, dipakai apa adanya."
else
  [ -t 0 ] || fail "belum ada .env. Salin .env dari PC ke $APP_DIR, lalu jalankan skrip ini lagi."
  echo "Belum ada .env. Cara termudah: salin .env dari PC pakai scp (lihat panduan),"
  echo "atau ketik/tempel key TESTNET sekarang. Enter tanpa isi = batal."
  read -r -p "  BINANCE_TESTNET_API_KEY: " key
  [ -n "$key" ] || fail "dibatalkan. Salin .env ke $APP_DIR, lalu jalankan skrip ini lagi."
  read -r -s -p "  BINANCE_TESTNET_API_SECRET (tidak tampil di layar): " secret
  echo
  [ -n "$secret" ] || fail "secret kosong"
  (umask 077; printf 'SCALPER_MODE=testnet\nBINANCE_TESTNET_API_KEY=%s\nBINANCE_TESTNET_API_SECRET=%s\n' \
    "$key" "$secret" > .env)
  echo ".env dibuat: mode testnet, key ...${key: -4}, secret ${#secret} karakter."
  unset key secret
fi
chmod 600 .env

# ---------------------------------------------------------------------------
say "4/5 Cek konfigurasi, koneksi & API key"
# shellcheck disable=SC2086  # CFG_ARG / MODE_ARG are deliberately split into words
venv/bin/python -m scalper $CFG_ARG check $MODE_ARG \
  || fail "ada [FAIL] di atas (misalnya key salah, saldo 0, atau VPS tidak bisa menjangkau Binance).
Perbaiki dulu, lalu jalankan skrip ini lagi. Bot belum dinyalakan."
MODE="$(venv/bin/python - "$CONFIG" "$RUN_MODE" 2>/dev/null <<'PY' || echo "?"
import sys
from scalper.config import load_settings
print(load_settings(sys.argv[1], mode_override=sys.argv[2] or None).mode)
PY
)"

# ---------------------------------------------------------------------------
say "5/5 Service 24/7"
PYBIN="$APP_DIR/venv/bin/python"
S="${SUDO:+sudo }"

# Port dashboard. Bot lain di VPS ini bisa saja sudah memakai port yang sama
# untuk dashboard-nya, jadi pilih port yang benar-benar kosong.
listening() {
  local out
  out="$(ss -ltn 2>/dev/null || true)"
  printf '%s\n' "$out" | awk -v p="$1" '{ n = split($4, a, ":"); if (a[n] == p) f = 1 } END { exit !f }'
}
OLD_PORT=""
if [ -f "$UNIT_DIR/$DASH.service" ]; then
  OLD_PORT="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$UNIT_DIR/$DASH.service" | head -n 1)"
fi
$SUDO systemctl stop "$DASH.service" >/dev/null 2>&1 || true
ports=()
if [ -n "${SCALPER_DASHBOARD_PORT:-}" ]; then ports+=("$SCALPER_DASHBOARD_PORT"); fi
if [ -n "$OLD_PORT" ]; then ports+=("$OLD_PORT"); fi
for p in $(seq 8777 8799); do ports+=("$p"); done
PORT=""
for p in "${ports[@]}"; do
  if ! listening "$p"; then PORT="$p"; break; fi
  echo "Port $p sudah dipakai program lain (misalnya dashboard bot lain); mencari port lain."
done
[ -n "$PORT" ] || fail "tidak menemukan port kosong untuk dashboard (8777-8799)"

$SUDO tee "$UNIT_DIR/$BOT.service" >/dev/null <<EOF
[Unit]
Description=Scalper bot Binance USD-M futures ($APP_DIR, $CONFIG)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$PYBIN -m scalper $CFG_ARG run $MODE_ARG --yes
Environment=PYTHONUNBUFFERED=1
# SIGINT = Ctrl+C: bot menyelesaikan langkahnya, menyimpan state, lalu berhenti.
# Posisi terbuka tetap dilindungi stop-loss di exchange.
KillSignal=SIGINT
TimeoutStopSec=90
Restart=on-failure
RestartSec=30
# exit code 2 = konfigurasi salah: jangan restart terus, perbaiki dulu
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
EOF

$SUDO tee "$UNIT_DIR/$DASH.service" >/dev/null <<EOF
[Unit]
Description=Scalper dashboard for $CONFIG (127.0.0.1:$PORT, buka lewat SSH tunnel)
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$PYBIN -m scalper $CFG_ARG dashboard --no-browser --port $PORT $MODE_ARG
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$BOT.service" "$DASH.service" >/dev/null 2>&1 \
  || fail "service tidak bisa diaktifkan (systemctl enable $BOT)"
$SUDO timedatectl set-ntp true >/dev/null 2>&1 || true  # jam akurat untuk tanda tangan API Binance
since="$(date '+%Y-%m-%d %H:%M:%S')"
$SUDO systemctl restart "$BOT.service" "$DASH.service" || true

printf 'Menunggu bot start'
started=""
for _ in $(seq 1 60); do
  sleep 2
  printf '.'
  log="$($SUDO journalctl -u "$BOT.service" --since "$since" --no-pager -q 2>/dev/null || true)"
  case "$log" in *"Scalper started"*) started=1; break ;; esac
  [ "$(systemctl is-active "$BOT.service" 2>/dev/null || true)" = "active" ] || break
done
echo
if [ -z "$started" ]; then
  $SUDO journalctl -u "$BOT.service" --since "$since" --no-pager -n 40 || true
  if [ "$(systemctl is-active "$BOT.service" 2>/dev/null || true)" = "active" ]; then
    echo "Bot masih dalam proses start. Pantau dengan: ${S}journalctl -u $BOT -f"
    exit 0
  fi
  fail "bot belum berhasil start; lihat log di atas (service mencoba lagi tiap 30 detik).
Setelah penyebabnya diperbaiki, jalankan lagi: bash deploy/setup_vps.sh $*"
fi

dash_ok=""
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$DASH.service" && listening "$PORT"; then dash_ok=1; break; fi
  sleep 1
done
if [ -z "$dash_ok" ]; then
  $SUDO journalctl -u "$DASH.service" --since "$since" --no-pager -n 15 || true
  echo "PERINGATAN: dashboard belum jalan (bot tetap jalan normal). Penyebabnya ada di log di atas."
fi

SETUP_CMD="bash deploy/setup_vps.sh${1:+ $*}"

# SSH_CONNECTION = "ip-klien port-klien ip-vps port-ssh": isi otomatis perintah tunnel
# shellcheck disable=SC2086
set -- ${SSH_CONNECTION:-}
IP="${3:-IP-VPS}"
SSH_PORT_OPT=""
if [ -n "${4:-}" ] && [ "${4}" != "22" ]; then SSH_PORT_OPT="-p $4 "; fi

cat <<EOF

============================================================
 SELESAI. Bot $BOT jalan 24/7 di VPS ini (mode ${MODE^^}, $CONFIG).
 Hidup lagi otomatis kalau crash atau VPS reboot.
============================================================

Di VPS:
  ${S}journalctl -u $BOT -f        log langsung (Ctrl+C = keluar, bot TETAP jalan)
  cd $APP_DIR && bash deploy/health.sh
                                      cek kesehatan bot (service, error, saldo, koneksi)
  ${S}systemctl stop $BOT          hentikan bot
  ${S}systemctl start $BOT         nyalakan lagi
  cd $APP_DIR && git pull && $SETUP_CMD
                                      update bot

Dashboard $BOT, dari PC (PowerShell):
  ssh -N ${SSH_PORT_OPT}-L $PORT:127.0.0.1:$PORT $RUN_USER@$IP
  Biarkan jendela itu terbuka, lalu buka http://127.0.0.1:$PORT di browser PC.
  Judul halamannya "Scalper Dashboard".
EOF
