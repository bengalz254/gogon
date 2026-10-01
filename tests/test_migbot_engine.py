"""Whole token lifecycles through the engine, in simulated time, with fake sources."""
import json
import os

import pytest

from migbot.engine import Engine, Sources
from migbot.storage import read_csv, read_paths
from migbot_fakes import (
    MINT_A, MINT_B, MINT_C, T0, Clock, FakeDex, FakeFeed, FakeGmgn, FakeJupiter, FakeRpc, FakeRug, settings,
)


def winner_path(s):
    """No data for a minute, flat until 10 min, then 2.5x, 3x, and back to 2x."""
    if s < 60:
        return None
    if s < 600:
        return {"price_usd": 0.0001}
    if s < 900:
        return {"price_usd": 0.00025}
    if s < 1200:
        return {"price_usd": 0.0003}
    return {"price_usd": 0.0002}


def thin_path(s):
    return None if s < 30 else {"price_usd": 0.0001, "liq": 5_000.0}


def dip_path(s):
    """Up 50% after the migration, a 40% dip, a bounce, a run to 2x, then 30% off that high."""
    if s < 30:
        return None
    if s < 120:
        return {"price_usd": 0.0001}
    if s < 240:
        return {"price_usd": 0.00015}  # the high after the migration
    if s < 600:
        return {"price_usd": 0.00009}  # 40% under it, still falling / flat: wait
    if s < 1200:
        return {"price_usd": 0.0001}  # +11% off the low, 33% under the high: buy the dip
    if s < 1500:
        return {"price_usd": 0.00015}  # +50%: take profit on half
    if s < 1800:
        return {"price_usd": 0.0002}
    return {"price_usd": 0.00014}  # 30% off the high: trailing stop


# Most tests only need a position quickly: the old entry, buying at 3 minutes once the filters pass.
MOMENTUM = {"entry__dip_pct": 0.0, "entry__delay_seconds": 180.0, "entry__window_seconds": 900.0}


def build(tmp_path, clock, paths, migrated, jupiter=True, rpc=None, rug=None, dip=False, **overrides):
    """dip=True runs the shipped entry rule (buy the dip) instead of MOMENTUM."""
    s = settings(tmp_path, **({} if dip else MOMENTUM), **overrides)
    feed = FakeFeed()
    dex = FakeDex(clock, migrated)
    dex.paths.update(paths)

    def price_native(mint):
        kw = dex.paths[mint](clock() - migrated[mint])
        return kw["price_usd"] / 150.0

    src = Sources(
        pumpportal=feed,
        dexscreener=dex,
        rpc=rpc or FakeRpc(),
        rugcheck=rug or FakeRug(),
        gmgn=FakeGmgn(),
        jupiter=FakeJupiter(price_native) if jupiter else None,
    )
    return Engine(s, src, clock=clock), feed, dex


def run_for(engine, clock, seconds, step=5):
    end = clock() + seconds
    while clock() < end:
        engine.tick()
        clock.advance(step)


def test_winner_is_bought_takes_profit_and_trails_out(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0, symbol="WIN")
    run_for(engine, clock, 200)
    assert MINT_A in engine.positions, engine.tokens[MINT_A].reasons
    pos = engine.positions[MINT_A]
    assert 180 <= pos.opened_at - T0 <= 200
    assert pos.method == "jupiter"
    start = engine.s.trading.paper_balance_sol
    assert engine.balance == pytest.approx(start - 0.1 - engine.s.costs.priority_fee_sol)

    run_for(engine, clock, 7300)  # past the 2-hour tracking window
    assert MINT_A not in engine.positions
    trades = read_csv(os.path.join(engine.s.data_dir, "trades.csv"))
    assert [t["side"] for t in trades] == ["BUY", "SELL", "SELL"]
    assert trades[1]["reason"] == "take profit +40%"
    assert trades[2]["reason"] == "trailing stop"
    total = float(trades[2]["position_pnl_sol"])
    assert total > 0.1  # bought ~1x, sold half at 2.5x and half at 2x
    assert engine.balance == pytest.approx(start + total)
    assert engine.stats.wins == 1 and engine.stats.closed == 1

    rows = read_csv(os.path.join(engine.s.data_dir, "tokens.csv"))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "dibeli" and row["symbol"] == "WIN"
    assert row["ret_1m"] == "" and row["ret_3m"] == ""  # before the first possible buy
    assert float(row["ret_10m"]) == pytest.approx(150.0)
    assert float(row["ret_15m"]) == pytest.approx(200.0)
    assert float(row["ret_120m"]) == pytest.approx(100.0)
    assert float(row["max_ret_pct"]) == pytest.approx(200.0)
    assert float(row["trade_pnl_sol"]) == pytest.approx(total, abs=1e-4)
    assert MINT_A in engine.done and MINT_A not in engine.tokens


def test_dip_is_bought_after_the_bounce_not_at_launch(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: dip_path}, {MINT_A: T0}, dip=True)
    feed.push(MINT_A, T0, symbol="DIP")
    run_for(engine, clock, 590)
    tok = engine.tokens[MINT_A]
    assert not engine.positions  # 40% under the high but no bounce yet
    assert tok.reasons and tok.reasons[0].startswith("dip: 40% di bawah puncak, baru naik 0% dari dasar")
    run_for(engine, clock, 30)
    assert MINT_A in engine.positions
    assert 600 <= engine.positions[MINT_A].opened_at - T0 <= 620

    run_for(engine, clock, 7300)
    trades = read_csv(os.path.join(engine.s.data_dir, "trades.csv"))
    assert [(t["side"], t["reason"]) for t in trades] == [
        ("BUY", "beli saat dip"), ("SELL", "take profit +40%"), ("SELL", "trailing stop"),
    ]
    close = trades[-1]
    assert float(close["position_pnl_sol"]) > 0.02
    assert 10 <= float(close["entry_age_min"]) <= 10.5 and float(trades[0]["entry_age_min"]) == float(close["entry_age_min"])
    assert 90 < float(close["peak_pct"]) < 100  # 2x the bought price, less the buy costs
    assert float(close["held_min"]) == pytest.approx(20, abs=0.5)


def test_no_buy_in_the_first_minutes_even_after_a_dip(tmp_path):
    def early_dip(s):
        if s < 30:
            return None
        if s < 60:
            return {"price_usd": 0.00015}
        if s < 120:
            return {"price_usd": 0.00009}
        return {"price_usd": 0.0001}  # dipped and bounced by minute 2

    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: early_dip}, {MINT_A: T0}, dip=True)
    feed.push(MINT_A, T0)
    run_for(engine, clock, 295)
    assert not engine.positions  # the launch minutes are never bought
    run_for(engine, clock, 20)
    assert MINT_A in engine.positions and engine.positions[MINT_A].opened_at - T0 >= 300


def test_token_that_never_dips_is_rejected_at_the_end_of_the_window(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: lambda s: {"price_usd": 0.0001 + s * 1e-8}}, {MINT_A: T0}, dip=True)
    feed.push(MINT_A, T0)
    run_for(engine, clock, 3500)
    assert engine.tokens[MINT_A].status == "dipantau"
    run_for(engine, clock, 200)
    tok = engine.tokens[MINT_A]
    assert tok.status == "ditolak" and not engine.positions
    assert tok.reasons[0].startswith("dip: baru 0% di bawah puncak")


def test_price_path_is_saved_when_tracking_ends(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_B: thin_path}, {MINT_B: T0})
    feed.push(MINT_B, T0, symbol="THIN")
    run_for(engine, clock, 7300)
    assert MINT_B not in engine.tokens and not engine.paths
    [record] = read_paths(os.path.join(engine.s.data_dir, "paths.jsonl.gz"))
    assert record["mint"] == MINT_B and record["symbol"] == "THIN" and record["status"] == "ditolak"
    points = record["points"]
    assert 700 <= len(points) <= 725  # every 10 s refresh for 2 hours
    t, price, liq = points[0][:3]
    assert 30 <= t <= 45 and price == 0.0001 and liq == 5000
    assert [p[0] for p in points] == sorted(p[0] for p in points)


def test_every_token_gets_holder_data_at_the_start_of_the_window(tmp_path):
    clock = Clock()
    rpc = FakeRpc(holders=321)
    engine, feed, _ = build(tmp_path, clock, {MINT_B: thin_path}, {MINT_B: T0}, rpc=rpc, dip=True)
    feed.push(MINT_B, T0)
    run_for(engine, clock, 320)
    tok = engine.tokens[MINT_B]
    assert rpc.calls == 1 and tok.safety_first["holder_count"] == 321  # checked although the market checks fail
    assert tok.ref_snapshot["liquidity_usd"] == 5_000.0 and 300 <= tok.ref_at - T0 <= 310
    run_for(engine, clock, 3500)
    assert rpc.calls == 1  # once for research; the token never became a buy candidate
    run_for(engine, clock, 3600)
    row = read_csv(os.path.join(engine.s.data_dir, "tokens.csv"))[0]
    assert row["holders"] == "321" and row["ref_liquidity_usd"] == "5000" and row["ref_txns_5m"] == "110"


def test_no_buy_when_jupiter_and_dexscreener_disagree(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    real = engine.src.jupiter.price_native_fn
    engine.src.jupiter.price_native_fn = lambda mint: real(mint) * 1.5  # DexScreener lags a 50% spike
    feed.push(MINT_A, T0)
    run_for(engine, clock, 250)
    assert not engine.positions
    assert engine.tokens[MINT_A].reasons[0].startswith("beli batal: harga Jupiter +")
    engine.src.jupiter.price_native_fn = real  # prices agree again: bought
    run_for(engine, clock, 30)
    assert MINT_A in engine.positions


def test_thin_liquidity_is_rejected_and_still_researched(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_B: thin_path}, {MINT_B: T0})
    feed.push(MINT_B, T0)
    run_for(engine, clock, 950)
    tok = engine.tokens[MINT_B]
    assert tok.status == "ditolak"
    assert any(r.startswith("likuiditas") for r in tok.reasons)
    assert not engine.positions
    run_for(engine, clock, 6400)
    row = read_csv(os.path.join(engine.s.data_dir, "tokens.csv"))[0]
    assert row["status"] == "ditolak" and "likuiditas" in row["reasons"]
    assert row["ret_60m"] == "0.00"


def test_token_without_market_data_is_marked_no_data(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {}, {MINT_C: T0})
    feed.push(MINT_C, T0)
    run_for(engine, clock, 950)
    assert engine.tokens[MINT_C].status == "tanpa data"


def test_unsafe_token_is_rejected_by_safety_checks(tmp_path):
    clock = Clock()
    rpc = FakeRpc(top10=55.0, dev=12.0)
    rug = FakeRug([{"name": "Freeze Authority still enabled", "level": "danger", "description": ""}])
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0}, rpc=rpc, rug=rug)
    feed.push(MINT_A, T0)
    run_for(engine, clock, 300)
    tok = engine.tokens[MINT_A]
    assert not engine.positions
    joined = " ".join(tok.reasons)
    assert "top10 holder" in joined and "dev pegang" in joined and "RugCheck" in joined
    # safety data is cached, not refetched on every 10 s snapshot
    assert rpc.calls <= 3


def test_token_with_too_few_holders_is_not_bought(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0}, rpc=FakeRpc(holders=120))
    feed.push(MINT_A, T0)
    run_for(engine, clock, 300)
    assert not engine.positions
    assert "jumlah holder: 120 < 200" in engine.tokens[MINT_A].reasons  # the RPC count wins over GMGN's 500


def test_rpc_that_cannot_count_holders_skips_the_check_and_warns_once(tmp_path):
    clock = Clock()
    paths, migrated = {MINT_A: winner_path, MINT_B: winner_path}, {MINT_A: T0, MINT_B: T0}
    engine, feed, _ = build(tmp_path, clock, paths, migrated, rpc=FakeRpc(holders=None))
    engine.src.gmgn = None  # and no GMGN count to fall back on
    sent = []
    engine.notify.send = sent.append
    feed.push(MINT_A, T0)
    feed.push(MINT_B, T0)
    run_for(engine, clock, 200)
    assert MINT_A in engine.positions and MINT_B in engine.positions
    checks = {c["name"]: c["status"] for c in engine.tokens[MINT_A].checks}
    assert checks["jumlah holder"] == "lewati"
    assert sum("tidak bisa menghitung holder" in m for m in sent) == 1


def test_ignored_rugcheck_risk_does_not_block(tmp_path):
    clock = Clock()
    rug = FakeRug([{"name": "Low Liquidity", "level": "danger", "description": ""}])
    engine, feed, _ = build(
        tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0}, rug=rug, safety__rugcheck__ignore_risks=["low liquidity"]
    )
    feed.push(MINT_A, T0)
    run_for(engine, clock, 200)
    assert MINT_A in engine.positions


def test_risk_limit_blocks_second_position(tmp_path):
    clock = Clock()
    migrated = {MINT_A: T0, MINT_B: T0}
    engine, feed, _ = build(
        tmp_path, clock, {MINT_A: winner_path, MINT_B: winner_path}, migrated, trading__max_open_positions=1
    )
    feed.push(MINT_A, T0)
    feed.push(MINT_B, T0)
    run_for(engine, clock, 250)
    assert len(engine.positions) == 1
    other = MINT_B if MINT_A in engine.positions else MINT_A
    assert engine.tokens[other].reasons[0].startswith("risiko: sudah 1 posisi terbuka")
    run_for(engine, clock, 700)  # the buy window closes while the slot is still taken
    assert engine.tokens[other].status == "lolos, tak dibeli"


def test_late_discovery_is_skipped(tmp_path):
    clock = Clock(T0 + 1200)
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0, received_at=T0 + 1200)
    engine.tick()
    assert MINT_A not in engine.tokens
    assert engine.stats.late == 1
    feed.push(MINT_A, T0, received_at=T0 + 1205)  # the same token again is ignored
    engine.tick()
    assert engine.stats.late == 1


def test_duplicate_sources_merge(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0 + 5)
    engine.tick()
    from migbot.models import MigrationEvent

    engine.register(MigrationEvent(MINT_A, "geckoterminal", T0, T0 + 40), clock())
    tok = engine.tokens[MINT_A]
    assert tok.sources == ["pumpportal", "geckoterminal"]
    assert tok.migrated_at == T0  # the earlier (pool creation) time wins
    assert engine.stats.migrations == 1
    engine.register(MigrationEvent(MINT_A, "other", T0 - 3600, T0 + 40), clock())
    assert tok.migrated_at == T0  # an hour-older pool is a different event


def test_restart_restores_open_position(tmp_path):
    clock = Clock()
    engine, feed, dex = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0)
    run_for(engine, clock, 200)
    assert MINT_A in engine.positions
    engine.save()
    balance = engine.balance

    engine2, _, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    assert MINT_A in engine2.positions and MINT_A in engine2.tokens
    assert engine2.balance == pytest.approx(balance)
    run_for(engine2, clock, 7300)
    assert not engine2.positions
    trades = read_csv(os.path.join(engine2.s.data_dir, "trades.csv"))
    assert [t["side"] for t in trades] == ["BUY", "SELL", "SELL"]


def test_stop_loss_and_estimated_fills_without_jupiter(tmp_path):
    def loser(s):
        if s < 30:
            return None
        return {"price_usd": 0.0001 if s < 400 else 0.00005}

    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: loser}, {MINT_A: T0}, jupiter=False)
    feed.push(MINT_A, T0)
    run_for(engine, clock, 500)
    trades = read_csv(os.path.join(engine.s.data_dir, "trades.csv"))
    assert [t["side"] for t in trades] == ["BUY", "SELL"]
    assert trades[0]["method"] == "estimasi"
    assert trades[1]["reason"] == "stop loss"
    assert float(trades[1]["pnl_sol"]) < -0.05
    assert engine.risk.realized_today == pytest.approx(float(trades[1]["pnl_sol"]), abs=1e-6)


def test_daily_loss_limit_halts_buying(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0}, trading__max_daily_loss_sol=0.05)
    engine.risk.record_realized(-0.06)
    feed.push(MINT_A, T0)
    run_for(engine, clock, 250)
    assert not engine.positions
    assert "batas rugi harian" in engine.tokens[MINT_A].reasons[0]


def test_rug_exit_on_liquidity_collapse(tmp_path):
    def rug(s):
        if s < 30:
            return None
        if s < 400:
            return {"price_usd": 0.0001}
        return {"price_usd": 0.00009, "liq": 3_000.0}

    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: rug}, {MINT_A: T0})
    feed.push(MINT_A, T0)
    run_for(engine, clock, 500)
    trades = read_csv(os.path.join(engine.s.data_dir, "trades.csv"))
    assert trades[-1]["reason"] == "likuiditas anjlok"


def test_status_file_is_strict_json(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0)
    run_for(engine, clock, 200)
    with open(engine.status_path, encoding="utf-8") as fh:
        text = fh.read()
    status = json.loads(text, parse_constant=lambda c: pytest.fail(f"non-strict JSON constant {c}"))
    assert status["mode"] == "PAPER" and status["positions"][0]["mint"] == MINT_A
    assert status["tokens"][0]["status"] == "dibeli"
    assert {f["name"] for f in status["feeds"]} >= {"PumpPortal", "DexScreener", "Solana RPC"}


def test_dexscreener_outage_does_not_crash(tmp_path):
    clock = Clock()
    engine, feed, dex = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0)
    dex.fail = True
    run_for(engine, clock, 100)
    assert engine.tokens[MINT_A].last == {}
    dex.fail = False
    run_for(engine, clock, 150)
    assert MINT_A in engine.positions


def test_day_rollover_resets_counters(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {}, {})
    engine.stats.migrations = 7
    engine.risk.buys_today = 3
    clock.advance(86400)
    engine.tick()
    assert engine.stats.migrations == 0 and engine.risk.buys_today == 0


def test_future_migration_time_is_clamped_to_now(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_A: winner_path}, {MINT_A: T0})
    feed.push(MINT_A, T0 + 86_400, received_at=T0)  # a source with a skewed clock
    engine.tick()
    assert engine.tokens[MINT_A].migrated_at == T0


def test_last_checkpoint_is_recorded_before_finalizing(tmp_path):
    clock = Clock()
    engine, feed, _ = build(tmp_path, clock, {MINT_B: thin_path}, {MINT_B: T0})
    feed.push(MINT_B, T0)
    run_for(engine, clock, 7400, step=7)  # ticks that never land exactly on the 120-minute mark
    row = read_csv(os.path.join(engine.s.data_dir, "tokens.csv"))[0]
    assert all(row[f"ret_{m}m"] != "" for m in (5, 10, 15, 30, 60, 120))
