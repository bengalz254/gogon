from decimal import Decimal

import pytest

from scalper.config import Settings, load_settings, settings_from_dict, validate
from scalper.exchange.rules import parse_symbol_rules
from scalper.models import LONG, SHORT, SymbolRules, fmt_decimal, snap


def rules(**kw) -> SymbolRules:
    base = dict(
        symbol="BTCUSDT",
        tick_size=Decimal("0.10"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
        market_step_size=Decimal("0.001"),
        market_min_qty=Decimal("0.001"),
        market_max_qty=Decimal("120"),
        min_notional=Decimal("100"),
    )
    base.update(kw)
    return SymbolRules(**base)


def test_snap_handles_float_noise():
    assert snap(0.30000000000000004, "0.001") == Decimal("0.300")
    assert snap(0.0029999999999, "0.001") == Decimal("0.003")
    assert snap(0.0029, "0.001") == Decimal("0.002")
    assert snap(123.41, "0.1", "ceil") == Decimal("123.5")
    assert snap(123.45, "0.1", "nearest") == Decimal("123.5")


def test_fmt_decimal_has_no_exponent_or_trailing_zeros():
    assert fmt_decimal(Decimal("100.000")) == "100"
    assert fmt_decimal(Decimal("0.00100")) == "0.001"
    assert fmt_decimal(Decimal("1E+2")) == "100"


def test_quantity_rounds_down_and_respects_market_max():
    r = rules()
    assert r.qty(0.0019) == Decimal("0.001")
    assert r.qty(500) == Decimal("120")  # MARKET_LOT_SIZE max
    assert r.qty(500, market=False) == Decimal("500")


def test_stop_and_target_rounding_directions():
    r = rules()
    # stops round AWAY from the market, targets TOWARD the entry
    assert r.stop_price(99.97, LONG) == Decimal("99.9")
    assert r.stop_price(100.03, SHORT) == Decimal("100.1")
    assert r.target_price(101.57, LONG) == Decimal("101.5")
    assert r.target_price(98.43, SHORT) == Decimal("98.5")


def test_check_order_min_qty_and_notional():
    r = rules()
    assert "below exchange minimum" in r.check_order(Decimal("0"), 50_000)
    assert "notional" in r.check_order(Decimal("0.001"), 50_000)  # 50 < 100 min notional
    assert r.check_order(Decimal("0.002"), 50_000) == ""


def test_parse_symbol_rules():
    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "marginAsset": "USDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
                    {"filterType": "MIN_NOTIONAL", "notional": "100"},
                ],
            },
            {"symbol": "BTCUSDT_260925", "contractType": "CURRENT_QUARTER", "status": "TRADING", "filters": []},
        ]
    }
    r = parse_symbol_rules(info, ["BTCUSDT"])["BTCUSDT"]
    assert r.tick_size == Decimal("0.10") and r.market_max_qty == Decimal("120") and r.min_notional == Decimal("100")
    with pytest.raises(ValueError, match="not PERPETUAL"):
        parse_symbol_rules(info, ["BTCUSDT_260925"])
    with pytest.raises(ValueError, match="not listed"):
        parse_symbol_rules(info, ["NOPEUSDT"])


def test_defaults_are_valid_and_yaml_matches_code_defaults(tmp_path):
    assert validate(Settings()) == []
    s = load_settings("config/scalper.yaml", env_path=str(tmp_path / "none.env"))
    d = Settings()
    assert s.risk == d.risk and s.strategy == d.strategy and s.execution == d.execution
    assert s.management == d.management and s.costs == d.costs


def test_unknown_key_is_rejected():
    with pytest.raises(ValueError, match="Unknown setting 'config.risk.risk_per_trade'"):
        settings_from_dict({"risk": {"risk_per_trade": 5}})


def test_secret_in_yaml_is_rejected():
    with pytest.raises(ValueError, match="looks like a secret"):
        settings_from_dict({"api_key": "abc"})


def test_type_coercion_and_bad_values():
    s = settings_from_dict({"risk": {"risk_per_trade_pct": "0.75"}, "symbols": "BTCUSDT, ETHUSDT"})
    assert s.risk.risk_per_trade_pct == 0.75 and s.symbols == ["BTCUSDT", "ETHUSDT"]
    with pytest.raises(ValueError, match="Invalid value"):
        settings_from_dict({"execution": {"leverage": "lots"}})


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"mode": "yolo"}, "mode must be one of"),
        ({"timeframe": "7m"}, "not supported"),
        ({"risk": {"risk_per_trade_pct": 10}}, "risk_per_trade_pct"),
        ({"execution": {"leverage": 200}}, "leverage"),
        ({"strategy": {"trend_pullback": {"htf_interval": "7m"}}}, "htf_interval"),
        ({"timeframe": "15m", "strategy": {"trend_pullback": {"htf_interval": "5m"}}}, "whole multiple"),
        ({"symbols": ["btc"]}, "does not look like"),
        ({"costs": {"taker_fee": 0.05}}, "taker_fee"),
    ],
)
def test_validation_errors(patch, message):
    s = settings_from_dict(patch)
    with pytest.raises(ValueError, match=message):
        validate(s)


def test_live_mode_requires_keys(monkeypatch, tmp_path):
    for var in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="BINANCE_API_KEY"):
        load_settings("config/scalper.yaml", env_path=str(tmp_path / "none.env"), mode_override="live")


def test_testnet_uses_testnet_keys_and_url(monkeypatch, tmp_path):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    s = load_settings("config/scalper.yaml", env_path=str(tmp_path / "none.env"), mode_override="testnet")
    assert s.credentials.api_key == "k"
    assert "testnet" in s.credentials.base_url and s.credentials.public_base_url == s.credentials.base_url


def test_warnings_for_aggressive_settings():
    s = settings_from_dict({"risk": {"risk_per_trade_pct": 3}, "execution": {"leverage": 50}, "timeframe": "1m",
                            "strategy": {"trend_pullback": {"htf_interval": "15m"}}})
    warnings = validate(s)
    assert any("aggressive" in w for w in warnings)
    assert any("leverage" in w for w in warnings)
    assert any("1m" in w for w in warnings)
