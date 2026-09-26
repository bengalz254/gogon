import logging

import pytest

from bot.notify import NotificationError, Notifier


def test_disabled_without_credentials():
    notifier = Notifier(None, None)
    assert not notifier.enabled
    assert notifier.send("hi") is False
    notifier.close()


def test_messages_are_delivered_in_order():
    sent = []
    notifier = Notifier("token", "chat", sender=sent.append)
    for i in range(3):
        notifier.send(f"m{i}")
    notifier.close()
    assert sent == ["m0", "m1", "m2"]


def test_repeated_key_is_throttled():
    sent = []
    notifier = Notifier("token", "chat", sender=sent.append)
    assert notifier.send("error", key="cycle_error", cooldown_s=60)
    assert not notifier.send("error again", key="cycle_error", cooldown_s=60)
    assert notifier.send("another problem", key="other", cooldown_s=60)
    notifier.close()
    assert sent == ["error", "another problem"]


def test_long_messages_are_truncated_to_telegram_limit():
    sent = []
    notifier = Notifier("token", "chat", sender=sent.append)
    notifier.send("x" * 5000)
    notifier.close()
    assert len(sent[0]) == 4000


def test_delivery_failures_are_logged_without_the_token(caplog):
    secret = "123456:SECRET-TOKEN"

    def failing_sender(text):
        raise ConnectionError(f"https://api.telegram.org/bot{secret}/sendMessage unreachable")

    notifier = Notifier(secret, "chat", sender=failing_sender)
    with caplog.at_level(logging.WARNING, logger="polybot.notify"):
        notifier.send("hello")
        notifier.close()
    assert "ConnectionError" in caplog.text
    assert secret not in caplog.text


def test_send_now_raises_when_not_configured():
    with pytest.raises(NotificationError):
        Notifier(None, None).send_now("hi")
