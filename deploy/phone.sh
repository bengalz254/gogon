#!/usr/bin/env bash
# Open the dashboard from your phone, privately, with Tailscale:
#   sudo bash deploy/phone.sh
# The dashboard stays closed to the internet; only devices logged into YOUR
# Tailscale account can reach it.
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo "Run with sudo: sudo bash deploy/phone.sh"; exit 1; fi
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8766

if ! command -v tailscale >/dev/null; then
  echo "==> Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

if ! tailscale ip -4 >/dev/null 2>&1; then
  echo "==> Log this server into Tailscale."
  echo "    A link appears below: open it (on your phone or PC) and log in with the"
  echo "    SAME account you will use in the Tailscale app on your phone."
  tailscale up
fi
TSIP="$(tailscale ip -4 | head -1)"
echo "==> Tailscale address of this server: $TSIP"

mkdir -p /etc/systemd/system/gogon-dashboard.service.d
cat > /etc/systemd/system/gogon-dashboard.service.d/phone.conf <<CONF
[Unit]
After=tailscaled.service
Wants=tailscaled.service

[Service]
ExecStart=
ExecStart=$APP_DIR/venv/bin/python scripts/updown_dashboard.py --no-browser --host 127.0.0.1 --host $TSIP --port $PORT
CONF

if command -v ufw >/dev/null; then
  ufw allow in on tailscale0 to any port "$PORT" proto tcp >/dev/null
fi
systemctl daemon-reload
systemctl restart gogon-dashboard
sleep 2
systemctl --no-pager --lines=0 status gogon-dashboard | grep -E "Active:"

cat <<MSG

Done. On your phone:
  1. Install the "Tailscale" app (Play Store / App Store).
  2. Log in with the same account you just used for this server, and switch it ON.
  3. Open in the phone browser:   http://$TSIP:$PORT
The dashboard is NOT open to the public internet: only your Tailscale devices can see it.
To undo:  sudo rm -r /etc/systemd/system/gogon-dashboard.service.d && sudo systemctl daemon-reload && sudo systemctl restart gogon-dashboard
MSG
