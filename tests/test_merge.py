import importlib.util
import json
import os
import time
from types import SimpleNamespace

import bot.main as main_mod
from bot.config import MergeConfig, RiskConfig
from bot.merge import merge_market
from bot.risk import RiskManager

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "positions.py")
_spec = importlib.util.spec_from_file_location("positions_cli", _PATH)
positions_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(positions_cli)


class FakeJournal:
    def __init__(self):
        self.rows = []

    def record(self, signal, mode, filled):
        self.rows.append((signal, mode, filled))


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, state):
        self.saved.append(state)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, key=None, cooldown_s=0.0):
        self.sent.append((text, key))
        return True


def arb_risk():
    risk = RiskManager(RiskConfig())
    risk.record_open("mkt1", "yes", "YES", size=10.0, cost_usd=4.87, outcome_count=2)
    risk.record_open("mkt1", "no", "NO", size=10.0, cost_usd=5.07, outcome_count=2)
    return risk


def test_merge_market_journals_each_outcome_as_a_sell():
    risk, journal = arb_risk(), FakeJournal()
    merged, pnl = merge_market(risk, journal, "mkt1", "paper")
    assert merged == 10.0 and abs(pnl - 0.06) < 1e-9
    assert [(s.strategy, s.side, s.size_shares) for s, _, _ in journal.rows] == [("merge", "SELL", 10.0)] * 2
    assert abs(sum(s.size_usd for s, _, _ in journal.rows) - 10.0) < 1e-9
    assert risk.positions == {}


def settings(paper_auto_merge=True, live_alert_min_sets=5.0):
    return SimpleNamespace(merge=MergeConfig(paper_auto_merge=paper_auto_merge, live_alert_min_sets=live_alert_min_sets))


def test_paper_merges_automatically_live_only_alerts():
    risk, journal, store, notifier = arb_risk(), FakeJournal(), FakeStore(), FakeNotifier()
    main_mod._handle_complete_sets(settings(), risk, journal, store, notifier, "paper")
    assert risk.positions == {} and len(store.saved) == 1 and notifier.sent == []

    risk, journal, store, notifier = arb_risk(), FakeJournal(), FakeStore(), FakeNotifier()
    main_mod._handle_complete_sets(settings(), risk, journal, store, notifier, "live")
    assert len(risk.positions) == 2 and journal.rows == [] and store.saved == []
    assert len(notifier.sent) == 1
    text, key = notifier.sent[0]
    assert "positions.py --live merge mkt1" in text and key == "merge:mkt1"

    notifier = FakeNotifier()
    main_mod._handle_complete_sets(settings(live_alert_min_sets=50), risk, journal, store, notifier, "live")
    assert notifier.sent == []  # 10 sets is below the alert threshold


def use_tmp_root(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(positions_cli, "_ROOT", str(tmp_path))
    monkeypatch.setattr(positions_cli, "STATUS_PATH", str(tmp_path / "data" / "status.json"))


def seed_state(tmp_path, live=True):
    store, _ = positions_cli.load(live)
    store.save(arb_risk().snapshot())
    store.close()


def test_cli_lists_merges_and_closes(tmp_path, monkeypatch, capsys):
    use_tmp_root(monkeypatch, tmp_path)
    seed_state(tmp_path)

    assert positions_cli.main(["--live", "list"]) == 0
    assert "10.00 complete sets (mergeable)" in capsys.readouterr().out

    assert positions_cli.main(["--live", "merge", "mkt1", "--sets", "4"]) == 0
    _, risk = positions_cli.load(True)
    assert risk.positions["yes"].size == 6.0

    assert positions_cli.main(["--live", "close", "yes", "--price", "0.9"]) == 0
    _, risk = positions_cli.load(True)
    assert "yes" not in risk.positions
    trades = (tmp_path / "data" / "trades.csv").read_text(encoding="utf-8")
    assert trades.count(",merge,") == 2 and trades.count(",manual,") == 1


def test_cli_refuses_to_edit_while_the_bot_runs(tmp_path, monkeypatch):
    use_tmp_root(monkeypatch, tmp_path)
    seed_state(tmp_path)
    (tmp_path / "data" / "status.json").write_text(json.dumps({"updated_ts": time.time()}), encoding="utf-8")
    assert positions_cli.main(["--live", "merge", "mkt1"]) == 2
    _, risk = positions_cli.load(True)
    assert risk.positions["yes"].size == 10.0  # untouched
    assert positions_cli.main(["--live", "merge", "mkt1", "--force"]) == 0


def test_cli_flags_work_before_or_after_the_command(tmp_path, monkeypatch):
    use_tmp_root(monkeypatch, tmp_path)
    seed_state(tmp_path, live=True)
    assert positions_cli.main(["list", "--live"]) == 0
    assert positions_cli.main(["--live", "merge", "mkt1", "--sets", "1"]) == 0
    assert positions_cli.main(["merge", "mkt1", "--live", "--sets", "1"]) == 0
    _, risk = positions_cli.load(True)
    assert risk.positions["yes"].size == 8.0
    _, paper = positions_cli.load(False)
    assert paper.positions == {}  # --live never touched the paper state


def test_cli_edits_right_after_a_clean_stop(tmp_path, monkeypatch):
    use_tmp_root(monkeypatch, tmp_path)
    seed_state(tmp_path)
    status = tmp_path / "data" / "status.json"
    status.write_text(json.dumps({"updated_ts": time.time(), "running": False}), encoding="utf-8")
    assert positions_cli.main(["--live", "merge", "mkt1", "--sets", "1"]) == 0
    status.write_text(json.dumps({"updated_ts": time.time() - 3600, "running": True}), encoding="utf-8")
    assert positions_cli.main(["--live", "merge", "mkt1", "--sets", "1"]) == 0  # crashed long ago
