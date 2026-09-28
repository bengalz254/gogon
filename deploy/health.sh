#!/usr/bin/env bash
# Cek kesehatan bot scalper di VPS. Hanya membaca, tidak mengubah apa pun,
# jadi aman dijalankan kapan saja saat bot sedang jalan.
#
#   cd ~/scalper-bot && bash deploy/health.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

WARNS=0
ok()      { printf '  [OK] %s\n' "$*"; }
warn()    { printf '  [!]  %s\n' "$*"; WARNS=$((WARNS + 1)); }
section() { printf '\n== %s\n' "$*"; }
indent()  { cut -c1-170 | sed "s/^/${1-    }/"; }

echo "Cek kesehatan bot scalper, $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "VPS: IP publik $(hostname -I 2>/dev/null | awk '{ print $1 }'), nama $(hostname)"

# ---------------------------------------------------------------------------
section "Service"
for unit in scalper scalper-dashboard; do
  state="$(systemctl is-active "$unit.service" 2>/dev/null || true)"
  if [ "$state" = "active" ]; then
    since="$(systemctl show "$unit.service" -p ActiveEnterTimestamp --value 2>/dev/null || true)"
    ok "$unit jalan sejak $since"
    restarts="$(systemctl show "$unit.service" -p NRestarts --value 2>/dev/null || true)"
    case "$restarts" in ''|*[!0-9]*) restarts=0 ;; esac
    if [ "$restarts" -gt 0 ]; then
      warn "$unit sudah $restarts kali dinyalakan ulang otomatis (pernah crash)"
      journalctl -u "$unit.service" --no-pager -n 300 2>/dev/null | grep -A 12 'Traceback' | tail -n 12 | indent
    fi
  else
    warn "$unit TIDAK jalan (status: ${state:-tidak terpasang})"
  fi
done

# ---------------------------------------------------------------------------
section "Aktivitas"
MODE="$(venv/bin/python -c 'from scalper.config import load_settings; print(load_settings().mode)' 2>/dev/null || true)"
STATE="data/scalper/state_${MODE:-unknown}.json"
if [ -f "$STATE" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$STATE") ))
  if [ "$age" -le 180 ]; then
    ok "mode ${MODE^^}: bot aktif, state terakhir disimpan $age detik lalu"
  else
    warn "mode ${MODE^^}: state terakhir disimpan $((age / 60)) menit lalu (normal: di bawah 2 menit). Bot macet atau tidak tersambung ke exchange."
  fi
else
  warn "file $STATE belum ada (bot belum pernah jalan di mode ${MODE:-?})"
fi

LOG="logs/scalper.log"
if [ -f "$LOG" ]; then
  since="$(date -d '24 hours ago' '+%Y-%m-%d %H:%M:%S')"
  # Baris lanjutan (misalnya isi Traceback) ikut umur baris bertanggal di atasnya.
  recent="$(awk -v s="$since" '/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { keep = (substr($0, 1, 19) >= s) } keep' "$LOG")"
  count() { printf '%s\n' "$recent" | grep -cE "$1" || true; }
  n_open="$(count 'OPENED ')"
  n_closed="$(count 'CLOSED ')"
  n_err="$(count ' (ERROR|CRITICAL) ')"
  n_warn="$(count ' WARNING ')"
  n_crash="$(count '^Traceback')"
  echo "  24 jam terakhir: $n_open trade dibuka, $n_closed ditutup, $n_warn WARNING"
  if [ "$n_err" -eq 0 ] && [ "$n_crash" -eq 0 ]; then
    ok "tidak ada ERROR di log 24 jam terakhir"
  else
    warn "$n_err ERROR dan $n_crash crash (Traceback) di log 24 jam terakhir"
  fi
  if [ $((n_err + n_warn)) -gt 0 ]; then
    echo "  Pesan terakhir yang perlu dilihat:"
    printf '%s\n' "$recent" | grep -E ' (WARNING|ERROR|CRITICAL) ' | tail -n 5 | indent
  fi
  if [ "$n_crash" -gt 0 ]; then
    echo "  Crash terakhir:"
    printf '%s\n' "$recent" | awk '
      /^Traceback/ { blk = ""; on = 1 }
      /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { on = 0 }
      on { blk = blk $0 "\n" }
      END { printf "%s", blk }' | tail -n 12 | indent
  fi
  beat="$(grep 'Heartbeat:' "$LOG" | tail -n 1)"
  if [ -n "$beat" ]; then
    echo "  Heartbeat terakhir:"
    printf '%s\n' "$beat" | indent
  fi
else
  warn "log $LOG belum ada"
fi

# ---------------------------------------------------------------------------
section "Posisi, saldo & risiko"
status_out="$(venv/bin/python -m scalper status 2>&1 || true)"
printf '%s\n' "$status_out" | indent "  "
case "$status_out" in
  *HALTED*) warn "bot BERHENTI membuka trade karena batas risiko (lihat HALTED di atas)" ;;
esac

# ---------------------------------------------------------------------------
section "Koneksi exchange & API key"
check_out="$(venv/bin/python -m scalper check 2>&1 || true)"
printf '%s\n' "$check_out" | grep -E '\[(OK|WARN|FAIL)\]' | indent ""
case "$check_out" in
  *"All checks passed"*) ok "check lulus" ;;
  *) warn "check menemukan masalah (lihat [FAIL] di atas)" ;;
esac

# ---------------------------------------------------------------------------
section "VPS"
if [ -f /var/run/reboot-required ]; then
  warn "VPS perlu restart untuk menerapkan update keamanan (ketik: reboot). Bot scalper menyala lagi sendiri."
fi
ntp="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
if [ "$ntp" = "yes" ]; then ok "jam tersinkron (NTP)"; else warn "jam VPS belum tersinkron (NTP: ${ntp:-tidak diketahui})"; fi
disk="$(df -P . | awk 'NR == 2 { gsub("%", "", $5); print $5 }')"
case "$disk" in ''|*[!0-9]*) disk=0 ;; esac
if [ "$disk" -lt 90 ]; then ok "disk terpakai ${disk}%"; else warn "disk hampir penuh: ${disk}% terpakai"; fi
mem="$(free -m | awk '/^Mem:/ { print $7 }')"
case "$mem" in ''|*[!0-9]*) mem=0 ;; esac
if [ "$mem" -ge 150 ]; then ok "RAM tersedia ${mem} MB"; else warn "RAM hampir habis: ${mem} MB tersedia"; fi

echo
if [ "$WARNS" -eq 0 ]; then
  echo "KESIMPULAN: semua sehat."
else
  echo "KESIMPULAN: $WARNS hal perlu dicek (tanda [!] di atas)."
fi
