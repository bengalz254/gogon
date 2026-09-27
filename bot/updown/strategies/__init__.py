from bot.updown.strategies.base import Quote, QuoteView, StrategyContext, Take, UpDownStrategy
from bot.updown.strategies.cheap_asymmetric import CheapAsymmetricStrategy
from bot.updown.strategies.constellation import ConstellationStrategy
from bot.updown.strategies.fair_value import FairValueStrategy
from bot.updown.strategies.late_certainty import LateCertaintyMaker
from bot.updown.strategies.pair_barbell import PairBarbellStrategy

# Priority order: when several strategies act in the same window on the
# same step, earlier ones get first claim on the risk budget.
STRATEGY_CLASSES = {
    "fair_value": FairValueStrategy,
    "late_certainty": LateCertaintyMaker,
    "constellation": ConstellationStrategy,
    "pair_barbell": PairBarbellStrategy,
    "cheap_asymmetric": CheapAsymmetricStrategy,
}


def build_strategies(strategies_cfg) -> list[UpDownStrategy]:
    out = []
    for name, cls in STRATEGY_CLASSES.items():
        cfg = getattr(strategies_cfg, name)
        if cfg.enabled:
            out.append(cls(cfg))
    return out


__all__ = [
    "Quote", "QuoteView", "StrategyContext", "Take", "UpDownStrategy", "STRATEGY_CLASSES",
    "build_strategies", "FairValueStrategy", "LateCertaintyMaker", "ConstellationStrategy",
    "PairBarbellStrategy", "CheapAsymmetricStrategy",
]
