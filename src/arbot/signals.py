from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class QuantMeta:
    p: float
    sigma: float
    f_star: float
    kelly_fraction: float
    source: str = ""


@dataclass
class Signal:
    action: Literal["buy", "sell"]
    slug: str
    outcome: str
    reason: str
    amount_usd: float | None = None
    shares: float | None = None
    quant: QuantMeta | None = None
    order_type: Literal["fak", "limit"] = "fak"
    limit_price: float | None = None
    paper_fill_at_limit: bool = False
    partial_exit: bool = False
    ladder_multiple: float | None = None
    market_condition_id: str | None = None
