"""Side-effect-free market objects for the experimental strategy lab.

All timestamps are timezone-aware. ``Bar.timestamp`` is the OPEN of a five-minute
bar, not its close. Prices are per unit; quantities are exchange trading units,
not lots. Plan stop/profit/loss values are positive Indian-rupee amounts for the
whole plan. A stop is a trigger, never a guarantee of an execution price.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


@dataclass(frozen=True)
class Contract:
    symbol: str
    token: str
    exchange: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int
    tick_size: float


@dataclass(frozen=True)
class Quote:
    contract: Contract
    timestamp: datetime
    bid: float
    ask: float
    last: float
    bid_size: int = 0
    ask_size: int = 0


@dataclass(frozen=True)
class Leg:
    quote: Quote
    side: str
    quantity: int


@dataclass
class Plan:
    strategy: str
    legs: list[Leg]
    reason: str
    stop_loss: float
    take_profit: float
    max_loss: float | None
    direction: int = 0
