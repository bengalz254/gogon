from bot.config import FeeConfig, RiskConfig, ThresholdConfig
from bot.fees import FeeModel
from bot.market_data import BookLevel, MarketInfo, TokenInfo
from bot.risk import RiskManager
from bot.strategies.threshold import ThresholdStrategy


def make_market():
    return MarketInfo(
        condition_id="mkt1",
        question="Will X happen?",
        tokens=[TokenInfo(token_id="tokYES", outcome="YES"), TokenInfo(token_id="tokNO", outcome="NO")],
        active=True,
        closed=False,
    )


def make_strategy(lookback=5, buy_drop_pct=0.1, sell_rise_pct=0.1, fees=None):
    cfg = ThresholdConfig(
        enabled=True, lookback_ticks=lookback, buy_drop_pct=buy_drop_pct, sell_rise_pct=sell_rise_pct
    )
    risk = RiskManager(
        RiskConfig(max_position_usd=100.0, max_total_exposure_usd=100.0, max_daily_loss_usd=100.0, min_order_size_usd=1.0)
    )
    return ThresholdStrategy(cfg, risk, fees or FeeModel.zero()), risk


def test_no_signal_without_enough_history():
    strat, _ = make_strategy()
    market = make_market()
    book = {"tokYES": BookLevel(0.50, 0.51, 100, 100), "tokNO": BookLevel(0.49, 0.50, 100, 100)}
    signals = strat.generate_signals(market, lambda tid: book[tid])
    assert signals == []  # first sample just seeds history, avg == mid


def test_buy_signal_on_price_drop():
    strat, _ = make_strategy(lookback=3, buy_drop_pct=0.1)
    market = make_market()

    stable_book = {"tokYES": BookLevel(0.60, 0.61, 100, 100), "tokNO": BookLevel(0.39, 0.40, 100, 100)}
    for _ in range(3):
        strat.generate_signals(market, lambda tid: stable_book[tid])

    # avg mid for YES ~ 0.605; now drop far below that
    dropped_book = {"tokYES": BookLevel(0.40, 0.41, 100, 100), "tokNO": BookLevel(0.39, 0.40, 100, 100)}
    signals = strat.generate_signals(market, lambda tid: dropped_book[tid])

    buys = [s for s in signals if s.side == "BUY" and s.outcome == "YES"]
    assert len(buys) == 1
    assert buys[0].limit_price == 0.41


def test_sell_signal_on_price_rise_when_holding_position():
    strat, risk = make_strategy(lookback=3, sell_rise_pct=0.1)
    market = make_market()
    risk.record_open("mkt1", "tokYES", "YES", size=10.0, cost_usd=4.0)

    stable_book = {"tokYES": BookLevel(0.40, 0.41, 100, 100), "tokNO": BookLevel(0.59, 0.60, 100, 100)}
    for _ in range(3):
        strat.generate_signals(market, lambda tid: stable_book[tid])

    risen_book = {"tokYES": BookLevel(0.60, 0.61, 100, 100), "tokNO": BookLevel(0.39, 0.40, 100, 100)}
    signals = strat.generate_signals(market, lambda tid: risen_book[tid])

    sells = [s for s in signals if s.side == "SELL" and s.outcome == "YES"]
    assert len(sells) == 1
    assert sells[0].size_shares == 10.0


def test_disabled_strategy_returns_nothing():
    cfg = ThresholdConfig(enabled=False, lookback_ticks=5, buy_drop_pct=0.1, sell_rise_pct=0.1)
    risk = RiskManager(RiskConfig(max_position_usd=100, max_total_exposure_usd=100, max_daily_loss_usd=100, min_order_size_usd=1))
    strat = ThresholdStrategy(cfg, risk, FeeModel.zero())
    market = make_market()
    book = {"tokYES": BookLevel(0.1, 0.2, 100, 100), "tokNO": BookLevel(0.8, 0.9, 100, 100)}
    assert strat.generate_signals(market, lambda tid: book[tid]) == []


def test_buy_size_leaves_room_for_the_taker_fee():
    strat, risk = make_strategy(lookback=3, buy_drop_pct=0.1, fees=FeeModel(FeeConfig()))
    market = make_market()  # untagged -> default 0.07 rate

    stable_book = {"tokYES": BookLevel(0.60, 0.61, 100, 100), "tokNO": BookLevel(0.39, 0.40, 100, 100)}
    for _ in range(3):
        strat.generate_signals(market, lambda tid: stable_book[tid])
    dropped_book = {"tokYES": BookLevel(0.40, 0.41, 1000, 1000), "tokNO": BookLevel(0.39, 0.40, 100, 100)}
    buy = next(s for s in strat.generate_signals(market, lambda tid: dropped_book[tid]) if s.side == "BUY")

    assert buy.fee_usd > 0
    assert buy.size_usd + buy.fee_usd <= risk.cfg.max_position_usd + 1e-9
    allowed, _ = risk.can_open(buy.market_id, buy.size_usd + buy.fee_usd)
    assert allowed
