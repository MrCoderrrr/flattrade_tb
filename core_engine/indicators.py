"""Small deterministic streaming indicators; no third-party dependency required."""
from __future__ import annotations

import math
from collections import deque
from typing import Iterable, Optional
from .models import Bar


class ContinuousEMA:
    def __init__(self, half_life_seconds: float):
        self.half_life_seconds, self.value, self.timestamp = half_life_seconds, None, None

    def update(self, value: float, timestamp: float) -> float:
        if self.value is None or self.timestamp is None:
            self.value = value
        else:
            alpha = 1.0 - math.exp(-math.log(2) * max(0.0, timestamp - self.timestamp) / self.half_life_seconds)
            self.value += alpha * (value - self.value)
        self.timestamp = timestamp
        return self.value


class RollingVolatility:
    def __init__(self, window: int):
        self.values = deque(maxlen=window)

    def update(self, value: float) -> Optional[float]:
        self.values.append(float(value))
        if len(self.values) < 2:
            return None
        mean = sum(self.values) / len(self.values)
        return math.sqrt(sum((x - mean) ** 2 for x in self.values) / (len(self.values) - 1))


class AdaptivePersistence:
    """Map volatility to a persistence duration, bounded by the requested limits."""
    def __init__(self, minimum: float = 3.0, maximum: float = 30.0):
        self.minimum, self.maximum = minimum, maximum

    def seconds(self, volatility: float, reference: float) -> float:
        return max(self.minimum, min(self.maximum, self.raw(volatility)))

    @staticmethod
    def raw(volatility_ratio: float, minimum: float = 3.0, maximum: float = 30.0) -> float:
        """P_raw = 10/VR, clamped to the strategy's safe persistence bounds."""
        return max(minimum, min(maximum, 10.0 / max(float(volatility_ratio), 1e-12)))


def atr(bars: Iterable, period: int = 14) -> Optional[float]:
    bars = list(bars)
    if len(bars) < period + 1:
        return None
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
           for p, b in zip(bars[-period - 1:-1], bars[-period:])]
    return sum(trs) / period


def adx(bars: Iterable, period: int = 300) -> Optional[float]:
    bars = list(bars)
    if len(bars) < period + 1:
        return None
    highs, lows, closes = [b.high for b in bars], [b.low for b in bars], [b.close for b in bars]
    trs, plus, minus = [], [], []
    for i in range(-period, 0):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    tr = sum(trs) or 1e-12
    p, m = 100 * sum(plus) / tr, 100 * sum(minus) / tr
    return 100 * abs(p - m) / (p + m) if p + m else 0.0


class ContinuousIndicatorEngine:
    """Deterministic streaming indicator pipeline.

    Quotes are sampled into one-second bars; ADX uses the most recent 300
    samples and ATR uses one-minute bars.  ``update`` accepts either a Bar or
    quote-like values and returns a complete snapshot.
    """
    def __init__(self, config=None):
        self.config = config
        self.emas = {h: ContinuousEMA(h) for h in (15.0, 90.0, 300.0)}
        self.rv = {w: RollingVolatility(w) for w in (60, 300)}
        self.second_bars = deque(maxlen=301)
        self.minute_bars = deque(maxlen=16)
        self.prices = deque(maxlen=301)
        self.last_second = None
        self.slow_history = deque(maxlen=11)
        self._minute_key = None
        self._forming_minute = None
        self.atr_1m = None

    def update(self, bar: Bar, *, volatility_ratio: float | None = None) -> dict:
        if not isinstance(bar, Bar):
            raise TypeError("update expects a Bar")
        second = bar.timestamp.timestamp()
        if self.last_second is None or second - self.last_second >= 1:
            self.second_bars.append(bar)
            self.last_second = second
        minute = int(second // 60)
        # Keep a forming bar separate: ATR is calculated only from completed bars.
        if self._minute_key != minute:
            if self._forming_minute is not None:
                self.minute_bars.append(self._forming_minute)
                self.atr_1m = atr(self.minute_bars, 14)
            self._minute_key = minute
            self._forming_minute = Bar(bar.symbol, bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
        else:
            f = self._forming_minute
            self._forming_minute = Bar(f.symbol, f.timestamp, f.open, max(f.high, bar.high),
                                       min(f.low, bar.low), bar.close, f.volume + bar.volume)
        for ema in self.emas.values():
            ema.update(bar.close, second)
        self.slow_history.append(self.emas[300.0].value)
        rv_values = {window: indicator.update(bar.close) for window, indicator in self.rv.items()}
        self.prices.append(bar.close)
        rv60 = rv_values[60]
        rv300 = rv_values[300]
        vr = volatility_ratio
        if vr is None:
            vr = (rv60 / rv300) if rv60 is not None and rv300 else 1.0
        slow_slope = (
            self.slow_history[-1] - self.slow_history[0]
            if len(self.slow_history) == self.slow_history.maxlen else None
        )
        return {"ema_15": self.emas[15.0].value, "ema_90": self.emas[90.0].value,
                "ema_300": self.emas[300.0].value, "slow_slope": slow_slope,
                "rv60": rv60, "rv300": rv300, "adx_300_1s": adx(self.second_bars, 300),
                "atr_1m": self.atr_1m, "vr_ratio": vr,
                "persistence_raw": None if vr is None else AdaptivePersistence.raw(vr),
                "warmup": len(self.second_bars) < 301 or len(self.minute_bars) < 15}


class IndicatorRegistry:
    """One continuous engine per underlying (never mixes symbols)."""
    def __init__(self, config=None):
        self.config, self.engines = config, {}

    def update(self, underlying: str, bar: Bar, **kwargs) -> dict:
        if underlying not in self.engines:
            self.engines[underlying] = ContinuousIndicatorEngine(self.config)
        return self.engines[underlying].update(bar, **kwargs)
