import os

import pytest

from hlbot.backtest import resolve_maintenance_margin
from hlbot.config import load_hl_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_shipped_config(monkeypatch):
    monkeypatch.setenv("HL_LIVE_TRADING", "false")
    s = load_hl_settings(config_path=os.path.join(ROOT, "config", "hyperliquid.yaml"), env_path=os.devnull)
    t = s.trade
    assert (s.strategy.coin, s.strategy.interval, s.strategy.ema_fast, s.strategy.ema_slow) == ("SOL", "4h", 9, 21)
    assert t.leverage == 5 and t.pct_basis == "margin"
    assert t.margin_usd == 100 and t.notional_usd == 500
    assert t.take_profit_pct is None and t.tp_price_pct is None  # no TP: exit on opposite cross / SL
    assert t.stop_loss_pct == 0.3 and t.stop_loss_price_pct == pytest.approx(0.06)  # 30% of margin at 5x
    assert s.backtest.maintenance_margin_rate is None  # "auto"
    assert s.backtest.days == 800


def test_tp_null_with_fixed_mode_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HL_LIVE_TRADING", "false")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("trade:\n  take_profit_pct: null\n  exit_mode: fixed\n")
    with pytest.raises(ValueError):
        load_hl_settings(config_path=str(cfg), env_path=os.devnull)


def test_rejects_unknown_basis(tmp_path, monkeypatch):
    monkeypatch.setenv("HL_LIVE_TRADING", "false")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("trade:\n  pct_basis: roi\n")
    with pytest.raises(ValueError):
        load_hl_settings(config_path=str(cfg), env_path=os.devnull)


class FakeMetaInfo:
    def __init__(self, max_lev=None, error=None):
        self.max_lev = max_lev
        self.error = error

    def max_leverage(self, coin):
        if self.error:
            raise self.error
        return self.max_lev


def _settings(monkeypatch):
    monkeypatch.setenv("HL_LIVE_TRADING", "false")
    return load_hl_settings(config_path=os.path.join(ROOT, "config", "hyperliquid.yaml"), env_path=os.devnull)


def test_auto_maintenance_margin_from_max_leverage(monkeypatch):
    s = _settings(monkeypatch)
    resolve_maintenance_margin(s, FakeMetaInfo(max_lev=10))
    assert s.backtest.maintenance_margin_rate == pytest.approx(0.05)
    s = _settings(monkeypatch)
    resolve_maintenance_margin(s, FakeMetaInfo(max_lev=40))
    assert s.backtest.maintenance_margin_rate == pytest.approx(0.0125)


def test_leverage_above_coin_max_is_rejected(monkeypatch):
    s = _settings(monkeypatch)
    with pytest.raises(ValueError):
        resolve_maintenance_margin(s, FakeMetaInfo(max_lev=3))


def test_unlisted_coin_is_rejected(monkeypatch):
    s = _settings(monkeypatch)
    with pytest.raises(KeyError):
        resolve_maintenance_margin(s, FakeMetaInfo(error=KeyError("ZEC")))


def test_offline_falls_back_to_conservative_maintenance_margin(monkeypatch):
    s = _settings(monkeypatch)
    resolve_maintenance_margin(s, FakeMetaInfo(error=ConnectionError("offline")))
    assert s.backtest.maintenance_margin_rate == pytest.approx(1 / 10)  # as if max leverage were 5x
