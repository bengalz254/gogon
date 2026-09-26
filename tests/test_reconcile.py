import bot.reconcile as reconcile_mod
from bot.reconcile import diff_positions, parse_positions


def test_parse_positions_handles_payload_variants():
    payload = [
        {"asset": "111", "size": 10.5},
        {"asset": "111", "size": "1.5"},  # same token twice -> summed
        {"asset_id": "222", "size": 3},
        {"asset": "333", "size": 0},  # empty -> dropped
        {"asset": "444", "size": "n/a"},  # unparseable -> dropped
        {"asset": "555", "size": 9, "redeemable": True},  # resolved, awaiting redemption -> dropped
        "garbage",
    ]
    assert parse_positions(payload) == {"111": 12.0, "222": 3.0}
    assert parse_positions({"data": [{"asset": "1", "size": 2}]}) == {"1": 2.0}
    assert parse_positions(None) == {}


def test_diff_flags_real_drift_only():
    local = {"a": 10.0, "b": 20.0, "c": 5.0}
    # a: small fee-sized gap (fine), b: clearly wrong, c: missing, d: unknown to the bot
    remote = {"a": 9.65, "b": 12.0, "d": 7.0}
    mismatches = diff_positions(local, remote)
    text = "\n".join(mismatches)
    assert len(mismatches) == 3
    assert "bot 20.00 vs exchange 12.00" in text
    assert "bot 5.00 vs exchange 0.00" in text
    assert "bot 0.00 vs exchange 7.00" in text


def test_reconcile_returns_none_when_the_api_is_unreachable(monkeypatch):
    def unreachable(*args, **kwargs):
        raise reconcile_mod.requests.ConnectionError("down")

    monkeypatch.setattr(reconcile_mod.requests, "get", unreachable)
    assert reconcile_mod.reconcile("0xabc", {"a": 1.0}) is None
