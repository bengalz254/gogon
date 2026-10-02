#!/usr/bin/env bash
# Pasang swarmbot (mode PAPER) di VPS Ubuntu/Debian supaya jalan 24/7.
#
#   cd ~/swarmbot
#   bash deploy/setup_swarmbot.sh
#
# Aman dijalankan ulang untuk update:  git pull && bash deploy/setup_swarmbot.sh
# .env dan data bot (data/swarmbot/) tidak pernah diubah oleh skrip ini.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
UNIT_DIR="${SWARMBOT_UNIT_DIR:-/etc/systemd/system}"
BOT="swarmbot"
DASH="swarmbot-dashboard"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mGAGAL: %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  fail "jalankan TANPA sudo:  bash deploy/setup_swarmbot.sh"
fi
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "perintah sudo tidak ada; login sebagai root"
  SUDO="sudo"
fi

cd "$APP_DIR"
[ -f requirements-swarmbot.txt ] && [ -d swarmbot ] || fail "folder bot tidak lengkap: $APP_DIR (sudah git pull?)"
case "$APP_DIR" in *" "*) fail "nama folder mengandung spasi ($APP_DIR)" ;; esac
command -v systemctl >/dev/null 2>&1 || fail "systemd tidak ditemukan. Skrip ini untuk VPS Ubuntu/Debian."

for unit in "$BOT" "$DASH"; do
  f="$UNIT_DIR/$unit.service"
  if [ -f "$f" ] && ! grep -qxF "WorkingDirectory=$APP_DIR" "$f"; then
    fail "$f sudah ada dan menunjuk ke folder lain. Tidak ditimpa; periksa dulu (cat $f)."
  fi
done

say "1/5 Python"
if command -v apt-get >/dev/null 2>&1 && ! venv/bin/python -m pip --version >/dev/null 2>&1; then
  $SUDO apt-get -o DPkg::Lock::Timeout=300 update -qq || echo "(apt-get update error; lanjut)"
  $SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 \
    apt-get -o DPkg::Lock::Timeout=300 install -y -qq python3 python3-venv python3-pip git >/dev/null
fi
command -v python3 >/dev/null 2>&1 || fail "python3 tidak ada"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "butuh Python 3.10 atau lebih baru"
echo "Memakai $(python3 --version)"

say "2/5 Virtualenv, paket & tes"
if ! venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf venv
  python3 -m venv venv || fail "venv gagal dibuat. Coba: sudo apt-get install python3-venv"
fi
venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip
venv/bin/python -m pip install -q --disable-pip-version-check -r requirements-swarmbot.txt
venv/bin/python -m pytest -q -p no:cacheprovider tests/test_swarmbot.py || fail "tes otomatis gagal; kirim tulisan di atas ke Claude"

say "3/5 File .env"
if [ -f .env ]; then
  echo ".env sudah ada, dipakai apa adanya."
else
  cp swarmbot.env.example .env
  echo ".env dibuat. Mode paper tidak butuh isian apa pun."
fi
chmod 600 .env

say "4/5 Cek koneksi ke Jupiter dan mood saat ini"
venv/bin/python -m swarmbot check || fail "data token Jupiter tidak bisa dibaca. Bot belum dinyalakan."

say "5/5 Service 24/7"
$SUDO tee "$UNIT_DIR/$BOT.service" >/dev/null <<UNIT
[Unit]
Description=swarmbot: paper bot mood dotswarm ($APP_DIR)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m swarmbot run
Environment=PYTHONUNBUFFERED=1
KillSignal=SIGINT
TimeoutStopSec=30
Restart=on-failure
RestartSec=20
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
UNIT
listening() {
  ss -ltn 2>/dev/null | awk -v p="$1" '{ n = split($4, a, ":"); if (a[n] == p) f = 1 } END { exit !f }'
}
OLD_PORT=""
if [ -f "$UNIT_DIR/$DASH.service" ]; then
  OLD_PORT="$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$UNIT_DIR/$DASH.service" | head -n 1)"
fi
$SUDO systemctl stop "$DASH.service" >/dev/null 2>&1 || true
PORT=""
for p in ${SWARMBOT_DASHBOARD_PORT:-} $OLD_PORT $(seq 8790 8799); do
  if ! listening "$p"; then PORT="$p"; break; fi
  echo "Port $p sudah dipakai program lain; mencari port lain."
done
[ -n "$PORT" ] || fail "tidak menemukan port kosong untuk dashboard (8790-8799)"

$SUDO tee "$UNIT_DIR/$DASH.service" >/dev/null <<UNIT
[Unit]
Description=swarmbot dashboard (127.0.0.1:$PORT, buka lewat SSH tunnel)
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m swarmbot dashboard --port $PORT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$BOT.service" "$DASH.service" >/dev/null 2>&1 || fail "service tidak bisa diaktifkan"
since="$(date '+%Y-%m-%d %H:%M:%S')"
$SUDO systemctl restart "$BOT.service" "$DASH.service" || true

printf 'Menunggu bot start'
started=""
for _ in $(seq 1 20); do
  sleep 2; printf '.'
  log="$($SUDO journalctl -u "$BOT.service" --since "$since" --no-pager -q 2>/dev/null || true)"
  case "$log" in *"swarmbot started"*) started=1; break ;; esac
done
echo
if [ -z "$started" ]; then
  $SUDO journalctl -u "$BOT.service" --since "$since" --no-pager -n 40 || true
  fail "bot belum berhasil start; lihat log di atas."
fi

dash_ok=""
for _ in $(seq 1 15); do
  if systemctl is-active --quiet "$DASH.service" && listening "$PORT"; then dash_ok=1; break; fi
  sleep 1
done
[ -n "$dash_ok" ] || echo "PERINGATAN: dashboard belum jalan (bot tetap jalan). Cek: journalctl -u $DASH -n 30"

# shellcheck disable=SC2086
set -- ${SSH_CONNECTION:-}
IP="${3:-IP-VPS}"
SSH_PORT_OPT=""
if [ -n "${4:-}" ] && [ "${4}" != "22" ]; then SSH_PORT_OPT="-p $4 "; fi

S="${SUDO:+sudo }"
cat <<DONE

============================================================
 SELESAI. swarmbot jalan 24/7 di VPS ini (mode PAPER).
 Hidup lagi otomatis kalau crash atau VPS reboot.
============================================================

  ${S}journalctl -u $BOT -f          lihat log langsung (Ctrl+C = keluar, bot TETAP jalan)
  cd $APP_DIR && venv/bin/python -m swarmbot report     laporan untung/rugi
  cd $APP_DIR && venv/bin/python -m swarmbot moods      token yang mood-nya cocok sekarang
  ${S}systemctl stop $BOT / start $BOT     hentikan / nyalakan
  cd $APP_DIR && git pull && bash deploy/setup_swarmbot.sh   update bot

Dashboard dari PC (PowerShell, jendela BARU):
  ssh -N ${SSH_PORT_OPT}-L $PORT:127.0.0.1:$PORT $RUN_USER@$IP
  Biarkan jendela itu terbuka, lalu buka http://127.0.0.1:$PORT di browser.
DONE
