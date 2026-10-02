import os

import pytest

from hlbot.config import load_hl_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_shipped_config_uses_margin_basis(monkeypatch):
    monkeypatch.setenv("HL_LIVE_TRADING", "false")
    s = load_hl_settings(config_path=os.path.join(ROOT, "config", "hyperliquid.yaml"), env_path=os.devnull)
    t = s.trade
    assert (s.strategy.interval, s.strategy.ema_fast, s.strategy.ema_slow) == ("30m", 9, 21)
    assert t.leverage == 10 and t.pct_basis == "margin"
    assert t.tp_price_pct == pytest.approx(0.002)  # 2% of margin at 10x
    assert t.trailing_price_pct == pytest.approx(0.0005)  # 0.5% of margin at 10x
    assert s.backtest.intrabar == "conservative"


def test_rejects_unknown_basis(tmp_path, monkeypatch):
    monkeypatch.setenv("HL_LIVE_TRADING", "false")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("trade:\n  pct_basis: roi\n")
    with pytest.raises(ValueError):
        load_hl_settings(config_path=str(cfg), env_path=os.devnull)
