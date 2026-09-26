"""NIFTY v3: active one-minute EMA/RSI candidate with protective wings."""
from strategy_lab.active_v3 import build_plan as _build, explain_signal as _signal
def build_plan(bars, quotes, now, multiplier=1, capital=200000):
    return _build("NIFTY", bars, quotes, now, multiplier, capital)
def explain_signal(bars, now):
    return _signal("NIFTY", bars, now)
