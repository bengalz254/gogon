"""Sanity-check your .env / config before running the bot live.

Usage: python scripts/check_setup.py
"""
from __future__ import annotations

import os
import sys

# Allow running this script directly (e.g. `python scripts/check_setup.py`)
# by making sure the project root (containing the `bot` package) is on
# sys.path, regardless of the current working directory.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot.config import load_settings


def main() -> int:
    try:
        settings = load_settings()
    except Exception as e:
        print(f"[FAIL] Could not load configuration: {e}")
        return 1

    print("Configuration loaded OK.")
    print(f"  Mode:              {'LIVE' if settings.wallet.live_trading else 'PAPER (simulation)'}")
    print(f"  CLOB host:         {settings.wallet.clob_host}")
    print(f"  Chain id:          {settings.wallet.chain_id}")
    print(f"  Signature type:    {settings.wallet.signature_type}")
    print(f"  Funder address:    {settings.wallet.funder_address or '(not set)'}")
    print(f"  Private key set:   {'yes' if settings.wallet.private_key else 'no'}")
    print(f"  Polling interval:  {settings.polling_interval_seconds}s")
    print(f"  Arbitrage enabled: {settings.arbitrage.enabled}")
    print(f"  Threshold enabled: {settings.threshold.enabled}")
    print(f"  Max position:      ${settings.risk.max_position_usd}")
    print(f"  Max exposure:      ${settings.risk.max_total_exposure_usd}")
    print(f"  Max daily loss:    ${settings.risk.max_daily_loss_usd}")

    try:
        from bot.updown.config import load_updown_config

        ud = load_updown_config()
        enabled = [n for n in ("fair_value", "late_certainty", "constellation", "pair_barbell", "cheap_asymmetric")
                   if getattr(ud.strategies, n).enabled]
        print("\nUp/Down engine (python -m bot.updown):")
        print(f"  Assets / interval: {', '.join(ud.markets.assets)} / {ud.markets.interval}")
        print(f"  Settlement rule:   {ud.settlement.rule}"
              + (f" ({ud.settlement.twap_seconds}s)" if ud.settlement.rule == "twap" else ""))
        print(f"  Strategies:        {', '.join(enabled) or '(none)'}")
        print(f"  Bankroll / Kelly:  ${ud.risk.bankroll_usd} / {ud.risk.kelly_fraction}")
        print(f"  Max daily loss:    ${ud.risk.max_daily_loss_usd}")
        print(f"  allow_live:        {ud.execution.allow_live}")
        if settings.wallet.live_trading and not ud.execution.allow_live:
            print("  [NOTE] LIVE_TRADING=true but execution.allow_live=false: the Up/Down engine will refuse to start live.")
    except Exception as e:
        print(f"[FAIL] Up/Down config invalid: {e}")
        return 1

    if settings.wallet.live_trading:
        print("\nAttempting to connect and derive API credentials (LIVE mode)...")
        try:
            from bot.client import build_client

            client = build_client(settings.wallet)
            print("[OK] Connected and authenticated with the Polymarket CLOB.")
            try:
                book = client.get_sampling_markets()
                n = len(book.get("data", [])) if isinstance(book, dict) else 0
                print(f"[OK] Fetched sampling markets ({n} returned).")
            except Exception as e:
                print(f"[WARN] Could not fetch sampling markets: {e}")
        except Exception as e:
            print(f"[FAIL] Could not connect/authenticate: {e}")
            return 1
    else:
        print("\nPaper trading mode — skipping live authentication check.")
        print("Set LIVE_TRADING=true in .env (with POLY_PRIVATE_KEY and")
        print("POLY_FUNDER_ADDRESS filled in) when you're ready to go live.")

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
