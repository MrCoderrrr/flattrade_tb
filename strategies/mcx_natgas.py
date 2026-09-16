from dataclasses import dataclass
from datetime import datetime
from collections import deque
from core_engine.config import DEFAULT_CONFIG, EngineConfig
from core_engine.models import Order, Side, TriggerType, Quote, Position


@dataclass
class MCXNatGasState:
    direction: int = 0  # survivor: +1 CE, -1 PE
    flips: int = 0
    last_flip: datetime | None = None
    strangle_initialized: bool = False
    entry_price: float = 0.0
    best_price: float = 0.0
    spot_history: deque = None
    extreme_spot: float = 0.0
    trend_velocity: float = 0.0
    reversal_pullback: float = 0.0
    reversal_latched: bool = False

    def __post_init__(self):
        if self.spot_history is None:
            self.spot_history = deque(maxlen=60)


class MCXNatGasStrategy:
    """Survivor flip-flop state machine; signal generation is broker independent."""
    def __init__(self, config: EngineConfig = DEFAULT_CONFIG):
        self.config, self.state = config, MCXNatGasState()
        self.position = Position("MCX-NATGAS")

    def can_flip(self, now: datetime) -> bool:
        return self.state.flips < self.config.mcx_max_flips and (
            self.state.last_flip is None or (now - self.state.last_flip).total_seconds() >= self.config.mcx_flip_cooldown_seconds)

    def flip(self, now: datetime, direction: int) -> bool:
        if direction not in (-1, 1) or not self.can_flip(now):
            return False
        self.state.direction, self.state.flips, self.state.last_flip = direction, self.state.flips + 1, now
        return True

    def initialize_strangle(self, now: datetime) -> None:
        """The two ATM legs are opened once; only a survivor may be active."""
        if self.state.strangle_initialized:
            return
        self.state.strangle_initialized = True
        self.state.last_flip = now

    def initial_orders(self, ce_symbol: str, pe_symbol: str, quantity: int) -> list[Order]:
        """Return the one-time ATM CE+PE entry (the survivor is selected later)."""
        if self.state.strangle_initialized:
            return []
        return [Order(ce_symbol, Side.SELL, quantity, strategy="MCXNatGas",
                      trigger_type=TriggerType.RE_CENTER),
                Order(pe_symbol, Side.SELL, quantity, strategy="MCXNatGas",
                      trigger_type=TriggerType.RE_CENTER)]

    def survivor_stop(self, entry_price: float, direction: int = 1) -> float:
        return entry_price * (1.0 - self.stop_loss_fraction if direction > 0 else 1.0 + self.stop_loss_fraction)

    def chandelier_stop(self, highest_price: float, atr_value: float) -> float:
        return highest_price - self.volatility_k * atr_value

    def opposite_signal(self, now: datetime, direction: int, confirmed: bool) -> bool:
        return bool(confirmed and self.state.direction and direction == -self.state.direction
                    and self.can_flip(now))

    def confirm_flip(self, now: datetime, direction: int, confirmed: bool) -> bool:
        if not self.opposite_signal(now, direction, confirmed):
            return False
        return self.flip(now, direction)

    def order(self, symbol: str, quantity: int, side: Side,
              trigger: TriggerType = TriggerType.RE_CENTER) -> Order:
        if self.state.direction == 0 and trigger != TriggerType.RE_CENTER:
            raise RuntimeError("cannot manage an uninitialized MCX position")
        return Order(symbol, side, quantity, trigger_type=trigger, strategy="MCXNatGas")

    def on_price(self, now: datetime, price: float, stop: float, trailing_stop: float | None = None) -> str | None:
        if self.position.quantity == 0:
            return None
        adverse = (self.position.quantity > 0 and price <= stop) or (self.position.quantity < 0 and price >= stop)
        trailing = trailing_stop is not None and ((self.position.quantity > 0 and price <= trailing_stop) or (self.position.quantity < 0 and price >= trailing_stop))
        if adverse: return TriggerType.STOP_LOSS_HIT.value
        if trailing: return TriggerType.TRAILING_STOP_HIT.value
        return None

    @property
    def stop_loss_fraction(self): return self.config.mcx_stop_loss_pct

    @property
    def volatility_k(self): return self.config.mcx_k

    @property
    def loss_limit(self) -> float:
        return self.config.account_reference * self.config.mcx_loss_limit_pct

    def reset_session(self) -> None:
        self.state = MCXNatGasState()
        self.position = Position("MCX-NATGAS")

    def update_momentum(self, spot: float) -> bool:
        """Latch a 0.80-point pullback, or 0.50-point pullback with reversal velocity."""
        self.state.spot_history.append(spot)
        if self.state.direction == 0:
            return False
        history = list(self.state.spot_history)
        if len(history) >= self.config.mcx_velocity_ticks:
            self.state.trend_velocity = history[-1] - history[-self.config.mcx_velocity_ticks]
        if self.state.extreme_spot <= 0:
            self.state.extreme_spot = spot
        if self.state.direction > 0:
            self.state.extreme_spot = max(self.state.extreme_spot, spot)
            self.state.reversal_pullback = self.state.extreme_spot - spot
            reversing = self.state.trend_velocity < 0
        else:
            self.state.extreme_spot = min(self.state.extreme_spot, spot)
            self.state.reversal_pullback = spot - self.state.extreme_spot
            reversing = self.state.trend_velocity > 0
        self.state.reversal_latched = (
            self.state.reversal_pullback >= self.config.mcx_reversal_min_points
            or (self.state.reversal_pullback >= self.config.mcx_micro_reversal_points and reversing)
        )
        return self.state.reversal_latched

    def consume_momentum_reversal(self) -> None:
        self.state.reversal_latched = False
        self.state.extreme_spot = 0.0
        self.state.reversal_pullback = 0.0
