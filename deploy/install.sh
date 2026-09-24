#!/usr/bin/env bash
# One-shot install on a fresh Ubuntu server (22.04/24.04). Run from the repo:
#   sudo bash deploy/install.sh
# Installs Python deps into ./venv, runs the bot and the dashboard as systemd
# services under a dedicated 'gogon' user, and starts them. Paper mode.
set -euo pipefail

if [ "$(id -u)" != 0 ]; then echo "Run with sudo: sudo bash deploy/install.sh"; exit 1; fi
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER=gogon

echo "==> Packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip git curl >/dev/null

echo "==> Clock sync (5-minute windows need an accurate clock)"
timedatectl set-ntp true || true

echo "==> User '$APP_USER' owns $APP_DIR"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"

echo "==> Python environment"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "    created .env from .env.example (LIVE_TRADING=false)"
fi
chmod 600 "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# let the admin keep using git in this folder
git config --system --add safe.directory "$APP_DIR" || true

echo "==> systemd services"
for svc in gogon-bot gogon-dashboard; do
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" "$APP_DIR/deploy/$svc.service" > "/etc/systemd/system/$svc.service"
done
systemctl daemon-reload
systemctl enable --now gogon-bot gogon-dashboard
systemctl restart gogon-bot gogon-dashboard

echo "==> Firewall: SSH only (the dashboard stays private, reach it through an SSH tunnel)"
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null
  ufw --force enable >/dev/null
fi

sleep 3
systemctl --no-pager --lines=0 status gogon-bot gogon-dashboard | grep -E "●|Active:"
cat <<MSG

Done. The bot runs in PAPER mode and restarts itself if it crashes or the server reboots.
  Live log:     journalctl -u gogon-bot -f        (Ctrl+C to stop watching)
  Report:       sudo bash deploy/report.sh
  Update:       sudo bash deploy/update.sh
  Dashboard:    on your PC run  ssh -L 8766:127.0.0.1:8766 root@<server-ip>
                then open       http://127.0.0.1:8766
MSG
