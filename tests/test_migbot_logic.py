"""Filters, paper fills, exit rules, risk limits, config loading and Solana helpers."""
import hashlib
import os
import textwrap

import pytest

from migbot.config import SOL_MINT, ConfigError, CostConfig, ExitConfig, FilterConfig, RugcheckConfig, TradingConfig, load_settings
from migbot.filters import FAIL, OK, SKIP, FilterResult, fmt_usd, market_checks, safety_checks
from migbot.models import GmgnInfo, MarketSnapshot, Quote, RugcheckReport, SafetyReport
from migbot.solana import b58decode, b58encode, bonding_curve_creator, is_pubkey, BONDING_CURVE_DISCRIMINATOR
from migbot.sources.jupiter import NoRoute
from migbot.trading import NoFill, PaperBroker, Position, RiskManager, evaluate_exit
from migbot_fakes import ROOT


def snap(**kw):
    base = dict(ts=0.0, pair_address="P", dex_id="pumpswap", price_usd=0.0001, price_native=0.0001 / 150,
                liquidity_usd=20_000.0, market_cap_usd=80_000.0, volume_m5=9_000.0, buys_m5=70, sells_m5=40)
    base.update(kw)
    return MarketSnapshot(**base)


def by_name(checks):
    return {c.name: c for c in checks}


# ---------------------------------------------------------------- filters
def test_market_checks_pass():
    checks = market_checks(snap(), 0.0001, FilterConfig())
    assert FilterResult(checks).passed and all(c.status == OK for c in checks)


@pytest.mark.parametrize(
    "kw,first,name,text",
    [
        (dict(liquidity_usd=8_000.0), 0.0001, "likuiditas", "$8.0k < $10.0k"),
        (dict(market_cap_usd=30_000.0), 0.0001, "mcap", "$30.0k < $40.0k"),
        (dict(market_cap_usd=2_000_000.0), 0.0001, "mcap", "$2.00M > $1.50M"),
        (dict(volume_m5=100.0), 0.0001, "volume 5m", "$100 < $5.0k"),
        (dict(buys_m5=20, sells_m5=20), 0.0001, "transaksi 5m", "40 < 60"),
        (dict(buys_m5=30, sells_m5=70), 0.0001, "rasio beli 5m", "30% < 50%"),
        (dict(price_usd=0.00005), 0.0001, "harga vs awal", "turun 50%"),
        (dict(price_usd=0.0005), 0.0001, "harga vs awal", "sudah naik 400%"),
    ],
)
def test_market_check_failures(kw, first, name, text):
    result = FilterResult(market_checks(snap(**kw), first, FilterConfig()))
    assert not result.passed
    c = by_name(result.checks)[name]
    assert c.status == FAIL and text in c.detail


def test_missing_market_data_skips_unless_strict():
    s = snap(volume_m5=None, buys_m5=None, sells_m5=None)
    assert FilterResult(market_checks(s, None, FilterConfig())).passed
    strict = FilterResult(market_checks(s, None, FilterConfig(strict_missing_data=True)))
    assert {c.name for c in strict.checks if c.status == FAIL} == {"volume 5m", "transaksi 5m", "rasio beli 5m", "harga vs awal"}


def test_socials_required():
    cfg = FilterConfig(require_socials=True)
    assert by_name(market_checks(snap(), 0.0001, cfg))["sosial"].status == FAIL
    assert by_name(market_checks(snap(socials=["https://x.com/a"]), 0.0001, cfg))["sosial"].status == OK


def safe(**kw):
    base = dict(ts=0.0, decimals=6, authorities_known=True, top10_pct=20.0, top_holder_pct=5.0, dev_pct=0.0)
    base.update(kw)
    return SafetyReport(**base)


def rug(*risks, score=5):
    return RugcheckReport(ts=0, score_normalised=score, risks=[{"name": n, "level": lvl, "description": ""} for n, lvl in risks])


def test_safety_checks_pass_and_fail():
    cfg, rc = FilterConfig(), RugcheckConfig()
    assert FilterResult(safety_checks(safe(), rug(), None, cfg, rc)).passed
    bad = FilterResult(
        safety_checks(
            safe(mint_authority="X", freeze_authority="Y", extensions=["permanentDelegate", "metadataPointer"],
                 top10_pct=45.0, top_holder_pct=12.5, dev_pct=7.0),
            rug(("Freeze Authority still enabled", "danger"), ("Mutable metadata", "warn")),
            None, cfg, rc,
        )
    )
    names = {c.name for c in bad.checks if c.status == FAIL}
    assert names == {"mint authority", "freeze authority", "token-2022", "top10 holder", "holder terbesar", "dev pegang", "RugCheck"}
    assert "permanentDelegate" in by_name(bad.checks)["token-2022"].detail
    assert "Mutable" not in by_name(bad.checks)["RugCheck"].detail


def test_rugcheck_ignore_and_score_limit():
    cfg = FilterConfig()
    risky = rug(("Low Liquidity", "danger"), score=70)
    assert by_name(safety_checks(safe(), risky, None, cfg, RugcheckConfig(ignore_risks=["low liquidity"])))["RugCheck"].status == OK
    limited = RugcheckConfig(ignore_risks=["Low Liquidity"], max_score=50)
    assert by_name(safety_checks(safe(), risky, None, cfg, limited))["RugCheck"].status == FAIL
    assert "RugCheck" not in by_name(safety_checks(safe(), None, None, cfg, RugcheckConfig(enabled=False)))


def test_missing_safety_data():
    cfg, rc = FilterConfig(), RugcheckConfig()
    loose = safety_checks(None, None, None, cfg, rc)
    assert FilterResult(loose).passed and all(c.status == SKIP for c in loose)
    strict = safety_checks(None, None, None, FilterConfig(strict_missing_data=True), rc)
    assert not FilterResult(strict).passed


def test_gmgn_checks_only_with_gmgn_data():
    cfg = FilterConfig(min_smart_buys=1, max_sniper_count=10, strict_missing_data=True)
    without = by_name(safety_checks(safe(), rug(), None, cfg, RugcheckConfig()))
    assert not any("GMGN" in n for n in without)
    with_data = by_name(safety_checks(safe(), rug(), GmgnInfo(ts=0, holders=120, smart_buys=0, sniper_count=25), cfg, RugcheckConfig()))
    assert with_data["smart money (GMGN)"].status == FAIL
    assert with_data["sniper (GMGN)"].status == FAIL


def test_holder_count_from_rpc_with_gmgn_as_fallback():
    rc = RugcheckConfig()

    def check(safety, gmgn=None, cfg=FilterConfig(min_holders=200)):
        return by_name(safety_checks(safety, rug(), gmgn, cfg, rc))["jumlah holder"]

    assert check(safe(holder_count=450)).status == OK
    low = check(safe(holder_count=150))
    assert low.status == FAIL and low.detail == "150 < 200"
    # The RPC count wins; GMGN's count is used only when the RPC could not count.
    assert check(safe(holder_count=150), GmgnInfo(ts=0, holders=900)).status == FAIL
    assert check(safe(), GmgnInfo(ts=0, holders=900)).status == OK
    # Nothing counted: skipped, or failed with strict_missing_data.
    assert check(safe()).status == SKIP
    assert check(safe(), cfg=FilterConfig(min_holders=200, strict_missing_data=True)).status == FAIL
    # More holders than the pages that were read.
    assert check(safe(holder_count=2000, holder_count_complete=False)).detail == "2000+ ≥ 200"
    assert check(safe(holder_count=2000, holder_count_complete=False), cfg=FilterConfig(min_holders=5000)).status == SKIP
    off = by_name(safety_checks(safe(holder_count=5), rug(), None, FilterConfig(min_holders=0), rc))
    assert "jumlah holder" not in off


def test_fmt_usd():
    assert fmt_usd(None) == "?" and fmt_usd(950) == "$950" and fmt_usd(12_345) == "$12.3k" and fmt_usd(2_500_000) == "$2.50M"


# ---------------------------------------------------------------- paper fills
class Jup:
    def __init__(self, out=None, exc=None):
        self.out, self.exc, self.calls = out, exc, []

    def quote(self, input_mint, output_mint, amount, slippage_bps=500):
        self.calls.append((input_mint, output_mint, amount))
        if self.exc:
            raise self.exc
        return Quote(in_amount=amount, out_amount=self.out, price_impact_pct=0.8, route="Pump.fun Amm")


def test_buy_with_jupiter_quote():
    broker = PaperBroker(CostConfig(extra_slippage_pct=1.0, priority_fee_sol=0.0005), Jup(out=1_000_000_000))
    fill = broker.buy("MINT", 0.1, snap(), 6)
    assert fill.method == "jupiter" and fill.tokens_raw == 990_000_000  # 1% extra slippage
    assert fill.sol == pytest.approx(0.1005) and fill.fee_sol == 0.0005
    assert fill.price_native == pytest.approx(0.1005 / 990)
    assert broker.jupiter.calls[0] == (SOL_MINT, "MINT", 100_000_000)


def test_unknown_decimals_are_inferred_from_the_quote():
    # 0.1 SOL at 0.0001 SOL/token = 1000 whole tokens; the quote returns 1000 * 10^9 raw units
    broker = PaperBroker(CostConfig(extra_slippage_pct=0.0), Jup(out=1000 * 10**9))
    fill = broker.buy("MINT", 0.1, snap(price_native=0.0001), None)
    assert fill.decimals == 9 and fill.price_native == pytest.approx((0.1 + CostConfig().priority_fee_sol) / 1000)
    assert PaperBroker(CostConfig(), Jup(out=10**6)).buy("MINT", 0.1, None, None).decimals == 6
    assert PaperBroker(CostConfig(use_jupiter=False)).buy("MINT", 0.1, snap(), None).decimals == 6


def test_buy_estimate_when_jupiter_unreachable():
    broker = PaperBroker(CostConfig(est_swap_fee_pct=1.25, est_slippage_pct=3.0), Jup(exc=RuntimeError("timeout")))
    fill = broker.buy("MINT", 0.1, snap(price_native=0.0001), 6)
    assert fill.method == "estimasi"
    assert fill.tokens_raw == int(0.1 / 0.0001 * (1 - 0.0425) * 1e6)


def test_buy_without_route_or_price_fails():
    with pytest.raises(NoFill):
        PaperBroker(CostConfig(), Jup(exc=NoRoute("no route"))).buy("MINT", 0.1, snap(), 6)
    with pytest.raises(NoFill):
        PaperBroker(CostConfig(use_jupiter=False)).buy("MINT", 0.1, snap(price_native=None), 6)


def test_sell_paths():
    fill = PaperBroker(CostConfig(extra_slippage_pct=1.0, priority_fee_sol=0.0005), Jup(out=200_000_000)).sell("MINT", 5_000_000, 6, snap())
    assert fill.method == "jupiter" and fill.sol == pytest.approx(0.2 * 0.99 - 0.0005)
    est = PaperBroker(CostConfig(est_swap_fee_pct=1.0, est_slippage_pct=1.0, priority_fee_sol=0.0), Jup(exc=NoRoute("x")))
    fill = est.sell("MINT", 5_000_000, 6, snap(price_native=0.01))
    assert fill.method == "estimasi" and fill.sol == pytest.approx(5 * 0.01 * 0.98)
    nothing = PaperBroker(CostConfig(use_jupiter=False)).sell("MINT", 5_000_000, 6, None)
    assert nothing.method == "tanpa harga" and nothing.sol == 0.0


# ---------------------------------------------------------------- exits
def position(**kw):
    base = dict(mint="M", symbol="T", pair_address="P", opened_at=0.0, cost_sol=0.1, tokens_raw_initial=1000, tokens_raw=1000,
                decimals=0, entry_price_native=0.0001, entry_price_usd=None, entry_mcap_usd=None, liquidity_at_entry=20_000.0,
                peak_price_native=0.0001, last_price_native=0.0001, last_price_at=0.0, method="jupiter")
    base.update(kw)
    return Position(**base)


def test_exit_rules():
    cfg = ExitConfig()
    assert evaluate_exit(position(), 0.0001, 20_000.0, 60, cfg) is None
    assert evaluate_exit(position(), 0.00006, 20_000.0, 60, cfg).reason == "stop loss"
    assert evaluate_exit(position(), 0.0001, 9_000.0, 60, cfg).reason == "likuiditas anjlok"
    assert evaluate_exit(position(last_price_at=3590), 0.0001, 20_000.0, 3600, cfg).reason == "waktu habis"
    assert evaluate_exit(position(last_price_at=0), 0.0001, 20_000.0, 601, cfg).reason == "data harga hilang"
    tp = evaluate_exit(position(), 0.00021, 20_000.0, 60, cfg)
    assert tp.reason == "take profit +100%" and tp.tokens_raw == 500 and tp.tp_index == 0
    assert evaluate_exit(position(tp_done=[0], tokens_raw=500, peak_price_native=0.00021), 0.00021, 20_000.0, 60, cfg) is None
    trail = evaluate_exit(position(peak_price_native=0.0003), 0.0002, 20_000.0, 60, cfg)
    assert trail.reason == "trailing stop" and trail.tokens_raw == 1000
    # below the activation level the trailing stop stays off
    assert evaluate_exit(position(peak_price_native=0.00014), 0.00009, 20_000.0, 60, cfg) is None


def test_take_profit_ladder_and_dust():
    cfg = ExitConfig(take_profit=[[50, 0.3], [150, 0.695]], trailing_pct=0)
    first = evaluate_exit(position(), 0.00016, None, 60, cfg)
    assert first.tokens_raw == 300 and first.tp_index == 0
    second = evaluate_exit(position(tp_done=[0], tokens_raw=700), 0.00026, None, 60, cfg)
    assert second.tokens_raw == 700  # 695 would leave 5 (<1%): sell everything


# ---------------------------------------------------------------- risk
def test_risk_limits():
    rm = RiskManager(TradingConfig(buy_sol=0.1, max_open_positions=2, max_buys_per_day=2, max_daily_loss_sol=0.3), now=0)
    assert rm.check_buy(1.0, 0, 0.0, 0.0005) is None
    assert "posisi terbuka" in rm.check_buy(1.0, 2, 0.0, 0.0005)
    assert "tidak cukup" in rm.check_buy(0.1, 0, 0.0, 0.0005)
    rm.record_buy(); rm.record_buy()
    assert "beli hari ini" in rm.check_buy(1.0, 0, 0.0, 0.0005)
    rm2 = RiskManager(TradingConfig(max_daily_loss_sol=0.3), now=0)
    rm2.record_realized(-0.2)
    assert rm2.check_buy(1.0, 0, +0.5, 0.0) is None  # open profits do not offset losses...
    assert "batas rugi" in rm2.check_buy(1.0, 0, -0.1, 0.0)  # ...but open losses count
    assert rm2.roll(86_400) and rm2.realized_today == 0 and rm2.buys_today == 0
    restored = RiskManager(TradingConfig(), saved=rm.to_dict(), now=10)
    assert restored.buys_today == 2


# ---------------------------------------------------------------- config
def write(tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def test_shipped_config_matches_defaults():
    s = load_settings(os.path.join(ROOT, "config", "migbot.yaml"), env_path=None)
    assert s.filters == FilterConfig() and s.exits == ExitConfig() and s.trading == TradingConfig()
    assert s.costs == CostConfig()


def test_config_rejects_typos_and_bad_values(tmp_path):
    with pytest.raises(ConfigError, match="filters.min_liqudity_usd"):
        load_settings(write(tmp_path, "filters:\n  min_liqudity_usd: 5\n"), env_path=None)
    with pytest.raises(ConfigError, match="bukan '10.000'"):
        load_settings(write(tmp_path, "filters:\n  min_liquidity_usd: '10.000'\n"), env_path=None)
    with pytest.raises(ConfigError, match="window_seconds"):
        load_settings(write(tmp_path, "entry:\n  delay_seconds: 600\n  window_seconds: 300\n"), env_path=None)
    with pytest.raises(ConfigError, match="lebih dari 1"):
        load_settings(write(tmp_path, "exits:\n  take_profit: [[50, 0.7], [100, 0.7]]\n"), env_path=None)
    with pytest.raises(ConfigError, match="tidak ditemukan"):
        load_settings(str(tmp_path / "missing.yaml"), env_path=None)


def test_env_values(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLANA_RPC_URL", "https://rpc.example/?api-key=abc")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    s = load_settings(write(tmp_path, "poll_seconds: 3\n"), env_path=None)
    assert s.rpc_url == "https://rpc.example/?api-key=abc" and s.telegram_token == "123:abc" and s.poll_seconds == 3


# ---------------------------------------------------------------- solana helpers
def test_base58_known_vectors():
    assert b58encode(bytes(32)) == "1" * 32
    assert b58decode("1" * 32) == bytes(32)
    assert b58encode(b58decode(SOL_MINT)) == SOL_MINT and len(b58decode(SOL_MINT)) == 32
    assert is_pubkey(SOL_MINT) and not is_pubkey("0OIl") and not is_pubkey(123)


def test_bonding_curve_creator():
    creator = hashlib.sha256(b"c").digest()
    data = BONDING_CURVE_DISCRIMINATOR + bytes(40) + b"\x01" + creator + b"\x00"
    assert bonding_curve_creator(data) == b58encode(creator)
    assert bonding_curve_creator(data[:60]) is None
    assert bonding_curve_creator(b"x" * 8 + data[8:]) is None
    assert bonding_curve_creator(BONDING_CURVE_DISCRIMINATOR + bytes(73)) is None
