#!/usr/bin/env bash
# Trading report + service health:  sudo bash deploy/report.sh [--hours 24]
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR" && "$APP_DIR/venv/bin/python" scripts/updown_report.py "$@"
echo
systemctl --no-pager --lines=0 status gogon-bot gogon-dashboard | grep -E "●|Active:"
