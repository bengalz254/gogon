#!/usr/bin/env bash
# Pasang bot meme coin migrated (mode PAPER) di VPS Linux (Ubuntu/Debian) supaya jalan 24/7.
#
#   cd ~/gogon
#   bash deploy/setup_migbot.sh
#
# Yang dilakukan:
#   1. memasang Python 3 + venv lewat apt (minta password sudo kalau perlu)
#   2. membuat virtualenv dan memasang paket bot, lalu menjalankan tes otomatis
#   3. membuat .env dari .env.example kalau belum ada (bisa diisi Telegram belakangan)
#   4. menjalankan "python -m migbot check" (koneksi ke semua sumber data)
#   5. memasang dan menyalakan dua service systemd:
#        migbot            bot-nya; hidup lagi otomatis setelah crash / reboot
#        migbot-dashboard  dashboard di 127.0.0.1:8780 (atau port kosong berikutnya),
#                          dibuka dari PC lewat SSH tunnel atau dari HP lewat Tailscale
#
# Aman dijalankan ulang, misalnya untuk update:  git pull && bash deploy/setup_migbot.sh
# .env dan data bot (data/migbot/) tidak pernah diubah oleh skrip ini.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
UNIT_DIR="${MIGBOT_UNIT_DIR:-/etc/systemd/system}"
BOT="migbot"
DASH="migbot-dashboard"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mGAGAL: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  fail "jalankan TANPA sudo:  bash deploy/setup_migbot.sh   (skrip meminta sudo sendiri kalau perlu)"
fi
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "perintah sudo tidak ada; login sebagai root atau pasang sudo dulu"
  SUDO="sudo"
fi

cd "$APP_DIR"
[ -f requirements-migbot.txt ] && [ -d migbot ] || fail "folder bot tidak lengkap: $APP_DIR (sudah git pull?)"
case "$APP_DIR" in *" "*) fail "nama folder mengandung spasi ($APP_DIR); pindahkan ke folder tanpa spasi" ;; esac
command -v systemctl >/dev/null 2>&1 || fail "systemd tidak ditemukan. Skrip ini untuk VPS Ubuntu/Debian."

# Jangan pernah menimpa service milik folder lain dengan nama yang sama.
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
  for cand in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
[ -n "$PY" ] || fail "butuh Python 3.10 atau lebih baru (Ubuntu 22.04/24.04 sudah punya)."
echo "Memakai $("$PY" --version)"

# ---------------------------------------------------------------------------
say "2/5 Virtualenv, paket & tes"
if ! venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf venv
  "$PY" -m venv venv || fail "venv gagal dibuat. Coba: sudo apt-get install python3-venv"
fi
venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip
venv/bin/python -m pip install -q --disable-pip-version-check -r requirements-migbot.txt
venv/bin/python -m pytest -q -p no:cacheprovider tests/test_migbot_*.py \
  || fail "tes otomatis gagal; laporkan hasil di atas"

# ---------------------------------------------------------------------------
say "3/5 File .env"
if [ -f .env ]; then
  echo ".env sudah ada, dipakai apa adanya."
else
  cp .env.example .env
  echo ".env dibuat dari .env.example. Mode paper tidak butuh isian apa pun;"
  echo "Telegram dan SOLANA_RPC_URL bisa diisi nanti dengan: nano .env"
fi
chmod 600 .env

# ---------------------------------------------------------------------------
say "4/5 Cek koneksi ke sumber data"
venv/bin/python -m migbot check \
  || fail "sumber data wajib gagal (lihat [GAGAL] di atas). Bot belum dinyalakan."

# ---------------------------------------------------------------------------
say "5/5 Service 24/7"
PYBIN="$APP_DIR/venv/bin/python"
S="${SUDO:+sudo }"

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
if [ -n "${MIGBOT_DASHBOARD_PORT:-}" ]; then ports+=("$MIGBOT_DASHBOARD_PORT"); fi
if [ -n "$OLD_PORT" ]; then ports+=("$OLD_PORT"); fi
for p in $(seq 8780 8799); do ports+=("$p"); done
PORT=""
for p in "${ports[@]}"; do
  if ! listening "$p"; then PORT="$p"; break; fi
  echo "Port $p sudah dipakai program lain (misalnya dashboard bot lain); mencari port lain."
done
[ -n "$PORT" ] || fail "tidak menemukan port kosong untuk dashboard (8780-8799)"

$SUDO tee "$UNIT_DIR/$BOT.service" >/dev/null <<EOF
[Unit]
Description=migbot: meme coin migrated paper bot ($APP_DIR)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$PYBIN -m migbot run
Environment=PYTHONUNBUFFERED=1
# SIGINT = Ctrl+C: bot menyimpan state lalu berhenti; posisi paper dilanjutkan saat start lagi.
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
RestartSec=20
# exit code 2 = konfigurasi salah: jangan restart terus, perbaiki dulu
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
EOF

$SUDO tee "$UNIT_DIR/$DASH.service" >/dev/null <<EOF
[Unit]
Description=migbot dashboard (127.0.0.1:$PORT, buka lewat SSH tunnel / Tailscale)
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$PYBIN -m migbot dashboard --no-browser --port $PORT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$BOT.service" "$DASH.service" >/dev/null 2>&1 \
  || fail "service tidak bisa diaktifkan (systemctl enable $BOT)"
$SUDO timedatectl set-ntp true >/dev/null 2>&1 || true
since="$(date '+%Y-%m-%d %H:%M:%S')"
$SUDO systemctl restart "$BOT.service" "$DASH.service" || true

printf 'Menunggu bot start'
started=""
for _ in $(seq 1 30); do
  sleep 2
  printf '.'
  log="$($SUDO journalctl -u "$BOT.service" --since "$since" --no-pager -q 2>/dev/null || true)"
  case "$log" in *"migbot "*"started"*) started=1; break ;; esac
  [ "$(systemctl is-active "$BOT.service" 2>/dev/null || true)" = "active" ] || break
done
echo
if [ -z "$started" ]; then
  $SUDO journalctl -u "$BOT.service" --since "$since" --no-pager -n 40 || true
  fail "bot belum berhasil start; lihat log di atas. Setelah diperbaiki: bash deploy/setup_migbot.sh"
fi

dash_ok=""
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$DASH.service" && listening "$PORT"; then dash_ok=1; break; fi
  sleep 1
done
[ -n "$dash_ok" ] || echo "PERINGATAN: dashboard belum jalan (bot tetap jalan). Cek: ${S}journalctl -u $DASH -n 30"

# shellcheck disable=SC2086
set -- ${SSH_CONNECTION:-}
IP="${3:-IP-VPS}"
SSH_PORT_OPT=""
if [ -n "${4:-}" ] && [ "${4}" != "22" ]; then SSH_PORT_OPT="-p $4 "; fi

cat <<EOF

============================================================
 SELESAI. Bot migbot jalan 24/7 di VPS ini (mode PAPER).
 Hidup lagi otomatis kalau crash atau VPS reboot.
============================================================

Di VPS:
  ${S}journalctl -u $BOT -f                    log langsung (Ctrl+C = keluar, bot TETAP jalan)
  cd $APP_DIR && venv/bin/python -m migbot report
                                                  laporan: apakah filter memilih token yang lebih baik?
  ${S}systemctl stop $BOT / start $BOT     hentikan / nyalakan
  cd $APP_DIR && git pull && bash deploy/setup_migbot.sh
                                                  update bot

Dashboard dari PC (PowerShell):
  ssh -N ${SSH_PORT_OPT}-L $PORT:127.0.0.1:$PORT $RUN_USER@$IP
  Biarkan jendela itu terbuka, lalu buka http://127.0.0.1:$PORT di browser.

Dashboard dari HP (kalau Tailscale sudah terpasang di VPS):
  ${S}tailscale serve --bg --https=8453 $PORT
EOF
