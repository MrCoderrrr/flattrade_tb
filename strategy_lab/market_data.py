"""Read-only Flattrade adapter. No order endpoint exists in this module.

Uses broker timestamps, depth prices, and contract metadata; never substitutes
LTP for the order book or receipt time for a missing exchange timestamp.
"""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Bar, Contract, Quote, IST


class FeedError(RuntimeError):
    pass


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite market data")
    return result


def exchange_time(value):
    if value is None or value == "":
        raise FeedError("Quote has no exchange timestamp; entry blocked")
    try:
        return datetime.fromtimestamp(float(value), IST)
    except (ValueError, TypeError, OverflowError):
        for fmt in ("%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S"):
            try:
                return datetime.strptime(str(value), fmt).replace(tzinfo=IST)
            except ValueError:
                pass
    raise FeedError("Unrecognized exchange timestamp")


def parse_quote(contract, row):
    # ft is an exchange feed timestamp. request_time is only a REST response time.
    stamp = row.get("ft") or row.get("ltt")
    quote = Quote(contract, exchange_time(stamp), number(row["bp1"]),
                  number(row["sp1"]), number(row["lp"]),
                  int(row.get("bq1", 0)), int(row.get("sq1", 0)))
    if quote.bid <= 0 or quote.ask < quote.bid or quote.last <= 0:
        raise FeedError("Empty or crossed order book")
    return quote


class FlattradeReadOnly:
    BASE = "https://piconnect.flattrade.in/PiConnectAPI/"
    ALLOWED = {"GetQuotes", "TPSeries", "GetSecurityInfo", "GetOptionChain"}

    def __init__(self, root: Path):
        self.root = Path(root)
        self._bars_cache = {}
        self._contracts_cache = {}

    def _call(self, endpoint, **fields):
        if endpoint not in self.ALLOWED:
            raise FeedError("Only read-only market data endpoints are enabled")
        user = os.environ.get("FLATTRADE_USER_ID", "").strip()
        token_path = Path(os.environ.get("FLATTRADE_TOKEN_FILE", str(self.root / "token.txt")))
        token = token_path.read_text().strip() if token_path.is_file() else ""
        if not user or not token:
            raise FeedError("Set FLATTRADE_USER_ID and a valid FLATTRADE_TOKEN_FILE for market data")
        body = urlencode({"jData": json.dumps({"uid": user, **fields}), "jKey": token}).encode()
        request = Request(self.BASE + endpoint, data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout=8) as response:
                result = json.load(response)
        except Exception:
            raise FeedError("Flattrade market data request failed; check connectivity/session") from None
        if isinstance(result, dict) and result.get("stat") != "Ok":
            raise FeedError("Flattrade rejected market data; refresh the broker login token")
        return result

    def contracts(self, market, now):
        exchange, underlying = ("NFO", "NIFTY") if market == "NIFTY" else ("MCX", "NATURALGAS")
        key = (market, now.date())
        if key in self._contracts_cache:
            return self._contracts_cache[key]
        files = sorted(self.root.glob(f"{exchange}_symbols_*.csv"), reverse=True)
        if not files:
            raise FeedError(f"Missing {exchange} symbol master")
        # Token mappings can change. The master must be refreshed for this session.
        if now.date().isoformat() not in files[0].name:
            raise FeedError(f"Refresh {exchange} symbol master for {now.date().isoformat()}; stale tokens blocked")
        rows = []
        with files[0].open(newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("Symbol") != underlying:
                    continue
                try:
                    expiry = datetime.strptime(row["Expiry"], "%d-%b-%Y").date()
                    if expiry <= now.date():
                        continue
                    contract = Contract(row["TradingSymbol"], str(row["Token"]), exchange,
                                        expiry, number(row["StrikePrice"]), row["OptionType"],
                                        int(number(row["LotSize"])), number(row["TickSize"]))
                    if contract.lot_size > 0 and contract.tick_size > 0:
                        rows.append(contract)
                except (ValueError, KeyError):
                    continue
        if not rows:
            raise FeedError("Symbol master contains no usable contracts")
        self._contracts_cache[key] = rows
        return rows

    def underlying(self, market, now):
        if market == "NIFTY":
            return "NSE", "26000"
        contracts = self.contracts(market, now)
        expiries = sorted({c.expiry for c in contracts if c.option_type in {"CE", "PE"}
                           and (c.expiry-now.date()).days >= 2})
        if not expiries:
            raise FeedError("No eligible Natural Gas option expiry")
        # Options devolve into the same-month future; avoid a front future from a
        # different month when selecting a later option expiry.
        futures = [c for c in contracts if c.option_type == "XX" and
                   (c.expiry.year, c.expiry.month) == (expiries[0].year, expiries[0].month)]
        if not futures:
            raise FeedError("Cannot match Natural Gas options to their underlying future")
        return "MCX", min(futures, key=lambda c: c.expiry).token

    def bars(self, market, now, interval=5):
        bucket = (market, interval, now.date(), now.hour, now.minute // interval)
        if bucket in self._bars_cache:
            return self._bars_cache[bucket]
        exchange, token = self.underlying(market, now)
        start = now.replace(hour=9 if market == "NIFTY" else 16,
                            minute=15 if market == "NIFTY" else 0, second=0, microsecond=0)
        if interval == 1:
            start -= timedelta(days=7)
        result = self._call("TPSeries", exch=exchange, token=token,
                            st=str(int(start.timestamp())), et=str(int(now.timestamp())), intrv=str(interval))
        if not isinstance(result, list):
            raise FeedError("No intraday bars available")
        bars = []
        try:
            for row in result:
                stamp = exchange_time(row["time"])
                if stamp + timedelta(minutes=interval) <= now:
                    bars.append(Bar(stamp, number(row["into"]), number(row["inth"]),
                                    number(row["intl"]), number(row["intc"]), number(row.get("intv", 0)), interval))
        except (ValueError, KeyError):
            raise FeedError("Malformed intraday bars") from None
        bars.sort(key=lambda b: b.timestamp)
        self._bars_cache = {bucket: bars}
        return bars

    def quotes(self, contracts, now):
        result = []
        for contract in contracts:
            try:
                data = self._call("GetQuotes", exch=contract.exchange, token=contract.token)
                if data.get("tsym") != contract.symbol or int(number(data.get("ls", 0))) != contract.lot_size:
                    raise FeedError("Broker contract metadata differs from symbol master")
                result.append(parse_quote(contract, data))
            except (KeyError, ValueError):
                raise FeedError("Broker returned incomplete depth data") from None
        return result

    def snapshot(self, market, now, held=(), strategy_id=None):
        from .catalog import resolve
        interval = resolve(market, strategy_id).bar_minutes
        if held:
            quotes = self.quotes(held, now)
            try:
                bars = self.bars(market, now, interval) if interval == 1 else []
            except FeedError:
                bars = []  # Missing indicators must not prevent pricing an exit.
            return bars, quotes
        bars = self.bars(market, now, interval)
        if not bars:
            return bars, []
        spot = bars[-1].close
        contracts = [c for c in self.contracts(market, now) if c.option_type in {"CE", "PE"}
                     and (c.expiry-now.date()).days >= 2]
        if not contracts:
            return bars, []
        expiry = min(c.expiry for c in contracts)
        contracts = [c for c in contracts if c.expiry == expiry]
        strikes = sorted({c.strike for c in contracts}, key=lambda k: abs(k-spot))[:14]
        selected = [c for c in contracts if c.strike in strikes]
        return bars, self.quotes(selected, now)
