"""Live / testnet order execution on Binance USDⓈ-M futures.

Protective orders live ON THE EXCHANGE, not in the bot's memory: the moment
an entry fills, a STOP_MARKET `closePosition` stop-loss is placed, so the
position stays protected even if the bot crashes, loses internet or the
machine reboots.

Broker interface (shared with PaperBroker):
    prepare(symbols) -> list[str]
    balances() -> (wallet_balance, available_balance)
    positions() -> {symbol: PositionInfo}
    market_order(symbol, side, qty, reduce_only, client_id) -> Fill
    place_stop(symbol, trade_side, stop_price, client_id) -> OrderRef
    place_take_profit(symbol, trade_side, price, qty, client_id) -> OrderRef
    cancel(symbol, ref) / cancel_all(symbol) / open_orders(symbol)
    close_position(symbol, position) -> Fill
    closed_trade_info(symbol, trade, final) -> ClosedTradeInfo | None
    on_candle(symbol, candle)   # paper fills; no-op live
"""
from __future__ import annotations

import logging
import time
import uuid
from decimal import Decimal
from typing import Callable

from scalper.config import Settings
from scalper.exchange.client import BinanceAPIError, BinanceFuturesClient, OrderStatusUnknown
from scalper.models import (
    ClosedTradeInfo,
    Fill,
    OrderRef,
    PositionInfo,
    SymbolRules,
    close_side,
    fmt_decimal,
)

logger = logging.getLogger("scalper.broker")

FINAL_STATUSES = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
_GONE_CODES = {-2011, -2013}
_STABLES = {"USDT", "USDC", "FDUSD", "BUSD"}


class BrokerError(Exception):
    pass


def new_client_id(purpose: str) -> str:
    """Client order id: 'sc' + purpose + random hex (Binance max is 36 chars)."""
    return f"sc{purpose}{uuid.uuid4().hex[:20]}"


def purpose_of(client_id: str, order_type: str = "", reduce_only: bool = False) -> str:
    for prefix, purpose in (("scsl", "sl"), ("sctp", "tp"), ("scen", "entry"), ("scex", "exit")):
        if client_id.startswith(prefix):
            return purpose
    t = (order_type or "").upper()
    if t in ("STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"):
        return "sl"
    if t.startswith("TAKE_PROFIT") or (t == "LIMIT" and reduce_only):
        return "tp"
    return ""


def is_bot_order(ref: OrderRef) -> bool:
    return ref.client_id.startswith("sc")


class BinanceBroker:
    kind = "binance"

    def __init__(
        self,
        client: BinanceFuturesClient,
        settings: Settings,
        rules: dict[str, SymbolRules],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.settings = settings
        self.exec = settings.execution
        self.rules = rules
        self.sleep = sleep
        self._cond_api = self.exec.conditional_order_api  # auto | algo | legacy
        self.fee_rates: dict[str, tuple[float, float]] = {}  # symbol -> (maker, taker)
        self._asset_px: dict[str, tuple[float, float]] = {}  # asset -> (price, fetched_at)

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def prepare(self, symbols: list[str]) -> list[str]:
        warnings: list[str] = []
        if self.client.position_mode():
            if self.exec.auto_one_way_mode:
                self.client.set_one_way_mode()
                warnings.append("Account switched from Hedge Mode to One-way Mode.")
            else:
                raise BrokerError(
                    "Your futures account is in Hedge Mode. This bot needs One-way Mode: "
                    "Binance Futures → ⚙ Preferences → Position Mode → One-way "
                    "(only possible with no open positions/orders), or set "
                    "execution.auto_one_way_mode: true."
                )
        try:
            if self.client.multi_assets_mode() and self.exec.margin_type == "ISOLATED":
                warnings.append(
                    "Multi-Assets Mode is on, which only supports CROSSED margin; "
                    "isolated margin cannot be set."
                )
        except BinanceAPIError:
            pass
        for sym in symbols:
            try:
                self.client.set_margin_type(sym, self.exec.margin_type)
            except BinanceAPIError as e:
                warnings.append(f"{sym}: margin type not changed to {self.exec.margin_type} ({e.msg})")
            try:
                self.client.set_leverage(sym, self.exec.leverage)
            except BinanceAPIError as e:
                raise BrokerError(f"{sym}: could not set leverage {self.exec.leverage}x: {e.msg}") from e
            if self.settings.costs.use_exchange_fee_rates:
                try:
                    r = self.client.commission_rate(sym)
                    self.fee_rates[sym] = (float(r["makerCommissionRate"]), float(r["takerCommissionRate"]))
                except (BinanceAPIError, KeyError, ValueError) as e:
                    warnings.append(f"{sym}: could not read fee rates, using config values ({e})")
        return warnings

    # ------------------------------------------------------------------
    # account state
    # ------------------------------------------------------------------
    def _margin_asset(self) -> str:
        for r in self.rules.values():
            return r.margin_asset
        return "USDT"

    def balances(self) -> tuple[float, float]:
        asset = self._margin_asset()
        for b in self.client.balances():
            if b.get("asset") == asset:
                wallet = float(b.get("balance") or b.get("walletBalance") or 0.0)
                available = float(b.get("availableBalance") or b.get("maxWithdrawAmount") or 0.0)
                return wallet, available
        return 0.0, 0.0

    def positions(self) -> dict[str, PositionInfo]:
        out: dict[str, PositionInfo] = {}
        for p in self.client.position_risk():
            sym = p.get("symbol")
            if sym not in self.rules:
                continue
            amt = float(p.get("positionAmt") or 0.0)
            if amt == 0:
                continue
            out[sym] = PositionInfo(
                symbol=sym,
                qty=amt,
                entry_price=float(p.get("entryPrice") or 0.0),
                mark_price=float(p.get("markPrice") or 0.0),
                unrealized_pnl=float(p.get("unRealizedProfit") or p.get("unrealizedProfit") or 0.0),
                liquidation_price=float(p.get("liquidationPrice") or 0.0),
            )
        return out

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def _resolve_unknown(self, symbol: str, client_id: str) -> dict | None:
        """After a timeout: look the order up by client id before doing anything else."""
        for attempt in range(4):
            self.sleep(1.0 + attempt)
            try:
                return self.client.get_order(symbol, client_id=client_id)
            except BinanceAPIError as e:
                if e.code == -2013 or e.status == 0 or e.status >= 500:
                    continue
                raise
        return None

    def _wait_final(self, symbol: str, order: dict, timeout_s: float = 5.0) -> dict:
        waited = 0.0
        while order.get("status") not in FINAL_STATUSES and waited < timeout_s:
            self.sleep(0.5)
            waited += 0.5
            try:
                order = self.client.get_order(symbol, order_id=str(order.get("orderId")))
            except BinanceAPIError:
                break
        return order

    @staticmethod
    def _fill_from(order: dict) -> Fill:
        qty = float(order.get("executedQty") or 0.0)
        avg = float(order.get("avgPrice") or 0.0)
        if avg <= 0 and qty > 0:
            avg = float(order.get("cumQuote") or 0.0) / qty
        return Fill(
            qty=qty,
            avg_price=avg,
            order_id=str(order.get("orderId", "")),
            client_id=str(order.get("clientOrderId", "")),
        )

    def market_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        reduce_only: bool = False,
        client_id: str | None = None,
    ) -> Fill:
        cid = client_id or new_client_id("ex" if reduce_only else "en")
        params = dict(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=fmt_decimal(qty),
            newClientOrderId=cid,
            newOrderRespType="RESULT",
        )
        if reduce_only:
            params["reduceOnly"] = "true"
        try:
            order = self.client.new_order(**params)
        except OrderStatusUnknown as e:
            logger.warning("%s market order %s status unknown (%s); looking it up", symbol, cid, e)
            order = self._resolve_unknown(symbol, cid)
            if order is None:
                logger.warning("%s order %s not found after timeout; treating as not placed", symbol, cid)
                return Fill(qty=0.0, avg_price=0.0, client_id=cid)
        order = self._wait_final(symbol, order)
        fill = self._fill_from(order)
        fill.client_id = fill.client_id or cid
        return fill

    def _find_open(self, symbol: str, client_id: str) -> OrderRef | None:
        try:
            for ref in self.open_orders(symbol):
                if ref.client_id == client_id:
                    return ref
        except BinanceAPIError:
            pass
        return None

    def _place_conditional(
        self, symbol: str, side: str, order_type: str, trigger: Decimal, cid: str, purpose: str
    ) -> OrderRef:
        wt = self.exec.stop_working_type

        def via_algo() -> OrderRef:
            resp = self.client.new_algo_order(
                algoType="CONDITIONAL",
                symbol=symbol,
                side=side,
                type=order_type,
                triggerPrice=fmt_decimal(trigger),
                closePosition="true",
                workingType=wt,
                clientAlgoId=cid,
            )
            return OrderRef("algo", str(resp.get("algoId", "")), str(resp.get("clientAlgoId") or cid),
                            purpose, order_type, float(trigger))

        def via_legacy() -> OrderRef:
            resp = self.client.new_order(
                symbol=symbol,
                side=side,
                type=order_type,
                stopPrice=fmt_decimal(trigger),
                closePosition="true",
                workingType=wt,
                newClientOrderId=cid,
            )
            return OrderRef("regular", str(resp.get("orderId", "")), str(resp.get("clientOrderId") or cid),
                            purpose, order_type, float(trigger))

        try:
            if self._cond_api in ("auto", "algo"):
                try:
                    return via_algo()
                except BinanceAPIError as e:
                    if self._cond_api == "auto" and e.missing_endpoint and not isinstance(e, OrderStatusUnknown):
                        logger.info("Algo order API not available on this server; using classic conditional orders")
                        self._cond_api = "legacy"
                        return via_legacy()
                    raise
            try:
                return via_legacy()
            except BinanceAPIError as e:
                if e.code == -4120 and self.exec.conditional_order_api == "auto":
                    logger.info("Server requires the Algo order API for conditional orders; switching")
                    self._cond_api = "algo"
                    return via_algo()
                raise
        except OrderStatusUnknown:
            found = self._find_open(symbol, cid)
            if found is not None:
                return found
            raise

    def place_stop(self, symbol: str, trade_side: str, stop_price: float, client_id: str | None = None) -> OrderRef:
        rules = self.rules[symbol]
        trigger = rules.stop_price(stop_price, trade_side)
        return self._place_conditional(
            symbol, close_side(trade_side), "STOP_MARKET", trigger, client_id or new_client_id("sl"), "sl"
        )

    def place_take_profit(
        self, symbol: str, trade_side: str, price: float, qty: Decimal, client_id: str | None = None
    ) -> OrderRef:
        rules = self.rules[symbol]
        tp = rules.target_price(price, trade_side)
        cid = client_id or new_client_id("tp")
        side = close_side(trade_side)
        if self.exec.take_profit_order != "limit":
            return self._place_conditional(symbol, side, "TAKE_PROFIT_MARKET", tp, cid, "tp")
        limit_qty = rules.qty(float(qty), market=False)
        try:
            resp = self.client.new_order(
                symbol=symbol,
                side=side,
                type="LIMIT",
                timeInForce="GTC",
                quantity=fmt_decimal(limit_qty),
                price=fmt_decimal(tp),
                reduceOnly="true",
                newClientOrderId=cid,
            )
        except OrderStatusUnknown:
            found = self._find_open(symbol, cid)
            if found is not None:
                return found
            raise
        return OrderRef("regular", str(resp.get("orderId", "")), str(resp.get("clientOrderId") or cid),
                        "tp", "LIMIT", float(tp))

    def cancel(self, symbol: str, ref: OrderRef) -> None:
        try:
            if ref.kind == "algo":
                self.client.cancel_algo_order(
                    algo_id=ref.order_id or None, client_algo_id=None if ref.order_id else ref.client_id
                )
            else:
                self.client.cancel_order(
                    symbol, order_id=ref.order_id or None, client_id=None if ref.order_id else ref.client_id
                )
        except BinanceAPIError as e:
            text = (e.msg or "").lower()
            if e.code in _GONE_CODES or "not exist" in text or "unknown order" in text or "not found" in text:
                return  # already filled / cancelled
            raise

    def cancel_all(self, symbol: str) -> list[OrderRef]:
        """Cancel every open order on `symbol` (regular + algo). Returns what is STILL open."""
        try:
            self.client.cancel_all_orders(symbol)
        except BinanceAPIError as e:
            logger.warning("%s cancel all regular orders failed: %s", symbol, e)
        if self._cond_api != "legacy":
            try:
                self.client.cancel_all_algo_orders(symbol)
            except BinanceAPIError as e:
                if e.missing_endpoint and self._cond_api == "auto":
                    self._cond_api = "legacy"
                elif not e.missing_endpoint:
                    logger.debug("%s cancel all algo orders: %s", symbol, e)
        remaining = self.open_orders(symbol)
        for ref in remaining:
            try:
                self.cancel(symbol, ref)
            except BinanceAPIError as e:
                logger.warning("%s could not cancel %s: %s", symbol, ref.client_id or ref.order_id, e)
        return self.open_orders(symbol) if remaining else []

    def open_orders(self, symbol: str) -> list[OrderRef]:
        refs: list[OrderRef] = []
        for o in self.client.open_orders(symbol):
            cid = str(o.get("clientOrderId", ""))
            otype = str(o.get("type", ""))
            reduce_only = str(o.get("reduceOnly", "")).lower() == "true" or o.get("closePosition") in (True, "true")
            price = float(o.get("stopPrice") or 0.0) or float(o.get("price") or 0.0)
            refs.append(OrderRef("regular", str(o.get("orderId", "")), cid, purpose_of(cid, otype, reduce_only), otype, price))
        if self._cond_api != "legacy":
            try:
                for a in self.client.open_algo_orders(symbol):
                    if a.get("symbol") not in (None, symbol):
                        continue
                    cid = str(a.get("clientAlgoId", ""))
                    otype = str(a.get("orderType") or a.get("type") or "")
                    refs.append(
                        OrderRef("algo", str(a.get("algoId", "")), cid, purpose_of(cid, otype, True), otype,
                                 float(a.get("triggerPrice") or 0.0))
                    )
            except BinanceAPIError as e:
                if not e.missing_endpoint:
                    raise
                if self._cond_api == "auto":
                    self._cond_api = "legacy"
        return refs

    def close_position(self, symbol: str, pos: PositionInfo) -> Fill:
        rules = self.rules[symbol]
        side = "SELL" if pos.qty > 0 else "BUY"
        remaining = abs(pos.qty)
        total_qty = 0.0
        notional = 0.0
        for _ in range(5):  # a position bigger than the market max qty needs several orders
            qty = rules.qty(remaining, market=True)
            if qty <= 0:
                break
            fill = self.market_order(symbol, side, qty, reduce_only=True)
            if fill.qty <= 0:
                break
            total_qty += fill.qty
            notional += fill.qty * fill.avg_price
            remaining -= fill.qty
            if remaining <= float(rules.market_step_size) / 2:
                break
        return Fill(qty=total_qty, avg_price=notional / total_qty if total_qty else 0.0)

    # ------------------------------------------------------------------
    # post-trade accounting
    # ------------------------------------------------------------------
    def _asset_price(self, asset: str) -> float:
        cached = self._asset_px.get(asset)
        now = time.time()
        if cached and now - cached[1] < 600:
            return cached[0]
        try:
            px = float(self.client.premium_index(f"{asset}USDT")["markPrice"])
        except (BinanceAPIError, KeyError, ValueError):
            return 0.0
        self._asset_px[asset] = (px, now)
        return px

    def _fee_in_margin(self, fill: dict, margin_asset: str) -> float:
        amt = float(fill.get("commission") or 0.0)
        asset = str(fill.get("commissionAsset") or margin_asset)
        if asset == margin_asset or (asset in _STABLES and margin_asset in _STABLES):
            return amt
        return amt * self._asset_price(asset)

    def closed_trade_info(self, symbol: str, trade, final: bool = False) -> ClosedTradeInfo | None:
        """Exit price, P&L and fees of a closed trade, from the account's own fills.

        Returns None if the closing fills are not visible yet (the caller
        retries); with `final=True` it settles for whatever is available.
        """
        fills = self.client.user_trades(symbol, start_time=trade.opened_at - 5_000, limit=1000)
        margin = self.rules[symbol].margin_asset
        exit_side = close_side(trade.side)
        entry_oid = getattr(trade, "entry_order_id", "") or ""
        entry_fills = [f for f in fills if entry_oid and str(f.get("orderId")) == entry_oid]
        exit_fills = [
            f for f in fills
            if f.get("side") == exit_side
            and int(f.get("time") or 0) >= trade.opened_at - 1_000
            and str(f.get("orderId")) != entry_oid
        ]
        exit_qty = sum(float(f.get("qty") or 0.0) for f in exit_fills)
        if exit_qty <= 0 or (exit_qty < trade.qty * 0.999 and not final):
            return None
        exit_price = sum(float(f["price"]) * float(f["qty"]) for f in exit_fills) / exit_qty
        gross = sum(float(f.get("realizedPnl") or 0.0) for f in exit_fills)
        fees = sum(self._fee_in_margin(f, margin) for f in entry_fills + exit_fills)
        if not entry_fills:
            fees += trade.entry_fee
        funding = 0.0
        try:
            incomes = self.client.income(symbol, "FUNDING_FEE", start_time=trade.opened_at)
            funding = -sum(float(i.get("income") or 0.0) for i in incomes)
        except BinanceAPIError:
            pass
        return ClosedTradeInfo(
            exit_price=exit_price,
            qty=exit_qty,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            exit_time=max(int(f.get("time") or 0) for f in exit_fills),
            approximate=exit_qty < trade.qty * 0.999,
        )

    def on_candle(self, symbol: str, candle) -> None:
        """Live mode: stops and targets are handled by the exchange itself."""
        return None
