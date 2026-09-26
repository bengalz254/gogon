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
