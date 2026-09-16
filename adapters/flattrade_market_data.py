"""Flattrade market data wrapper; importing this module never requires credentials."""
from __future__ import annotations
from datetime import datetime, timezone
import os
import csv
import re
from pathlib import Path
from dataclasses import dataclass
from core_engine.models import Quote
from typing import Protocol


class ATMStraddleIVProvider(Protocol):
    """Optional live-IV contract used by NIFTY sizing."""
    def atm_straddle_iv(self, underlying: str) -> float: ...


class FlattradeMarketData:
    def __init__(self, api=None, symbols: dict[str, tuple[str, str]] | None = None):
        self.api = api
        self.symbols = symbols or {
            "NIFTY": ("NSE", os.getenv("NIFTY_TOKEN", "26000")),
            "MCX-NATGAS": ("MCX", os.getenv("MCX_NATGAS_TOKEN", "")),
        }
        self.latest: dict[str, Quote] = {}
        self._contract_cache: dict[str, dict] = {}

    def _ensure_api(self):
        if self.api is not None:
            return self.api
        # api_helper itself is safe to import, while authentication remains
        # entirely opt-in through the existing Flattrade token flow.
        token_file = os.getenv("FLATTRADE_TOKEN_FILE", "token.txt")
        user_id = os.getenv("FLATTRADE_USER_ID")
        if not user_id or not os.path.exists(token_file):
            return None
        try:
            from api_helper import NorenApiPy
            api = NorenApiPy()
            with open(token_file) as token:
                api.set_session(userid=user_id, password="", usertoken=token.read().strip())
            self.api = api
        except Exception:
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
                return Quote(symbol, now, float(result["bp"]), float(result["sp"]), float(result["lp"]))
            except Exception:
                pass
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
                return None
            item = candidates[0]
            contract = str(item.get("token", ""))
            response = api.get_quotes(exchange=search_exchange, token=contract)
            now = datetime.now(timezone.utc)
            return Quote(str(item.get("tsym", logical_symbol)), now,
                         float(response.get("bp", response.get("lp", 0.0))),
                         float(response.get("sp", response.get("lp", 0.0))),
                         float(response.get("lp", 0.0)))
        except (KeyError, TypeError, ValueError):
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
                match = re.search(r"(\d{2})([A-Z]{3})(\d{2,4})", tsym)
                if match:
                    for fmt in ("%d%b%Y", "%d%b%y"):
                        try:
                            expiry = datetime.strptime("".join(match.groups()), fmt).date()
                            break
                        except ValueError:
                            pass
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
