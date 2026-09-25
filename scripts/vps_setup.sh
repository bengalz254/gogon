#!/usr/bin/env bash
# One-time setup of the Up/Down bot on an Ubuntu/Debian VPS.
#
#   git clone https://github.com/bengalz254/gogon.git && cd gogon
#   git checkout claude/epic-brahmagupta-gxpbv2
#   bash scripts/vps_setup.sh
#
# Installs Python, creates the venv, installs the requirements, runs the tests,
# then installs two systemd services that start on boot and restart after a
# crash: updown-bot (paper mode unless live trading is switched on in BOTH .env
# and config/updown.yaml) and updown-dashboard (listens on 127.0.0.1 only; view
# it through an SSH tunnel). Also adds three commands: updown-update,
# updown-log and updown-status. Safe to run again.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

say() { printf '\n==> %s\n' "$*"; }

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script supports Ubuntu/Debian (apt-get) only." >&2
    exit 1
fi

say "Installing Python and git"
# Non-interactive: Ubuntu's needrestart would otherwise stop and ask which services to restart.
$SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -y -q
$SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -q python3 python3-venv python3-pip git curl

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python {sys.version.split()[0]} is too old: use Ubuntu 22.04+ or Debian 12+ (Python 3.10 or newer).")
PY

say "Creating the virtual environment and installing the requirements"
python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example (paper mode: LIVE_TRADING=false)."
fi
chmod 600 .env
mkdir -p data logs

say "Running the tests"
venv/bin/python -m pytest -q

say "Checking the connection to Polymarket from this server"
if t=$(curl -sS -o /dev/null --max-time 15 -w 'connect %{time_connect}s, first byte %{time_starttransfer}s' \
        https://clob.polymarket.com/time 2>/dev/null); then
    echo "clob.polymarket.com: $t"
else
    echo "clob.polymarket.com: not reachable"
fi
echo "Region check (live trading must be allowed where this server is):"
curl -s --max-time 15 https://polymarket.com/api/geoblock | head -c 300 || echo "(no answer)"
echo

say "Installing the helper commands"
$SUDO tee /usr/local/bin/updown-update >/dev/null <<EOF
#!/usr/bin/env bash
# Download the latest bot version and restart it.
set -e
cd "$REPO"
git pull
venv/bin/pip install -q -r requirements.txt
S=""; [ "\$(id -u)" -ne 0 ] && S="sudo"
\$S systemctl restart updown-bot updown-dashboard 2>/dev/null || echo "(systemd not available: restart the bot yourself)"
echo "Now running: \$(git log --oneline -1)"
EOF
$SUDO tee /usr/local/bin/updown-log >/dev/null <<EOF
#!/usr/bin/env bash
# Recent connection events, plus where the event loop was last stuck (if ever).
cd "$REPO"
[ -f logs/bot.log ] || { echo "logs/bot.log does not exist yet"; exit 0; }
grep -E "Strategies enabled|clob feed (connected|disconnected)|Event loop" logs/bot.log | tail -n 15
if grep -q "stuck here" logs/bot.log; then
    echo
    echo "Last place the event loop was stuck:"
    grep -A 14 "stuck here" logs/bot.log | tail -n 15
fi
EOF
$SUDO tee /usr/local/bin/updown-status >/dev/null <<EOF
#!/usr/bin/env bash
# Is the bot running, which version, and its last log lines.
cd "$REPO"
echo "Version: \$(git log --oneline -1)"
systemctl is-active updown-bot >/dev/null 2>&1 && echo "Bot: running" || echo "Bot: NOT running"
systemctl is-active updown-dashboard >/dev/null 2>&1 && echo "Dashboard: running" || echo "Dashboard: NOT running"
echo
tail -n 20 logs/bot.log 2>/dev/null || true
EOF
$SUDO chmod 755 /usr/local/bin/updown-update /usr/local/bin/updown-log /usr/local/bin/updown-status

if [ ! -d /run/systemd/system ]; then
    say "systemd is not available on this server"
    echo "Start the bot by hand and keep it running after you log out:"
    echo "  cd $REPO && nohup venv/bin/python -m bot.updown --record > logs/console.log 2>&1 &"
    exit 0
fi

say "Installing the systemd services"
$SUDO tee /etc/systemd/system/updown-bot.service >/dev/null <<EOF
[Unit]
Description=Polymarket Up/Down bot (paper unless live is enabled in .env and config/updown.yaml)
Wants=network-online.target
After=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$REPO
ExecStart=$REPO/venv/bin/python -m bot.updown --record
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
$SUDO tee /etc/systemd/system/updown-dashboard.service >/dev/null <<EOF
[Unit]
Description=Up/Down bot dashboard (127.0.0.1:8766, read-only)
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$REPO
ExecStart=$REPO/venv/bin/python scripts/updown_dashboard.py --no-browser
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable updown-bot updown-dashboard >/dev/null
$SUDO systemctl restart updown-bot updown-dashboard
sleep 3
systemctl --no-pager --lines=0 status updown-bot updown-dashboard || true

say "Done"
cat <<EOF
The bot now runs by itself, also after a reboot or a crash.
  updown-status   is it running + last log lines
  updown-log      recent connection events (paste this into the chat)
  updown-update   download the latest version and restart
Dashboard: on your PC run  ssh -N -L 8767:127.0.0.1:8766 $RUN_USER@<server-ip>
           then open http://127.0.0.1:8767  (or double-click vps_dashboard.bat)
EOF
