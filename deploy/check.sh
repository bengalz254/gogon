#!/usr/bin/env bash
# Can this server reach everything the bot needs? Run BEFORE installing:
#   bash deploy/check.sh
# Needs only curl (preinstalled on Ubuntu).
set -u

ok=0; bad=0
check() {  # name, method, url, [json body]
  local name="$1" method="$2" url="$3" body="${4:-}"
  local out
  if [ "$method" = POST ]; then
    out=$(curl -sS -o /dev/null -m 8 -w '%{http_code} %{time_total}' -X POST -H 'Content-Type: application/json' -d "$body" "$url" 2>/dev/null)
  else
    out=$(curl -sS -o /dev/null -m 8 -w '%{http_code} %{time_total}' "$url" 2>/dev/null)
  fi
  local code="${out%% *}" secs="${out##* }"
  if [ "$code" = 200 ]; then
    printf '  \033[32mOK  \033[0m %-28s %4.0f ms\n' "$name" "$(awk "BEGIN{print $secs*1000}")"
    ok=$((ok+1))
  else
    local why="HTTP $code"
    [ "$code" = 000 ] && why="no connection"
    printf '  \033[31mFAIL\033[0m %-28s %s\n' "$name" "$why"
    bad=$((bad+1))
  fi
}

echo "Checking the services the bot talks to (from $(curl -s -m 5 https://ipinfo.io/country 2>/dev/null || echo '?')):"
check "Binance price feed"      GET  "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
check "Hyperliquid (HYPE feed)" POST "https://api.hyperliquid.xyz/info" '{"type":"allMids"}'
check "Polymarket Gamma"        GET  "https://gamma-api.polymarket.com/events?limit=1"
check "Polymarket CLOB"         GET  "https://clob.polymarket.com/time"
echo
if [ "$bad" = 0 ]; then
  echo "All $ok checks passed. Next: sudo bash deploy/install.sh"
else
  echo "$bad check(s) failed. A 451/403 from Binance usually means this server's region is blocked;"
  echo "pick another Vultr location (not the USA) before installing."
  exit 1
fi
