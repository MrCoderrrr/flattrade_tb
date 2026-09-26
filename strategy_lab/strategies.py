"""Unvalidated, deliberately selective intraday option-selling candidates.

These functions produce proposals only; they do not connect to a broker. No
return target, margin availability, or fill is inferred from the signal. Runtime
must enforce the exchange calendar, daily limits, portfolio exposure, execution
ordering, margin checks and mandatory intraday exit. Five-minute bar timestamps
represent opens. Only completed bars from this calendar day's strategy session
enter an indicator; missing/duplicate/out-of-order bars fail closed.

The risk budget is 1% of the allocated INR 200,000 per multiplier, including an
estimated round-trip cost reserve (INR 200 per multiplier). That reserve is an
assumption, not a broker fee calculation. Contract payoff bounds assume intact
hedges, European-style cash payoff at the quoted strikes and successful intraday
closure; commodity option devolution must be prevented by the runtime. The
strict budget can intentionally reject every available MCX contract.
"""

from datetime import date, datetime, time, timedelta
from itertools import product
from math import isfinite

from .models import Bar, Contract, IST, Leg, Plan, Quote


CAPITAL_PER_MULTIPLIER = 200_000.0
TRADE_RISK_FRACTION = 0.01
STOP_RISK_FRACTION = 0.005
COST_RESERVE_PER_MULTIPLIER = 200.0
MAX_QUOTE_AGE_SECONDS = 10
MAX_SPREAD_FRACTION = 0.10
BAR_LENGTH = timedelta(minutes=5)
_SESSIONS = {
    "NIFTY": (time(9, 15), time(9, 45), time(14, 0)),
    "MCX": (time(16, 0), time(16, 30), time(22, 30)),
}


def _finite_positive(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return isfinite(value) and value > 0
    except OverflowError:
        return False


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _market(market: str) -> str:
    return market.strip().upper() if isinstance(market, str) else ""


def _reject(reason: str, **extra: object) -> dict:
    return {"eligible": False, "reason": reason, "direction": 0, "indicators": {}, **extra}


def _completed_bars(market: str, bars: list[Bar], now: datetime) -> tuple[list[Bar], str]:
    start = _SESSIONS[market][0]
    local = now.astimezone(IST)
    result: list[Bar] = []
    previous: datetime | None = None
    for bar in bars:
        if not isinstance(bar, Bar) or not _aware(bar.timestamp):
            return [], "Every bar must have an aware timestamp."
        if previous is not None and bar.timestamp <= previous:
            return [], "Bars must be strictly chronological without duplicate timestamps."
        previous = bar.timestamp
        stamp = bar.timestamp.astimezone(IST)
        # Future/in-progress OHLC values are never inspected or used in a signal.
        if stamp.date() != local.date() or stamp.time() < start or bar.timestamp + BAR_LENGTH > now:
            continue
        if stamp.second or stamp.microsecond or stamp.minute % 5:
            return [], "Bar opens must align to a five-minute boundary."
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(_finite_positive(price) for price in prices):
            return [], "Bar OHLC prices must be positive and finite."
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
            return [], "Bar OHLC bounds are inconsistent."
        if not isinstance(bar.volume, (int, float)) or isinstance(bar.volume, bool) or not isfinite(bar.volume) or bar.volume < 0:
            return [], "Bar volume must be finite and nonnegative."
        if result and bar.timestamp - result[-1].timestamp != BAR_LENGTH:
            return [], "A gap in today's five-minute bars prevents a reliable signal."
        result.append(bar)
    if len(result) < 12:
        return [], "Waiting for at least 12 completed five-minute bars in today's strategy session."
    if now - (result[-1].timestamp + BAR_LENGTH) >= BAR_LENGTH:
        return [], "The latest completed bar is stale."
    return result, ""


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def explain_signal(market: str, bars: list[Bar], now: datetime) -> dict:
    """Return an explainable directional/range signal, never an order.

    The result always has ``eligible``, ``reason``, ``direction`` and
    ``indicators``. Eligibility concerns underlying bars only; quote, capital and
    option-payoff checks can still make :func:`build_plan` return ``None``.
    """
    market = _market(market)
    if market not in _SESSIONS:
        return _reject("Unsupported market; choose NIFTY or MCX.")
    if not _aware(now):
        return _reject("Current time must be timezone-aware.")
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        return _reject("No entries on weekends; the runtime must also check exchange holidays.")
    _, first_entry, last_entry = _SESSIONS[market]
    if not first_entry <= local.time() < last_entry:
        return _reject(f"Outside {market} entry window {first_entry:%H:%M}–{last_entry:%H:%M} IST.")
    completed, error = _completed_bars(market, bars, now)
    if error:
        return _reject(error)
    closes = [float(bar.close) for bar in completed]
    true_ranges = [float(completed[0].high - completed[0].low)]
    for previous, current in zip(completed, completed[1:]):
        true_ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    # Arithmetic ATR over up to 14 completed bars; its shorter warm-up is explicit.
    atr = sum(true_ranges[-14:]) / len(true_ranges[-14:])
    if not isfinite(atr) or atr <= 0:
        return _reject("Zero observed range does not provide a usable volatility estimate.")
    lookback = closes[-12:]
    travel = sum(abs(right - left) for left, right in zip(lookback, lookback[1:]))
    efficiency = abs(lookback[-1] - lookback[0]) / travel if travel else 0.0
    fast, slow = _ema(closes, 6), _ema(closes, 12)
    slope = (fast[-1] - fast[-4]) / atr
    gap = (fast[-1] - slow[-1]) / atr
    indicators = {
        "close": closes[-1], "atr": atr, "atr_fraction": atr / closes[-1],
        "efficiency": efficiency, "ema_fast": fast[-1], "ema_slow": slow[-1],
        "ema_gap_atr": gap, "ema_slope_atr": slope,
        "bar_count": len(completed), "last_bar_open": completed[-1].timestamp.isoformat(),
    }
    if market == "NIFTY":
        opening = [bar for bar in completed if bar.timestamp.astimezone(IST).time() < time(9, 45)]
        if len(opening) != 6 or opening[0].timestamp.astimezone(IST).time() != time(9, 15):
            return _reject("The full 09:15–09:45 opening range is required.", indicators=indicators)
        high, low = max(bar.high for bar in opening), min(bar.low for bar in opening)
        # NIFTY index volume can be zero; then use the explicit typical-price mean.
        volume = sum(bar.volume for bar in completed)
        anchor = (sum(((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in completed) / volume
                  if volume else sum((bar.high + bar.low + bar.close) / 3 for bar in completed) / len(completed))
        indicators.update(opening_high=high, opening_low=low, anchor=anchor,
                          anchor_kind="vwap" if volume else "mean_typical_price")
        if not 0.0004 <= atr / closes[-1] <= 0.005:
            return _reject("NIFTY volatility is outside the candidate's supported range.", indicators=indicators)
        if not 1.5 <= (high - low) / atr <= 8.0:
            return _reject("Opening range width is unsuitable relative to recent volatility.", indicators=indicators)
        if efficiency > 0.35 or abs(gap) > 0.55 or abs(slope) > 0.8:
            return _reject("NIFTY is trending; the range-selling candidate stays flat.", indicators=indicators)
        if not all(low < value < high for value in closes[-2:]) or abs(closes[-1] - anchor) > 0.8 * atr:
            return _reject("NIFTY must remain inside its opening range near the session mean.", indicators=indicators)
        return {"eligible": True, "reason": "Contained opening range with low directional efficiency.",
                "direction": 0, "indicators": indicators}
    if not 0.001 <= atr / closes[-1] <= 0.03:
        return _reject("Natural-gas volatility is outside the candidate's supported range.", indicators=indicators)
    resistance = max(bar.high for bar in completed[-8:-2])
    support = min(bar.low for bar in completed[-8:-2])
    indicators.update(breakout_high=resistance, breakout_low=support)
    bullish = gap >= 0.5 and slope >= 0.3 and all(value > resistance for value in closes[-2:])
    bearish = gap <= -0.5 and slope <= -0.3 and all(value < support for value in closes[-2:])
    if efficiency < 0.65 or not (bullish or bearish):
        return _reject("MCX needs an efficient trend and two completed breakout confirmations.", indicators=indicators)
    return {"eligible": True, "reason": "Efficient trend confirmed by two completed breakout bars.",
            "direction": 1 if bullish else -1, "indicators": indicators}


def _next_weekday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _option_type(contract: Contract) -> str:
    return {"C": "CE", "CALL": "CE", "CE": "CE", "P": "PE", "PUT": "PE", "PE": "PE"}.get(
        str(contract.option_type).upper(), "")


def _valid_quote(quote: Quote, market: str, now: datetime) -> bool:
    if not isinstance(quote, Quote) or not isinstance(quote.contract, Contract):
        return False
    contract = quote.contract
    if not _aware(quote.timestamp) or not 0 <= (now - quote.timestamp).total_seconds() <= MAX_QUOTE_AGE_SECONDS:
        return False
    if not all(_finite_positive(value) for value in (quote.bid, quote.ask, quote.last, contract.strike, contract.tick_size)):
        return False
    if quote.bid > quote.ask or (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) > MAX_SPREAD_FRACTION:
        return False
    # A stale LTP need not lie inside the current bid/ask; it is never a fill price.
    if not isinstance(contract.lot_size, int) or isinstance(contract.lot_size, bool) or contract.lot_size <= 0:
        return False
    if not all(isinstance(size, int) and not isinstance(size, bool) and size >= 0 for size in (quote.bid_size, quote.ask_size)):
        return False
    if not isinstance(contract.expiry, date) or isinstance(contract.expiry, datetime):
        return False
    if contract.expiry <= _next_weekday(now.astimezone(IST).date()) or not _option_type(contract):
        return False
    if not isinstance(contract.symbol, str) or not isinstance(contract.token, str) or not contract.token.strip():
        return False
    expected_exchange = "NFO" if market == "NIFTY" else "MCX"
    prefixes = ("NIFTY",) if market == "NIFTY" else ("NATURALGAS", "NATGASMINI")
    if str(contract.exchange).upper() != expected_exchange or not contract.symbol.upper().startswith(prefixes):
        return False
    # Reject impossible broker snapshots, including prices off the contract tick.
    return all(abs(value / contract.tick_size - round(value / contract.tick_size)) < 1e-6 for value in (quote.bid, quote.ask))


def _leg(quote: Quote, side: str, multiplier: int) -> Leg | None:
    quantity = quote.contract.lot_size * multiplier
    displayed = quote.ask_size if side == "BUY" else quote.bid_size
    return Leg(quote, side, quantity) if displayed >= quantity else None


def _candidate(legs: list[Leg], width: float, strategy: str, direction: int,
               multiplier: int, reason: str) -> Plan | None:
    quantity = legs[0].quantity
    credit = sum((leg.quote.bid if leg.side == "SELL" else -leg.quote.ask) * leg.quantity for leg in legs)
    reserve = COST_RESERVE_PER_MULTIPLIER * multiplier
    # Credits must be executable-side prices, not midpoints or LTP.
    if credit <= 2 * reserve or credit >= width * quantity:
        return None
    bounded_loss = width * quantity - credit + reserve
    allocation = CAPITAL_PER_MULTIPLIER * multiplier
    if bounded_loss > allocation * TRADE_RISK_FRACTION:
        return None
    stop = min(allocation * STOP_RISK_FRACTION, 0.6 * bounded_loss)
    target = 0.5 * credit - reserve
    if target <= 0 or target < 0.20 * stop:
        return None
    return Plan(strategy=strategy, legs=legs, reason=reason,
                stop_loss=round(stop, 2), take_profit=round(target, 2),
                max_loss=round(bounded_loss, 2), direction=direction)


def build_plan(market: str, bars: list[Bar], quotes: list[Quote], now: datetime,
               multiplier: int, capital: float) -> Plan | None:
    """Build at most one hedged proposal within its allocated payoff-risk cap.

    ``capital`` is the currently available account allocation in rupees, not
    broker margin. ``multiplier`` selects one exchange lot per leg per unit.
    Runtime must reserve allocations across strategies to avoid double spending.
    Long hedges appear first in the returned legs for hedge-first execution.
    No eligible/liquid/risk-compliant candidate results in ``None``.
    """
    if not isinstance(multiplier, int) or isinstance(multiplier, bool) or multiplier <= 0:
        return None
    if not _finite_positive(capital) or multiplier > capital / CAPITAL_PER_MULTIPLIER:
        return None
    market = _market(market)
    signal = explain_signal(market, bars, now)
    if not signal["eligible"]:
        return None
    available = [quote for quote in quotes if _valid_quote(quote, market, now)]
    # Duplicate instrument snapshots are ambiguous; do not select a lucky price.
    identities = [(quote.contract.exchange, quote.contract.token) for quote in available]
    if len(identities) != len(set(identities)):
        return None
    groups: dict[tuple, list[Quote]] = {}
    for quote in available:
        # The natural-gas mini and full contracts are different underlyings.
        family = "NATGASMINI" if quote.contract.symbol.upper().startswith("NATGASMINI") else market
        key = (quote.contract.expiry, quote.contract.lot_size, quote.contract.tick_size, family)
        groups.setdefault(key, []).append(quote)
    indicators, direction = signal["indicators"], signal["direction"]
    close, atr = indicators["close"], indicators["atr"]
    candidates: list[Plan] = []
    # Prefer the nearest eligible expiry that can actually satisfy the risk cap.
    for key in sorted(groups):
        chain = groups[key]
        if market == "NIFTY":
            puts = sorted((quote for quote in chain if _option_type(quote.contract) == "PE"), key=lambda q: q.contract.strike, reverse=True)
            calls = sorted((quote for quote in chain if _option_type(quote.contract) == "CE"), key=lambda q: q.contract.strike)
            put_boundary = min(indicators["opening_low"] - 0.5 * atr, close - atr)
            call_boundary = max(indicators["opening_high"] + 0.5 * atr, close + atr)
            short_puts = [quote for quote in puts if quote.contract.strike <= put_boundary][:4]
            short_calls = [quote for quote in calls if quote.contract.strike >= call_boundary][:4]
            for short_put, short_call in product(short_puts, short_calls):
                put_hedges = [quote for quote in puts if quote.contract.strike < short_put.contract.strike][:3]
                call_hedges = [quote for quote in calls if quote.contract.strike > short_call.contract.strike][:3]
                for long_put, long_call in product(put_hedges, call_hedges):
                    width = max(short_put.contract.strike - long_put.contract.strike,
                                long_call.contract.strike - short_call.contract.strike)
                    legs = [_leg(long_put, "BUY", multiplier), _leg(long_call, "BUY", multiplier),
                            _leg(short_put, "SELL", multiplier), _leg(short_call, "SELL", multiplier)]
                    if all(leg is not None for leg in legs):
                        plan = _candidate(legs, width, "nifty_range_iron_condor", 0, multiplier,
                                          signal["reason"] + " OTM shorts beyond the opening range; intact long wings cap payoff risk.")
                        if plan:
                            candidates.append(plan)
        else:
            option = "PE" if direction == 1 else "CE"
            chain = [quote for quote in chain if _option_type(quote.contract) == option]
            short_quotes = sorted((quote for quote in chain if
                                   (quote.contract.strike <= close - 0.5 * atr if direction == 1 else quote.contract.strike >= close + 0.5 * atr)),
                                  key=lambda q: abs(q.contract.strike - close))[:4]
            for short in short_quotes:
                hedges = sorted((quote for quote in chain if
                                 (quote.contract.strike < short.contract.strike if direction == 1 else quote.contract.strike > short.contract.strike)),
                                key=lambda q: abs(q.contract.strike - short.contract.strike))[:3]
                for hedge in hedges:
                    legs = [_leg(hedge, "BUY", multiplier), _leg(short, "SELL", multiplier)]
                    if all(leg is not None for leg in legs):
                        plan = _candidate(legs, abs(short.contract.strike - hedge.contract.strike),
                                          "mcx_naturalgas_trend_credit_spread", direction, multiplier,
                                          signal["reason"] + " OTM directional credit spread with a same-expiry long hedge.")
                        if plan:
                            candidates.append(plan)
        if candidates:
            # Choose the best conservative profit-target/risk ratio in this expiry.
            return max(candidates, key=lambda plan: plan.take_profit / plan.max_loss)
    return None
