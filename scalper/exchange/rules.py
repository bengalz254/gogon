"""Parse /fapi/v1/exchangeInfo into per-symbol trading rules."""
from __future__ import annotations

from decimal import Decimal

from scalper.models import SymbolRules


def _d(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def parse_symbol_rules(exchange_info: dict, symbols: list[str]) -> dict[str, SymbolRules]:
    """Return rules for each requested symbol.

    Raises ValueError for symbols that don't exist, aren't perpetual
    contracts, or aren't currently trading — better to refuse to start than
    to discover it on the first order.
    """
    by_symbol = {s.get("symbol"): s for s in exchange_info.get("symbols", [])}
    out: dict[str, SymbolRules] = {}
    problems: list[str] = []
    for sym in symbols:
        info = by_symbol.get(sym)
        if info is None:
            problems.append(f"{sym}: not listed on Binance USDⓈ-M futures")
            continue
        if info.get("contractType", "PERPETUAL") != "PERPETUAL":
            problems.append(f"{sym}: contract type {info.get('contractType')} is not PERPETUAL")
            continue
        if info.get("status", "TRADING") != "TRADING":
            problems.append(f"{sym}: status is {info.get('status')}, not TRADING")
            continue
        filters = {f.get("filterType"): f for f in info.get("filters", [])}
        price_f = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        mlot = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("MIN_NOTIONAL", {})
        out[sym] = SymbolRules(
            symbol=sym,
            tick_size=_d(price_f.get("tickSize"), "0.01"),
            step_size=_d(lot.get("stepSize"), "0.001"),
            min_qty=_d(lot.get("minQty")),
            max_qty=_d(lot.get("maxQty")),
            market_step_size=_d(mlot.get("stepSize") or lot.get("stepSize"), "0.001"),
            market_min_qty=_d(mlot.get("minQty") or lot.get("minQty")),
            market_max_qty=_d(mlot.get("maxQty") or lot.get("maxQty")),
            min_notional=_d(notional.get("notional", notional.get("minNotional", "0"))),
            margin_asset=str(info.get("marginAsset") or info.get("quoteAsset") or "USDT"),
        )
    if problems:
        raise ValueError("Symbol problems:\n  - " + "\n  - ".join(problems))
    return out
