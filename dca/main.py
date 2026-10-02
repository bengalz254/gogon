"""Entry point: python -m dca.main"""
from __future__ import annotations

import logging
import os
import signal as signal_module
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from dca.config import load_dca_settings
from dca.engine import DcaEngine
from dca.exchange import BinanceFutures
from dca.journal import DcaJournal
from dca.live import LiveBroker
from dca.paper import PaperBroker
from dca.signals import candles_needed
from dca.state import StateStore

_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def setup_logging(log_dir: str = "logs") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("dcabot")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        for h in (logging.StreamHandler(),
                  RotatingFileHandler(os.path.join(log_dir, "dca.log"), maxBytes=5_000_000, backupCount=5)):
            h.setFormatter(fmt)
            logger.addHandler(h)
    return logger


def run() -> None:
    settings = load_dca_settings()
    log = setup_logging()

    ex = BinanceFutures(settings.symbol, settings.api_key, settings.api_secret,
                        demo=settings.demo_trading)
    if settings.live_trading:
        broker = LiveBroker(settings, ex)
        if broker.mode == "live":
            log.warning("*** LIVE TRADING on Binance with REAL funds *** Ctrl+C to stop.")
        else:
            log.info("Binance DEMO trading (demo.binance.com keys, no real funds).")
        ex.setup_account(settings.leverage)
    else:
        broker = PaperBroker(settings)
        log.info("PAPER trading — public Binance prices, no orders sent.")

    engine = DcaEngine(
        settings, broker,
        StateStore(f"data/dca_state_{broker.mode}.json"),
        DcaJournal("data/dca_trades.csv"),
    )
    log.info("DCA bot %s | sides=%s | leverage=%dx | capital=$%.0f | resumed deals: %s",
             settings.symbol, settings.sides, settings.leverage, settings.capital_usdt,
             list(engine.state.deals) or "none")

    signal_module.signal(signal_module.SIGINT, _request_stop)
    signal_module.signal(signal_module.SIGTERM, _request_stop)

    window = candles_needed(settings.entry)
    ticks = 0
    while not _stop:
        started = time.time()
        try:
            closes = [c[4] for c in ex.closed_candles(settings.timeframe, window)]
            last = ex.last_price()
            now = datetime.now(timezone.utc)
            engine.tick(now, closes, last, last, last)
            if ticks % 30 == 0:
                open_info = ", ".join(
                    f"{s}: avg {d.avg_price:.3f} SO {d.safety_orders_filled}/{len(d.orders) - 1} "
                    f"uPnL ${d.unrealized_pnl(last):.2f}"
                    for s, d in engine.state.deals.items()
                ) or "no open deals"
                st = engine.state
                log.info("price %.3f | %s | today $%.2f | total $%.2f (W%d/L%d)",
                         last, open_info, engine.pnl_today(now), st.total_pnl, st.wins, st.losses)
        except Exception:
            log.exception("error during tick; retrying next cycle")
        ticks += 1

        remaining = max(0.0, settings.poll_interval_seconds - (time.time() - started))
        while remaining > 0 and not _stop:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    log.info("DCA bot stopped. Open deals stay on Binance (orders keep working) and resume on restart.")


if __name__ == "__main__":
    run()
