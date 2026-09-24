"""Entry point for the 5-minute Up/Down bot.

    python -m updown.main              # paper trading against the real market (default)
    LIVE_TRADING=true python -m updown.main   # real orders, real money
    python -m updown.main --simulate   # offline simulator, no network
"""
from __future__ import annotations

import argparse
import logging
import signal as signal_module
import time

from bot.logger import setup_logging
from updown.config import load_updown_settings

_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def run_live(settings, logger) -> None:
    from bot.client import build_client
    from bot.journal import TradeJournal
    from updown.broker import LiveBroker, PaperBroker
    from updown.engine import UpDownEngine
    from updown.feeds import BinanceFeed, PriceHistory
    from updown.markets import PolymarketGateway

    live = settings.wallet.live_trading
    logger.info("Starting 5-minute Up/Down bot | assets=%s | live_trading=%s", settings.assets, live)
    if live:
        logger.warning("*** LIVE TRADING ENABLED *** Real orders with real funds. Ctrl+C to stop.")
        broker = LiveBroker(build_client(settings.wallet))
    else:
        logger.info("PAPER mode: real prices and order books, simulated fills. No orders are sent.")
        broker = PaperBroker()

    history = PriceHistory()
    gateway = PolymarketGateway(settings.gamma_host, settings.wallet.clob_host, settings.slug_template, settings.window_seconds)
    engine = UpDownEngine(settings, gateway, broker, history, TradeJournal(settings.journal_path))

    feed = BinanceFeed(settings.assets, settings.feed, history, on_tick=engine.on_price)
    for asset in settings.assets:
        sigma = feed.seed_sigma(asset)
        if sigma:
            engine.vol[asset].seed(sigma)
            logger.info("[%s] seeded volatility %.2e per sqrt(s) (~%.0f%% annualized)", asset, sigma, sigma * (365 * 86400) ** 0.5 * 100)
    feed.start()

    signal_module.signal(signal_module.SIGINT, _request_stop)
    signal_module.signal(signal_module.SIGTERM, _request_stop)
    logger.info("Waiting for the next window to open (the current one is skipped: its strike wasn't seen).")

    while not _stop:
        started = time.time()
        engine.tick(started)
        time.sleep(max(0.0, settings.tick_seconds - (time.time() - started)))

    feed.stop()
    st = engine.stats
    logger.info(
        "Stopped. %d windows traded (%dW/%dL), P&L $%+.2f, fees $%.2f. Open positions are still on Polymarket and settle on their own.",
        st.windows_traded, st.wins, st.losses, st.pnl_usd, st.fees_usd,
    )


def run_sim(settings, args, logger) -> None:
    from updown.broker import PaperBroker
    from updown.engine import UpDownEngine
    from updown.feeds import PriceHistory
    from updown.sim import SimGateway, SimWorld, run_simulation

    settings.assets = ["btc"]
    world = SimWorld(seed=args.seed, lag_s=args.lag, mm_noise=args.noise)
    history = PriceHistory(maxlen=4000)
    engine = UpDownEngine(settings, SimGateway(world), PaperBroker(), history, journal=None)
    engine.vol["btc"].seed(world.sigma)
    run_simulation(engine, world, history, args.windows)
    st = engine.stats
    print(
        f"\nSIMULATION (lag={args.lag}s, noise={args.noise}, seed={args.seed}, {args.windows} windows)\n"
        f"  windows traded : {st.windows_traded} ({st.wins} won / {st.losses} lost)\n"
        f"  orders         : {st.orders}\n"
        f"  fees paid      : ${st.fees_usd:.2f}\n"
        f"  P&L            : ${st.pnl_usd:+.2f} on a ${settings.sizing.bankroll_usd:.0f} bankroll\n"
        "  (Synthetic market: shows the machinery works, NOT that it profits on Polymarket.)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket 5-minute Up/Down bot")
    parser.add_argument("--config", help="path to updown.yaml (default config/updown.yaml)")
    parser.add_argument("--simulate", action="store_true", help="run the offline simulator instead")
    parser.add_argument("--windows", type=int, default=288, help="simulator: number of 5-minute windows (288 = 1 day)")
    parser.add_argument("--lag", type=float, default=3.0, help="simulator: seconds the fake market maker lags the price")
    parser.add_argument("--noise", type=float, default=0.01, help="simulator: noise in the fake market maker's quotes")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logger = setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    settings = load_updown_settings(args.config)
    if args.simulate:
        if not args.verbose:
            logging.getLogger("polybot.updown").setLevel(logging.WARNING)
        run_sim(settings, args, logger)
    else:
        run_live(settings, logger)


if __name__ == "__main__":
    main()
