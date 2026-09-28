"""Opening-drive direction from completed NIFTY candles only.

This is a veto on opposite-side *new exposure*, not a profit forecast. It does
not override an existing leg stop or a requested exit.
"""

from datetime import timedelta
from math import isfinite

from .models import IST
from .strategies import _ema
from .nifty_flow import _adx


def opening_drive(bars, now):
    now = now.astimezone(IST)
    if not "09:20" <= now.strftime("%H:%M") < "09:36":
        return 0
    today = [b for b in bars if b.interval_minutes == 1 and
             b.timestamp.astimezone(IST).date() == now.date() and
             b.timestamp.astimezone(IST).strftime("%H:%M") >= "09:15" and
             b.timestamp + timedelta(minutes=1) <= now]
    if len(today) < 5 or today[0].timestamp.astimezone(IST).strftime("%H:%M") != "09:15":
        return 0
    if any(b.timestamp-a.timestamp != timedelta(minutes=1) for a,b in zip(today,today[1:])):
        return 0
    if any(not all(isfinite(v) and v > 0 for v in (b.open,b.high,b.low,b.close)) or
           b.low > min(b.open,b.close) or b.high < max(b.open,b.close) for b in today):
        return 0
    closes = [b.close for b in today]
    ranges = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
              for a,b in zip(today,today[1:])]
    atr = sum(ranges[-14:])/len(ranges[-14:])
    if atr <= 0:
        return 0
    move = closes[-1]-today[0].open
    if abs(move) < max(1.25*atr, .0007*today[0].open):
        return 0
    direction = 1 if move > 0 else -1
    ema8, ema21 = _ema(closes,8)[-1], _ema(closes,21)[-1]
    if direction*(ema8-ema21) <= 0:
        return 0
    if len(closes) >= 11:
        # Same KAMA(10,3,30) update as the v1/v3 signal modules.
        from .active_v3 import _kama
        kama = _kama(closes,10,3,30)
        if direction*(kama[-1]-kama[-2]) <= 0:
            return 0
    adx = _adx(today,7)
    if adx is not None and adx < 18:
        return 0
    return direction
