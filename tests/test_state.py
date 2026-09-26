from datetime import date, datetime, timezone

from bot.risk import Position, RiskState
from bot.state import StateStore, state_path

OPENED = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_empty_store_loads_nothing(tmp_path):
    assert StateStore(str(tmp_path / "state.sqlite3")).load() is None


def test_save_and_load_round_trip_across_reopen(tmp_path):
    path = str(tmp_path / "sub" / "state.sqlite3")  # parent folder is created
    state = RiskState(
        day=date(2026, 9, 26),
        realized_pnl_today=-3.25,
        positions=[
            Position("mkt1", "tokA", "YES", 10.0, 4.87, opened_at=OPENED, outcome_count=2),
            Position("mkt2", "tokB", "NO", 2.5, 1.0, opened_at=OPENED),
        ],
    )
    store = StateStore(path)
    store.save(state)
    store.close()

    assert StateStore(path).load() == state


def test_save_replaces_the_previous_state(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    store.save(RiskState(date(2026, 9, 26), 0.0, [Position("mkt1", "tokA", "YES", 1.0, 0.5, opened_at=OPENED)]))
    store.save(RiskState(date(2026, 9, 26), 1.5, []))
    loaded = store.load()
    assert loaded.positions == []
    assert loaded.realized_pnl_today == 1.5


def test_paper_and_live_state_are_kept_apart():
    assert state_path(live=False) != state_path(live=True)
