#!/usr/bin/env bash
# Pull the latest code and restart the services:  sudo bash deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
git pull --ff-only
"$APP_DIR/venv/bin/pip" install -q -r requirements.txt
chown -R gogon:gogon "$APP_DIR"
systemctl restart gogon-bot gogon-dashboard
echo "Updated to $(git log --oneline -1) and restarted."
