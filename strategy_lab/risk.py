"""Bounded option-premium and market-volatility adjustments for paper stops."""
from __future__ import annotations

import math
from statistics import median


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
