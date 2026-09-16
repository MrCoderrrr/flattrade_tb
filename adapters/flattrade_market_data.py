"""Flattrade market data wrapper; importing this module never requires credentials."""
from __future__ import annotations
from datetime import datetime, timezone
import os
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
            "NIFTY": ("NSE", os.getenv("NIFTY_TOKEN", "NIFTY")),
            "MCX-NATGAS": ("MCX", os.getenv("MCX_NATGAS_TOKEN", "NATGAS")),
        }
        self.latest: dict[str, Quote] = {}

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
        if len(parts) != 2 or parts[1] not in {"CE", "PE"}:
            return None
        underlying, option_type = parts
        if underlying == "NIFTY":
            exchange, token = "NSE", self.symbols["NIFTY"][1]
            quote = self.latest.get("NIFTY")
            if quote is None:
                quote = self.quote(exchange, token)
            if quote is None:
                return None
            strike = int(round(quote.last / 50.0) * 50)
            search_exchange, search_text = "NFO", f"NIFTY {strike} {option_type}"
        elif underlying == "MCX-NATGAS":
            exchange, token = "MCX", self.symbols["MCX-NATGAS"][1]
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
            if not values:
                return None
            item = values[0]
            contract = str(item.get("token", ""))
            response = api.get_quotes(exchange=search_exchange, token=contract)
            now = datetime.now(timezone.utc)
            return Quote(str(item.get("tsym", logical_symbol)), now,
                         float(response.get("bp", response.get("lp", 0.0))),
                         float(response.get("sp", response.get("lp", 0.0))),
                         float(response.get("lp", 0.0)))
        except (KeyError, TypeError, ValueError):
            return None

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
