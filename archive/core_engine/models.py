"""Typed value objects shared by strategies, adapters, and risk controls."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class TriggerType(str, Enum):
    PROACTIVE_CUT = "PROACTIVE_CUT"
    RE_CENTER = "RE_CENTER"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    TRAILING_STOP_HIT = "TRAILING_STOP_HIT"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    iv: Optional[float] = None


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: int
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    trigger_type: TriggerType = TriggerType.RE_CENTER
    strategy: str = ""
    client_order_id: str = ""


@dataclass
class Trade:
    order: Order
    fill_price: float
    timestamp: datetime
    order_id: str


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    average_price: float = 0.0
    realized_pnl: float = 0.0
    entry_side: Optional[Side] = None
    metadata: dict = field(default_factory=dict)

    def apply(self, side: Side, quantity: int, price: float) -> float:
        signed = quantity if side is Side.BUY else -quantity
        pnl = 0.0
        if self.quantity and (self.quantity > 0) != (signed > 0):
            closed = min(abs(self.quantity), abs(signed))
            pnl = closed * (price - self.average_price) * (1 if self.quantity > 0 else -1)
            self.realized_pnl += pnl
        new_quantity = self.quantity + signed
        if new_quantity == 0:
            self.average_price = 0.0
            self.entry_side = None
        elif self.quantity and (self.quantity > 0) != (signed > 0):
            # A reversal opens only the residual quantity at the fill price.
            self.average_price = price
            self.entry_side = Side.BUY if new_quantity > 0 else Side.SELL
        elif not self.quantity or (self.quantity > 0) == (signed > 0):
            total = abs(self.quantity) * self.average_price + abs(signed) * price
            self.average_price = total / abs(new_quantity)
            self.entry_side = Side.BUY if new_quantity > 0 else Side.SELL
        self.quantity = new_quantity
        return pnl
