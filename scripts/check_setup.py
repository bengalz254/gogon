"""Sanity-check your .env / config before running the bot live.

Usage: python scripts/check_setup.py [--telegram-test]

--telegram-test also sends a test message to your Telegram chat.
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
    print(f"  Market maker:      {settings.market_maker.enabled}")
    print(f"  Multi-outcome arb: {settings.negrisk_arbitrage.enabled}")
    print(f"  WebSocket books:   {settings.market_data.websocket}")
    if settings.market_maker.enabled and not settings.market_data.websocket:
        print("[WARN] market_maker needs market_data.websocket: true — it will stay off.")
    print(f"  Max position:      ${settings.risk.max_position_usd}")
    print(f"  Max exposure:      ${settings.risk.max_total_exposure_usd}")
    print(f"  Max daily loss:    ${settings.risk.max_daily_loss_usd}")
    print(
        f"  Taker fee rates:   default {settings.fees.default_taker_rate}, "
        f"{len(settings.fees.category_taker_rates)} categories (verify against Polymarket's fee page)"
    )
    telegram_on = bool(settings.notifications.telegram_bot_token and settings.notifications.telegram_chat_id)
    print(f"  Telegram alerts:   {'on' if telegram_on else 'off (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)'}")

    if "--telegram-test" in sys.argv:
        from bot.notify import NotificationError, Notifier

        notifier = Notifier(settings.notifications.telegram_bot_token, settings.notifications.telegram_chat_id)
        try:
            notifier.send_now("✅ Tes notifikasi dari bot gogon — Telegram sudah tersambung.")
            print("[OK] Telegram test message sent — check your chat.")
        except NotificationError as e:
            print(f"[FAIL] Telegram test failed: {e}")
            return 1
        except Exception as e:
            # Only the type: request errors embed the URL, which holds the token.
            print(f"[FAIL] Telegram test failed ({type(e).__name__}) — check your network and token.")
            return 1
        finally:
            notifier.close(timeout=1)

    from bot.client import build_client

    if settings.wallet.live_trading:
        print("\nConnecting to the Polymarket CLOB (V2) and deriving API credentials (LIVE mode)...")
        try:
            client = build_client(settings.wallet)
            print("[OK] Connected and authenticated with the Polymarket CLOB.")
        except Exception as e:
            print(f"[FAIL] Could not connect/authenticate: {e}")
            return 1
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            balance = float(resp.get("balance", 0)) / 1e6 if isinstance(resp, dict) else 0.0
            print(f"[OK] Trading balance: {balance:,.2f} pUSD")
            if balance <= 0:
                print(
                    "[WARN] No pUSD to trade with. Deposits through polymarket.com are converted to pUSD\n"
                    "       automatically; USDC.e sent straight to the wallet has to be wrapped into pUSD first."
                )
        except Exception as e:
            print(f"[WARN] Could not read the pUSD balance: {e}")
    else:
        client = build_client(settings.wallet)
        print("\nPaper trading mode — skipping live authentication check.")
        print("Set LIVE_TRADING=true in .env (with POLY_PRIVATE_KEY and")
        print("POLY_FUNDER_ADDRESS filled in) when you're ready to go live.")

    print("\nChecking public market data...")
    try:
        markets = client.get_sampling_markets()
        n = len(markets.get("data", [])) if isinstance(markets, dict) else 0
        print(f"[OK] Fetched sampling markets ({n} returned).")
    except Exception as e:
        print(f"[FAIL] Could not fetch market data from {settings.wallet.clob_host}: {e}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
