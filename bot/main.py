"""Entry point: python -m bot.main"""
from __future__ import annotations

import json
import logging
import os
import signal as signal_module
import time
from datetime import datetime, timezone

from bot.client import build_client
from bot.config import load_settings
from bot.execution import OrderExecutor
from bot.fees import FeeModel
from bot.journal import TradeJournal
from bot.logger import setup_logging
from bot.market_data import BookLevel, best_levels, iter_active_markets
from bot.notify import Notifier
from bot.reconcile import reconcile
from bot.risk import RiskManager
from bot.settlement import settle_resolved_markets
from bot.state import StateStore, state_path
from bot.strategies import ArbitrageStrategy, ThresholdStrategy

logger = logging.getLogger("polybot.main")

# Small JSON heartbeat, rewritten every cycle (read by the Docker healthcheck).
STATUS_PATH = "data/status.json"
# How often held markets are checked for resolution (and settled).
SETTLE_INTERVAL_SECONDS = 600
# How often, in live mode, positions are re-checked against Polymarket.
RECONCILE_INTERVAL_SECONDS = 3600
# The same recurring problem alerts at most this often.
ERROR_ALERT_COOLDOWN_SECONDS = 900
RECONCILE_ALERT_COOLDOWN_SECONDS = 6 * 3600
# Alert when this many cycles in a row found no markets at all (API down?).
EMPTY_SCAN_ALERT_CYCLES = 3

_stop = False


def _request_stop(signum, frame):
    global _stop
    _stop = True


def _usd(amount: float) -> str:
    return f"{'-' if amount < 0 else '+'}${abs(amount):.2f}"


def _pnl_summary(risk: RiskManager) -> str:
    return (
        f"posisi {len(risk.positions)} | eksposur ${risk.total_exposure_usd:.2f} | "
        f"P&L hari ini {_usd(risk.daily_pnl)} (realized {_usd(risk.realized_pnl_today)}, "
        f"unrealized {_usd(risk.unrealized_pnl)})"
    )


def _marks_for_positions(risk: RiskManager, get_book) -> dict[str, float]:
    """Best bid for every held token — what each position could be sold for now."""
    marks = {}
    for token_id in list(risk.positions):
        bid = get_book(token_id).best_bid
        if bid is not None:
            marks[token_id] = bid
    return marks


def _guarded(notifier: Notifier, step: str, fn, *args) -> None:
    """Run a housekeeping step; on failure log and alert instead of stopping the bot."""
    try:
        fn(*args)
    except Exception as exc:
        logger.exception("%s failed; continuing", step)
        notifier.send(
            f"⚠️ Langkah '{step}' gagal ({type(exc).__name__}) — bot tetap jalan, cek logs/bot.log",
            key=f"step_error:{step}",
            cooldown_s=ERROR_ALERT_COOLDOWN_SECONDS,
        )


def _settle_resolved(
    client, risk: RiskManager, journal: TradeJournal, store: StateStore, notifier: Notifier, mode: str
) -> None:
    settled = settle_resolved_markets(client, risk, journal, mode)
    if not settled:
        return
    try:
        store.save(risk.snapshot())
    except Exception:
        logger.exception("Failed to persist risk state after settlement")
    redeem_note = "\nKlaim (redeem) USDC-nya di Polymarket." if mode == "live" else ""
    for market_id, pnl in settled:
        notifier.send(
            f"🏁 [{mode.upper()}] Market {market_id[:12]} sudah selesai — posisi ditutup, P&L {_usd(pnl)}{redeem_note}"
        )


def _reconcile_positions(address: str, risk: RiskManager, notifier: Notifier) -> None:
    local = {token_id: pos.size for token_id, pos in risk.positions.items()}
    mismatches = reconcile(address, local)
    if mismatches is None:
        notifier.send(
            "⚠️ Tidak bisa mengecek posisi ke Polymarket (Data API) — cek logs/bot.log",
            key="reconcile_failed",
            cooldown_s=RECONCILE_ALERT_COOLDOWN_SECONDS,
        )
    elif mismatches:
        logger.warning("Positions differ from Polymarket (%d): %s", len(mismatches), "; ".join(mismatches))
        shown = mismatches[:10]
        more = f"\n…dan {len(mismatches) - len(shown)} lainnya" if len(mismatches) > len(shown) else ""
        notifier.send(
            "⚠️ Posisi yang dicatat bot beda dengan Polymarket:\n" + "\n".join(shown) + more,
            key="reconcile_mismatch",
            cooldown_s=RECONCILE_ALERT_COOLDOWN_SECONDS,
        )
    else:
        logger.info("Positions match Polymarket (%d tokens checked).", len(local))


def _write_status(
    mode: str, risk: RiskManager, cycles: int, last_cycle_seconds: float, markets_scanned: int
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_ts": time.time(),
        "mode": mode,
        "cycles": cycles,
        "last_cycle_seconds": round(last_cycle_seconds, 2),
        "markets_scanned": markets_scanned,
        "open_positions": len(risk.positions),
        "exposure_usd": round(risk.total_exposure_usd, 4),
        "realized_pnl_today_usd": round(risk.realized_pnl_today, 4),
        "unrealized_pnl_usd": round(risk.unrealized_pnl, 4),
        "kill_switch": risk.daily_loss_limit_hit,
    }
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    tmp_path = STATUS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, STATUS_PATH)


def run() -> None:
    settings = load_settings()
    setup_logging()
    live = settings.wallet.live_trading
    mode = "live" if live else "paper"

    logger.info("Starting Polymarket bot | live_trading=%s", live)
    if live:
        logger.warning(
            "*** LIVE TRADING ENABLED *** Real orders will be placed with real funds. "
            "Ctrl+C to stop between cycles."
        )
    else:
        logger.info("Running in PAPER TRADING mode — no real orders will be sent.")

    notifier = Notifier(settings.notifications.telegram_bot_token, settings.notifications.telegram_chat_id)
    if notifier.enabled:
        logger.info("Telegram notifications enabled.")

    client = build_client(settings.wallet)
    risk = RiskManager(settings.risk)
    store = StateStore(state_path(live))
    saved = store.load()
    if saved is not None:
        risk.restore(saved)
        logger.info(
            "Restored state from %s: %d open positions | exposure=$%.2f | realized_pnl_today=$%.2f",
            store.path,
            len(risk.positions),
            risk.total_exposure_usd,
            risk.realized_pnl_today,
        )
    fees = FeeModel(settings.fees)
    journal = TradeJournal()
    executor = OrderExecutor(
        client,
        risk,
        journal,
        live=live,
        store=store,
        notifier=notifier,
        notify_fills=settings.notifications.fills,
    )

    strategies = []
    if settings.arbitrage.enabled:
        strategies.append(ArbitrageStrategy(settings.arbitrage, risk, fees))
    if settings.threshold.enabled:
        strategies.append(ThresholdStrategy(settings.threshold, risk, fees))
    if not strategies:
        logger.warning("No strategies enabled in config/settings.yaml — bot will idle.")

    signal_module.signal(signal_module.SIGINT, _request_stop)
    signal_module.signal(signal_module.SIGTERM, _request_stop)

    notifier.send(f"🟢 Bot mulai ({mode.upper()}) — {_pnl_summary(risk)}")

    # Before trading, settle markets that resolved while the bot was down and
    # check positions against the exchange; both then repeat periodically.
    last_settle = last_reconcile = time.time()
    _guarded(notifier, "settlement", _settle_resolved, client, risk, journal, store, notifier, mode)
    if live:
        _guarded(notifier, "reconcile", _reconcile_positions, settings.wallet.funder_address, risk, notifier)

    heartbeat_seconds = settings.notifications.heartbeat_hours * 3600
    last_heartbeat = time.time()
    kill_switch_on = False
    cycles = 0
    empty_scans = 0

    while not _stop:
        cycle_start = time.time()
        book_cache: dict[str, BookLevel] = {}

        def get_book(token_id: str) -> BookLevel:
            if token_id not in book_cache:
                try:
                    raw_book = client.get_order_book(token_id)
                    book_cache[token_id] = best_levels(raw_book)
                except Exception:
                    logger.exception("Failed to fetch order book for token %s", token_id)
                    book_cache[token_id] = BookLevel(None, None, 0.0, 0.0)
            return book_cache[token_id]

        market_count = 0
        try:
            for market in iter_active_markets(client, settings.markets):
                if _stop:
                    break
                market_count += 1
                for strategy in strategies:
                    executor.execute_signals(strategy.generate_signals(market, get_book))
            risk.update_marks(_marks_for_positions(risk, get_book))
            logger.info(
                "Cycle complete: scanned %d markets | open_positions=%d | "
                "exposure=$%.2f | realized_pnl_today=$%.2f | unrealized_pnl=$%.2f",
                market_count,
                len(risk.positions),
                risk.total_exposure_usd,
                risk.realized_pnl_today,
                risk.unrealized_pnl,
            )
        except Exception as exc:
            logger.exception("Unhandled error during scan cycle; continuing")
            notifier.send(
                f"⚠️ Error di siklus scan ({type(exc).__name__}) — bot tetap jalan, cek logs/bot.log",
                key="cycle_error",
                cooldown_s=ERROR_ALERT_COOLDOWN_SECONDS,
            )

        empty_scans = empty_scans + 1 if market_count == 0 and not _stop else 0
        if empty_scans >= EMPTY_SCAN_ALERT_CYCLES:
            notifier.send(
                f"⚠️ {empty_scans} siklus berturut-turut tidak ada market yang bisa dipindai — "
                "API Polymarket tidak bisa diakses, atau filter `markets:` terlalu ketat. Cek logs/bot.log",
                key="empty_scan",
                cooldown_s=3600,
            )

        if risk.daily_loss_limit_hit:
            logger.warning(
                "Daily loss limit hit (realized_pnl_today=$%.2f, unrealized_pnl=$%.2f) — "
                "no new positions until P&L recovers or UTC midnight.",
                risk.realized_pnl_today,
                risk.unrealized_pnl,
            )
            if not kill_switch_on:
                notifier.send(f"⛔ Batas rugi harian tercapai — bot berhenti membuka posisi baru.\n{_pnl_summary(risk)}")
            kill_switch_on = True
        elif kill_switch_on:
            kill_switch_on = False
            notifier.send(f"✅ Batas rugi harian tidak aktif lagi — bot boleh membuka posisi.\n{_pnl_summary(risk)}")

        if time.time() - last_settle >= SETTLE_INTERVAL_SECONDS:
            last_settle = time.time()
            _guarded(notifier, "settlement", _settle_resolved, client, risk, journal, store, notifier, mode)

        if live and time.time() - last_reconcile >= RECONCILE_INTERVAL_SECONDS:
            last_reconcile = time.time()
            _guarded(notifier, "reconcile", _reconcile_positions, settings.wallet.funder_address, risk, notifier)

        if heartbeat_seconds > 0 and time.time() - last_heartbeat >= heartbeat_seconds:
            last_heartbeat = time.time()
            notifier.send(f"💓 Bot masih jalan ({mode.upper()}) — {_pnl_summary(risk)}")

        cycles += 1
        elapsed = time.time() - cycle_start
        try:
            _write_status(mode, risk, cycles, elapsed, market_count)
        except OSError:
            logger.exception("Failed to write %s", STATUS_PATH)

        remaining = max(0.0, settings.polling_interval_seconds - elapsed)
        # Sleep in small increments so Ctrl+C is responsive.
        while remaining > 0 and not _stop:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    logger.info("Bot stopped.")
    notifier.send(f"🔴 Bot berhenti ({mode.upper()}) — {_pnl_summary(risk)}")
    notifier.close()
    store.close()


if __name__ == "__main__":
    run()
