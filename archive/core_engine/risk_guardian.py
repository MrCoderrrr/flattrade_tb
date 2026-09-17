"""Account-level circuit breaker and data-quality guard."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from .config import DEFAULT_CONFIG, EngineConfig


@dataclass
class RiskGuardian:
    config: EngineConfig = DEFAULT_CONFIG
    halted: bool = False
    _loss_halted: bool = False
    _stale_since: datetime | None = None
    _recovery_ticks: int = 0
    _feeds: dict = None
    _ivr: list = None

    def __post_init__(self):
        self._feeds = {}
        self._ivr = []

    @property
    def loss_limit(self) -> float:
        return self.config.account_reference * self.config.combined_loss_limit_pct

    def check_pnl(self, combined_realized_pnl: float) -> bool:
        if combined_realized_pnl <= -self.loss_limit:
            self._loss_halted = self.halted = True
        return not self.halted

    def check_quote(self, now: datetime, quote_timestamp: datetime, feed: str = "default") -> bool:
        stale = (now - quote_timestamp).total_seconds() > self.config.stale_quote_seconds
        state = self._feeds.setdefault(feed, {"stale_since": None, "ticks": 0, "paused": False})
        if stale:
            state["stale_since"], state["ticks"], state["paused"] = state["stale_since"] or now, 0, True
        elif state["paused"]:
            # Recovery starts only after one full second of fresh data and must
            # contain ten consecutive ticks. Any stale tick resets the streak.
            if (now - quote_timestamp).total_seconds() <= self.config.recovery_seconds:
                state["ticks"] += 1
                if state["ticks"] >= self.config.recovery_ticks:
                    state.update(stale_since=None, ticks=0, paused=False)
            else:
                state["ticks"] = 0
        self.halted = self._loss_halted or any(x["paused"] for x in self._feeds.values())
        return not (self._loss_halted or state["paused"])

    def update_ivr(self, iv: float) -> float | None:
        self._ivr.append(float(iv))
        self._ivr = self._ivr[-self.config.ivr_window:]
        if len(self._ivr) < 2:
            return None
        lo, hi = min(self._ivr), max(self._ivr)
        return 0.0 if hi == lo else 100.0 * (self._ivr[-1] - lo) / (hi - lo)

    @property
    def ivr20(self) -> float | None:
        if len(self._ivr) < 2:
            return None
        lo, hi = min(self._ivr), max(self._ivr)
        return 0.0 if hi == lo else 100.0 * (self._ivr[-1] - lo) / (hi - lo)
