"""Shared EMA, option type, and broker quote validation for active strategies."""
from datetime import date, datetime, timedelta
from math import isfinite
from .models import Contract, IST, Quote

MAX_QUOTE_AGE_SECONDS = 10
MAX_SPREAD_FRACTION = 0.10

def _finite_positive(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return isfinite(value) and value > 0
    except OverflowError:
        return False


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _next_weekday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _option_type(contract: Contract) -> str:
    return {"C": "CE", "CALL": "CE", "CE": "CE", "P": "PE", "PUT": "PE", "PE": "PE"}.get(
        str(contract.option_type).upper(), "")


def _valid_quote(quote: Quote, market: str, now: datetime, *, protective_wing: bool = False) -> bool:
    if not isinstance(quote, Quote) or not isinstance(quote.contract, Contract):
        return False
    contract = quote.contract
    if not _aware(quote.timestamp) or not 0 <= (now - quote.timestamp).total_seconds() <= MAX_QUOTE_AGE_SECONDS:
        return False
    if not all(_finite_positive(value) for value in (quote.bid, quote.ask, quote.last, contract.strike, contract.tick_size)):
        return False
    spread = quote.ask - quote.bid
    if quote.bid > quote.ask or (spread / ((quote.ask + quote.bid) / 2) > MAX_SPREAD_FRACTION
                               and not (protective_wing and market == "NIFTY"
                                        and spread <= 2*contract.tick_size + 1e-9)):
        return False
    # A stale LTP need not lie inside the current bid/ask; it is never a fill price.
    if not isinstance(contract.lot_size, int) or isinstance(contract.lot_size, bool) or contract.lot_size <= 0:
        return False
    if not all(isinstance(size, int) and not isinstance(size, bool) and size >= 0 for size in (quote.bid_size, quote.ask_size)):
        return False
    if not isinstance(contract.expiry, date) or isinstance(contract.expiry, datetime):
        return False
    if (contract.expiry < now.astimezone(IST).date() if market == "NIFTY"
            else contract.expiry <= _next_weekday(now.astimezone(IST).date())) or not _option_type(contract):
        return False
    if not isinstance(contract.symbol, str) or not isinstance(contract.token, str) or not contract.token.strip():
        return False
    expected_exchange = "NFO" if market == "NIFTY" else "MCX"
    prefixes = ("NIFTY",) if market == "NIFTY" else ("NATURALGAS", "NATGASMINI")
    if str(contract.exchange).upper() != expected_exchange or not contract.symbol.upper().startswith(prefixes):
        return False
    # Reject impossible broker snapshots, including prices off the contract tick.
    return all(abs(value / contract.tick_size - round(value / contract.tick_size)) < 1e-6 for value in (quote.bid, quote.ask))
