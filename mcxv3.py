"""MCX v3: active one-minute EMA/RSI candidate, no hedge, uncapped call risk."""
from strategy_lab.active_v3 import build_plan as _build, explain_signal as _signal
def build_plan(bars, quotes, now, multiplier=1, capital=200000):
    return _build("MCX", bars, quotes, now, multiplier, capital)
def explain_signal(bars, now):
    return _signal("MCX", bars, now)
