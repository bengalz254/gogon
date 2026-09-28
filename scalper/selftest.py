"""Exchange self-test: runs the whole order lifecycle once on the TESTNET.

    python -m scalper selftest --mode testnet [--symbol BTCUSDT]

Waiting for the strategy's first signal can take hours, and the first trade
is the wrong moment to discover that the exchange rejects a stop-loss. This
opens the SMALLEST allowed position and checks, step by step, everything the
live bot relies on:

  1. market entry fills
  2. stop-loss (reduce-only STOP_MARKET) is accepted and visible
  3. reduce-only limit take-profit is accepted and visible
  4. the stop can be moved (new stop first, then the old one cancelled)
  5. the position closes at market
  6. no orders are left behind
  7. the fills can be read back to compute P&L and fees

It takes about half a minute, costs a few cents of (testnet) fees, and always
cleans up after itself — even when a step fails.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from scalper.exchange.broker import new_client_id
from scalper.models import LONG, snap
from scalper.trade import Trade


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


def run_selftest(
    broker,
    market,
    rules: dict,
    symbol: str,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Step]:
    steps: list[Step] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        steps.append(Step(name, ok, detail))
        log(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        return ok

    if symbol in broker.positions() or broker.open_orders(symbol):
        record("clean start", False,
               f"{symbol} already has a position or open orders; close them first or use --symbol")
        return steps
    record("clean start", True, symbol)

    rule = rules[symbol]
    bid, ask = market.quote(symbol)
    smallest = max(float(rule.market_min_qty), float(rule.min_notional) * 1.1 / ask)
    qty = snap(smallest, rule.market_step_size, "ceil")

    try:
        opened_at = market.now_ms()
        fill = broker.market_order(symbol, "BUY", qty, reduce_only=False, client_id=new_client_id("en"))
        ok = fill.qty > 0 and fill.avg_price > 0
        detail = (f"bought {fill.qty:g} @ {fill.avg_price:.6g}" if ok
                  else "not filled" if fill.qty <= 0 else "filled, but the fill price could not be determined")
        if not record("market entry", ok, detail):
            return steps

        size = Decimal(str(fill.qty))
        sl = broker.place_stop(symbol, LONG, fill.avg_price * 0.97, size, new_client_id("sl"))
        api = "Algo Order API" if sl.kind == "algo" else "classic order endpoint"
        if not record("stop-loss accepted", any(o.purpose == "sl" for o in broker.open_orders(symbol)),
                      f"via {api}, trigger {sl.price:.6g}"):
            return steps

        tp = broker.place_take_profit(symbol, LONG, fill.avg_price * 1.03, qty, new_client_id("tp"))
        record("take-profit accepted", any(o.purpose == "tp" for o in broker.open_orders(symbol)),
               f"{tp.order_type} reduce-only @ {tp.price:.6g}")

        moved = broker.place_stop(symbol, LONG, fill.avg_price * 0.98, size, new_client_id("sl"))
        broker.cancel(symbol, sl)
        orders = broker.open_orders(symbol)
        stops = [o for o in orders if o.purpose == "sl"]
        tp_note = ("take-profit still open" if any(o.purpose == "tp" for o in orders)
                   else "exchange removed the take-profit (the bot re-places it automatically)")
        record("stop moved", len(stops) == 1 and abs(stops[0].price - moved.price) < 1e-9,
               f"{len(stops)} stop(s) open, trigger {stops[0].price:.6g}; {tp_note}" if stops else "no stop left")

        position = broker.positions().get(symbol)
        closed = broker.close_position(symbol, position) if position else None
        record("market close", symbol not in broker.positions(),
               f"sold {closed.qty:g} @ {closed.avg_price:.6g}" if closed else "position missing")

        leftover = broker.cancel_all(symbol)
        record("orders cleaned up", not leftover, f"{len(leftover)} still open" if leftover else "")

        probe = Trade("selftest", symbol, LONG, "selftest", fill.qty, fill.avg_price, moved.price,
                      moved.price, tp.price, opened_at=opened_at, entry_order_id=fill.order_id)
        info = None
        for _ in range(6):
            info = broker.closed_trade_info(symbol, probe)
            if info is not None:
                break
            sleep(1.0)
        record("fills and P&L readable", info is not None,
               f"net {info.gross_pnl - info.fees - info.funding:+.4f} incl. fees {info.fees:.4f}"
               if info else "closing fills not visible")
    except Exception as e:  # noqa: BLE001 - report every failure, then clean up
        record("unexpected error", False, f"{type(e).__name__}: {e}")
    finally:
        try:
            position = broker.positions().get(symbol)
            if position is not None:
                broker.close_position(symbol, position)
            broker.cancel_all(symbol)
        except Exception as e:  # noqa: BLE001
            record("final cleanup", False, f"{e} - check the testnet UI and close manually")
    return steps
