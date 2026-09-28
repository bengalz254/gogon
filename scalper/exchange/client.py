"""Minimal, dependency-light REST client for Binance USDⓈ-M futures.

Why hand-rolled instead of a big SDK: every request the bot can make is in
this one file, and the failure handling that matters for real money is
explicit and unit-tested:

* HMAC-SHA256 signing, with the server-time offset tracked so requests are
  not rejected for clock drift (-1021 triggers a resync and one retry);
* 429 / 418 rate-limit responses honour `Retry-After`, and the used-weight
  header throttles the bot before it gets banned;
* read-only requests are retried on network errors and 5xx; ORDER-PLACING
  requests are never blindly retried — a timeout raises OrderStatusUnknown
  so the caller can look the order up by its client id instead of risking a
  duplicate position.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Callable
from urllib.parse import urlencode

import requests

logger = logging.getLogger("scalper.client")

# Binance error codes that mean "the request reached the matching engine but
# we don't know what happened".
_UNKNOWN_STATUS_CODES = {-1006, -1007}
_MISSING_ENDPOINT_CODES = {-5000}


class BinanceAPIError(Exception):
    def __init__(self, status: int, code: int | None, msg: str, path: str = ""):
        self.status = status
        self.code = code
        self.msg = msg
        self.path = path
        super().__init__(f"HTTP {status} code={code} {msg} ({path})")

    @property
    def missing_endpoint(self) -> bool:
        """The endpoint doesn't exist on this server (older/newer API version)."""
        if self.status == 404 or self.code in _MISSING_ENDPOINT_CODES:
            return True
        text = (self.msg or "").lower()
        return self.status in (400, 405) and ("path" in text and ("invalid" in text or "not" in text))


class OrderStatusUnknown(BinanceAPIError):
    """A state-changing request may or may not have been executed."""


class RateLimited(BinanceAPIError):
    def __init__(self, status: int, code: int | None, msg: str, path: str, retry_after: float):
        super().__init__(status, code, msg, path)
        self.retry_after = retry_after


def _fmt_param(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class BinanceFuturesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window: int = 5000,
        timeout: tuple[float, float] = (5.0, 15.0),
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        max_weight_per_minute: int = 2400,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.timeout = timeout
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.max_weight = max_weight_per_minute
        self.max_retries = max_retries
        self.time_offset_ms = 0
        self.used_weight = 0
        self._version_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def now_ms(self) -> int:
        return int(self.clock() * 1000) + self.time_offset_ms

    def sign(self, query: str) -> str:
        if not self.api_secret:
            raise RuntimeError("API secret required for signed endpoints")
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def sync_time(self) -> int:
        t0 = self.clock()
        server = int(self.request("GET", "/fapi/v1/time")["serverTime"])
        t1 = self.clock()
        self.time_offset_ms = int(server - (t0 + t1) / 2 * 1000)
        if abs(self.time_offset_ms) > 1000:
            logger.warning(
                "Local clock is %.1fs off Binance server time; compensating. "
                "Consider enabling NTP time sync on this machine.",
                self.time_offset_ms / 1000,
            )
        return self.time_offset_ms

    def _throttle(self, headers) -> None:
        raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if not raw:
            return
        try:
            self.used_weight = int(raw)
        except ValueError:
            return
        if self.used_weight >= 0.8 * self.max_weight:
            wait = 61 - (self.clock() % 60)
            logger.warning(
                "Request weight %d/%d this minute; pausing %.0fs to stay clear of a ban",
                self.used_weight, self.max_weight, wait,
            )
            self.sleep(wait)

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        signed: bool = False,
        base_url: str | None = None,
    ) -> Any:
        method = method.upper()
        mutating = method != "GET"
        clean = {k: _fmt_param(v) for k, v in (params or {}).items() if v is not None}
        base = (base_url or self.base_url).rstrip("/")
        attempt = 0
        resynced = False

        while True:
            attempt += 1
            query_params = dict(clean)
            if signed:
                query_params["recvWindow"] = str(self.recv_window)
                query_params["timestamp"] = str(self.now_ms())
            qs = urlencode(query_params)
            if signed:
                sig = self.sign(qs)
                qs = f"{qs}&signature={sig}" if qs else f"signature={sig}"
            url = f"{base}{path}" + (f"?{qs}" if qs else "")
            headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}

            try:
                resp = self.session.request(method, url, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                if mutating:
                    raise OrderStatusUnknown(0, None, f"network error: {e}", path) from e
                if attempt >= self.max_retries:
                    raise BinanceAPIError(0, None, f"network error: {e}", path) from e
                self.sleep(min(2 ** attempt, 10))
                continue

            self._throttle(resp.headers)
            status = resp.status_code

            if status in (429, 418):
                retry_after = float(resp.headers.get("Retry-After") or (60 if status == 418 else 5))
                code, msg = self._error_body(resp)
                if status == 418 or attempt >= self.max_retries:
                    raise RateLimited(status, code, msg or "rate limited", path, retry_after)
                logger.warning("Rate limited on %s; waiting %.0fs", path, retry_after)
                self.sleep(retry_after)
                continue

            if status >= 500:
                code, msg = self._error_body(resp)
                if mutating:
                    raise OrderStatusUnknown(status, code, msg or "server error", path)
                if attempt >= self.max_retries:
                    raise BinanceAPIError(status, code, msg or "server error", path)
                self.sleep(min(2 ** attempt, 10))
                continue

            try:
                data = resp.json()
            except ValueError:
                if status >= 400:
                    raise BinanceAPIError(status, None, resp.text[:200], path) from None
                raise BinanceAPIError(status, None, f"non-JSON response: {resp.text[:200]}", path) from None

            code = data.get("code") if isinstance(data, dict) else None
            if isinstance(code, int) and code < 0:
                msg = str(data.get("msg", ""))
                if code == -1021 and not resynced:
                    resynced = True
                    logger.warning("Timestamp outside recvWindow; resyncing clock and retrying")
                    self.sync_time()
                    continue
                if code in _UNKNOWN_STATUS_CODES and mutating:
                    raise OrderStatusUnknown(status, code, msg, path)
                raise BinanceAPIError(status, code, msg, path)
            if status >= 400:
                raise BinanceAPIError(status, code, str(data)[:200], path)
            return data

    @staticmethod
    def _error_body(resp) -> tuple[int | None, str]:
        try:
            d = resp.json()
            if isinstance(d, dict):
                return d.get("code"), str(d.get("msg", ""))
        except ValueError:
            pass
        return None, (resp.text or "")[:200]

    def _versioned(self, key: str, method: str, paths: list[str], params: dict | None, signed: bool):
        """Call the newest endpoint version this server supports (cached after first success)."""
        cached = self._version_cache.get(key)
        ordered = ([cached] if cached else []) + [p for p in paths if p != cached]
        last_err: BinanceAPIError | None = None
        for path in ordered:
            try:
                data = self.request(method, path, params, signed=signed)
                self._version_cache[key] = path
                return data
            except BinanceAPIError as e:
                if not e.missing_endpoint:
                    raise
                last_err = e
        assert last_err is not None
        raise last_err

    # ------------------------------------------------------------------
    # public market data
    # ------------------------------------------------------------------
    def ping(self) -> dict:
        return self.request("GET", "/fapi/v1/ping")

    def server_time(self) -> int:
        return int(self.request("GET", "/fapi/v1/time")["serverTime"])

    def exchange_info(self) -> dict:
        return self.request("GET", "/fapi/v1/exchangeInfo")

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list:
        return self.request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit, "startTime": start_time, "endTime": end_time},
        )

    def book_ticker(self, symbol: str) -> dict:
        return self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    def premium_index(self, symbol: str) -> dict:
        return self.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    # ------------------------------------------------------------------
    # account (signed)
    # ------------------------------------------------------------------
    def balances(self) -> list:
        return self._versioned("balance", "GET", ["/fapi/v3/balance", "/fapi/v2/balance"], None, True)

    def position_risk(self, symbol: str | None = None) -> list:
        return self._versioned(
            "positionRisk", "GET", ["/fapi/v3/positionRisk", "/fapi/v2/positionRisk"],
            {"symbol": symbol}, True,
        )

    def position_mode(self) -> bool:
        """True if hedge mode (dual-side positions) is enabled."""
        data = self.request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition"))

    def set_one_way_mode(self) -> None:
        try:
            self.request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": False}, signed=True)
        except BinanceAPIError as e:
            if e.code != -4059:  # "No need to change position side."
                raise

    def multi_assets_mode(self) -> bool:
        data = self.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)
        return bool(data.get("multiAssetsMargin"))

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceAPIError as e:
            if e.code != -4046:  # "No need to change margin type."
                raise

    def commission_rate(self, symbol: str) -> dict:
        return self.request("GET", "/fapi/v1/commissionRate", {"symbol": symbol}, signed=True)

    def user_trades(self, symbol: str, start_time: int | None = None, limit: int = 500) -> list:
        return self.request(
            "GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_time, "limit": limit}, signed=True
        )

    def income(self, symbol: str, income_type: str, start_time: int | None = None, limit: int = 100) -> list:
        return self.request(
            "GET",
            "/fapi/v1/income",
            {"symbol": symbol, "incomeType": income_type, "startTime": start_time, "limit": limit},
            signed=True,
        )

    def api_restrictions(self, spot_base_url: str = "https://api.binance.com") -> dict:
        """Key permissions (live keys only; the futures testnet has no equivalent)."""
        return self.request("GET", "/sapi/v1/account/apiRestrictions", signed=True, base_url=spot_base_url)

    # ------------------------------------------------------------------
    # orders (signed)
    # ------------------------------------------------------------------
    def new_order(self, **params) -> dict:
        return self.request("POST", "/fapi/v1/order", params, signed=True)

    def get_order(self, symbol: str, order_id: str | None = None, client_id: str | None = None) -> dict:
        return self.request(
            "GET", "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True,
        )

    def cancel_order(self, symbol: str, order_id: str | None = None, client_id: str | None = None) -> dict:
        return self.request(
            "DELETE", "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id, "origClientOrderId": client_id}, signed=True,
        )

    def cancel_all_orders(self, symbol: str) -> dict:
        return self.request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def open_orders(self, symbol: str | None = None) -> list:
        return self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    # Conditional orders (STOP_MARKET / TAKE_PROFIT_MARKET / ...) moved to the
    # Algo Order service; these are its endpoints.
    def new_algo_order(self, **params) -> dict:
        return self.request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def get_algo_order(self, algo_id: str | None = None, client_algo_id: str | None = None) -> dict:
        return self.request(
            "GET", "/fapi/v1/algoOrder", {"algoId": algo_id, "clientAlgoId": client_algo_id}, signed=True
        )

    def cancel_algo_order(self, algo_id: str | None = None, client_algo_id: str | None = None) -> dict:
        return self.request(
            "DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id, "clientAlgoId": client_algo_id}, signed=True
        )

    def cancel_all_algo_orders(self, symbol: str) -> dict:
        return self.request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}, signed=True)

    def open_algo_orders(self, symbol: str | None = None) -> list:
        data = self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
        if isinstance(data, dict):
            for key in ("orders", "rows", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        return data or []
