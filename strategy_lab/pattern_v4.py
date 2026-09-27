"""NIFTY one-minute structure signals for a hedged, paper-only candidate.

The score ranks rule matches; it is not a calibrated probability. Only fully
closed candles participate. Every candidate has a price invalidation and a
target before an option proposal is considered.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, time
from math import isfinite

from .models import Bar, IST, Leg, Plan
from .strategies import _ema, _option_type, _valid_quote


@dataclass(frozen=True)
class Candidate:
    name: str
    direction: int
    entry: float
    stop: float
    target: float
    base: float
    level: float


def _candidate(name, direction, entry, stop, target, base, level):
    return Candidate(name, direction, entry, stop, target, base, level)


def _clamp(value, low=0., high=1.):
    return min(high, max(low, value))


def _reject(reason, indicators=None, matches=None):
    return {"eligible": False, "direction": 0, "reason": reason,
            "pattern": None, "score": 0., "matches": matches or [],
            "stop_level": None, "target_level": None,
            "indicators": indicators or {}}


def _closed_session(bars, now):
    completed, previous = [], None
    for bar in bars:
        if not isinstance(bar, Bar) or bar.timestamp.tzinfo is None or bar.interval_minutes != 1:
            return None, "NIFTY v4 needs timestamped one-minute bars"
        if previous is not None and bar.timestamp <= previous:
            return None, "Bars are duplicated or out of order"
        previous = bar.timestamp
        stamp = bar.timestamp.astimezone(IST)
        if stamp.date() != now.date() or not time(9, 15) <= stamp.time() < time(15, 35):
            continue
        if stamp.second or stamp.microsecond or bar.timestamp + timedelta(minutes=1) > now:
            continue
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(type(value) not in (int, float) or not isfinite(value) or value <= 0 for value in prices):
            return None, "Invalid candle price"
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            return None, "Invalid candle range"
        if type(bar.volume) not in (int, float) or not isfinite(bar.volume) or bar.volume < 0:
            return None, "Invalid candle volume"
        if completed and bar.timestamp-completed[-1].timestamp != timedelta(minutes=1):
            return None, "Missing one-minute candles"
        completed.append(bar)
    if len(completed) < 16 or completed[0].timestamp.astimezone(IST).time() != time(9, 15):
        return None, "Need the complete 09:15–09:30 opening range and one later candle"
    if now-(completed[-1].timestamp+timedelta(minutes=1)) > timedelta(seconds=90):
        return None, "Latest completed candle is stale"
    return completed, ""


def _five_minute(bars):
    groups = []
    for i in range(0, len(bars)-4, 5):
        block = bars[i:i+5]
        if len(block) == 5:
            groups.append((max(b.high for b in block), min(b.low for b in block), block[-1].close))
    return groups


def _score(candidate, atr, trend, efficiency, candle, five_bias):
    risk = (candidate.entry-candidate.stop)*candidate.direction
    reward = (candidate.target-candidate.entry)*candidate.direction
    if not .25*atr <= risk <= 2.5*atr or reward/risk < 1.5:
        return None
    aligned = _clamp(candidate.direction*trend, 0., 1.)
    structure = _clamp(candidate.direction*five_bias, 0., 1.)
    continuation = candidate.name in ("opening_break", "range_break", "compression_break", "breakout_retest")
    quality = efficiency if continuation else 1-efficiency
    candle_range = max(candle.high-candle.low, 1e-9)
    shape = (min(candle.open, candle.close)-candle.low if candidate.direction > 0 else
             candle.high-max(candle.open, candle.close)) / candle_range if candidate.name == "level_rejection" else (
             abs(candle.close-candle.open)/candle_range)
    score = (candidate.base + .10*aligned + .05*structure +
             .09*_clamp(shape) + .05*quality +
             .04*_clamp((reward/risk-1.5)/.7))
    return round(min(score, .99), 4)


def explain_signal(market, bars, now):
    if market != "NIFTY" or now.tzinfo is None:
        return _reject("V4 supports NIFTY only")
    now = now.astimezone(IST)
    if now.weekday() >= 5 or not "09:31" <= now.strftime("%H:%M") < "15:15":
        return _reject("Outside NIFTY v4 paper entry window")
    today, error = _closed_session(bars, now)
    if error:
        return _reject(error)
    opening = today[:15]
    high, low = max(b.high for b in opening), min(b.low for b in opening)
    previous, current = today[-2:]
    entry = current.close
    ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
              for a, b in zip(today, today[1:])]
    atr = sum(ranges[-14:])/14
    if atr <= 0 or not .0001 <= atr/entry <= .012:
        return _reject("Observed volatility is outside v4's supported range")
    closes = [bar.close for bar in today]
    ema8, ema21 = _ema(closes, 8)[-1], _ema(closes, 21)[-1]
    trend = _clamp((ema8-ema21)/atr, -1., 1.)
    window = closes[-11:]
    travel = sum(abs(right-left) for left, right in zip(window, window[1:]))
    efficiency = abs(window[-1]-window[0])/travel if travel else 0.
    five = _five_minute(today)
    five_bias = _clamp((five[-1][2]-five[-2][2])/atr, -1., 1.) if len(five) >= 2 else 0.
    recent_ranges = ranges[-5:]
    prior_ranges = ranges[-14:-5]
    ratio = (sum(recent_ranges)/len(recent_ranges))/(sum(prior_ranges)/len(prior_ranges)) if prior_ranges else 1.
    prior = today[max(15, len(today)-21):-1]
    resistance = max((b.high for b in prior), default=high)
    support = min((b.low for b in prior), default=low)
    candle_range = max(current.high-current.low, 1e-9)
    body = abs(current.close-current.open)
    body_fraction = body/candle_range
    indicators = {"close": entry, "atr14": round(atr, 4), "ema8": round(ema8, 4),
                  "ema21": round(ema21, 4), "efficiency": round(efficiency, 4),
                  "volatility_ratio": round(ratio, 4), "opening_high": high,
                  "opening_low": low, "support": support, "resistance": resistance,
                  "five_minute_bias": round(five_bias, 4),
                  "last_bar_open": current.timestamp.isoformat()}
    if ratio > 2.5 and candle_range > 2.2*atr:
        return _reject("Volatility shock; wait for a new stable structure", indicators)

    candidates = []
    for direction, level, opposite in ((1, high, low), (-1, low, high)):
        signed = lambda value: direction*(value-level)
        if signed(entry) >= .12*atr and signed(previous.close) <= .08*atr and body_fraction >= .42:
            stop = level-direction*.55*atr
            target = entry+direction*max(1.25*abs(level-opposite), 1.5*atr)
            candidates.append(_candidate("opening_break", direction, entry, stop, target, .56, level))
        roll_level = resistance if direction > 0 else support
        if (direction*(entry-roll_level) >= .12*atr and
                direction*(previous.close-roll_level) <= .08*atr and body_fraction >= .45):
            stop = roll_level-direction*.55*atr
            target = entry+direction*max(1.2*abs(resistance-support), 1.5*atr)
            candidates.append(_candidate("range_break", direction, entry, stop, target, .55, roll_level))
        # A closed candle outside the level, followed by a closed candle back
        # inside, is a failed break in the opposite direction.
        if (signed(previous.close) >= .10*atr and signed(entry) <= -.07*atr and
                direction*(previous.high if direction > 0 else previous.low) > direction*level):
            reversal = -direction
            extreme = max(previous.high, current.high) if direction > 0 else min(previous.low, current.low)
            stop = extreme+direction*.12*atr
            target = opposite
            candidates.append(_candidate("failed_break", reversal, entry, stop, target, .62, level))
    for direction, level, opposite in ((1, support, resistance), (-1, resistance, support)):
        wick = (min(current.open, current.close)-current.low if direction > 0 else
                current.high-max(current.open, current.close))
        # Rejection candles are meaningful only at a previously known level.
        touch = current.low <= level+.12*atr if direction > 0 else current.high >= level-.12*atr
        return_inside = direction*(entry-level) >= .25*atr
        if touch and return_inside and wick >= max(body, .1*atr) and body_fraction <= .55:
            stop = (min(current.low, level)-.12*atr if direction > 0 else
                    max(current.high, level)+.12*atr)
            target = opposite
            candidates.append(_candidate("level_rejection", direction, entry, stop, target, .65, level))
        engulf = (previous.close < previous.open and current.close > current.open and
                  current.open <= previous.close and current.close >= previous.open) if direction > 0 else (
                  previous.close > previous.open and current.close < current.open and
                  current.open >= previous.close and current.close <= previous.open)
        if engulf and touch and return_inside:
            stop = (min(current.low, previous.low)-.12*atr if direction > 0 else
                    max(current.high, previous.high)+.12*atr)
            candidates.append(_candidate("engulfing_at_level", direction, entry, stop, opposite, .59, level))

    if len(today) >= 22:
        previous_six = today[-7:-1]
        squeeze_high, squeeze_low = max(b.high for b in previous_six), min(b.low for b in previous_six)
        if squeeze_high-squeeze_low <= 2.2*atr and body >= .5*atr:
            for direction, level in ((1, squeeze_high), (-1, squeeze_low)):
                if direction*(entry-level) >= .10*atr:
                    stop = level-direction*.5*atr
                    target = entry+direction*max(1.25*(squeeze_high-squeeze_low), 1.4*atr)
                    candidates.append(_candidate("compression_break", direction, entry, stop, target, .57, level))
    # A retest needs a break in the preceding four candles and a new close back
    # on the broken side. The current candle alone cannot create its own level.
    if len(today) >= 20:
        earlier = today[-5:-1]
        for direction, level in ((1, high), (-1, low)):
            broke = any(direction*(bar.close-level) >= .2*atr for bar in earlier)
            touched = (current.low <= level+.1*atr if direction > 0 else current.high >= level-.1*atr)
            if broke and touched and direction*(entry-level) >= .15*atr and direction*(entry-current.open) > 0:
                stop = level-direction*.45*atr
                target = entry+direction*max(.8*abs(high-low), 1.5*atr)
                candidates.append(_candidate("breakout_retest", direction, entry, stop, target, .61, level))

    # Do not project through a known completed five-minute swing or the
    # opening-range boundary. A nearby barrier can make a shape non-tradable.
    levels = {high, low}
    for block_high, block_low, _ in five:
        levels.update((block_high, block_low))
    ranked = []
    for candidate in candidates:
        barriers = [level for level in levels if
                    (level-candidate.entry)*candidate.direction > .1*atr]
        if barriers:
            nearest = min(barriers, key=lambda level: abs(level-candidate.entry))
            if abs(nearest-candidate.entry) < abs(candidate.target-candidate.entry):
                candidate = Candidate(candidate.name, candidate.direction, candidate.entry,
                                      candidate.stop, nearest, candidate.base, candidate.level)
        score = _score(candidate, atr, trend, efficiency, current, five_bias)
        if score is not None:
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    matches = [{"pattern": c.name, "direction": c.direction, "score": score,
                "level": round(c.level, 4)} for score, c in ranked]
    if not ranked or ranked[0][0] < .70:
        return _reject("No clear pattern with at least 1.5:1 underlying target-to-stop room", indicators, matches)
    score, best = ranked[0]
    if any(c.direction != best.direction and score-other_score <= .08 for other_score, c in ranked[1:]):
        return _reject("Opposing patterns have similar scores", indicators, matches)
    risk = abs(best.entry-best.stop)
    return {"eligible": True, "direction": best.direction,
            "reason": f"{best.name}: closed-candle structure and {abs(best.target-best.entry)/risk:.2f}:1 target/stop room",
            "pattern": best.name, "score": score, "matches": matches,
            "stop_level": round(best.stop, 4), "target_level": round(best.target, 4),
            "indicators": indicators}


def build_plan(market, bars, quotes, now, multiplier, capital):
    if market != "NIFTY" or type(multiplier) is not int or multiplier < 1 or capital < 200000*multiplier:
        return None
    signal = explain_signal(market, bars, now)
    if not signal['eligible']:
        return None
    available = [q for q in quotes if _valid_quote(q, market, now)]
    if len({(q.contract.exchange, q.contract.token) for q in available}) != len(available):
        return None
    option = 'PE' if signal['direction'] > 0 else 'CE'
    spot = signal['indicators']['close']
    for expiry in sorted({q.contract.expiry for q in available}):
        group = [q for q in available if q.contract.expiry == expiry and _option_type(q.contract) == option]
        shorts = sorted(group, key=lambda q: abs(q.contract.strike-spot))[:3]
        plans = []
        for short in shorts:
            quantity = short.contract.lot_size*multiplier
            if short.bid_size < quantity:
                continue
            wings = [q for q in group if q.contract.lot_size == short.contract.lot_size
                     and q.ask_size >= quantity and
                     (short.contract.strike-q.contract.strike if option == 'PE' else
                      q.contract.strike-short.contract.strike) >= 100 and
                     (short.contract.strike-q.contract.strike if option == 'PE' else
                      q.contract.strike-short.contract.strike) <= 200]
            for hedge in wings:
                width = abs(short.contract.strike-hedge.contract.strike)
                credit = (short.bid-hedge.ask)*quantity
                max_loss = width*quantity-credit+400*multiplier
                if credit <= 500*multiplier or max_loss <= 0 or max_loss > 8000*multiplier:
                    continue
                target = .45*credit-200*multiplier
                if target <= 0:
                    continue
                plans.append(Plan('nfv4', [Leg(hedge, 'BUY', quantity), Leg(short, 'SELL', quantity)],
                                  signal['reason'], round(min(2500*multiplier, .55*max_loss), 2),
                                  round(target, 2), round(max_loss, 2), signal['direction'],
                                  signal['stop_level'], signal['target_level'],
                                  signal['pattern'], signal['score']))
        if plans:
            return max(plans, key=lambda p: p.take_profit/p.max_loss)
    return None
