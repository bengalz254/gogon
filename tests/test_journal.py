import csv

from bot.journal import FIELDS, TradeJournal
from bot.strategies.base import Signal

OLD_FIELDS = [f for f in FIELDS if f != "fee_usd"]


def make_signal():
    return Signal(
        strategy="arbitrage",
        market_id="mkt1",
        token_id="tokYES",
        outcome="YES",
        side="BUY",
        limit_price=0.47,
        size_shares=10,
        size_usd=4.7,
        reason="test",
        fee_usd=0.17,
    )


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_new_journal_records_the_fee(tmp_path):
    path = tmp_path / "trades.csv"
    TradeJournal(str(path)).record(make_signal(), mode="paper", filled=True)
    assert read_rows(path)[0]["fee_usd"] == "0.1700"


def test_journal_from_an_older_version_is_upgraded_in_place(tmp_path):
    path = tmp_path / "trades.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(OLD_FIELDS)
        writer.writerow(
            ["2026-09-01T00:00:00+00:00", "paper", "arbitrage", "mkt1", "tokYES", "YES", "BUY",
             "0.4700", "10.0000", "4.7000", "True", "g1", "old row"]
        )

    TradeJournal(str(path)).record(make_signal(), mode="paper", filled=True)

    rows = read_rows(path)
    assert list(rows[0].keys()) == FIELDS
    assert rows[0]["reason"] == "old row"
    assert rows[0]["fee_usd"] == ""
    assert rows[1]["fee_usd"] == "0.1700"


def test_journal_with_unknown_columns_is_set_aside_not_overwritten(tmp_path):
    path = tmp_path / "trades.csv"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    TradeJournal(str(path))
    assert read_rows(path) == []
    backups = list(tmp_path.glob("trades.csv.bak.*"))
    assert len(backups) == 1
    assert "foo,bar" in backups[0].read_text(encoding="utf-8")
