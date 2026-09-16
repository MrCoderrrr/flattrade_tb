"""Deterministic, paper-only trading engine.

The runtime deliberately knows nothing about broker order placement.  A quote
source may be injected in tests (or by a paper simulator); an unauthenticated
Flattrade source simply returns no data.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from adapters.flattrade_market_data import FlattradeMarketData
from adapters.paper_execution import PaperExecution
from adapters.telegram_notifier import TelegramNotifier
from core_engine.config import DEFAULT_CONFIG
from core_engine.indicators import IndicatorRegistry
from core_engine.models import Bar, Order, Quote, Side, TriggerType
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
            if symbol.startswith(prefix) and p.quantity
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
        lines.extend([
            self._line("╠", "═", "╣", width),
            self._row("  STRATEGY STATUS", width),
            self._line("╟", "─", "╢", width),
            self._row(
                f"  STATE  {self._strategy_state(runtime, prefix):<24}"
                f"│  FLIPS  {runtime.mcx.state.flips}/4"
                f"│  ACTIVE LEGS  {len(active):>2}",
                width,
            ),
            self._line("╠", "═", "╣", width),
            self._row("  OPEN PAPER POSITIONS", width),
            self._line("╟", "─", "╢", width),
            self._row(
                f"  {'SYMBOL':<22}│{'QTY':>6}│{'SIDE':<6}│"
                f"{'ENTRY':>10}│{'CURRENT':>10}│{'SL':>10}│"
                f"{'TSL':>10}│{'UNREAL PNL':>14}",
                width,
            )
        ])
        if not active:
            lines.append(self._row("  No open paper positions", width))
        for p in active:
            q = runtime.quotes.get(p.symbol) or runtime.execution.last_quotes.get(p.symbol)
            mark = (q.bid if p.quantity > 0 else q.ask) if q else None
            unreal = p.quantity * (mark - p.average_price) if mark is not None else None
            side = "LONG" if p.quantity > 0 else "SHORT"
            sl, tsl = self._stops(runtime, p, prefix, snapshot)
            lines.append(self._row(
                f"  {p.symbol:<22}│{p.quantity:>+6}│{side:<6}│"
                f"{p.average_price:>10.2f}│{self._num(mark):>10}│"
                f"{self._num(sl):>10}│{self._num(tsl):>10}│"
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
    def _stops(runtime, position, prefix: str, snapshot: dict):
        if position.quantity >= 0:
            return None, None
        if prefix == "MCX-NATGAS-":
            sl = position.average_price * (1.0 + runtime.config.mcx_stop_loss_pct)
            best = runtime._mcx_high.get(position.symbol, position.average_price)
            atr_value = snapshot.get("atr_1m")
            tsl = best + runtime.config.mcx_k * atr_value if atr_value else None
            return sl, tsl
        return position.metadata.get("sl"), position.metadata.get("tsl")

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
    def _num(value) -> str:
        return "n/a" if value is None else f"{float(value):.3f}"

    @staticmethod
    def _warmup(runtime) -> str:
        return "YES" if any(s.get("warmup") for s in runtime.snapshots.values()) else "NO"

    @staticmethod
    def _feed(runtime, now) -> str:
        ages = []
        for q in runtime.quotes.values():
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
                    result = fn(symbol)
                    if isinstance(result, Quote):
                        return result
                except Exception:
                    log.debug("option quote lookup failed for %s", symbol,
                              exc_info=True)
        return None

    def _submit(self, order: Order, quote: Quote, now: datetime) -> None:
        trade = self.execution.submit(order, quote)
        self.logger.log(trade, state_at_entry=order.trigger_type.value)

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
        self._session_date = date
        if not _in_window(now, "09:15", "15:15"):
            self._flatten(now, lambda symbol: symbol.startswith("NIFTY-"))
        if not _in_window(now, "18:00", "23:25"):
            self._flatten(now, lambda symbol: symbol.startswith("MCX-NATGAS-"))

    def _persisted_signal(self, underlying: str, snapshot: dict,
                          now: datetime) -> int:
        if snapshot.get("warmup"):
            self._pending.pop(underlying, None)
            self._emitted.pop(underlying, None)
            return 0
        fast, slow = snapshot.get("ema_15"), snapshot.get("ema_90")
        if fast is None or slow is None:
            return 0
        direction = 1 if fast > slow else -1
        if (direction > 0 and snapshot.get("slow_slope", 0) <= 0) or (
                direction < 0 and snapshot.get("slow_slope", 0) >= 0):
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
        if self._emitted.get(underlying) == direction:
            return 0
        self._emitted[underlying] = direction
        return direction

    def _manage_mcx(self, now: datetime, signal: int, quotes: dict[str, Quote]):
        positions = {s: p for s, p in self.execution.positions.items()
                     if s.startswith("MCX-NATGAS-") and p.quantity}
        for symbol, position in positions.items():
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
                     if s.startswith("NIFTY-") and p.quantity}
        if not positions:
            return
        dte = int(getattr(self.market_data, "nifty_dte", 5))
        if self.nifty.should_flatten(now, dte):
            for symbol, position in positions.items():
                quote = self.quotes.get(symbol)
                if quote:
                    self._submit(Order(symbol, Side.BUY, abs(position.quantity),
                                       trigger_type=TriggerType.CIRCUIT_BREAKER,
                                       strategy="NiftyOptions"), quote, now)
            return
        signal = self.signal_events[-1]["direction"] if self.signal_events and self.signal_events[-1]["underlying"] == "NIFTY" else 0
        if signal == 0:
            return
        if signal == self._last_nifty_action_signal:
            return
        self._last_nifty_action_signal = signal
        losing_symbol = "NIFTY-CE" if signal > 0 else "NIFTY-PE"
        position = positions.get(losing_symbol)
        quote = self.quotes.get(losing_symbol)
        if not position or not quote:
            return
        self._submit(Order(losing_symbol, Side.BUY, abs(position.quantity),
                           trigger_type=TriggerType.PROACTIVE_CUT,
                           strategy="NiftyOptions"), quote, now)
        adx_value = snapshot.get("adx_300_1s") or 0.0
        if adx_value <= self.config.nifty_adx_threshold and (
                dte > 1 or now.strftime("%H:%M") < self.config.nifty_low_dte_cutoff):
            new_quote = self._option_quote(losing_symbol, quotes)
            if new_quote:
                self._submit(Order(losing_symbol, Side.SELL,
                                   self.nifty.size(1, getattr(self.risk, "ivr20", None)),
                                   trigger_type=TriggerType.RE_CENTER,
                                   strategy="NiftyOptions"), new_quote, now)

    def process_quotes(self, quotes: dict[str, Quote], now: datetime) -> None:
        self.quotes.update({s: q for s, q in quotes.items() if isinstance(q, Quote)})
        self._session_controls(now)
        if not quotes:
            self.next_action = "waiting for an authenticated market-data feed; no fabricated quotes"
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
            signal = self._persisted_signal(underlying, snapshot, now)
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
                    legs = [(s, self._option_quote(s, quotes))
                            for s in ("NIFTY-CE", "NIFTY-PE")]
                    if all(q for _, q in legs):
                        for symbol, leg_quote in legs:
                            self._submit(Order(symbol, Side.SELL, 1,
                                               strategy="NiftyOptions",
                                               trigger_type=TriggerType.RE_CENTER),
                                         leg_quote, now)
                        self.nifty.state, self._nifty_entered = "OPEN", True
                self._manage_nifty(now, snapshot, quotes)
        pnl = self.execution.mark_to_market(self.quotes)
        if not self.risk.check_pnl(pnl) and not self._guardian_liquidated:
            self.execution.close_all(self.quotes)
            self._guardian_liquidated = True
            self.next_action = "risk halt: liquidating positions; awaiting operator reset"
            log.error("combined paper loss limit reached (%.2f); guardian halted", pnl)
        elif not self.risk.halted:
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
        self.last_status = (f"PAPER heartbeat={self.heartbeat_count} positions="
                            f"{sum(abs(p.quantity) for p in self.execution.positions.values())} "
                            f"pnl={self.execution.mark_to_market(self.quotes):.2f} "
                            f"halted={self.risk.halted}")
        if self.dashboard:
            rendered = self.dashboard.render(self, now)
            if sys.stdout.isatty() and os.getenv("PAPER_NO_DASHBOARD") != "1":
                sys.stdout.write("\033[2J\033[H" + rendered + "\n")
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
