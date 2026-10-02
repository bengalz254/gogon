from __future__ import annotations

from dataclasses import dataclass

OPEN = "open"
SAFETY = "safety_order"
TAKE_PROFIT = "take_profit"
STOP_LOSS = "stop_loss"
EXTERNAL_CLOSE = "closed_external"


@dataclass
class Event:
    kind: str
    price: float
    qty: float
    reason: str = ""
