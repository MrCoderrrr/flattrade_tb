"""Deterministic, paper-only trading engine.

The runtime deliberately knows nothing about broker order placement.  A quote
source may be injected in tests (or by a paper simulator); an unauthenticated
Flattrade source simply returns no data.
"""
from __future__ import annotations

import logging
import json
import atexit
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adapters.flattrade_market_data import FlattradeMarketData
from adapters.paper_execution import PaperExecution
from adapters.telegram_notifier import TelegramNotifier
from core_engine.config import DEFAULT_CONFIG
from core_engine.indicators import IndicatorRegistry
from core_engine.models import Bar, Order, Position, Quote, Side, TriggerType
from core_engine.risk_guardian import RiskGuardian
from core_engine.trade_logger import TradeLogger
from strategies.mcx_natgas import MCXNatGasStrategy
from strategies.nifty_options import NiftyOptionsStrategy

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _in_window(now: datetime, start: str, end: str) -> bool:
    value = now.strftime("%H:%M")
    return start <= value < end


class TerminalDashboard:
    """Boxed once-per-heartbeat dashboard (ANSI redraw, never scrolls)."""

    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"

    def render(self, runtime: "TradingRuntime", now: datetime) -> str:
        now_ist = now.astimezone(IST)
        if now_ist.weekday() < 5 and _in_window(now_ist, "09:15", "15:15"):
            strategy = "NIFTY"
            prefix = "NIFTY-"
        elif now_ist.weekday() != 5 and _in_window(now_ist, "18:00", "23:25"):
            strategy = "MCX NATURAL GAS"
            prefix = "MCX-NATGAS-"
        else:
            return self._inactive(runtime, now_ist)
        return self._strategy_dashboard(runtime, now_ist, strategy, prefix)

    def _strategy_dashboard(self, runtime, now, strategy: str, prefix: str) -> str:
        width = 118
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S IST")
        underlying = "NIFTY" if prefix == "NIFTY-" else "MCX-NATGAS"
        quote = runtime.quotes.get(underlying)
        snapshot = runtime.snapshots.get(underlying, {})
        active = [
            p for symbol, p in runtime.execution.positions.items()
            if (symbol.startswith(prefix) or symbol.startswith(underlying)) and p.quantity
        ]
        lines = [
            self._line("╔", "═", "╗", width),
            self._row(f"  PAPER {strategy} DASHBOARD{' ' * 25}{timestamp}", width),
            self._line("╠", "═", "╣", width),
            self._row(
                f"  FEED  {self._feed(runtime, now):<26}"
                f"  WARMUP  {self._warmup(runtime):<5}"
                f"  RISK HALT  {'YES' if runtime.risk.halted else 'NO':<3}"
                f"  HEARTBEAT  {runtime.heartbeat_count:<8}",
                width,
            ),
            self._line("╠", "═", "╣", width),
            self._row("  MARKET DATA", width),
            self._line("╟", "─", "╢", width),
            self._row(
                f"  {'UNDERLYING':<14}│{'SPOT':>11}│{'EMA 15':>11}│"
                f"{'EMA 90':>11}│{'SLOW SLOPE':>12}│{'VR':>8}│"
                f"{'PERSIST':>9}│{'ADX 300':>10}│{'ATR 14':>10}",
                width,
            ),
        ]
        if quote is None:
            lines.append(self._row(f"  {underlying:<14}│{'NO QUOTE - NO TRADE':>11}", width))
        else:
            lines.append(self._row(
                f"  {underlying:<14}│{quote.last:>11.2f}│"
                f"{self._num(snapshot.get('ema_15')):>11}│"
                f"{self._num(snapshot.get('ema_90')):>11}│"
                f"{self._signed(snapshot.get('slow_slope'), 12)}│"
                f"{self._signed(snapshot.get('vr_ratio'), 8)}│"
                f"{self._num(snapshot.get('persistence_raw')):>9}│"
                f"{self._signed(snapshot.get('adx_300_1s'), 10)}│"
                f"{self._signed(snapshot.get('atr_1m'), 10)}",
                width,
            ))
        stat_mid = f"FLIPS  {runtime.mcx.state.flips}/4" if prefix == "MCX-NATGAS-" else f"DTE    {int(getattr(runtime.market_data, 'nifty_dte', 0))}d  "
        lines.extend([
            self._line("╠", "═", "╣", width),
            self._row("  STRATEGY STATUS", width),
            self._line("╟", "─", "╢", width),
            self._row(
                f"  STATE  {self._strategy_state(runtime, prefix):<24}"
                f"│  {stat_mid}"
                f"│  ACTIVE LEGS  {len(active):>2}",
                width,
            ),
            self._row(
                f"  SIGNAL  {self._signal_status(runtime, underlying):<20}"
                f"│  OPTION DATA  {self._option_status(runtime, prefix):<40}",
                width,
            ),
            self._line("╠", "═", "╣", width),
            self._row("  OPEN PAPER POSITIONS", width),
            self._line("╟", "─", "╢", width),
            self._row(
                f"  {'LEG':<10}│{'CONTRACT':<20}│{'STRIKE':>7}│{'SPOT':>10}│{'SIDE':<6}│"
                f"{'QTY':>5}│{'ENTRY':>9}│{'CURRENT':>9}│{'SL':>9}│"
                f"{'TSL':>9}│{'UNREAL PNL':>14}",
                width,
            )
        ])
        if not active:
            lines.append(self._row("  No open paper positions", width))
        spot_q = runtime.quotes.get(underlying)
        spot_str = f"{spot_q.last:>10.2f}" if spot_q else "       n/a"
        for p in active:
            q = runtime.quotes.get(p.symbol) or runtime.execution.last_quotes.get(p.symbol)
            mark = (q.bid if p.quantity > 0 else q.ask) if q else None
            unreal = p.quantity * (mark - p.average_price) if mark is not None else None
            side = "LONG" if p.quantity > 0 else "SHORT"
            sl, tsl = self._stops(runtime, p, prefix, snapshot)

            _, strike_val, opt_type, is_hedge = FlattradeMarketData.parse_option_symbol(p.symbol)
            leg_name = f"{opt_type}_HEDGE" if is_hedge else opt_type

            if strike_val:
                strike_str = f"{strike_val:>7}"
            elif spot_q:
                step = 50.0 if underlying == "NIFTY" else 5.0
                atm = int(round(spot_q.last / step) * step)
                hedge_offset = 1000 if underlying == "NIFTY" else 20
                s = atm + (hedge_offset if opt_type == "CE" else -hedge_offset) if is_hedge else atm
                strike_str = f"{s:>7}"
            else:
                strike_str = "    n/a"

            current_ltp = q.last if q and q.last > 0 else mark
            lines.append(self._row(
                f"  {leg_name:<10}│{p.symbol[:20]:<20}│{strike_str}│{spot_str}│{side:<6}│"
                f"{p.quantity:>+5}│{p.average_price:>9.2f}│{self._num(current_ltp):>9}│"
                f"{self._num(sl):>9}│{self._num(tsl):>9}│"
                f"{self._money(unreal or 0.0, 14)}",
                width,
            ))
        realized = runtime.execution.cash_pnl
        net = runtime.execution.mark_to_market(runtime.quotes)
        unrealized = net - realized
        lines.extend([
            self._line("╠", "═", "╣", width),
            self._row("  P&L AND NEXT ACTION", width),
            self._line("╟", "─", "╢", width),
            self._row(
                f"  {'REALIZED':<12}│{self._money(realized, 15)}│"
                f"{'UNREALIZED':<12}│{self._money(unrealized, 15)}│"
                f"{'NET':<12}│{self._money(net, 15)}│"
                f"{'OPEN':<6}{len(active):>3}",
                width,
            ),
            self._row(f"  NEXT  {runtime.next_action}", width),
            self._line("╚", "═", "╝", width),
        ])
        return "\n".join(lines)

    def _inactive(self, runtime, now) -> str:
        width = 118
        return "\n".join([
            self._line("╔", "═", "╗", width),
            self._row(f"  PAPER TRADING SYSTEM{' ' * 39}{now.strftime('%Y-%m-%d %H:%M:%S IST')}", width),
            self._line("╠", "═", "╣", width),
            self._row("  NO STRATEGY SESSION ACTIVE", width),
            self._row("  NIFTY: 09:15–15:15 IST  │  MCX NATURAL GAS: 18:00–23:25 IST", width),
            self._row("  Scheduler/runtime is waiting for the next configured paper session.", width),
            self._line("╚", "═", "╝", width),
        ])

    @staticmethod
    def _strategy_state(runtime, prefix: str) -> str:
        if prefix == "NIFTY-":
            return runtime.nifty.state
        return TerminalDashboard._mcx_state(runtime)

    @staticmethod
    def _signal_status(runtime, underlying: str) -> str:
        snapshot = runtime.snapshots.get(underlying, {})
        if snapshot.get("warmup"):
            return "WARMUP"
        fast, medium, slope = snapshot.get("ema_15"), snapshot.get("ema_90"), snapshot.get("slow_slope")
        if fast is None or medium is None or slope is None:
            return "INDICATORS INCOMPLETE"
        
        direction = 1 if fast > medium and slope > 0 else (-1 if fast < medium and slope < 0 else 0)
        if direction == 0:
            return "NO CONFIRMED SETUP"
            
        base_str = "UP CANDIDATE" if direction == 1 else "DOWN CANDIDATE"
        prior = runtime._pending.get(underlying)
        if prior and prior[0] == direction:
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            elapsed = (now - prior[1]).total_seconds()
            required = snapshot.get("persistence_raw", 30.0)
            if elapsed < required:
                return f"{base_str} (WAIT {int(required - elapsed)}s)"
            else:
                return f"{base_str} (READY)"
        return base_str

    @staticmethod
    def _option_status(runtime, prefix: str) -> str:
        if prefix != "NIFTY-":
            return "n/a"
        quote = runtime.quotes.get("NIFTY")
        if quote is None:
            return "waiting for spot"
        atm = int(round(quote.last / 50.0) * 50)
        dte = int(getattr(runtime.market_data, "nifty_dte", 0))
        if runtime._nifty_entered:
            return f"RESOLVED (ATM {atm} | DTE {dte}d)"
        return f"ATM {atm} (CE/PE {atm} | HEDGE {atm+1000}/{atm-1000} | DTE {dte}d)"

    @staticmethod
    def _stops(runtime, position, prefix: str, snapshot: dict):
        if position.quantity >= 0:
            return None, None
        if prefix == "MCX-NATGAS-":
            sl = position.average_price * (1.0 + runtime.config.mcx_stop_loss_pct)
            best = runtime._mcx_high.get(position.symbol, position.average_price)
            atr_value = snapshot.get("atr_1m")
            tsl = best + runtime.config.mcx_k * atr_value if atr_value else None
            return sl, tsl
        entry_p = position.average_price
        lowest_p = runtime._nifty_low.get(position.symbol, entry_p) if hasattr(runtime, "_nifty_low") else entry_p
        dte = int(getattr(runtime.market_data, "nifty_dte", 0))
        trail_pct = 0.05 if dte <= 1 else 0.07
        initial_sl = entry_p * 1.35
        tsl = lowest_p * (1.0 + trail_pct) if lowest_p <= entry_p * 0.95 else None
        return initial_sl, tsl

    @staticmethod
    def _line(left: str, fill: str, right: str, width: int) -> str:
        return f"{left}{fill * width}{right}"

    @staticmethod
    def _row(text: str, width: int) -> str:
        visible = TerminalDashboard._visible_length(text)
        return f"║{text}{' ' * max(0, width - visible)}║"

    @staticmethod
    def _visible_length(value: str) -> int:
        result = value
        for code in (
            TerminalDashboard.GREEN,
            TerminalDashboard.RED,
            TerminalDashboard.YELLOW,
            TerminalDashboard.RESET,
        ):
            result = result.replace(code, "")
        return len(result)

    @classmethod
    def _signed(cls, value, width: int) -> str:
        if value is None:
            return f"{'n/a':>{width}}"
        number = float(value)
        color = cls.GREEN if number > 0 else cls.RED if number < 0 else cls.YELLOW
        text = f"{number:.3f}".rjust(width)
        return f"{color}{text}{cls.RESET}"

    @classmethod
    def _money(cls, value: float, width: int) -> str:
        color = cls.GREEN if value > 0 else cls.RED if value < 0 else cls.YELLOW
        text = f"₹{value:,.2f}".rjust(width)
        return f"{color}{text}{cls.RESET}"

    @staticmethod
    def _mcx_state(runtime) -> str:
        if not runtime.mcx.state.strangle_initialized:
            return "WAITING"
        if runtime.mcx.state.direction > 0:
            return "SINGLE CE / STRANGLE"
        if runtime.mcx.state.direction < 0:
            return "SINGLE PE / STRANGLE"
        return "FLAT"

    @staticmethod
    def _num(value, decimals: int = 2) -> str:
        if value is None:
            return "n/a"
        try:
            d = decimals if decimals <= 6 else 2
            return f"{float(value):.{d}f}"
        except Exception:
            return "n/a"

    @staticmethod
    def _warmup(runtime) -> str:
        return "YES" if any(s.get("warmup") for s in runtime.snapshots.values()) else "NO"

    @staticmethod
    def _feed(runtime, now) -> str:
        now_ist = now.astimezone(IST)
        active_underlyings = []
        if now_ist.weekday() < 5 and _in_window(now_ist, "09:15", "15:15"):
            active_underlyings.append("NIFTY")
        if now_ist.weekday() != 5 and _in_window(now_ist, "18:00", "23:25"):
            active_underlyings.append("MCX-NATGAS")
        ages = []
        for underlying in active_underlyings:
            q = runtime.quotes.get(underlying)
            if q is not None:
                ages.append(max(0.0, (now - q.timestamp).total_seconds()))
        if not ages:
            return "DOWN (no quote)"
        age = max(ages)
        return f"OK age={age:.1f}s" if age <= runtime.config.stale_quote_seconds else f"STALE age={age:.1f}s"


class TradingRuntime:
    """Single-heartbeat paper orchestrator with injectable quotes."""

    def __init__(self, market_data=None, execution=None, *, logger=None,
                 notifier=None, config=DEFAULT_CONFIG, dashboard=True):
        # Runtime refuses live adapters even if one is accidentally supplied.
        if execution is not None and not isinstance(execution, PaperExecution):
            raise ValueError("TradingRuntime is paper-only; inject PaperExecution")
        self.market_data = market_data or FlattradeMarketData()
        self.execution = execution or PaperExecution()
        self.config = config
        self.indicators = IndicatorRegistry(config)
        self.risk = RiskGuardian(config)
        self.mcx = MCXNatGasStrategy(config)
        self.nifty = NiftyOptionsStrategy(config)
        self.logger = logger or TradeLogger()
        self.notifier = notifier or TelegramNotifier(10.0)
        self.quotes: dict[str, Quote] = {}
        self.snapshots: dict[str, dict] = {}
        self.signal_events: list[dict] = []
        self.heartbeat_count = 0
        self.last_status = ""
        self._pending: dict[str, tuple[int, datetime]] = {}
        self._emitted: dict[str, int] = {}
        self._guardian_liquidated = False
        self._mcx_high: dict[str, float] = {}
        self._nifty_entered = False
        self._last_nifty_action_signal = 0
        self.dashboard = TerminalDashboard() if dashboard else None
        self.next_action = "waiting for an authenticated market-data feed"
        self._session_date = None
        self._history_limit = 1200
        self._state_file = Path(os.getenv(
            "PAPER_STATE_FILE",
            str(Path(__file__).resolve().parent / "paper_runtime_state.json"),
        ))
        self._history: dict[str, list[Quote]] = {"NIFTY": [], "MCX-NATGAS": []}
        self._restore_indicator_state()
        atexit.register(self._persist_indicator_state)
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, self._handle_shutdown)
        except ValueError:
            pass

    def _handle_shutdown(self, signum, frame) -> None:
        self._persist_indicator_state()
        raise SystemExit(128 + signum)

    @staticmethod
    def _quote_to_dict(quote: Quote) -> dict:
        return {
            "timestamp": quote.timestamp.isoformat(),
            "bid": quote.bid,
            "ask": quote.ask,
            "last": quote.last,
            "iv": quote.iv,
        }

    @staticmethod
    def _quote_from_dict(symbol: str, value: dict) -> Quote | None:
        try:
            timestamp = datetime.fromisoformat(str(value["timestamp"]))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return Quote(
                symbol,
                timestamp,
                float(value["bid"]),
                float(value["ask"]),
                float(value["last"]),
                None if value.get("iv") is None else float(value["iv"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _restore_indicator_state(self) -> None:
        """Replay today's persisted ticks so a restart does not reset warm-up."""
        if not self._state_file.is_file():
            return
        try:
            payload = json.loads(self._state_file.read_text())
            saved_date = payload.get("date")
            today = datetime.now(IST).date().isoformat()
            if saved_date != today:
                return
            for underlying in ("NIFTY", "MCX-NATGAS"):
                values = payload.get("quotes", {}).get(underlying, [])
                restored = [
                    quote for value in values
                    if isinstance(value, dict)
                    for quote in [self._quote_from_dict(underlying, value)]
                    if quote is not None
                ][-self._history_limit:]
                self._history[underlying] = restored
                for quote in restored:
                    self.quotes[underlying] = quote
                    self.snapshots[underlying] = self.indicators.update(
                        underlying, self._bar(underlying, quote)
                    )
            for symbol, value in payload.get("positions", {}).items():
                position = self.execution.positions.setdefault(
                    symbol, self.execution.positions.get(symbol)
                    or Position(symbol)
                )
                position.quantity = int(value.get("quantity", 0))
                position.average_price = float(value.get("average_price", 0.0))
                position.realized_pnl = float(value.get("realized_pnl", 0.0))
            self.execution.cash_pnl = float(payload.get("cash_pnl", 0.0))
            if any(symbol.startswith("NIFTY-") and position.quantity
                   for symbol, position in self.execution.positions.items()):
                self.nifty.state = "OPEN"
                self._nifty_entered = True
            if any(symbol.startswith("MCX-NATGAS-") and position.quantity
                   for symbol, position in self.execution.positions.items()):
                self.mcx.state.strangle_initialized = True
            restored_count = sum(len(values) for values in self._history.values())
            if restored_count:
                self.next_action = f"restored {restored_count} persisted market ticks"
                log.info("Restored %d persisted market ticks from %s",
                         restored_count, self._state_file)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Could not restore paper runtime state: %s", exc)

    def _persist_indicator_state(self) -> None:
        payload = {
            "date": datetime.now(IST).date().isoformat(),
            "quotes": {
                underlying: [
                    self._quote_to_dict(quote)
                    for quote in values[-self._history_limit:]
                ]
                for underlying, values in self._history.items()
            },
            "positions": {
                symbol: {
                    "quantity": position.quantity,
                    "average_price": position.average_price,
                    "realized_pnl": position.realized_pnl,
                }
                for symbol, position in self.execution.positions.items()
                if position.quantity
            },
            "cash_pnl": self.execution.cash_pnl,
        }
        temporary = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, separators=(",", ":")))
            os.replace(temporary, self._state_file)
        except OSError as exc:
            log.warning("Could not persist paper runtime state: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _bar(self, underlying: str, quote: Quote) -> Bar:
        return Bar(underlying, quote.timestamp, quote.last, quote.last,
                   quote.last, quote.last)

    def _option_quote(self, symbol: str, quotes: dict[str, Quote]) -> Quote | None:
        if symbol in quotes:
            return quotes[symbol]
        for name in ("option_quote", "quote_option", "lookup_option"):
            fn = getattr(self.market_data, name, None)
            if callable(fn):
                try:
                    try:
                        result = fn(symbol, quotes=quotes)
                    except TypeError:
                        result = fn(symbol)
                    if isinstance(result, Quote):
                        return result
                except Exception:
                    log.debug("option quote lookup failed for %s", symbol,
                              exc_info=True)
        return None

    def _submit(self, order: Order, quote: Quote, now: datetime) -> None:
        self.quotes[order.symbol] = quote
        trade = self.execution.submit(order, quote)
        self.logger.log(trade, state_at_entry=order.trigger_type.value)
        if "NIFTY" in order.symbol:
            if not hasattr(self, "_nifty_low"):
                self._nifty_low = {}
            if order.side is Side.SELL:
                # Entering new short leg - anchor lowest observed ask at entry
                init_ask = quote.ask if quote and quote.ask > 0 else trade.price
                self._nifty_low[order.symbol] = init_ask
            elif order.side is Side.BUY:
                # Exiting short leg - clear lowest observed ask so stale data is never reused
                self._nifty_low.pop(order.symbol, None)

    def _flatten(self, now: datetime, predicate) -> None:
        """Flatten only from a real quote; never invent a close price."""
        for symbol, position in list(self.execution.positions.items()):
            if not position.quantity or not predicate(symbol):
                continue
            # Session/risk flattening must use a current quote.  A cached quote
            # is useful for display only and must not create a false fill.
            quote = self.quotes.get(symbol)
            if quote is None:
                self.next_action = f"waiting for quote to flatten {symbol}"
                continue
            if (now - quote.timestamp).total_seconds() > self.config.stale_quote_seconds:
                self.next_action = f"waiting for fresh quote to flatten {symbol}"
                continue
            self._submit(Order(symbol, Side.SELL if position.quantity > 0 else Side.BUY,
                               abs(position.quantity), strategy="SessionFlatten",
                               trigger_type=TriggerType.CIRCUIT_BREAKER), quote, now)

    def _session_controls(self, now: datetime) -> None:
        date = now.astimezone(IST).date()
        if self._session_date is not None and date != self._session_date:
            self._flatten(now, lambda symbol: True)
            self.mcx.reset_session()
            self.nifty.state = "FLAT"
            self.nifty.position = None
            self._nifty_entered = False
            self._last_nifty_action_signal = 0
            self._pending.clear()
            self._emitted.clear()
            self.snapshots.clear()
            self.indicators = IndicatorRegistry(self.config)
            self.risk = RiskGuardian(self.config)
            self._guardian_liquidated = False
            self._history = {"NIFTY": [], "MCX-NATGAS": []}
            self._persist_indicator_state()
        self._session_date = date
        if not _in_window(now.astimezone(IST), "09:15", "15:15"):
            self._flatten(now, lambda symbol: symbol.startswith("NIFTY"))
        if not _in_window(now.astimezone(IST), "18:00", "23:25"):
            self._flatten(now, lambda symbol: symbol.startswith("MCX-NATGAS-"))

    def _persisted_signal(self, underlying: str, snapshot: dict,
                          now: datetime, *, emit: bool = True) -> int:
        if snapshot.get("warmup"):
            self._pending.pop(underlying, None)
            self._emitted.pop(underlying, None)
            return 0
        fast, slow = snapshot.get("ema_15"), snapshot.get("ema_90")
        if fast is None or slow is None:
            return 0
        direction = 1 if fast > slow else -1
        slope = snapshot.get("slow_slope")
        if slope is None:
            return 0
        if (direction > 0 and slope <= 0) or (direction < 0 and slope >= 0):
            self._pending.pop(underlying, None)
            self._emitted.pop(underlying, None)
            return 0
        prior = self._pending.get(underlying)
        if not prior or prior[0] != direction:
            self._pending[underlying] = (direction, now)
            return 0
        required = snapshot.get("persistence_raw")
        if required is None:
            return 0
        if (now - prior[1]).total_seconds() < required:
            return 0
        if emit and self._emitted.get(underlying) == direction:
            return 0
        if emit:
            self._emitted[underlying] = direction
        return direction

    def _manage_mcx(self, now: datetime, signal: int, quotes: dict[str, Quote]):
        positions = {s: p for s, p in self.execution.positions.items()
                     if s.startswith("MCX-NATGAS-") and p.quantity and p.quantity < 0}
        for symbol, position in positions.items():
            q = quotes.get(symbol) or self._option_quote(symbol, quotes)
            if q:
                self.quotes[symbol] = q
            quote = self.quotes.get(symbol)
            if quote is None:
                continue
            self._mcx_high[symbol] = min(self._mcx_high.get(symbol, quote.ask),
                                         quote.ask)
            stop = position.average_price * (1 + self.config.mcx_stop_loss_pct)
            atr_value = self.snapshots.get("MCX-NATGAS", {}).get("atr_1m")
            trailing = (self._mcx_high[symbol] + self.config.mcx_k * atr_value
                        if atr_value else None)
            self.mcx.position = position
            hit = self.mcx.on_price(now, quote.ask, stop, trailing)
            if hit:
                order = self.mcx.order(symbol, abs(position.quantity), Side.BUY,
                                       TriggerType(hit))
                self._submit(order, quote, now)
                if symbol.endswith("-CE"):
                    self.mcx.state.direction = -1
                elif symbol.endswith("-PE"):
                    self.mcx.state.direction = 1
        underlying_quote = quotes.get("MCX-NATGAS")
        if (len(positions) == 1 and underlying_quote
                and self.mcx.update_momentum(underlying_quote.last)):
            surviving = next(iter(positions))
            missing = ("MCX-NATGAS-PE" if surviving.endswith("-CE")
                       else "MCX-NATGAS-CE")
            missing_quote = quotes.get(missing) or self._option_quote(missing, quotes)
            if missing_quote and self.mcx.can_flip(now):
                self._submit(self.mcx.order(missing, 1, Side.SELL,
                                            TriggerType.RE_CENTER),
                             missing_quote, now)
                direction = -1 if missing.endswith("-PE") else 1
                self.mcx.flip(now, direction)
                self.mcx.consume_momentum_reversal()
        if signal and self.mcx.state.direction and signal == -self.mcx.state.direction:
            if self.mcx.confirm_flip(now, signal, True):
                old = "-CE" if signal < 0 else "-PE"
                new = "-PE" if signal < 0 else "-CE"
                old_quote = self.quotes.get("MCX-NATGAS" + old)
                new_quote = self.quotes.get("MCX-NATGAS" + new)
                old_pos = self.execution.positions.get("MCX-NATGAS" + old)
                if old_quote and old_pos and old_pos.quantity:
                    self._submit(self.mcx.order(old_pos.symbol, abs(old_pos.quantity),
                                                Side.BUY, TriggerType.RE_CENTER),
                                 old_quote, now)
                if new_quote:
                    self._submit(self.mcx.order("MCX-NATGAS" + new, 1, Side.SELL,
                                                TriggerType.RE_CENTER), new_quote, now)

    def _manage_nifty(self, now: datetime, snapshot: dict, quotes: dict[str, Quote]):
        positions = {s: p for s, p in self.execution.positions.items()
                     if (s.startswith("NIFTY") or "NIFTY" in s) and p.quantity}
        if not positions:
            return

        if not hasattr(self, "_nifty_low"):
            self._nifty_low = {}
        for symbol, position in positions.items():
            q = quotes.get(symbol) or self._option_quote(symbol, quotes)
            if q:
                self.quotes[symbol] = q
                if position.quantity < 0:
                    curr_low = self._nifty_low.get(symbol, position.average_price)
                    if curr_low <= 0 or curr_low < position.average_price * 0.5:
                        curr_low = position.average_price
                    self._nifty_low[symbol] = min(curr_low, q.ask)

        dte = int(getattr(self.market_data, "nifty_dte", 0))
        if self.nifty.should_flatten(now, dte):
            for symbol, position in positions.items():
                quote = self.quotes.get(symbol)
                if quote:
                    close_side = Side.SELL if position.quantity > 0 else Side.BUY
                    self._submit(Order(symbol, close_side, abs(position.quantity),
                                       trigger_type=TriggerType.CIRCUIT_BREAKER,
                                       strategy="NiftyOptions"), quote, now)
            return

        short_positions = {s: p for s, p in positions.items() if p.quantity < 0}
        hedge_positions = {
            s: p for s, p in positions.items()
            if p.quantity > 0 and "HEDGE" in s.upper()
        }
        # If both short ATM legs were stopped, retain the protective hedges but
        # immediately rebuild the ATM short strangle.
        if hedge_positions and not short_positions and len(hedge_positions) == 2:
            if not self.risk.halted and snapshot.get("adx_300_1s", 0.0) <= self.config.nifty_adx_threshold:
                legs = [(name, self._option_quote(name, quotes))
                        for name in ("NIFTY-CE", "NIFTY-PE")]
                if all(quote is not None and quote.ask > 0 for _, quote in legs):
                    quantity = self.nifty.size(1, getattr(self.risk, "ivr20", None), dte)
                    for logical_symbol, leg_quote in legs:
                        self._submit(
                            Order(
                                leg_quote.symbol,
                                Side.SELL,
                                quantity,
                                trigger_type=TriggerType.RE_CENTER,
                                strategy="NiftyOptions",
                            ),
                            leg_quote,
                            now,
                        )
                    self._last_nifty_action_signal = 0
                    self.next_action = "hedge-only recovery: ATM short strangle re-entered"
                    log.info("Re-entered ATM Nifty short strangle after hedge-only state")
            return

        trail_pct = 0.05 if dte <= 1 else 0.07
        for symbol, position in list(short_positions.items()):
            quote = self.quotes.get(symbol)
            if not quote or position.average_price <= 0:
                continue
            entry_p = position.average_price
            lowest_p = self._nifty_low.get(symbol, entry_p)
            initial_sl = entry_p * 1.35
            tsl = lowest_p * (1.0 + trail_pct)
            
            trigger = None
            if quote.ask >= initial_sl:
                trigger = TriggerType.STOP_LOSS_HIT
            elif lowest_p <= entry_p * 0.95 and quote.ask >= tsl:
                trigger = TriggerType.TRAILING_STOP_HIT
            if trigger:
                self._submit(Order(symbol, Side.BUY, abs(position.quantity),
                                   trigger_type=trigger,
                                   strategy="NiftyOptions"), quote, now)
                log.info("Nifty short position %s stopped out via %s (ask=%.2f)",
                         symbol, trigger.value, quote.ask)

        # Refresh active short positions after stop evaluations
        active_shorts = {
            s: p for s, p in self.execution.positions.items()
            if (s.startswith("NIFTY") or "NIFTY" in s) and p.quantity < 0 and p.average_price > 0
        }

        # Solo leg safeguard & re-centering when only 1 short leg remains:
        if len(active_shorts) == 1:
            surviving_sym = next(iter(active_shorts))
            is_surviving_ce = "CE" in surviving_sym or bool(re.search(r"C\d+", surviving_sym))
            missing_type = "PE" if is_surviving_ce else "CE"
            missing_logical = f"NIFTY-{missing_type}"

            adx_value = snapshot.get("adx_300_1s") or 0.0
            last_recenter = getattr(self, "_last_nifty_recenter_time", 0.0)
            now_ts = now.timestamp()
            # If market is ranging/calm (ADX <= threshold) and 45s cooldown elapsed, re-center the missing leg
            if (now_ts - last_recenter >= 45.0
                    and adx_value <= self.config.nifty_adx_threshold
                    and (dte > 1 or now.strftime("%H:%M") < self.config.nifty_low_dte_cutoff)):
                new_quote = self._option_quote(missing_logical, quotes)
                if new_quote and new_quote.ask > 0:
                    new_sym = new_quote.symbol
                    self._submit(Order(new_sym, Side.SELL,
                                       self.nifty.size(1, getattr(self.risk, "ivr20", None), dte),
                                       trigger_type=TriggerType.RE_CENTER,
                                       strategy="NiftyOptions"), new_quote, now)
                    self._last_nifty_recenter_time = now_ts
                    log.info("Re-centered Nifty strangle by selling missing leg %s (ask=%.2f)",
                             new_sym, new_quote.ask)
            return

        if len(active_shorts) <= 1:
            return

        signal = self.signal_events[-1]["direction"] if self.signal_events and self.signal_events[-1]["underlying"] == "NIFTY" else 0
        if signal == 0 or signal == self._last_nifty_action_signal:
            return

        is_call_loss = signal > 0
        losing_pos = next(
            (p for s, p in active_shorts.items()
             if (("CE" in s or "C" in s) if is_call_loss else ("PE" in s or "P" in s))),
            None
        )
        if not losing_pos or losing_pos.average_price <= 0:
            return
        losing_symbol = losing_pos.symbol
        quote = self.quotes.get(losing_symbol)
        if not quote:
            return
        loss_pct = (quote.ask - losing_pos.average_price) / losing_pos.average_price
        if loss_pct >= 0.05:
            self._submit(Order(losing_symbol, Side.BUY, abs(losing_pos.quantity),
                               trigger_type=TriggerType.PROACTIVE_CUT,
                               strategy="NiftyOptions"), quote, now)
            self._last_nifty_action_signal = signal
            adx_value = snapshot.get("adx_300_1s") or 0.0
            if adx_value <= self.config.nifty_adx_threshold and (
                    dte > 1 or now.strftime("%H:%M") < self.config.nifty_low_dte_cutoff):
                recenter_logical = "NIFTY-CE" if is_call_loss else "NIFTY-PE"
                new_quote = self._option_quote(recenter_logical, quotes)
                if new_quote:
                    new_sym = new_quote.symbol
                    self._submit(Order(new_sym, Side.SELL,
                                       self.nifty.size(1, getattr(self.risk, "ivr20", None), dte),
                                       trigger_type=TriggerType.RE_CENTER,
                                       strategy="NiftyOptions"), new_quote, now)
                    self._last_nifty_recenter_time = now.timestamp()

    def process_quotes(self, quotes: dict[str, Quote], now: datetime) -> None:
        self.quotes.update({s: q for s, q in quotes.items() if isinstance(q, Quote)})
        if hasattr(self.market_data, "latest"):
            self.market_data.latest.update(self.quotes)
        self._session_controls(now)
        if not quotes:
            detail = getattr(self.market_data, "_last_error", "")
            self.next_action = (
                f"feed unavailable: {detail}" if detail
                else "waiting for an authenticated market-data feed; no fabricated quotes"
            )
            log.warning("market-data feed unavailable; no tradable quotes")
            return
        for underlying in ("MCX-NATGAS", "NIFTY"):
            quote = quotes.get(underlying)
            if quote is None or not self.risk.check_quote(now, quote.timestamp, underlying):
                continue
            if quote.iv is not None and underlying == "NIFTY":
                self.risk.update_ivr(quote.iv)
            snapshot = self.indicators.update(underlying, self._bar(underlying, quote))
            self.snapshots[underlying] = snapshot
            self._history.setdefault(underlying, []).append(quote)
            self._history[underlying] = self._history[underlying][-self._history_limit:]
            signal = self._persisted_signal(
                underlying, snapshot, now, emit=underlying != "NIFTY")
            if underlying == "NIFTY":
                if not _in_window(now, "09:15", "15:15"):
                    continue
                dte = int(getattr(self.market_data, "nifty_dte", 5))
                signal = signal if self.nifty.confirm_signal(now, dte, signal) else 0
            if signal:
                self.signal_events.append({"underlying": underlying, "direction": signal,
                                           "timestamp": now, "confirmed": True,
                                           "indicators": snapshot})
            if underlying == "MCX-NATGAS":
                if not _in_window(now, "18:00", "23:25"):
                    continue
                if (_in_window(now, "18:00", "23:24") and not snapshot.get("warmup")
                        and not self.mcx.state.strangle_initialized
                        and not self.risk.halted):
                    legs = [(s, self._option_quote(s, quotes))
                            for s in ("MCX-NATGAS-CE", "MCX-NATGAS-PE")]
                    if all(q for _, q in legs):
                        for symbol, leg_quote in legs:
                            self._submit(Order(symbol, Side.SELL, 1,
                                               strategy="MCXNatGas",
                                               trigger_type=TriggerType.RE_CENTER),
                                         leg_quote, now)
                        self.mcx.initialize_strangle(now)
                        self.mcx.state.direction = signal or 1
                self._manage_mcx(now, signal, quotes)
            else:
                ivr = self.risk.ivr20
                if (signal and _in_window(now, "09:15", "15:15")
                        and not snapshot.get("warmup") and not self._nifty_entered
                        and not self.risk.halted and self.nifty.ivr_allows_entry(ivr)):
                    dte = int(getattr(self.market_data, "nifty_dte", 5))
                    quantity = self.nifty.size(1, ivr, dte)
                    legs = [(s, self._option_quote(s, quotes))
                            for s in ("NIFTY-CE-HEDGE", "NIFTY-PE-HEDGE",
                                      "NIFTY-CE", "NIFTY-PE")]
                    if all(q for _, q in legs):
                        for symbol, leg_quote in legs[:2]:
                            order_sym = leg_quote.symbol if leg_quote else symbol
                            self._submit(Order(order_sym, Side.BUY, quantity,
                                               strategy="NiftyOptions",
                                               trigger_type=TriggerType.RE_CENTER),
                                         leg_quote, now)
                        for symbol, leg_quote in legs[2:]:
                            order_sym = leg_quote.symbol if leg_quote else symbol
                            self._submit(Order(order_sym, Side.SELL, quantity,
                                               strategy="NiftyOptions",
                                               trigger_type=TriggerType.RE_CENTER),
                                         leg_quote, now)
                        self.nifty.state, self._nifty_entered = "OPEN", True
                        self._last_nifty_action_signal = signal
                    else:
                        missing = [symbol for symbol, leg_quote in legs if leg_quote is None]
                        self.next_action = f"waiting for option contracts: {', '.join(missing)}"
                self._manage_nifty(now, snapshot, quotes)
        self._persist_indicator_state()
        pnl = self.execution.mark_to_market(self.quotes)
        if not self.risk.check_pnl(pnl) and not self._guardian_liquidated:
            fresh_quotes = {
                symbol: quote for symbol, quote in quotes.items()
                if (now - quote.timestamp).total_seconds() <= self.config.stale_quote_seconds
            }
            self.execution.close_all(fresh_quotes)
            self._guardian_liquidated = True
            self.next_action = "risk halt: liquidating positions; awaiting operator reset"
            log.error("combined paper loss limit reached (%.2f); guardian halted", pnl)
        elif not self.risk.halted:
            if not self.next_action.startswith("waiting for option contracts"):
                self.next_action = "monitoring feed and strategy gates"

    def heartbeat(self, now: datetime | None = None) -> None:
        now = now or datetime.now(IST)
        self.heartbeat_count += 1
        try:
            if hasattr(self.market_data, "heartbeat"):
                quotes = self.market_data.heartbeat(now, self) or {}
            else:
                quotes = self.market_data.poll(now) if hasattr(self.market_data, "poll") else {}
            self.process_quotes(quotes, now)
        except Exception:
            log.exception("market-data heartbeat failed; entries halted for tick")
            self.next_action = "feed error; entries halted for this heartbeat"
        finally:
            self._persist_indicator_state()
        self.last_status = (f"PAPER heartbeat={self.heartbeat_count} positions="
                            f"{sum(abs(p.quantity) for p in self.execution.positions.values())} "
                            f"pnl={self.execution.mark_to_market(self.quotes):.2f} "
                            f"halted={self.risk.halted}")
        if self.dashboard:
            rendered = self.dashboard.render(self, now)
            if os.getenv("PAPER_NO_DASHBOARD") != "1":
                if sys.stdout.isatty():
                    sys.stdout.write("\033[2J\033[H" + rendered + "\n")
                else:
                    sys.stdout.write("\n" + rendered + "\n")
                sys.stdout.flush()
        self.notifier.send(self.last_status, now)

    def run_forever(self, interval: float = 1.0) -> None:
        while True:
            started = time.monotonic()
            self.heartbeat()
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    TradingRuntime().run_forever()
