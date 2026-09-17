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

    def option_quote(self, logical_symbol: str) -> Quote | None:
        """Resolve and quote a logical option symbol such as NIFTY-CE.

        The runtime uses logical symbols so paper tests remain deterministic.
        In an authenticated deployment this method resolves the nearest valid
        expiry through the broker contract search before requesting its quote.
        """
        api = self._ensure_api()
        if api is None:
            return None
        parts = logical_symbol.split("-")
        if len(parts) < 2 or parts[1] not in {"CE", "PE"}:
            return None
        underlying, option_type = parts
        hedge = len(parts) == 3 and parts[2] == "HEDGE"
        if underlying == "NIFTY":
            exchange, token = "NSE", self.symbols["NIFTY"][1]
            quote = self.latest.get("NIFTY")
            if quote is None:
                quote = self.quote(exchange, token)
            if quote is None:
                return None
            strike = int(round(quote.last / 50.0) * 50)
            if hedge:
                strike += 1000 if option_type == "CE" else -1000
            search_exchange, search_text = "NFO", f"NIFTY {strike} {option_type}"
        elif underlying == "MCX-NATGAS":
            exchange, token = "MCX", self.symbols["MCX-NATGAS"][1] or self._front_month_natgas_token()
            quote = self.latest.get("MCX-NATGAS")
            if quote is None:
                quote = self.quote(exchange, token)
            if quote is None:
                return None
            strike = round(quote.last / 5.0) * 5
            search_exchange, search_text = "MCX", f"NATURALGAS {strike} {option_type}"
        else:
            return None
        try:
            result = api.searchscrip(exchange=search_exchange, searchtext=search_text)
            values = result.get("values", []) if isinstance(result, dict) else []
            candidates = self._select_contracts(values, underlying, option_type, strike)
            if not candidates:
                self._last_error = (
                    f"no current {underlying} {option_type} contract for strike {strike}; "
                    f"search returned {len(values)} results"
                )
                return None
            item = candidates[0]
            contract = str(item.get("token", ""))
            response = api.get_quotes(exchange=search_exchange, token=contract)
            if not isinstance(response, dict):
                self._last_error = f"invalid option quote response for {contract}: {response}"
                return None
            now = datetime.now(timezone.utc)
            last = float(response.get("lp", response.get("ltp", 0.0)) or 0.0)
            bid = float(response.get("bp", response.get("bid", last)) or last)
            ask = float(response.get("sp", response.get("ask", last)) or last)
            if min(last, bid, ask) <= 0:
                self._last_error = f"option quote has no positive prices for {contract}: {response}"
                return None
            return Quote(str(item.get("tsym", logical_symbol)), now, bid, ask, last)
        except (KeyError, TypeError, ValueError) as exc:
            self._last_error = f"option lookup failed for {logical_symbol}: {exc}"
            return None

    @staticmethod
    def _select_contracts(values: list[dict], underlying: str,
                          option_type: str, strike: int) -> list[dict]:
        """Select the nearest valid expiry and exact strike, never values[0]."""
        today = datetime.now(timezone.utc).date()
        candidates = []
        for item in values:
            tsym = str(item.get("tsym", "")).upper()
            if underlying == "NIFTY" and (
                not tsym.startswith("NIFTY")
                or tsym.startswith(("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"))
            ):
                continue
            if option_type not in tsym:
                continue
            item_strike = item.get("strprc", item.get("strike", item.get("StrikePrice")))
            if item_strike is not None:
                try:
                    if abs(float(item_strike) - strike) > 0.01:
                        continue
                except (TypeError, ValueError):
                    continue
            expiry_text = str(item.get("exd", item.get("expiry", "")))
            expiry = None
            for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
                try:
                    expiry = datetime.strptime(expiry_text, fmt).date()
                    break
                except ValueError:
                    pass
            if expiry is None:
                # Flattrade symbols commonly encode expiry as 25SEP26,
                # immediately after the NIFTY prefix. Parse two-digit years
                # before attempting a four-digit fallback.
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
            if expiry is not None and expiry >= today:
                candidates.append((expiry, item))
        candidates.sort(key=lambda pair: pair[0])
        return [item for _, item in candidates]

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
