from bot.config import ArbitrageConfig, FeeConfig, RiskConfig
from bot.fees import FeeModel
from bot.market_data import BookLevel, MarketInfo, TokenInfo
from bot.risk import RiskManager
from bot.strategies.arbitrage import ArbitrageStrategy, guaranteed_profit_per_set


def make_market(tags=None):
    return MarketInfo(
        condition_id="mkt1",
        question="Will X happen?",
        tokens=[TokenInfo(token_id="tokYES", outcome="YES"), TokenInfo(token_id="tokNO", outcome="NO")],
        active=True,
        closed=False,
        tags=list(tags or []),
    )


def make_strategy(
    min_edge=0.015, fee_buffer=0.005, max_position_usd=100.0, max_total_exposure_usd=100.0, fees=None
):
    cfg = ArbitrageConfig(enabled=True, min_edge=min_edge, fee_buffer=fee_buffer)
    risk = RiskManager(
        RiskConfig(
            max_position_usd=max_position_usd,
            max_total_exposure_usd=max_total_exposure_usd,
            max_daily_loss_usd=1000.0,
            min_order_size_usd=1.0,
        )
    )
    return ArbitrageStrategy(cfg, risk, fees or FeeModel.zero()), risk


def default_fees():
    return FeeModel(FeeConfig())  # the shipped Sept 2026 schedule


def test_no_signal_when_no_edge():
    strat, _ = make_strategy()
    market = make_market()

    books = {
        "tokYES": BookLevel(best_bid=0.50, best_ask=0.51, best_bid_size=100, best_ask_size=100),
        "tokNO": BookLevel(best_bid=0.48, best_ask=0.50, best_bid_size=100, best_ask_size=100),
    }
    # combined ask = 1.01 -> no arbitrage
    signals = strat.generate_signals(market, lambda tid: books[tid])
    assert signals == []


def test_signal_when_combined_ask_below_one():
    strat, _ = make_strategy(min_edge=0.015, fee_buffer=0.005)
    market = make_market()

    # combined ask = 0.96 -> edge = 1 - 0.96 - 0.005 = 0.035 >= 0.015
    books = {
        "tokYES": BookLevel(best_bid=0.45, best_ask=0.47, best_bid_size=50, best_ask_size=50),
        "tokNO": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=50, best_ask_size=50),
    }
    signals = strat.generate_signals(market, lambda tid: books[tid])
    assert len(signals) == 2

    yes_sig = next(s for s in signals if s.outcome == "YES")
    no_sig = next(s for s in signals if s.outcome == "NO")

    assert yes_sig.side == "BUY"
    assert no_sig.side == "BUY"
    assert yes_sig.group_id == no_sig.group_id
    # equal share counts on both legs (required for true arbitrage)
    assert abs(yes_sig.size_shares - no_sig.size_shares) < 1e-9


def test_size_limited_by_liquidity():
    strat, risk = make_strategy(max_position_usd=1000.0, max_total_exposure_usd=1000.0)
    market = make_market()

    books = {
        "tokYES": BookLevel(best_bid=0.45, best_ask=0.47, best_bid_size=3, best_ask_size=3),
        "tokNO": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=500, best_ask_size=500),
    }
    signals = strat.generate_signals(market, lambda tid: books[tid])
    assert len(signals) == 2
    for s in signals:
        assert abs(s.size_shares - 3.0) < 1e-9  # capped by the thinner (YES) book


def test_size_limited_by_risk_budget():
    strat, risk = make_strategy(max_position_usd=5.0, max_total_exposure_usd=5.0)
    market = make_market()

    books = {
        "tokYES": BookLevel(best_bid=0.45, best_ask=0.47, best_bid_size=1000, best_ask_size=1000),
        "tokNO": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=1000, best_ask_size=1000),
    }
    combined_ask = 0.47 + 0.49
    signals = strat.generate_signals(market, lambda tid: books[tid])
    assert len(signals) == 2
    total_cost = sum(s.size_usd for s in signals)
    assert total_cost <= 5.0 + 1e-6
    # 5.0 / 0.96 = 5.2083..., rounded down to the CLOB's 0.01-share precision
    assert abs(signals[0].size_shares - 5.20) < 1e-9
    assert signals[0].size_shares <= 5.0 / combined_ask


def test_no_signal_for_non_binary_market():
    strat, _ = make_strategy()
    market = MarketInfo(
        condition_id="mkt2",
        question="Multi-outcome market",
        tokens=[TokenInfo(token_id="a", outcome="A")],
        active=True,
        closed=False,
    )
    signals = strat.generate_signals(market, lambda tid: BookLevel(0.5, 0.5, 10, 10))
    assert signals == []


def test_disabled_strategy_returns_nothing():
    cfg = ArbitrageConfig(enabled=False, min_edge=0.0, fee_buffer=0.0)
    risk = RiskManager(RiskConfig(max_position_usd=100, max_total_exposure_usd=100, max_daily_loss_usd=100, min_order_size_usd=1))
    strat = ArbitrageStrategy(cfg, risk, FeeModel.zero())
    market = make_market()
    books = {
        "tokYES": BookLevel(0.1, 0.2, 100, 100),
        "tokNO": BookLevel(0.1, 0.2, 100, 100),
    }
    assert strat.generate_signals(market, lambda tid: books[tid]) == []


# -- fees ------------------------------------------------------------------


def test_fees_turn_a_two_cent_discount_into_no_trade_in_crypto():
    # combined ask 0.98 clears the old fee-less 2c trigger, but crypto taker
    # fees (0.07 * 0.49 * 0.51 per share, per leg) are ~3.5c per set.
    strat, _ = make_strategy(fees=default_fees())
    books = {
        "tokYES": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=100, best_ask_size=100),
        "tokNO": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=100, best_ask_size=100),
    }
    assert strat.generate_signals(make_market(tags=["Crypto"]), lambda tid: books[tid]) == []


def test_fee_free_category_still_trades():
    strat, _ = make_strategy(fees=default_fees())
    books = {
        "tokYES": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=100, best_ask_size=100),
        "tokNO": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=100, best_ask_size=100),
    }
    signals = strat.generate_signals(make_market(tags=["Geopolitics"]), lambda tid: books[tid])
    assert len(signals) == 2
    assert all(s.fee_usd == 0.0 for s in signals)


def test_untagged_market_is_costed_at_the_conservative_default_rate():
    # 0.96 combined clears a 1c min edge at the 0.04 politics rate (~1.9c
    # guaranteed), but an unknown market is charged the highest rate (0.07,
    # ~0.4c left): no trade.
    strat, _ = make_strategy(min_edge=0.01, fee_buffer=0.0, fees=default_fees())
    books = {
        "tokYES": BookLevel(best_bid=0.46, best_ask=0.48, best_bid_size=100, best_ask_size=100),
        "tokNO": BookLevel(best_bid=0.46, best_ask=0.48, best_bid_size=100, best_ask_size=100),
    }
    assert strat.generate_signals(make_market(tags=["Politics"]), lambda tid: books[tid]) != []
    assert strat.generate_signals(make_market(tags=[]), lambda tid: books[tid]) == []


def test_signals_carry_fees_and_budget_covers_them():
    strat, _ = make_strategy(max_position_usd=10.0, max_total_exposure_usd=10.0, fees=default_fees())
    books = {
        "tokYES": BookLevel(best_bid=0.40, best_ask=0.42, best_bid_size=1000, best_ask_size=1000),
        "tokNO": BookLevel(best_bid=0.45, best_ask=0.47, best_bid_size=1000, best_ask_size=1000),
    }
    signals = strat.generate_signals(make_market(tags=["Politics"]), lambda tid: books[tid])
    assert len(signals) == 2
    for s in signals:
        assert abs(s.fee_usd - s.size_shares * 0.04 * s.limit_price * (1 - s.limit_price)) < 1e-9
        assert s.outcome_count == 2
    assert sum(s.size_usd + s.fee_usd for s in signals) <= 10.0 + 1e-9


def test_thinner_book_leg_comes_first():
    strat, _ = make_strategy()
    books = {
        "tokYES": BookLevel(best_bid=0.45, best_ask=0.47, best_bid_size=500, best_ask_size=500),
        "tokNO": BookLevel(best_bid=0.47, best_ask=0.49, best_bid_size=40, best_ask_size=40),
    }
    signals = strat.generate_signals(make_market(), lambda tid: books[tid])
    assert [s.outcome for s in signals] == ["NO", "YES"]


def test_guaranteed_profit_takes_the_worse_fee_collection_model():
    # Balanced prices: both models agree closely.
    balanced = guaranteed_profit_per_set([0.49, 0.49], 0.07)
    assert abs(balanced - (1 - 0.98 - 0.07 * 0.51)) < 1e-12  # fee-in-shares is the lower one
    # Lopsided prices: taking the fee in shares shrinks the cheap leg a lot,
    # so the guaranteed payout is much lower than the USD-fee view suggests.
    lopsided = guaranteed_profit_per_set([0.95, 0.03], 0.07)
    usd_view = 1 - 0.98 - 0.07 * (0.95 * 0.05 + 0.03 * 0.97)
    assert usd_view > 0 > lopsided
    # No fees: plain 1 - sum of prices.
    assert abs(guaranteed_profit_per_set([0.47, 0.49], 0.0) - 0.04) < 1e-12


def test_no_signal_when_the_cheap_leg_is_below_the_minimum_order():
    strat, _ = make_strategy(max_position_usd=20.0, max_total_exposure_usd=20.0)
    books = {
        "tokYES": BookLevel(best_bid=0.90, best_ask=0.93, best_bid_size=1000, best_ask_size=1000),
        "tokNO": BookLevel(best_bid=0.01, best_ask=0.02, best_bid_size=1000, best_ask_size=1000),
    }
    # 5c discount, but $20 buys ~21 sets: the NO leg would be a $0.42 order
    assert strat.generate_signals(make_market(), lambda tid: books[tid]) == []
