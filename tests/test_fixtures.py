from dataclasses import replace
from datetime import date,datetime,timedelta
from strategy_lab.models import Bar,Contract,IST,Quote
DAY = date(2026, 9, 22)  # Tuesday; avoids accidental dependence on today's date.
EXPIRY = date(2026, 9, 29)


def candles(market="NIFTY", direction=0):
    start = datetime(2026, 9, 22, 9, 15, tzinfo=IST) if market == "NIFTY" else datetime(2026, 9, 22, 16, tzinfo=IST)
    result = []
    for index in range(12):
        if market == "NIFTY":
            close = 25_000 + (5 if index % 2 else -5)
            result.append(Bar(start + timedelta(minutes=5 * index), 25_000,
                              25_030 if index == 0 else 25_010,
                              24_970 if index == 0 else 24_990, close, 100))
        else:
            close = 250 + (index * direction if direction else (0.5 if index % 2 else -0.5))
            result.append(Bar(start + timedelta(minutes=5 * index), close - direction * 0.6,
                              close + 0.3, close - 0.8 if direction >= 0 else close - 0.3,
                              close, 100))
            if direction < 0:
                result[-1] = replace(result[-1], high=close + 0.8)
    return result


def quote(market, strike, option, bid, ask, now, lot=65, expiry=EXPIRY, size=100_000):
    prefix = "NIFTY" if market == "NIFTY" else "NATGASMINI"
    contract = Contract(f"{prefix}{expiry:%d%b%y}{option}{strike}", f"{market}:{option}:{strike}",
                        "NFO" if market == "NIFTY" else "MCX", expiry, strike, option, lot, 0.05)
    return Quote(contract, now, bid, ask, (bid + ask) / 2, size, size)


def nifty_fixture():
    bars = candles()
    now = bars[-1].timestamp + timedelta(minutes=5)
    quotes = [quote("NIFTY", 24_950, "PE", 30, 31, now),
              quote("NIFTY", 24_900, "PE", 14, 15, now),
              quote("NIFTY", 25_050, "CE", 30, 31, now),
              quote("NIFTY", 25_100, "CE", 14, 15, now)]
    return bars, quotes, now


def mcx_fixture(direction=1):
    bars = candles("MCX", direction)
    now = bars[-1].timestamp + timedelta(minutes=5)
    short, hedge, option = (260, 255, "PE") if direction == 1 else (240, 245, "CE")
    quotes = [quote("MCX", short, option, 4.5, 4.6, now, lot=250),
              quote("MCX", hedge, option, 1.8, 1.9, now, lot=250)]
    return bars, quotes, now



from strategy_lab.market_data import FeedError
class Feed:
    def __init__(self,bars,quotes):
        self.bars,self.quotes=bars,quotes
        self.error=False
    def snapshot(self,market,now,held=()):
        if self.error: raise FeedError("Test feed unavailable")
        symbols={contract.symbol for contract in held}
        return self.bars,[quote for quote in self.quotes if not held or quote.contract.symbol in symbols]
