"""Active one-minute candidates. Paper proposals only; no return guarantee.

EMA9/21, Wilder RSI14, Wilder ATR14 and today's volume-weighted typical
price vote on direction. Neutral conditions sell both near-ATM sides. NIFTY
always has farther-strike protection; Natural Gas intentionally has none.
"""
from datetime import timedelta
from math import isfinite

from .catalog import resolve
from .models import Bar, IST, Leg, Plan
from .strategies import _ema, _valid_quote, _option_type


def explain_signal(market, bars, now):
    result = {"eligible": False, "direction": 0, "reason": "Waiting for indicator warm-up", "indicators": {}}
    if market not in ("NIFTY", "MCX") or now.tzinfo is None:
        return result
    spec = resolve(market, "nfv3" if market == "NIFTY" else "mcxv3")
    now = now.astimezone(IST)
    if now.weekday() >= 5 or not spec.entry_start <= now.strftime("%H:%M") < spec.entry_end:
        result["reason"] = "Outside v3 entry window"
        return result
    completed, previous = [], None
    for bar in bars:
        if not isinstance(bar, Bar) or bar.timestamp.tzinfo is None or bar.interval_minutes != 1:
            result["reason"] = "V3 requires timezone-aware one-minute bars"
            return result
        stamp = bar.timestamp.astimezone(IST)
        if previous is not None and stamp <= previous:
            result["reason"] = "Duplicate or unordered bars"
            return result
        previous = stamp
        if stamp + timedelta(minutes=1) > now:
            continue
        if stamp.second or stamp.microsecond:
            return result
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(v) and v > 0
                   for v in (bar.open, bar.high, bar.low, bar.close)):
            return result
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            return result
        if not isinstance(bar.volume, (int, float)) or not isfinite(bar.volume) or bar.volume < 0:
            return result
        if stamp.date() < now.date()-timedelta(days=7):
            continue
        completed.append(bar)
    today = [b for b in completed if b.timestamp.astimezone(IST).date() == now.date()
             and b.timestamp.astimezone(IST).strftime("%H:%M") >= ("09:15" if market == "NIFTY" else "16:00")]
    if len(completed) < 30 or len(today) < 5:
        result["reason"] = "Need 30 completed one-minute bars and five from today's session; prior-session bars warm up indicators"
        return result
    recent = completed[-60:]
    for left, right in zip(recent, recent[1:]):
        if left.timestamp.astimezone(IST).date() == right.timestamp.astimezone(IST).date() and right.timestamp-left.timestamp != timedelta(minutes=1):
            result["reason"] = "Missing one-minute bars; awaiting contiguous indicator history"
            return result
    if now - (recent[-1].timestamp+timedelta(minutes=1)) >= timedelta(seconds=90):
        result["reason"] = "One-minute signal is stale"
        return result
    closes = [b.close for b in recent]
    ema9, ema21 = _ema(closes, 9)[-1], _ema(closes, 21)[-1]
    changes = [b-a for a,b in zip(closes,closes[1:])]
    gain = sum(max(0,c) for c in changes[:14])/14
    loss = sum(max(0,-c) for c in changes[:14])/14
    for c in changes[14:]:
        gain, loss = (gain*13+max(0,c))/14, (loss*13+max(0,-c))/14
    rsi = 50. if gain == loss == 0 else 100. if loss == 0 else 100-100/(1+gain/loss)
    ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close)) for a,b in zip(recent,recent[1:])]
    atr = sum(ranges[:14])/14
    for value in ranges[14:]:
        atr = (atr*13+value)/14
    if atr <= 0:
        return result
    volume = sum(b.volume for b in today)
    anchor = sum((b.high+b.low+b.close)/3*(b.volume if volume else 1) for b in today)/(volume or len(today))
    # Small dead bands prevent a one-tick change from voting as a trend.
    gap = (ema9-ema21)/atr
    votes = [(1 if gap > .10 else -1 if gap < -.10 else 0),
             (1 if rsi >= 52 else -1 if rsi <= 48 else 0),
             (1 if closes[-1] > anchor+.05*atr else -1 if closes[-1] < anchor-.05*atr else 0)]
    direction = 1 if votes.count(1) >= 2 else -1 if votes.count(-1) >= 2 else 0
    return {"eligible": True, "direction": direction,
            "reason": {1:"Bullish indicator vote: sell put premium",-1:"Bearish indicator vote: sell call premium",0:"Balanced indicators: sell both near-ATM sides"}[direction],
            "indicators": {"close":closes[-1],"ema9":ema9,"ema21":ema21,"rsi14":round(rsi,2),"atr14":atr,
                           "anchor":anchor,"anchor_kind":"vwap" if volume else "mean_typical_price",
                           "votes":votes,"bar_minutes":1,"last_bar_open":recent[-1].timestamp.isoformat()}}


def build_plan(market, bars, quotes, now, multiplier, capital):
    if type(multiplier) is not int or multiplier < 1 or not isinstance(capital,(int,float)) or not isfinite(capital) or capital < multiplier*200000:
        return None
    signal = explain_signal(market,bars,now)
    if not signal["eligible"]:
        return None
    available = [q for q in quotes if _valid_quote(q,market,now) and q.bid_size >= q.contract.lot_size*multiplier]
    if len({(q.contract.exchange,q.contract.token) for q in available}) != len(available):
        return None
    groups = {}
    for q in available:
        family = "MINI" if q.contract.symbol.startswith("NATGASMINI") else market
        groups.setdefault((q.contract.expiry,q.contract.lot_size,q.contract.tick_size,family),[]).append(q)
    direction, spot, atr = signal["direction"],signal["indicators"]["close"],signal["indicators"]["atr14"]
    sides = ["PE"] if direction == 1 else ["CE"] if direction == -1 else ["PE","CE"]
    for group in sorted(groups):
        chain=groups[group]; quantity=group[1]*multiplier
        shorts,hedges=[],[]
        for option in sides:
            candidates=[q for q in chain if _option_type(q.contract)==option]
            if not candidates:break
            short=min(candidates,key=lambda q:abs(q.contract.strike-spot))
            shorts.append(Leg(short,"SELL",quantity))
            if market=="NIFTY":
                width=max(100.,min(200.,2*atr))
                wings=[q for q in candidates if q.ask_size>=quantity and
                       (short.contract.strike-q.contract.strike>=width if option=="PE" else q.contract.strike-short.contract.strike>=width)]
                if not wings:break
                hedge=min(wings,key=lambda q:abs(q.contract.strike-short.contract.strike))
                hedges.append(Leg(hedge,"BUY",quantity))
        if len(shorts)!=len(sides) or (market=="NIFTY" and len(hedges)!=len(sides)):
            continue
        legs=hedges+shorts
        credit=sum((l.quote.bid if l.side=="SELL" else -l.quote.ask)*quantity for l in legs)
        reserve=200*multiplier
        if credit <= reserve:
            continue
        if market=="NIFTY":
            width=max(abs(a.quote.contract.strike-b.quote.contract.strike) for a,b in zip(shorts,hedges))
            max_loss=width*quantity-credit+reserve
            if max_loss<=0 or max_loss>10000*multiplier:continue
            stop=min(2000*multiplier,max_loss*.6)
            target=max(200*multiplier,credit*.4-reserve)
        else:
            # None means uncapped by a hedge. Never label a stop as maximum loss.
            max_loss=None
            stop=min(3000*multiplier,max(1000*multiplier,credit*.12))
            target=max(500*multiplier,credit*.15-reserve)
        return Plan("nfv3" if market=="NIFTY" else "mcxv3",legs,signal["reason"],round(stop,2),round(target,2),
                    round(max_loss,2) if max_loss is not None else None,direction)
    return None
