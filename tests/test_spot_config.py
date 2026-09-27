"""Loading and validating config/spot.yaml."""
import os

import pytest

from spot.config import ConfigError, load_settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SPOT_CONFIG_PATH"):
        monkeypatch.setenv(name, "")


def write(tmp_path, text):
    path = tmp_path / "spot.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def load(tmp_path, text):
    return load_settings(write(tmp_path, text), env_path=str(tmp_path / "missing.env"))


def test_the_shipped_config_loads_with_its_documented_defaults():
    s = load_settings(os.path.join(ROOT, "config", "spot.yaml"), env_path=os.devnull)
    assert s.symbol == "BTC/IDR" and (s.base, s.quote) == ("BTC", "IDR")
    assert s.grid.levels == 15 and s.grid.order_value == 100_000
    assert s.costs.sell_tax_pct == 0.21 and s.risk.stop_loss_pct == 10


def test_values_are_cast_and_symbol_upper_cased(tmp_path):
    s = load(tmp_path, "symbol: eth/idr\npoll_seconds: '30'\ngrid:\n  levels: 21.0\nnotifications:\n  fills: 'no'\n")
    assert s.symbol == "ETH/IDR"
    assert s.poll_seconds == 30.0
    assert s.grid.levels == 21 and isinstance(s.grid.levels, int)
    assert s.notifications.fills is False


def test_telegram_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " 123:abc ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    s = load(tmp_path, "symbol: BTC/IDR\n")
    assert (s.notifications.telegram_bot_token, s.notifications.telegram_chat_id) == ("123:abc", "42")


@pytest.mark.parametrize(
    "text, message",
    [
        ("symbol: BTCIDR\n", "BASE/QUOTE"),
        ("poll_seconds: 1\n", "poll_seconds"),
        ("grid:\n  levels: 2\n", "grid.levels"),
        ("grid:\n  lower_price: 100\n", "lower < upper"),
        ("grid:\n  lower_price: 120\n  upper_price: 100\n", "lower < upper"),
        ("grid:\n  range_pct: 80\n", "range_pct"),
        ("grid:\n  order_value: 1.000.000\n", "tanpa titik ribuan"),
        ("grid:\n  level: 10\n", "tidak dikenal"),
        ("costs:\n  sell_tax_pct: 21\n", "costs.sell_tax_pct"),
        ("risk:\n  paper_balance: 0\n", "paper_balance"),
        ("risk:\n  stop_loss_pct: 90\n", "stop_loss_pct"),
        ("notifications:\n  fills: maybe\n", "notifications.fills"),
        ("grid: 5\n", "'grid'"),
        ("mode: live\n", "tidak dikenal"),
    ],
)
def test_invalid_settings_are_explained(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, text)


def test_missing_and_broken_files(tmp_path):
    with pytest.raises(ConfigError, match="tidak ditemukan"):
        load_settings(str(tmp_path / "nope.yaml"), env_path=os.devnull)
    with pytest.raises(ConfigError, match="YAML"):
        load(tmp_path, "grid: [unclosed\n")
