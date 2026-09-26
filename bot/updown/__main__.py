"""Entry point: python -m bot.updown [--config config/updown.yaml] [--record] [--log-file logs/bot.log] [--paper]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import replace

from bot.config import load_wallet_config
from bot.logger import setup_logging
from bot.updown.config import ConfigError, load_updown_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Polymarket crypto Up/Down engine")
    parser.add_argument("--config", default=None, help="path to updown.yaml (default: config/updown.yaml)")
    parser.add_argument("--record", action="store_true", help="record all input events for backtesting")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--log-file", default=os.path.join("logs", "bot.log"),
                        help="log file (default: logs/bot.log); give a second engine its own")
    parser.add_argument("--paper", action="store_true",
                        help="always paper trade, even with LIVE_TRADING=true (for experiments running next to a live engine)")
    args = parser.parse_args(argv)

    logger = setup_logging(os.path.dirname(args.log_file) or ".", filename=os.path.basename(args.log_file),
                           level=logging.DEBUG if args.debug else logging.INFO)
    try:
        wallet = load_wallet_config()
        cfg = load_updown_config(args.config)
    except (ConfigError, ValueError) as e:
        logger.error("Configuration error: %s", e)
        return 2
    if args.paper:
        wallet = replace(wallet, live_trading=False)

    if wallet.live_trading and not cfg.execution.allow_live:
        logger.error(
            "LIVE_TRADING=true, but execution.allow_live is false in the Up/Down config. This engine "
            "has not been validated against the live API: paper trade first, then set "
            "execution.allow_live: true deliberately. Refusing to start."
        )
        return 2
    if wallet.live_trading:
        logger.warning("*** LIVE TRADING ENABLED for the Up/Down engine: real orders, real money ***")
    else:
        logger.info("PAPER mode: fills are simulated against the live order books; no orders are sent.")

    from bot.updown.runner import Runner

    try:
        return asyncio.run(Runner(cfg, wallet, record=args.record).run()) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
