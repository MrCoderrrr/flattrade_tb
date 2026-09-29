"""Dashboard-safe paper adaptation of the original v1 option ideas.

The standalone v1 launchers are intentionally never imported: they own separate
state and can perform broker I/O. This module only returns simulated plans from
validated read-only market data. It retains the original ATM/hedge layout and
KAMA-led direction, but does not claim tick-for-tick parity with those engines.
"""
from datetime import timedelta
from math import isfinite

from .active_v3 import _kama
from .catalog import resolve
from .models import Bar, IST, Leg, Plan
from .nifty_flow import _adx
from .strategies import _ema, _option_type, _valid_quote


def explain_signal(market, bars, now):
    result = {"eligible": False, "direction": 0,
              "reason": "Waiting for completed one-minute bars", "indicators": {}}
    if market not in ("NIFTY", "MCX") or now.tzinfo is None:
        return result
    spec = resolve(market, "nfv1" if market == "NIFTY" else "mcxv1")
    now = now.astimezone(IST)
    if now.weekday() >= 5 or not spec.entry_start <= now.strftime("%H:%M") < spec.entry_end:
        result["reason"] = "Outside v1 entry window"
        return result
    completed = []
    previous = None
    for bar in bars:
        if not isinstance(bar, Bar) or bar.timestamp.tzinfo is None or bar.interval_minutes != 1:
            result["reason"] = "V1 paper requires timezone-aware one-minute bars"
            return result
        if previous is not None and bar.timestamp <= previous:
            result["reason"] = "Duplicate or unordered bars"
            return result
        previous = bar.timestamp
        if bar.timestamp + timedelta(minutes=1) > now:
            continue
        values = (bar.open, bar.high, bar.low, bar.close)
        if any(not isinstance(v, (int, float)) or not isfinite(v) or v <= 0 for v in values):
            result["reason"] = "Invalid OHLC data"
            return result
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            result["reason"] = "Inconsistent OHLC data"
            return result
        completed.append(bar)
    recent = completed[-40:]
    today = [b for b in recent if b.timestamp.astimezone(IST).date() == now.date()]
    required = 11 if market == "NIFTY" else 6
    if len(recent) < required or not today:
        return result
    for left, right in zip(recent, recent[1:]):
        if left.timestamp.astimezone(IST).date() == right.timestamp.astimezone(IST).date() and right.timestamp-left.timestamp != timedelta(minutes=1):
            result["reason"] = "Missing one-minute bars"
            return result
    if now-(recent[-1].timestamp+timedelta(minutes=1)) > timedelta(seconds=90):
        result["reason"] = "One-minute signal is stale"
        return result
    closes = [b.close for b in recent]
    period = 10 if market == "NIFTY" else 5
    kama = _kama(closes, period=period, fast=3, slow=30)
    slope = kama[-1]-kama[-2]
    threshold = .15 if market == "NIFTY" else .02
    direction = 1 if slope >= threshold else -1 if slope <= -threshold else 0
    ema8, ema21 = _ema(closes, 8)[-1], _ema(closes, 21)[-1]
    efficiency = None
    ema_gap_floor = None
    if market == "MCX":
        # KAMA(5) reacts quickly, but a slope alone can reverse inside a noisy
        # minute. Require its efficiency ratio and EMA spread to agree before
        # removing a short leg; neutral signals still allow the two-leg entry.
        noise = sum(abs(closes[j]-closes[j-1]) for j in range(len(closes)-5, len(closes)))
        efficiency = abs(closes[-1]-closes[-6])/noise if noise else 0.
        atr_window = recent[-15:]
        ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
                  for a,b in zip(atr_window, atr_window[1:])]
        atr14 = sum(ranges)/len(ranges)
        ema_gap_floor = max(.02, .08*atr14)
        if efficiency < .35 or direction*(ema8-ema21) <= ema_gap_floor:
            direction = 0
    adx = _adx(today, 7)
    opening = 0
    if market == "NIFTY":
        from .opening_trend import opening_drive
        opening = opening_drive(bars, now)
    result.update(eligible=True, direction=direction,
                  reason=f"V1 paper ATM entry; KAMA({period},3,30) slope {slope:+.3f}",
                  indicators={"close": closes[-1], "kama": kama[-1],
                              "kama_slope": slope, "kama_threshold": threshold,
                              "ema8": ema8, "ema21": ema21, "adx7_1m": adx,
                              "efficiency5": efficiency, "ema_gap_floor": ema_gap_floor,
                              "opening_drive": opening,
                              "last_bar_open": recent[-1].timestamp.isoformat()})
    if opening and direction == -opening:
        result.update(eligible=False, direction=0,
                      reason="Opening EMA/KAMA/ADX drive opposes this new short leg")
    return result


def build_plan(market, bars, quotes, now, multiplier, capital):
    if type(multiplier) is not int or multiplier < 1 or capital < 200000*multiplier:
        return None
    signal = explain_signal(market, bars, now)
    if not signal["eligible"]:
        return None
    valid = [q for q in quotes if _valid_quote(q, market, now)]
    if len({(q.contract.exchange, q.contract.token) for q in valid}) != len(valid):
        return None
    spot = signal["indicators"]["close"]
    for expiry in sorted({q.contract.expiry for q in valid}):
        chain = [q for q in valid if q.contract.expiry == expiry]
        for lot in sorted({q.contract.lot_size for q in chain}):
            group = [q for q in chain if q.contract.lot_size == lot]
            if market == "MCX":
                # Full and mini natural-gas options are never mixed in one basket.
                families = ("NATURALGAS", "NATGASMINI")
            else:
                families = ("NIFTY",)
            for family in families:
                family_quotes = [q for q in group if q.contract.symbol.upper().startswith(family)
                                 and (family != "NATURALGAS" or not q.contract.symbol.upper().startswith("NATGASMINI"))]
                strikes = sorted({q.contract.strike for q in family_quotes}, key=lambda x: abs(x-spot))
                for strike in strikes[:3]:
                    shorts = []
                    quantity = lot*multiplier
                    for kind in ("CE", "PE"):
                        candidates = [q for q in family_quotes if q.contract.strike == strike
                                      and _option_type(q.contract) == kind and q.bid_size >= quantity]
                        if not candidates:
                            break
                        shorts.append(Leg(candidates[0], "SELL", quantity))
                    if len(shorts) != 2:
                        continue
                    if market == "MCX":
                        return Plan("mcxv1", shorts, signal["reason"],
                                    3000*multiplier, 3000*multiplier, None, signal["direction"])
                    hedges = []
                    for kind, target in (("CE", strike+1000), ("PE", strike-1000)):
                        candidates = [q for q in family_quotes if _option_type(q.contract) == kind
                                      and (q.contract.strike >= target if kind == "CE" else q.contract.strike <= target)
                                      and q.ask_size >= quantity]
                        if not candidates:
                            break
                        hedge = min(candidates, key=lambda q: abs(q.contract.strike-target))
                        hedges.append(Leg(hedge, "BUY", quantity))
                    if len(hedges) != 2:
                        continue
                    width = max(abs(hedges[0].quote.contract.strike-strike),
                                abs(hedges[1].quote.contract.strike-strike))
                    credit = sum(l.quote.bid*quantity for l in shorts)-sum(l.quote.ask*quantity for l in hedges)
                    if credit <= 0:
                        continue
                    max_loss = max(0, width*quantity-credit)+400*multiplier
                    return Plan("nfv1", hedges+shorts, signal["reason"]+"; 1000-point protective wings",
                                3000*multiplier, 3000*multiplier, max_loss, signal["direction"])
    return None
