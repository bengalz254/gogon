from bot.config import FeeConfig
from bot.fees import FeeModel, FeeSchedule, parse_fee_details, taker_fee_usd
from bot.market_data import MarketInfo, _parse_market


def market(tags):
    return MarketInfo(condition_id="mkt1", question="Will X happen?", tags=list(tags))


def test_fee_formula_matches_polymarket_examples():
    # 100 shares at 50c: crypto $1.75, sports $1.25, politics $1.00
    assert abs(taker_fee_usd(100, 0.5, 0.07) - 1.75) < 1e-9
    assert abs(taker_fee_usd(100, 0.5, 0.05) - 1.25) < 1e-9
    assert abs(taker_fee_usd(100, 0.5, 0.04) - 1.00) < 1e-9


def test_fee_is_symmetric_and_vanishes_at_the_extremes():
    assert abs(taker_fee_usd(100, 0.3, 0.07) - taker_fee_usd(100, 0.7, 0.07)) < 1e-12
    assert taker_fee_usd(100, 0.0, 0.07) == 0.0
    assert taker_fee_usd(100, 1.0, 0.07) == 0.0
    assert taker_fee_usd(0, 0.5, 0.07) == 0.0
    assert taker_fee_usd(100, 0.5, 0.0) == 0.0


def test_rate_from_tags_is_case_insensitive():
    fees = FeeModel(FeeConfig())
    assert fees.taker_rate(market(["CRYPTO"])) == 0.07
    assert fees.taker_rate(market([" politics "])) == 0.04
    assert fees.taker_rate(market(["Geopolitics"])) == 0.0


def test_highest_matching_rate_wins():
    fees = FeeModel(FeeConfig())
    assert fees.taker_rate(market(["Politics", "Sports"])) == 0.05
    # a fee-free tag never lowers the rate of a market that also has a fee category
    assert fees.taker_rate(market(["Geopolitics", "Crypto"])) == 0.07


def test_unknown_or_missing_tags_use_the_default_rate():
    fees = FeeModel(FeeConfig(default_taker_rate=0.07))
    assert fees.taker_rate(market([])) == 0.07
    assert fees.taker_rate(market(["Some New Category"])) == 0.07


def test_zero_model_charges_nothing():
    assert FeeModel.zero().taker_rate(market(["Crypto"])) == 0.0


def test_market_tags_parsed_from_strings_objects_and_category():
    raw = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "tokens": [{"token_id": "1", "outcome": "Yes"}, {"token_id": "2", "outcome": "No"}],
        "tags": ["Crypto", {"label": "Bitcoin", "slug": "bitcoin"}, 42, ""],
        "category": "Finance",
    }
    assert _parse_market(raw).tags == ["Crypto", "Bitcoin", "bitcoin", "Finance"]


def test_market_without_tags_parses_to_empty_list():
    assert _parse_market({"condition_id": "0xabc", "tokens": []}).tags == []


# -- fee terms from Polymarket's market info ------------------------------------


def test_parse_fee_details_reads_rate_exponent_and_maker_flag():
    sched = parse_fee_details({"t": [], "fd": {"r": 0.05, "e": 2, "to": False}})
    assert (sched.rate, sched.exponent, sched.taker_only, sched.source) == (0.05, 2.0, False, "api")
    assert parse_fee_details({"fd": {"r": 0.04}}).exponent == 1.0  # missing exponent = standard
    assert parse_fee_details({"fd": {"r": 0.04, "e": 0}}).exponent == 0.0  # explicit flat fee kept
    assert parse_fee_details({"t": []}) is None  # no fee block: fall back to config
    assert parse_fee_details({"fd": {"r": "bad"}}) is None
    assert parse_fee_details(None) is None


def test_exponent_and_maker_fee():
    sched = FeeSchedule(rate=0.08, exponent=2, taker_only=True)
    assert abs(sched.taker_fee(100, 0.5) - 100 * 0.08 * 0.25 ** 2) < 1e-12
    assert sched.maker_fee(100, 0.5) == 0.0
    assert FeeSchedule(rate=0.08, taker_only=False).maker_fee(100, 0.5) == 2.0


def test_api_fee_terms_win_over_tags_and_are_cached():
    calls = []

    def market_info(condition_id):
        calls.append(condition_id)
        return {"t": [], "fd": {"r": 0.03, "e": 1, "to": True}}

    fees = FeeModel(FeeConfig(), market_info=market_info)
    assert fees.schedule(market(["Crypto"])).rate == 0.03
    assert fees.schedule(market(["Crypto"])).rate == 0.03
    assert calls == ["mkt1"]  # second lookup served from cache


def test_api_failure_falls_back_to_config():
    def broken(condition_id):
        raise ConnectionError("down")

    fees = FeeModel(FeeConfig(), market_info=broken)
    sched = fees.schedule(market(["Sports"]))
    assert (sched.rate, sched.source) == (0.05, "tags")
    assert fees.schedule(market([])).source == "default"
