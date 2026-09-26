from bot.config import FeeConfig
from bot.fees import FeeModel, taker_fee_usd
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
