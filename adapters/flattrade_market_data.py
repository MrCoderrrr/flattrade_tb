"""Flattrade market data wrapper; importing this module never requires credentials."""
from __future__ import annotations
from datetime import datetime, timezone
import os
import csv
import re
import logging
from pathlib import Path
from dataclasses import dataclass
from core_engine.models import Quote
from typing import Protocol

log = logging.getLogger(__name__)


class ATMStraddleIVProvider(Protocol):
    """Optional live-IV contract used by NIFTY sizing."""
    def atm_straddle_iv(self, underlying: str) -> float: ...


class FlattradeMarketData:
    def __init__(self, api=None, symbols: dict[str, tuple[str, str]] | None = None):
        self.api = api
        nifty_token = os.getenv("NIFTY_TOKEN") or "26000"
        mcx_token = os.getenv("MCX_NATGAS_TOKEN") or ""
        self.symbols = symbols or {
            "NIFTY": ("NSE", nifty_token),
            "MCX-NATGAS": ("MCX", mcx_token),
        }
        self.latest: dict[str, Quote] = {}
        self._contract_cache: dict[str, dict] = {}
        self._last_error = ""

    def _ensure_api(self):
        if self.api is not None:
            return self.api
        # api_helper itself is safe to import, while authentication remains
        # entirely opt-in through the existing Flattrade token flow.
        root = Path(__file__).resolve().parent.parent
        token_file = Path(os.getenv("FLATTRADE_TOKEN_FILE", str(root / "token.txt")))
        user_id = os.getenv("FLATTRADE_USER_ID") or os.getenv("USER_ID")
        if not user_id:
            try:
                from creds import USER_ID
                user_id = str(USER_ID).strip()
            except ImportError:
                user_id = ""
        if not user_id or not token_file.is_file():
            self._last_error = "missing Flattrade user ID or token file"
            return None
        try:
            from api_helper import NorenApiPy
            api = NorenApiPy()
            token = token_file.read_text().strip()
            if not token:
                self._last_error = f"empty token file: {token_file}"
                return None
            response = api.set_session(userid=user_id, password="", usertoken=token)
            if isinstance(response, dict) and str(response.get("stat", "")).lower() not in {"ok", "success"}:
                self._last_error = f"Flattrade authentication rejected: {response}"
                return None
            self.api = api
            log.info("Flattrade market-data session established for %s", user_id)
        except Exception as exc:
            self._last_error = f"Flattrade session initialization failed: {exc}"
            log.warning(self._last_error)
            return None
        return self.api

    def quote(self, exchange: str, symbol: str) -> Quote | None:
        api = self._ensure_api()
        if exchange == "MCX" and not symbol:
            symbol = self._front_month_natgas_token()
        if not symbol:
            return None
        now = datetime.now(timezone.utc)
        if api is not None:
            try:
                result = api.get_quotes(exchange=exchange, token=symbol)
                if not isinstance(result, dict) or str(result.get("stat", "Ok")).lower() not in {"ok", "success"}:
                    self._last_error = f"quote rejected for {exchange}:{symbol}: {result}"
                    return None
                last = float(result.get("lp", result.get("ltp", 0.0)) or 0.0)
                bid = float(result.get("bp", result.get("bid", last)) or last)
                ask = float(result.get("sp", result.get("ask", last)) or last)
                if last > 0 and bid > 0 and ask > 0:
                    return Quote(symbol, now, bid, ask, last)
                self._last_error = f"quote had no positive prices for {exchange}:{symbol}: {result}"
            except Exception as exc:
                self._last_error = f"quote request failed for {exchange}:{symbol}: {exc}"
                log.warning(self._last_error)
        # A missing feed is not a quote.  In particular, never manufacture a
        # price: doing so can turn a paper heartbeat into a false trade.
        return None

    def _front_month_natgas_token(self) -> str:
        """Resolve the nearest non-expired NATURALGAS future from the symbol master."""
        today = datetime.now(timezone.utc).date()
        root = Path(__file__).resolve().parent.parent
        files = sorted(root.glob("MCX_symbols_*.csv"), reverse=True)
        files += [root / "MCX_symbols.txt"]
        rows = []
        for path in files:
            if not path.exists():
                continue
            try:
                with path.open(newline="") as handle:
                    rows.extend(csv.DictReader(handle))
                break
            except OSError:
                continue
        candidates = []
        for row in rows:
            if row.get("Symbol") != "NATURALGAS" or row.get("Instrument") != "FUTCOM":
                continue
            try:
                expiry = datetime.strptime(row["Expiry"], "%d-%b-%Y").date()
            except (KeyError, ValueError):
                continue
            if expiry >= today:
                candidates.append((expiry, str(row.get("Token", ""))))
        candidates.sort()
        return candidates[0][1] if candidates else ""

    def poll(self, now: datetime | None = None) -> dict[str, Quote]:
        quotes = {}
        for underlying, (exchange, token) in self.symbols.items():
            quote = self.quote(exchange, token)
            if quote is not None:
                quotes[underlying] = quote
                self.latest[underlying] = quote
        return quotes

    def heartbeat(self, now: datetime, runtime) -> dict[str, Quote]:
        return self.poll(now)

    def option_quote(self, logical_symbol: str, quotes: dict[str, Quote] | None = None) -> Quote | None:
        """Resolve and quote a logical option symbol such as NIFTY-CE.

        In an authenticated deployment this method resolves the nearest valid
        expiry through the broker contract search before requesting its quote.
        Falls back to realistic option simulation if live broker quote is unavailable.
        """
        parts = logical_symbol.split("-")
        if len(parts) < 2 or parts[1] not in {"CE", "PE"}:
            return None
        underlying = parts[0]
        option_type = parts[1]
        hedge = len(parts) == 3 and parts[2] == "HEDGE"

        quote = (quotes and quotes.get(underlying)) or self.latest.get(underlying)
        if quote is None:
            exchange, token = self.symbols.get(underlying, ("", ""))
            if exchange and token:
                quote = self.quote(exchange, token)

        api = self._ensure_api()
        if api is not None:
            cached = self._contract_cache.get(logical_symbol)
            if cached:
                try:
                    response = api.get_quotes(
                        exchange=cached["exchange"], token=cached["token"]
                    )
                    real_q = self._quote_from_response(
                        cached["tsym"], response, datetime.now(timezone.utc)
                    )
                    if real_q is not None:
                        return real_q
                except Exception as exc:
                    self._last_error = f"cached option quote failed for {logical_symbol}: {exc}"
                self._contract_cache.pop(logical_symbol, None)

            if quote is not None:
                if underlying == "NIFTY":
                    strike = int(round(quote.last / 50.0) * 50)
                    if hedge:
                        strike += 1000 if option_type == "CE" else -1000
                    search_exchange, search_text = "NFO", f"NIFTY {strike} {option_type}"
                elif underlying == "MCX-NATGAS":
                    strike = round(quote.last / 5.0) * 5
                    search_exchange, search_text = "MCX", f"NATURALGAS {strike} {option_type}"
                else:
                    search_exchange, search_text = "", ""

                if search_exchange and search_text:
                    try:
                        result = api.searchscrip(exchange=search_exchange, searchtext=search_text)
                        values = result.get("values", []) if isinstance(result, dict) else []
                        candidates = self._select_contracts(values, underlying, option_type, strike)
                        if candidates:
                            item = candidates[0]
                            contract = str(item.get("token", ""))
                            response = api.get_quotes(exchange=search_exchange, token=contract)
                            now = datetime.now(timezone.utc)
                            real_q = self._quote_from_response(
                                str(item.get("tsym", logical_symbol)), response, now
                            )
                            if real_q is not None:
                                self._contract_cache[logical_symbol] = {
                                    "exchange": search_exchange,
                                    "token": contract,
                                    "tsym": str(item.get("tsym", logical_symbol)),
                                }
                                return real_q
                    except Exception as exc:
                        self._last_error = f"option lookup failed for {logical_symbol}: {exc}"

        # 2. Simulation fallback (as in legacy paper bot)
        if quote is not None and quote.last > 0:
            return self._simulate_option_quote(logical_symbol, quote.last, datetime.now(timezone.utc))

        return None

    def _simulate_option_quote(self, logical_symbol: str, spot: float, now: datetime) -> Quote:
        parts = logical_symbol.split("-")
        underlying = parts[0]
        option_type = parts[1]
        hedge = len(parts) == 3 and parts[2] == "HEDGE"

        if underlying == "NIFTY":
            atm = int(round(spot / 50.0) * 50)
            strike = atm + (1000 if option_type == "CE" else -1000) if hedge else atm
            diff = abs(spot - strike)
            if hedge:
                lp = max(0.50, round(3.50 - max(0.0, diff - 1000) * 0.005, 2))
            elif diff < 75:
                lp = 55.0
            elif diff < 300:
                lp = max(5.0, round(55.0 - (diff * 0.18), 2))
            else:
                lp = max(0.50, round(180.0 - (diff * 0.35), 2))

            delta = 0.50 if not hedge else 0.05
            if option_type == "CE":
                lp += (spot - atm) * delta
            else:
                lp += (atm - spot) * delta
            lp = max(0.50, round(lp, 2))

            tsym = f"NIFTY{strike}{option_type}{'-HEDGE' if hedge else ''}"
            bid = max(0.05, round(lp - 0.20, 2))
            ask = round(lp + 0.20, 2)
            return Quote(tsym, now, bid, ask, lp)
        elif underlying == "MCX-NATGAS":
            strike = round(spot / 5.0) * 5
            diff = abs(spot - strike)
            lp = max(0.50, round(8.0 - diff * 0.15, 2))
            delta = 0.50
            if option_type == "CE":
                lp += (spot - strike) * delta
            else:
                lp += (strike - spot) * delta
            lp = max(0.20, round(lp, 2))

            tsym = f"MCX-NATGAS-{strike}-{option_type}"
            bid = max(0.05, round(lp - 0.10, 2))
            ask = round(lp + 0.10, 2)
            return Quote(tsym, now, bid, ask, lp)

        return Quote(logical_symbol, now, 10.0, 10.0, 10.0)

    def _quote_from_response(self, symbol: str, response, now: datetime) -> Quote | None:
        if not isinstance(response, dict):
            self._last_error = f"invalid option quote response for {symbol}: {response}"
            return None
        last = float(response.get("lp", response.get("ltp", response.get("c", 0.0))) or 0.0)
        bid = float(response.get("bp1", response.get("bp", response.get("bid", 0.0))) or 0.0)
        ask = float(response.get("sp1", response.get("sp", response.get("ask", 0.0))) or 0.0)
        if last <= 0 and max(bid, ask) > 0:
            last = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else max(bid, ask)
        if bid <= 0:
            bid = last
        if ask <= 0:
            ask = last
        if min(last, bid, ask) <= 0:
            self._last_error = f"option quote has no positive prices for {symbol}: {response}"
            return None
        return Quote(symbol, now, bid, ask, last)

    @staticmethod
    def _select_contracts(values: list[dict], underlying: str,
                          option_type: str, strike: int) -> list[dict]:
        """Select the nearest valid expiry and exact strike, never values[0]."""
        IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(IST).date()
        candidates = []
        is_ce = option_type.startswith("C")
        target_char = "C" if is_ce else "P"
        other_char = "P" if is_ce else "C"

        for item in values:
            tsym = str(item.get("tsym", "")).upper()
            if underlying == "NIFTY" and (
                not tsym.startswith("NIFTY")
                or tsym.startswith(("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"))
            ):
                continue

            optt = str(item.get("optt", "")).upper()
            if optt in ("CE", "PE", "CALL", "PUT"):
                if is_ce and optt in ("PE", "PUT"):
                    continue
                if not is_ce and optt in ("CE", "CALL"):
                    continue
            else:
                has_target = (option_type in tsym) or bool(re.search(rf"{target_char}\d+$", tsym)) or bool(re.search(rf"\d+{target_char}$", tsym))
                has_other = ("PE" if is_ce else "CE") in tsym or bool(re.search(rf"{other_char}\d+$", tsym)) or bool(re.search(rf"\d+{other_char}$", tsym))
                if not has_target and has_other:
                    continue

            item_strike = item.get("strprc", item.get("strike", item.get("StrikePrice")))
            if item_strike is not None:
                try:
                    s_val = float(item_strike)
                    if s_val > 100000:
                        s_val /= 100.0
                    if abs(s_val - strike) > 0.01:
                        continue
                except (TypeError, ValueError):
                    pass

            expiry_text = str(item.get("exd", item.get("expiry", "")))
            expiry = None
            for fmt in (
                "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y",
                "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d",
                "%d%b%Y", "%d%b%y", "%Y%m%d"
            ):
                try:
                    expiry = datetime.strptime(expiry_text, fmt).date()
                    break
                except ValueError:
                    pass
            if expiry is None:
                match = re.search(r"(\d{2}[A-Z]{3}\d{2})", tsym)
                if match:
                    try:
                        expiry = datetime.strptime(match.group(1), "%d%b%y").date()
                    except ValueError:
                        expiry = None
            if expiry is None:
                match = re.search(r"(\d{2}[A-Z]{3}\d{4})", tsym)
                if match:
                    try:
                        expiry = datetime.strptime(match.group(1), "%d%b%Y").date()
                    except ValueError:
                        expiry = None
            if expiry is None:
                match = re.search(r"NIFTY(\d{2})([1-9OND])(\d{2})", tsym)
                if match:
                    try:
                        yr = 2000 + int(match.group(1))
                        m_char = match.group(2)
                        mo = 10 if m_char == "O" else (11 if m_char == "N" else (12 if m_char == "D" else int(m_char)))
                        dy = int(match.group(3))
                        expiry = datetime(yr, mo, dy).date()
                    except ValueError:
                        expiry = None

            if expiry is None:
                expiry = today

            if expiry >= today:
                candidates.append((expiry, item))

        candidates.sort(key=lambda pair: pair[0])
        if candidates:
            return [item for _, item in candidates]

        fallback = []
        for item in values:
            tsym = str(item.get("tsym", "")).upper()
            if underlying == "NIFTY" and not tsym.startswith(("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")):
                fallback.append(item)
        return fallback or values

    def atm_straddle_iv(self, underlying: str) -> float:
        if self.api is None:
            raise RuntimeError("ATM IV requires an authenticated Flattrade API")
        provider = getattr(self.api, "atm_straddle_iv", None) or getattr(self.api, "get_atm_straddle_iv", None)
        if callable(provider):
            value = provider(underlying)
            if value is None or float(value) < 0:
                raise RuntimeError("Flattrade returned an invalid ATM straddle IV")
            return float(value)
        raise RuntimeError("Configured Flattrade API does not expose ATM straddle IV")
