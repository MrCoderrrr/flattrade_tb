"""Read-only Flattrade adapter. No order endpoint exists in this module.

Uses broker timestamps, depth prices, and contract metadata; never substitutes
LTP for the order book or receipt time for a missing exchange timestamp.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .models import Bar, Contract, Quote, IST


class FeedError(RuntimeError):
    pass


_master_lock = threading.Lock()
_master_attempts = {}


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
    if isinstance(stamp, str) and len(stamp) == 8 and stamp[2] == stamp[5] == ":":
        # PiConnect often sends ltt as HH:MM:SS. Use the broker response only
        # for its calendar date, never as a substitute for the trade time.
        try:
            received = datetime.strptime(row["request_time"], "%H:%M:%S %d-%m-%Y").replace(tzinfo=IST)
            traded = datetime.combine(received.date(), datetime.strptime(stamp, "%H:%M:%S").time(), IST)
        except (KeyError, TypeError, ValueError):
            raise FeedError("Time-only quote has no broker date") from None
        if traded > received:
            traded -= timedelta(days=1)
        timestamp = traded
    else:
        timestamp = exchange_time(stamp)
    # MCX depth is quoted in lots while order quantity and P&L use trading
    # units. The exchange's Natural Gas option chain labels bid/ask quantity
    # as lots; convert it before comparing with the symbol-master lot size.
    depth_unit = contract.lot_size if contract.exchange == "MCX" else 1
    observed_at = None
    if contract.exchange == "MCX":
        try:
            response_time = datetime.strptime(row["request_time"], "%H:%M:%S %d-%m-%Y").replace(tzinfo=IST)
            received_at = datetime.now(IST)
            if -2 <= (received_at-response_time).total_seconds() <= 5:
                observed_at = received_at
        except (KeyError, TypeError, ValueError):
            pass
    quote = Quote(contract, timestamp, number(row["bp1"]),
                  number(row["sp1"]), number(row["lp"]),
                  int(row.get("bq1", 0)) * depth_unit,
                  int(row.get("sq1", 0)) * depth_unit,
                  book_observed_at=observed_at)
    if quote.bid <= 0 or quote.ask < quote.bid or quote.last <= 0:
        raise FeedError("Empty or crossed order book")
    return quote


class FlattradeReadOnly:
    BASE = "https://piconnect.flattrade.in/PiConnectAPI/"
    ALLOWED = {"GetQuotes", "TPSeries", "GetSecurityInfo", "GetOptionChain"}

    def __init__(self, root: Path, auto_refresh: bool = False):
        self.root = Path(root)
        self.auto_refresh = auto_refresh
        self._bars_cache = {}
        self._contracts_cache = {}
        self._quote_cache = {}
        self._quote_lock = threading.Lock()
        self._quote_locks = {}
        self._nifty_stream = None

    def attach_nifty_stream(self, stream):
        self._nifty_stream = stream

    @staticmethod
    def _stream_quote(contract, book, now):
        """Price only from a complete, fresh broker depth update."""
        if not book or book.get('book_received_at') is None:
            return None
        stamp = book['book_received_at']
        if not 0 <= (now-stamp).total_seconds() <= 10:
            return None
        fields = book.get('fields', {})
        try:
            bid, ask, last = (number(fields[key]) for key in ('bp1','sp1','lp'))
            bid_size, ask_size = (number(fields[key]) for key in ('bq1','sq1'))
            if (bid <= 0 or ask < bid or last <= 0 or bid_size < 0 or ask_size < 0 or
                    bid_size != int(bid_size) or ask_size != int(ask_size)):
                return None
            return Quote(contract, stamp, bid, ask, last, int(bid_size), int(ask_size))
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def _refresh_master(self, exchange, underlying, now):
        target = self.root / f"{exchange}_symbols_{now.date().isoformat()}.csv"
        with _master_lock:
            if target.is_file():
                return
            key = (str(self.root.resolve()), exchange, now.date())
            last = _master_attempts.get(key)
            if last is not None and (now-last).total_seconds() < 300:
                raise FeedError(f"Refresh {exchange} symbol master for {now.date().isoformat()}; stale tokens blocked")
            _master_attempts[key] = now
            try:
                with urlopen(f"https://api.shoonya.com/{exchange}_symbols.txt.zip", timeout=15) as response:
                    raw = response.read(25_000_001)
                if len(raw) > 25_000_000:
                    raise ValueError("Oversized symbol archive")
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    name = next((name for name in archive.namelist()
                                 if name.endswith(f"{exchange}_symbols.txt")), None)
                    if name is None or archive.getinfo(name).file_size > 80_000_000:
                        raise ValueError("Invalid symbol archive")
                    body = archive.read(name)
                rows = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
                required = {"Symbol", "Token", "TradingSymbol", "Expiry", "StrikePrice",
                            "OptionType", "LotSize", "TickSize"}
                if not required.issubset(rows.fieldnames or []):
                    raise ValueError("Invalid symbol columns")
                eligible = sum(1 for row in rows if row.get("Symbol") == underlying
                               and row.get("OptionType") in {"CE", "PE"}
                               and str(row.get("Token", "")).isdigit())
                if eligible < 20:
                    raise ValueError("Insufficient current contracts")
                fd, temporary = tempfile.mkstemp(prefix=f".{exchange}-master-", dir=self.root)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(body)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except (OSError, ValueError, UnicodeError, zipfile.BadZipFile):
                raise FeedError(f"Refresh {exchange} symbol master for {now.date().isoformat()}; stale tokens blocked") from None

    def _call(self, endpoint, **fields):
        if endpoint not in self.ALLOWED:
            raise FeedError("Only read-only market data endpoints are enabled")
        user = os.environ.get("FLATTRADE_USER_ID", "").strip()
        token_path = Path(os.environ.get("FLATTRADE_TOKEN_FILE", str(self.root / "token.txt")))
        token = token_path.read_text().strip() if token_path.is_file() else ""
        if not user or not token:
            raise FeedError("Set FLATTRADE_USER_ID and a valid FLATTRADE_TOKEN_FILE for market data")
        # PiConnect expects literal JSON after ``jData=``. Encoding that JSON
        # with urlencode causes HTTP 400 "jData is not valid json object".
        body = ("jData=" + json.dumps({"uid": user, **fields}, separators=(",", ":")) +
                "&jKey=" + quote_plus(token)).encode()
        request = Request(self.BASE + endpoint, data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout=8) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code == 401:
                raise FeedError("Flattrade session expired; refresh the broker login token") from None
            raise FeedError(f"Flattrade {endpoint} returned HTTP {exc.code}") from None
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
        if self.auto_refresh and (not files or now.date().isoformat() not in files[0].name):
            self._refresh_master(exchange, underlying, now)
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
                    if expiry < now.date() or (market != "NIFTY" and expiry == now.date()):
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

    def quotes(self, contracts, now, *, strict=True):
        result = []
        last_error = None
        stream_books = {}
        if self._nifty_stream and contracts:
            keys = [f'NFO|{contract.token}' for contract in contracts if contract.exchange == 'NFO']
            if keys:
                stream_books = self._nifty_stream.latest_books(keys)
        for contract in contracts:
            try:
                if contract.exchange == 'NFO':
                    streamed = self._stream_quote(contract,stream_books.get(f'NFO|{contract.token}'),now)
                    if streamed is not None:
                        result.append(streamed)
                        continue
                key = (contract.exchange, contract.token)
                with self._quote_lock:
                    contract_lock = self._quote_locks.setdefault(key, threading.Lock())
                with contract_lock:
                    with self._quote_lock:
                        cached = self._quote_cache.get(key)
                    if cached is not None and time.monotonic() - cached[0] < 1.0:
                        data = cached[1]
                    else:
                        data = self._call("GetQuotes", exch=contract.exchange, token=contract.token)
                        with self._quote_lock:
                            self._quote_cache[key] = (time.monotonic(), data)
                            if len(self._quote_cache) > 64:
                                self._quote_cache = {k: v for k, v in self._quote_cache.items()
                                                     if time.monotonic() - v[0] < 2.0}
                if data.get("tsym") != contract.symbol or int(number(data.get("ls", 0))) != contract.lot_size:
                    raise FeedError("Broker contract metadata differs from symbol master")
                result.append(parse_quote(contract, data))
            except (KeyError, ValueError):
                last_error = FeedError("Broker returned incomplete depth data")
                if strict:
                    raise last_error from None
            except FeedError as exc:
                last_error = exc
                if strict:
                    raise
        if contracts and not result and last_error is not None:
            raise last_error
        return result

    def snapshot(self, market, now, held=(), strategy_id=None, spot_override=None):
        from .catalog import resolve
        interval = resolve(market, strategy_id).bar_minutes
        if held:
            quotes = self.quotes(held, now, strict=False)
            try:
                bars = self.bars(market, now, interval) if interval == 1 else []
            except FeedError:
                bars = []  # Missing indicators must not prevent pricing an exit.
            return bars, quotes
        bars = self.bars(market, now, interval)
        if not bars:
            return bars, []
        spot = spot_override if (strategy_id == 'nfv5' and isinstance(spot_override,(int,float))
                                 and math.isfinite(spot_override) and spot_override > 0) else bars[-1].close
        contracts = [c for c in self.contracts(market, now) if c.option_type in {"CE", "PE"}
                     and (c.expiry >= now.date() if market == "NIFTY" else (c.expiry-now.date()).days >= 2)]
        if not contracts:
            return bars, []
        expiry = min(c.expiry for c in contracts)
        contracts = [c for c in contracts if c.expiry == expiry]
        if market == "NIFTY" and strategy_id == "nfv3":
            calls = {c.strike for c in contracts if c.option_type == "CE"}
            puts = {c.strike for c in contracts if c.option_type == "PE"}
            common = calls & puts
            if not common:
                return bars, []
            atm = min(common, key=lambda strike: abs(strike - spot))
            selected = [c for c in contracts if
                        (c.strike == atm and c.option_type in {"CE", "PE"}) or
                        (c.strike == atm + 1000 and c.option_type == "CE") or
                        (c.strike == atm - 1000 and c.option_type == "PE")]
            if len(selected) != 4:
                return bars, []
            return bars, self.quotes(selected, now, strict=False)
        if market == "MCX" and strategy_id in ("mcxv1", "mcxv3"):
            # Both active MCX versions open at one common ATM strike. Fetching
            # the surrounding 14 strikes issued 28 quote calls per worker and
            # repeatedly exhausted the broker's quote endpoint.
            # v1 prefers the full contract; v3's existing plan prefers mini.
            # Choose one matched family/strike before requesting any depth.
            families = (False, True) if strategy_id == "mcxv1" else (True, False)
            for mini in families:
                group = [c for c in contracts if c.symbol.upper().startswith("NATGASMINI") == mini]
                calls = {c.strike for c in group if c.option_type == "CE"}
                puts = {c.strike for c in group if c.option_type == "PE"}
                common = calls & puts
                if common:
                    atm = min(common, key=lambda strike: abs(strike - spot))
                    selected = [c for c in group if c.strike == atm and c.option_type in ("CE", "PE")]
                    if len(selected) == 2:
                        return bars, self.quotes(selected, now, strict=False)
            return bars, []
        strikes = sorted({c.strike for c in contracts}, key=lambda k: abs(k-spot))[:(1 if strategy_id == "nfv5" else 14)]
        if strategy_id in ("nfv1", "nfv5"):
            # These paper baskets use wings at least 1000 points from ATM.
            # Request only the closest eligible wings, not the whole chain.
            strikes = strikes[:(1 if strategy_id == "nfv5" else 3)]
            atm = strikes[0]
            for predicate in (lambda strike: strike >= atm+1000,
                              lambda strike: strike <= atm-1000):
                wings = [c.strike for c in contracts if predicate(c.strike)]
                if wings:
                    strikes.append(min(wings, key=lambda strike: abs(abs(strike-atm)-1000)))
        selected = [c for c in contracts if c.strike in strikes and
                    (strategy_id != "nfv5" or c.strike == atm or
                     (c.strike > atm and c.option_type == 'CE') or
                     (c.strike < atm and c.option_type == 'PE'))]
        return bars, self.quotes(selected, now, strict=False)
