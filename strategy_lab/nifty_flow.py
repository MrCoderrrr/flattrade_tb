"""Pure, one-second NIFTY flow signal. No broker I/O or order submission.

The caller supplies observed index ticks and completed one-/five-minute bars.
This module never fills a missing second or promotes a partial bar to complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite, tanh
from statistics import median

from .models import Bar, IST
from .strategies import _ema


def _clip(value: float, low=-1.0, high=1.0) -> float:
    return min(high, max(low, value))


@dataclass(frozen=True)
class IndexTick:
    timestamp: datetime
    price: float


def _atr_series(bars: list[Bar], period=14) -> list[float]:
    ranges = []
    for previous, bar in zip(bars, bars[1:]):
        # Do not treat an overnight opening gap as a one-minute true range.
        same_day = previous.timestamp.astimezone(IST).date() == bar.timestamp.astimezone(IST).date()
        close = previous.close if same_day else bar.open
        ranges.append(max(bar.high-bar.low, abs(bar.high-close), abs(bar.low-close)))
    if len(ranges) < period:
        return []
    value = sum(ranges[:period])/period
    output = [value]
    for item in ranges[period:]:
        value = (value*(period-1)+item)/period
        output.append(value)
    return output


def _adx(bars: list[Bar], period=7) -> float | None:
    if len(bars) < period*2+1:
        return None
    tr, pos, neg = [], [], []
    for left, right in zip(bars, bars[1:]):
        up = right.high-left.high
        down = left.low-right.low
        pos.append(up if up > down and up > 0 else 0.)
        neg.append(down if down > up and down > 0 else 0.)
        tr.append(max(right.high-right.low, abs(right.high-left.close), abs(right.low-left.close)))
    atr = sum(tr[:period]); plus = sum(pos[:period]); minus = sum(neg[:period])
    dx = []
    for index in range(period-1, len(tr)):
        if index >= period:
            atr = atr-atr/period+tr[index]
            plus = plus-plus/period+pos[index]
            minus = minus-minus/period+neg[index]
        total = plus+minus
        dx.append(100*abs(plus-minus)/total if total else 0.)
    if len(dx) < period:
        return None
    value = sum(dx[:period])/period
    for item in dx[period:]:
        value = (value*(period-1)+item)/period
    return value


def _latest_before(ticks: list[IndexTick], target: datetime, tolerance_seconds: int) -> float | None:
    for tick in reversed(ticks):
        if tick.timestamp <= target:
            return tick.price if (target-tick.timestamp).total_seconds() <= tolerance_seconds else None
    return None


def _weights(quality: float, volatility: float) -> tuple[float, float, float, float]:
    chop = (.20, .20, .45, .15)
    transition = (.35, .20, .30, .15)
    trend = (.20, .45, .10, .25)
    if quality < .35:
        base = chop
    elif quality < .65:
        fraction = (quality-.35)/.30
        base = tuple(a+(b-a)*fraction for a,b in zip(chop,transition))
    else:
        fraction = min(1., (quality-.65)/.25)
        base = tuple(a+(b-a)*fraction for a,b in zip(transition,trend))
    shift = min(.10, max(0.,volatility-1.3)*.1)
    return (base[0]-shift,base[1]+shift,base[2],base[3])


def evaluate(bars1: list[Bar], bars5: list[Bar], ticks: list[IndexTick], now: datetime) -> dict:
    """Return one score for this second, or an ineligible reason; no lookahead."""
    result = {"eligible": False, "reason": "Insufficient fresh one-second observations"}
    if now.tzinfo is None or not ticks:
        return result
    now = now.astimezone(IST)
    if now.weekday() >= 5 or not '09:20' <= now.strftime('%H:%M') < '15:33':
        return {**result, "reason":"Outside NIFTY v3 entry window"}
    if any(t.timestamp.tzinfo is None or not isfinite(t.price) or t.price <= 0 for t in ticks):
        return result
    if any(a.timestamp >= b.timestamp for a,b in zip(ticks,ticks[1:])):
        return {**result,"reason":"Unordered index ticks"}
    last = ticks[-1]
    if last.timestamp > now or (now-last.timestamp).total_seconds() > 3:
        return {**result,"reason":"Index stream stale or future-dated"}
    if len(bars1) < 30 or any(b.interval_minutes != 1 or b.timestamp.tzinfo is None for b in bars1):
        return {**result,"reason":"Need 30 completed one-minute bars"}
    if any(a.timestamp >= b.timestamp for a,b in zip(bars1,bars1[1:])):
        return {**result,"reason":"Unordered one-minute bars"}
    completed = [b for b in bars1 if b.timestamp+timedelta(minutes=1) <= now]
    if len(completed) < 30:
        return result
    today = [b for b in completed if b.timestamp.astimezone(IST).date()==now.date()
             and b.timestamp.astimezone(IST).strftime('%H:%M') >= '09:15']
    if len(today) < 5 or (now-(today[-1].timestamp+timedelta(minutes=1))).total_seconds() > 90:
        return {**result,"reason":"Current-session one-minute bars missing or stale"}
    for left,right in zip(today,today[1:]):
        if right.timestamp-left.timestamp != timedelta(minutes=1):
            return {**result,"reason":"Missing current-session minute"}
    if not all(isfinite(v) and v > 0 for b in completed for v in (b.open,b.high,b.low,b.close)):
        return {**result,"reason":"Invalid OHLC"}
    if any(b.low > min(b.open,b.close) or b.high < max(b.open,b.close) for b in completed):
        return {**result,"reason":"Invalid OHLC range"}
    atr_values = _atr_series(completed)
    if not atr_values or atr_values[-1] <= 0:
        return result
    atr = atr_values[-1]
    vol_ratio = atr/median(atr_values[-21:-1]) if len(atr_values) >= 21 else 1.
    shock_recent = any(atr_values[i]/median(atr_values[max(0,i-20):i]) > 2.2
                       for i in range(max(1,len(atr_values)-3),len(atr_values)))
    if shock_recent:
        return {**result,"reason":"Volatility shock: no new short sales","volatility_ratio":round(vol_ratio,3)}
    fifteen = _latest_before(ticks,last.timestamp-timedelta(seconds=15),3)
    thirty = _latest_before(ticks,last.timestamp-timedelta(seconds=30),3)
    if fifteen is None or thirty is None:
        return {**result,"reason":"Need observed 15- and 30-second index prices"}
    # Session-seeded EMAs prevent yesterday's direction from biasing 09:20.
    closes = [b.close for b in today]
    ema8,ema21 = _ema(closes,8)[-1],_ema(closes,21)[-1]
    impulse = _clip(((last.price-fifteen)/atr+(last.price-today[-1].close)/atr)/2)
    ema_direction = _clip((ema8-ema21)/atr)
    prior = today[-5:]
    high,low = max(b.high for b in prior),min(b.low for b in prior)
    breakout = _clip((last.price-high)/atr if last.price > high else
                     (last.price-low)/atr if last.price < low else 0.)
    acceleration = _clip(((last.price-fifteen)-(fifteen-thirty))/atr)
    window = today[-10:] if len(today)>=10 else today[-5:]
    traveled = sum(abs(b.close-a.close) for a,b in zip(window,window[1:]))
    efficiency = abs(window[-1].close-window[0].close)/traveled if traveled else 0.
    current5 = [b for b in bars5 if b.interval_minutes==5 and b.timestamp.tzinfo is not None
                and b.timestamp.astimezone(IST).date()==now.date() and b.timestamp+timedelta(minutes=5)<=now]
    valid5 = all(isfinite(v) and v>0 for b in current5 for v in (b.open,b.high,b.low,b.close))
    valid5 = valid5 and all(b.low<=min(b.open,b.close) and b.high>=max(b.open,b.close) for b in current5)
    valid5 = valid5 and all(b.timestamp-a.timestamp==timedelta(minutes=5) for a,b in zip(current5,current5[1:]))
    adx = _adx(current5) if valid5 else None
    adx_strength = _clip(((adx or 15.)-15)/20,0,1)
    quality = efficiency if adx is None else .55*efficiency+.45*adx_strength
    weights = _weights(quality,vol_ratio)
    raw = sum(w*x for w,x in zip(weights,(impulse,ema_direction,breakout,acceleration)))
    damper = 1/(1+.4*max(0.,vol_ratio-1.3))
    # Quality already changes the component weights and action thresholds. A
    # large second penalty here would make early, low-quality breakouts
    # mathematically unable to reach the exit band at all.
    score = 100*(.80+.20*quality)*damper*tanh(1.5*raw)
    return {"eligible":True,"score":round(score,2),"reason":"Fresh one-second flow score",
            "timestamp":last.timestamp.isoformat(),"spot":last.price,"atr14":round(atr,4),
            "quality":round(quality,3),"efficiency":round(efficiency,3),
            "adx7_5m":round(adx,2) if adx is not None else None,
            "volatility_ratio":round(vol_ratio,3),
            "weights":dict(zip(('impulse','ema','breakout','acceleration'),(round(x,3) for x in weights))),
            "components":dict(zip(('impulse','ema','breakout','acceleration'),
                                   (round(x,3) for x in (impulse,ema_direction,breakout,acceleration))))}


def _when(row: dict) -> datetime | None:
    value = row.get('timestamp')
    try:
        value = datetime.fromisoformat(value) if isinstance(value,str) else value
    except ValueError:
        return None
    return value if isinstance(value,datetime) and value.tzinfo is not None else None


def _consecutive(history: list[dict], count: int) -> list[dict]:
    rows = history[-count:]
    if len(rows) != count or any(not row.get('eligible') or
                                 not isinstance(row.get('score'),(int,float)) or
                                 not isfinite(row['score']) for row in rows):
        return []
    times = [_when(row) for row in rows]
    if any(t is None for t in times):
        return []
    if any(not 0 < (b-a).total_seconds() <= 1.5 for a,b in zip(times,times[1:])):
        return []
    return rows


def decision_bands(history: list[dict]) -> dict[str,float]:
    """Adaptive, symmetric score bands using only observed past/current data.

    Score noise is the median absolute one-second change, not the score level.
    Outages are excluded rather than treated as zero movement. Numbers are
    starting hypotheses and require out-of-sample paper validation.
    """
    latest_time = _when(history[-1]) if history else None
    valid = [r for r in history[-61:] if latest_time is not None and r.get('eligible')
             and _when(r) is not None
             and 0 <= (latest_time-_when(r)).total_seconds() <= 60
             and isinstance(r.get('score'),(int,float)) and isfinite(r['score'])]
    changes = []
    for old,new in zip(valid,valid[1:]):
        elapsed = (_when(new)-_when(old)).total_seconds()
        if 0 < elapsed <= 1.5:
            changes.append(abs(new['score']-old['score']))
    noise = median(changes) if len(changes) >= 20 else 0.
    latest = valid[-1] if valid else {}
    quality = _clip(float(latest.get('quality',.5)),0,1)
    volatility = max(0.,float(latest.get('volatility_ratio',1.)))
    exit_band = _clip(55 + 8*max(0.,.35-quality)/.35
                      + 8*max(0.,volatility-1.) + min(12.,1.5*noise),55,72)
    return {'exit':round(exit_band,2),
            'urgent':round(min(90.,exit_band+20),2),
            'jump':round(exit_band-5,2),
            'open_both':round(max(32.,45-.6*(exit_band-55)),2),
            'reentry':round(max(12.,20-.45*(exit_band-55)),2),
            'reverse':round(max(35.,exit_band-20),2),
            'noise':round(noise,2)}


def decision(history: list[dict], state: str) -> str | None:
    """One new-exposure action from recent eligible second scores.

    Protective premium stops are evaluated separately and always take priority.
    """
    last_row = _consecutive(history,1)
    if not last_row:
        return None
    band = decision_bands(history)
    last = last_row[-1]['score']
    two = _consecutive(history,2)
    three = _consecutive(history,3)
    five = _consecutive(history,5)
    if state == 'FLAT':
        if three and all(abs(row['score']) < band['open_both'] for row in three):
            return 'OPEN_BOTH'
    if state == 'DUAL':
        if (last >= band['urgent'] or
            (three and all(row['score'] >= band['exit'] for row in three)) or
            (two and last >= band['jump'] and last-two[-2]['score'] >= 25)):
            return 'EXIT_CE'
        if (last <= -band['urgent'] or
            (three and all(row['score'] <= -band['exit'] for row in three)) or
            (two and last <= -band['jump'] and two[-2]['score']-last >= 25)):
            return 'EXIT_PE'
    if state == 'SOLO_PE':
        if ((five and all(row['score'] <= band['reentry'] for row in five)) or
            (three and all(row['score'] <= -band['reverse'] for row in three))):
            return 'REENTER_CE'
    if state == 'SOLO_CE':
        if ((five and all(row['score'] >= -band['reentry'] for row in five)) or
            (three and all(row['score'] >= band['reverse'] for row in three))):
            return 'REENTER_PE'
    return None
