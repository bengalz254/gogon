import pytest

from bot.config import DEFAULT_CATEGORY_TAKER_RATES, load_settings


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("LIVE_TRADING", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def load(tmp_path, yaml_text):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml_text, encoding="utf-8")
    # A nonexistent env file keeps a developer's real .env out of the test.
    return load_settings(config_path=str(cfg), env_path=str(tmp_path / "no.env"))


def test_fee_and_notification_defaults(clean_env):
    settings = load(clean_env, "polling_interval_seconds: 5\n")
    assert settings.fees.default_taker_rate == 0.07
    assert settings.fees.category_taker_rates == DEFAULT_CATEGORY_TAKER_RATES
    assert settings.notifications.telegram_bot_token is None
    assert settings.notifications.fills is True
    assert settings.notifications.heartbeat_hours == 6.0


def test_fees_and_notifications_from_yaml_and_env(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    settings = load(
        clean_env,
        """
fees:
  default_taker_rate: 0.06
  category_taker_rates:
    crypto: 0.07
notifications:
  fills: false
  heartbeat_hours: 2
""",
    )
    assert settings.fees.default_taker_rate == 0.06
    # the YAML list replaces the built-in one
    assert settings.fees.category_taker_rates == {"crypto": 0.07}
    assert settings.notifications.telegram_bot_token == "123:abc"
    assert settings.notifications.telegram_chat_id == "42"
    assert settings.notifications.fills is False
    assert settings.notifications.heartbeat_hours == 2.0


def test_out_of_range_fee_rate_is_rejected(clean_env):
    with pytest.raises(ValueError, match="crypto"):
        load(clean_env, "fees:\n  category_taker_rates:\n    crypto: 7\n")
