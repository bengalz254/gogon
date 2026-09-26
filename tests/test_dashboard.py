import importlib.util
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "dashboard.py")
_spec = importlib.util.spec_from_file_location("dashboard", _PATH)
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)


def row(side, size, usd, fee, ts, token="tokYES"):
    return {
        "timestamp": ts, "mode": "paper", "strategy": "threshold", "market_id": "mkt1", "token_id": token,
        "outcome": "YES", "side": side, "price": "0", "size_shares": str(size), "size_usd": str(usd),
        "fee_usd": fee, "filled": "True", "group_id": "", "reason": "",
    }


def test_summary_counts_fees_in_pnl_and_exposure():
    rows = [
        row("BUY", 10, 4.0, "0.10", "2026-09-26T00:00:01"),
        row("SELL", 10, 6.0, "0.12", "2026-09-26T00:00:02"),
        row("BUY", 5, 2.0, "", "2026-09-26T00:00:03", token="tokOLD"),  # logged before fees existed
    ]
    summary = dashboard.build_summary(rows)
    assert abs(summary["realized_pnl_usd"] - (6.0 - 0.12 - 4.10)) < 1e-9
    assert abs(summary["total_fees_usd"] - 0.22) < 1e-9
    assert abs(summary["open_exposure_usd"] - 2.0) < 1e-9
