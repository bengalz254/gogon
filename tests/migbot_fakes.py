"""Fake data sources and a controllable clock for migbot tests (no network)."""
from __future__ import annotations

import os
from types import SimpleNamespace

from migbot.config import load_settings
from migbot.http import Health
from migbot.models import GmgnInfo, MarketSnapshot, MigrationEvent, Quote, RugcheckReport, SafetyReport

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_790_000_000.0  # 2026-09-22, a fixed start time

MINT_A = "A1" + "1" * 38 + "pump"
MINT_B = "B2" + "2" * 38 + "pump"
MINT_C = "C3" + "3" * 38 + "pump"


def settings(tmp_path, **overrides):
    s = load_settings(os.path.join(ROOT, "config", "migbot.yaml"), env_path=None)
    s.data_dir = str(tmp_path / "data")
    s.log_dir = str(tmp_path / "logs")
    s.telegram_token = s.telegram_chat_id = ""
    for dotted, value in overrides.items():
        obj = s
        parts = dotted.split("__")
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
    return s


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _http(name: str):
    return SimpleNamespace(health=Health(name))


class FakeFeed:
    def __init__(self):
        self.pending: list[MigrationEvent] = []
        self.connected = True
        self.http = _http("PumpPortal")

    def push(self, mint: str, migrated_at: float, received_at: float | None = None, symbol: str = "") -> None:
        self.pending.append(
            MigrationEvent(mint=mint, source="pumpportal", migrated_at=migrated_at, received_at=received_at or migrated_at, symbol=symbol)
        )

    def drain(self):
        out, self.pending = self.pending, []
        return out

    def snapshot(self):
        return {"name": "PumpPortal", "connected": self.connected, "event_count": 0}

    def start(self):
        pass

    def stop(self):
        pass


def snap(now: float, price_usd: float, *, liq=25_000.0, mcap=80_000.0, vol=9_000.0, buys=70, sells=40, pair="PAIR") -> MarketSnapshot:
    return MarketSnapshot(
        ts=now,
        pair_address=pair,
        dex_id="pumpswap",
        price_usd=price_usd,
        price_native=price_usd / 150.0,  # SOL at $150
        liquidity_usd=liq,
        market_cap_usd=mcap,
        volume_m5=vol,
        buys_m5=buys,
        sells_m5=sells,
        symbol="TEST",
        name="Test token",
    )


class FakeDex:
    """`paths[mint]` is a function (seconds since migration) -> MarketSnapshot kwargs or None."""

    def __init__(self, clock: Clock, migrated_at: dict[str, float]):
        self.clock = clock
        self.migrated_at = migrated_at
        self.paths: dict = {}
        self.http = _http("DexScreener")
        self.fail = False

    def fetch(self, mints, known_pairs=None):
        if self.fail:
            raise RuntimeError("down")
        now = self.clock()
        out = {}
        for mint in mints:
            fn = self.paths.get(mint)
            if fn is None:
                continue
            kwargs = fn(now - self.migrated_at[mint])
            if kwargs is not None:
                out[mint] = snap(now, pair="PAIR" + mint[:4], **kwargs)
        self.http.health.ok()
        return out


class FakeRpc:
    """holders=None acts like an RPC without getTokenAccounts (not Helius)."""

    def __init__(self, top10=18.0, top1=4.0, dev=0.0, mint_authority=None, freeze_authority=None, holders=450):
        self.report = dict(
            top10=top10, top1=top1, dev=dev, mint_authority=mint_authority, freeze_authority=freeze_authority, holders=holders
        )
        self.calls = 0
        self.holders_supported = None
        self.http = _http("Solana RPC")

    def safety_report(self, mint, pair_address, amm_owners, creator=None, count_holders=False):
        self.calls += 1
        r = self.report
        if count_holders:
            self.holders_supported = r["holders"] is not None
        return SafetyReport(
            ts=0,
            decimals=6,
            supply_ui=1e9,
            token_program="spl-token",
            mint_authority=r["mint_authority"],
            freeze_authority=r["freeze_authority"],
            authorities_known=True,
            top10_pct=r["top10"],
            top_holder_pct=r["top1"],
            holders_seen=20,
            holder_count=r["holders"] if count_holders else None,
            creator="Creator1111111111111111111111111111111111",
            dev_pct=r["dev"],
        )


class FakeRug:
    def __init__(self, risks=None):
        self.risks = risks or []
        self.http = _http("RugCheck")

    def fetch(self, mint):
        return RugcheckReport(ts=0, score=1, score_normalised=1, risks=list(self.risks))


class FakeGmgn:
    available = True

    def __init__(self, holders=500):
        self.holders = holders
        self.http = _http("GMGN")

    def fetch(self, mint):
        return GmgnInfo(ts=0, holders=self.holders, smart_buys=2, sniper_count=5)


class FakeJupiter:
    """Quotes at the fake DexScreener price with a 1% pool fee."""

    def __init__(self, price_native_fn):
        self.price_native_fn = price_native_fn  # mint -> SOL per whole token
        self.http = _http("Jupiter")

    def quote(self, input_mint, output_mint, amount, slippage_bps=500):
        from migbot.config import SOL_MINT

        if input_mint == SOL_MINT:
            price = self.price_native_fn(output_mint)
            out = int(amount / 1e9 / price * 0.99 * 1e6)
        else:
            price = self.price_native_fn(input_mint)
            out = int(amount / 1e6 * price * 0.99 * 1e9)
        return Quote(in_amount=amount, out_amount=out, price_impact_pct=0.5, route="Pump.fun Amm")
