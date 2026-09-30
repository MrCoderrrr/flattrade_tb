"""Bounded option-premium and market-volatility adjustments for paper stops."""
from __future__ import annotations

import math
from statistics import median


def intraday_regime(completed_bars, atr, adx=None):
    """One bounded trend-strength value from completed same-session candles.

    A directional exit needs a two-bar range break; CHOP and ADX decide
    whether that break is more than a brief oscillation inside a range.
    """
    bars = completed_bars
    if len(bars) < 10 or not isinstance(atr, (int, float)) or atr <= 0:
        return {'strength': 0.0, 'breakout_direction': 0, 'choppiness': None}
    period = min(14, len(bars)-1)
    window = bars[-period:]
    previous = bars[-period-1]
    true_range_sum = 0.0
    for bar in window:
        true_range_sum += max(bar.high-bar.low, abs(bar.high-previous.close),
                              abs(bar.low-previous.close))
        previous = bar
    span = max(bar.high for bar in window)-min(bar.low for bar in window)
    ratio = true_range_sum/span if span > 0 else float('inf')
    chop = min(100., max(0., 100.*math.log10(max(1., ratio))/math.log10(period)))
    prior = bars[-min(20, len(bars)-2)-2:-2]
    high = max(bar.high for bar in prior)
    low = min(bar.low for bar in prior)
    threshold = .15*atr
    breakout = (1 if bars[-2].close > high+threshold and bars[-1].close > high+threshold
                else -1 if bars[-2].close < low-threshold and bars[-1].close < low-threshold
                else 0)
    adx_strength = min(1., max(0., (float(adx)-18.)/20.)) if adx is not None else 0.
    chop_strength = min(1., max(0., (60.-chop)/25.))
    strength = .55*bool(breakout)+.25*adx_strength+.20*chop_strength
    if chop >= 60.:
        strength = min(strength, .60)
    return {'strength': round(strength, 3), 'breakout_direction': breakout,
            'choppiness': round(chop, 2)}


def adaptive_short_stop(entry_premium, premium_marks, volatility_ratio,
                        base_stop, base_trail, *, solo=False,
                        stop_bounds=(0.10, 0.28), trail_bounds=(0.04, 0.16)):
    """Return hard-stop/trail fractions calibrated to premium and observed noise.

    Premium tiers cap rupee risk as entry premium rises. Recent executable
    premium marks and the underlying volatility regime add bounded room in
    unusually noisy conditions. This adjusts exits only; it never filters
    entries. Marks may be old floats or {"price": ...} samples.
    """
    try:
        premium = float(entry_premium)
    except (TypeError, ValueError):
        premium = 0.0
    tier = 0.0 if premium < 80 else 0.01 if premium < 150 else 0.02 if premium < 300 else 0.035

    try:
        regime = float(volatility_ratio)
    except (TypeError, ValueError):
        regime = 1.0
    if not math.isfinite(regime):
        regime = 1.0
    regime = min(2.5, max(0.5, regime))
    regime_adjustment = min(0.035, max(-0.01, (regime - 1.0) * 0.025))

    prices = []
    for sample in (premium_marks or [])[-31:]:
        try:
            value = float(sample.get("price") if isinstance(sample, dict) else sample)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            prices.append(value)
    returns = [abs(math.log(right / left)) for left, right in zip(prices, prices[1:])
               if left > 0 and right > 0]
    premium_noise = 0.0
    if len(returns) >= 6:
        premium_noise = min(0.035, max(-0.008, (median(returns[-20:]) - 0.003) * 2.0))

    stop = base_stop - tier + regime_adjustment + premium_noise
    trail = base_trail - tier * 0.5 + regime_adjustment * 0.5 + premium_noise * 0.5
    if solo:
        trail -= 0.005
    return (round(min(stop_bounds[1], max(stop_bounds[0], stop)), 4),
            round(min(trail_bounds[1], max(trail_bounds[0], trail)), 4))
