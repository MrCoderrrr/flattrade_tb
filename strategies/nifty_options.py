from dataclasses import dataclass
from datetime import datetime
from core_engine.config import DEFAULT_CONFIG, EngineConfig
from core_engine.models import Order, Side, TriggerType, Position


@dataclass
class NiftyOptionsStrategy:
    config: EngineConfig = DEFAULT_CONFIG
    state: str = "FLAT"
    position: Position | None = None
    _pending_signal: int = 0
    _signal_since: datetime | None = None

    def dte_profile(self, dte: int) -> float:
        if dte <= 1:
            return self.config.nifty_dte_profiles[2]
        if dte <= 3:
            return self.config.nifty_dte_profiles[1]
        return self.config.nifty_dte_profiles[0]

    def dte_bucket(self, dte: int) -> str:
        return "LOW" if dte <= 1 else ("MID" if dte <= 3 else "HIGH")

    def persistence_seconds(self, dte: int) -> float:
        return self.config.nifty_low_dte_persistence if dte <= 1 else self.config.nifty_high_dte_persistence

    def confirm_signal(self, now: datetime, dte: int, signal: int) -> bool:
        """Require P+5 seconds for normal expiry buckets; low DTE is fixed 5s."""
        if signal == 0:
            self._pending_signal, self._signal_since = 0, None
            return False
        if signal != self._pending_signal:
            self._pending_signal, self._signal_since = signal, now
            return False
        return self._signal_since is not None and (
            now - self._signal_since).total_seconds() >= self.persistence_seconds(dte)

    def k(self, adx_value: float, decelerating: bool = False) -> float:
        if decelerating:
            return self.config.nifty_deceleration_k
        return self.config.nifty_trend_k if adx_value > self.config.nifty_adx_threshold else self.config.nifty_recenter_k

    def management_action(self, now: datetime, dte: int, adx_value: float,
                          decelerating: bool = False) -> str:
        """Return the permitted action for the current market regime."""
        if dte <= 1 and now.strftime("%H:%M") >= self.config.nifty_low_dte_cutoff:
            return "HOLD"  # no recenter/flip after 14:00
        if adx_value > self.config.nifty_adx_threshold:
            return "FLAT"  # strong trend: do not add/re-center
        return "DECELERATE" if decelerating else "RE_CENTER"

    def size(self, base_quantity: int, iv_rank: float | None, dte: int = 5) -> int:
        """Apply DTE sizing, then halve it when IVR is below the 20th percentile."""
        quantity = max(1, int(base_quantity * self.dte_profile(dte)))
        if iv_rank is not None and iv_rank < 20.0:
            quantity = max(1, int(quantity * self.config.ivr_size_factor))
        return quantity

    def ivr_allows_entry(self, ivr20: float | None) -> bool:
        """IVR20 gate: elevated IV is allowed only at reduced size."""
        return ivr20 is None or ivr20 < 100.0

    def losing_leg_cut(self, call_pnl: float, put_pnl: float, threshold: float) -> str | None:
        if call_pnl <= -abs(threshold) and call_pnl <= put_pnl:
            return "CALL"
        if put_pnl <= -abs(threshold):
            return "PUT"
        return None

    def proactive_cut(self, pnl: float, threshold: float) -> bool:
        return pnl <= -abs(threshold)

    def enter(self, symbol: str, quantity: int, side: Side) -> Order:
        self.state, self.position = "OPEN", Position(symbol)
        return Order(symbol, side, self.size(quantity, None), trigger_type=TriggerType.RE_CENTER,
                     strategy="NiftyOptions")

    def manage(self, pnl: float, threshold: float, *, recenter: bool = False) -> TriggerType | None:
        if self.state != "OPEN":
            return None
        if self.proactive_cut(pnl, threshold):
            self.state = "FLAT"; return TriggerType.PROACTIVE_CUT
        if recenter:
            return TriggerType.RE_CENTER
        return None

    def should_flatten(self, now: datetime, dte: int) -> bool:
        hhmm = now.strftime("%H:%M")
        return hhmm >= self.config.nifty_flatten_time
