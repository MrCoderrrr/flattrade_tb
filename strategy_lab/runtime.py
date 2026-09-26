"""Persistent paper controller; live execution is locked pending independent validation.

No legacy engine is imported and no order submission API is reachable. Paper
fills cross the displayed book plus one tick, include estimated costs, and
refuse stale/insufficient depth. Synthetic fills are explicitly labelled.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Contract, IST
from .market_data import FlattradeReadOnly, FeedError
from .strategies import build_plan, explain_signal
from .catalog import resolve, catalog
from . import active_v3

MARKETS = ("NIFTY", "MCX")
LIVE_REASON = "Live is locked: these candidates have no verified out-of-sample or forward-paper record, and live order reconciliation has not been commissioned."


def fresh(quote, now):
    try:
        return (0 <= (now-quote.timestamp).total_seconds() <= 10 and
                all(math.isfinite(v) and v > 0 for v in (quote.bid, quote.ask, quote.last)) and
                quote.ask >= quote.bid)
    except (TypeError, ValueError):
        return False


def contract_dict(contract):
    row = asdict(contract)
    row["expiry"] = contract.expiry.isoformat()
    return row


def contract_from(row):
    return Contract(**{**row, "expiry": date.fromisoformat(row["expiry"])})


def cost(price, quantity):
    # Conservative research allowance, not a certified tax/broker tariff.
    return round(20 + price * quantity * 0.002, 4)


def blank_session():
    return {"state": "STOPPED", "mode": None, "multiplier": 1, "capital": 200000,
            "date": None, "reason": "Choose a mode and start this session",
            "realized_pnl": 0., "unrealized_pnl": 0., "costs": 0., "net_pnl": 0.,
            "positions": [], "trades": [], "feed_timestamp": None,
            "entries": 0, "last_exit": None, "stop_loss": 0., "take_profit": 0.,
            "trade_costs": 0., "exit_requested": False, "stop_requested": False,
            "locked": False, "last_signal_bar": None}


class Controller:
    def __init__(self, root: Path, feed=None, clock=None):
        self.root = Path(root)
        self.directory = self.root / "data" / "strategy_lab"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._file = (self.directory / "controller.lock").open("a+")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            raise RuntimeError("Another strategy controller is already running") from None
        self.lock = threading.RLock()
        self.clock = clock or (lambda: datetime.now(IST))
        self.feed = feed or FlattradeReadOnly(self.root)
        self.record_market_data = feed is None
        self._revision = 0
        self.db = sqlite3.connect(self.directory / "ledger.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS strategy_daily (day TEXT NOT NULL, strategy_id TEXT NOT NULL, market TEXT NOT NULL, mode TEXT NOT NULL, net_pnl REAL NOT NULL, capital REAL NOT NULL, entries INTEGER NOT NULL, PRIMARY KEY(day,strategy_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS market_snapshots (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, market TEXT NOT NULL, payload TEXT NOT NULL)")
        stored = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        self.data = json.loads(stored[0]) if stored else {
            "sessions": {m: blank_session() for m in MARKETS}, "events": [],
            "account": {"date": None, "capital": 200000, "daily_pnl": 0., "drawdown": 0.,
                        "halted": False, "lifetime_pnl": 0., "peak_pnl": 0.},
            "history": []}
        for session in self.data["sessions"].values():
            if session["positions"]:
                session.update(state="RECOVERY_REQUIRED", exit_requested=True, stop_requested=True,
                               reason="Recovered paper positions; fresh quotes required to flatten")
            else:
                session.update(state="STOPPED", reason="Session authorization expires on restart")
        self._stop = threading.Event()
        self.worker = None
        self._closed = False
        self._save()

    def _save(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)",
                            (json.dumps(self.data, allow_nan=False),))

    def _event(self, message):
        row = {"timestamp": self.clock().isoformat(), "message": message}
        self.data["events"] = (self.data["events"] + [row])[-150:]
        with self.db:
            self.db.execute("INSERT INTO journal(timestamp,payload) VALUES (?,?)",
                            (row["timestamp"], json.dumps(row)))

    def _snapshot(self, market, now, held=()):
        # Broker I/O must never hold the control lock: Stop/Kill remain responsive.
        revision = self._revision
        self.lock.release()
        try:
            if isinstance(self.feed, FlattradeReadOnly):
                bars, quotes = self.feed.snapshot(market, now, held, strategy_id=self.data['sessions'][market].get('strategy_id'))
            else:
                bars, quotes = self.feed.snapshot(market, now, held)
        finally:
            self.lock.acquire()
        if revision != self._revision and not held:
            raise FeedError("Control request changed during data fetch; pending entry discarded")
        if self.record_market_data:
            def encode(value):
                if isinstance(value, (datetime, date)):
                    return value.isoformat()
                raise TypeError("Unsupported snapshot value")
            record = {"timestamp": self.clock().isoformat(), "market": market,
                      "bars": [asdict(b) for b in bars], "quotes": [asdict(q) for q in quotes]}
            with self.db:
                self.db.execute("INSERT INTO market_snapshots(timestamp,market,payload) VALUES (?,?,?)",
                                (record['timestamp'], market, json.dumps(record, default=encode, allow_nan=False)))
        return bars, quotes

    def _roll_day(self, now):
        account = self.data["account"]
        if account["date"] == now.date().isoformat():
            return
        if any(s["positions"] for s in self.data["sessions"].values()):
            return  # Never abandon yesterday's exposure.
        if account["date"]:
            for market, session in self.data["sessions"].items():
                if session.get('date') == account['date']:
                    self.db.execute("INSERT OR REPLACE INTO strategy_daily VALUES (?,?,?,?,?,?,?)",
                                    (account['date'], session.get('strategy_id') or resolve(market).id,
                                     market, session.get('mode') or 'paper', float(session['net_pnl']),
                                     float(session['capital']), int(session['entries'])))
            self.data["history"].append({"date": account["date"], "net_pnl": account["daily_pnl"],
                                          "capital": account["capital"]})
            account["lifetime_pnl"] += account["daily_pnl"]
        account.update(date=now.date().isoformat(), daily_pnl=0.,
                       halted=account["drawdown"] >= .05 * account["capital"])
        self.data["sessions"] = {m: blank_session() for m in MARKETS}

    def start(self, market, mode, multiplier, capital, confirmation="", strategy_id=None):
        with self.lock:
            now = self.clock().astimezone(IST)
            if market not in MARKETS or mode not in ("paper", "live"):
                raise ValueError("Choose NIFTY/MCX and paper/live explicitly")
            if mode == "live":
                raise ValueError(LIVE_REASON)
            spec = resolve(market, strategy_id)
            if not spec.enabled:
                raise ValueError('Original v1 is archived; its legacy engine is not connected to this paper controller')
            if type(multiplier) is not int or not 1 <= multiplier <= 100:
                raise ValueError("Multiplier must be an integer from 1 to 100")
            if isinstance(capital, bool) or not isinstance(capital, (int, float)) or not math.isfinite(capital) or capital < 200000 * multiplier:
                raise ValueError("Each multiplier requires at least ₹200,000 of declared capital")
            self._roll_day(now)
            s = self.data["sessions"][market]
            if s['date'] and resolve(market, s.get('strategy_id')).id != spec.id:
                raise ValueError('Strategy version is fixed for this trading day to preserve its risk ledger')
            if s["positions"] or s["state"] not in ("STOPPED", "SESSION_COMPLETE"):
                raise ValueError("Stop and flatten the existing session before starting")
            if s["locked"] or self.data["account"]["halted"]:
                raise ValueError("Risk limit reached; restarting cannot clear the lock")
            if any(x["date"] for x in self.data["sessions"].values()) and capital != self.data["account"]["capital"]:
                raise ValueError("Both sessions share one account; capital is fixed for the trading day")
            if now.weekday() >= 5:
                raise ValueError("Regular trading sessions are closed on weekends")
            if now.strftime("%H:%M") >= spec.flatten:
                raise ValueError("The session's flatten deadline has passed")
            other = self.data["sessions"]["MCX" if market == "NIFTY" else "NIFTY"]
            if other["positions"]:
                raise ValueError("Flatten the other session before reusing account capital")
            self.data["account"]["capital"] = float(capital)
            self._revision += 1
            s.update(state="ARMED", mode=mode, multiplier=multiplier, capital=float(capital), strategy_id=spec.id,
                     date=now.date().isoformat(), reason="Paper session armed; waiting for valid market data and signal",
                     stop_requested=False, exit_requested=False, paused=False)
            self._event(f"{market}: PAPER session authorized at {multiplier}×; shared capital ₹{capital:,.0f}")
            self._save()
            return self.status()

    def stop(self, market):
        with self.lock:
            if market not in MARKETS:
                raise ValueError("Unknown market")
            s = self.data["sessions"][market]
            self._revision += 1
            s.update(exit_requested=bool(s["positions"]), stop_requested=True,
                     state="EXIT_PENDING" if s["positions"] else "STOPPED",
                     reason="Stop requested; flattening on fresh executable quotes" if s["positions"] else "Stopped; no open positions")
            self._event(f"{market}: stop requested")
            self._save()
            return self.status()

    def kill(self):
        with self.lock:
            self.data["account"]["halted"] = True
            for market in MARKETS:
                self.stop(market)
            self._event("Emergency stop latched for today; fresh quotes still required to close paper positions")
            self._save()
            return self.status()

    def pause(self, market, paused=True):
        with self.lock:
            if market not in MARKETS or type(paused) is not bool:
                raise ValueError("Invalid pause request")
            s = self.data["sessions"][market]
            if s["state"] in ("STOPPED", "SESSION_COMPLETE", "RECOVERY_REQUIRED", "EXIT_PENDING"):
                raise ValueError("Start a session before pausing or resuming")
            if s["locked"] or self.data["account"]["halted"]:
                raise ValueError("Risk lock active")
            self._revision += 1
            s["paused"] = paused
            s["reason"] = "New entries paused; open positions remain managed" if paused else "Entries resumed within current session authorization"
            self._event(f"{market}: {'paused new entries' if paused else 'resumed'}")
            self._save()
            return self.status()

    def status(self):
        with self.lock:
            now = self.clock().astimezone(IST)
            result = copy.deepcopy(self.data)
            result.update(today=now.date().isoformat(), server_time=now.isoformat(),
                          strategies=catalog(),
                          live_enabled=False, live_reason=LIVE_REASON,
                          execution="PAPER ONLY — simulated book fills, unvalidated candidates")
            history = [dict(date=day, strategy_id=sid, market=market, mode=mode,
                            net_pnl=pnl, capital=capital, entries=entries)
                       for day,sid,market,mode,pnl,capital,entries in self.db.execute(
                           "SELECT day,strategy_id,market,mode,net_pnl,capital,entries FROM strategy_daily ORDER BY day DESC LIMIT 400")]
            for market, session in self.data['sessions'].items():
                if session.get('date') == now.date().isoformat():
                    history.append(dict(date=session['date'], strategy_id=session.get('strategy_id') or resolve(market).id,
                                        market=market, mode=session.get('mode') or 'paper',
                                        net_pnl=session['net_pnl'], capital=session['capital'], entries=session['entries']))
            result['strategy_history'] = history
            for market, s in result["sessions"].items():
                spec = resolve(market, s.get('strategy_id'))
                s.update(strategy_id=spec.id, max_entries=spec.max_entries, flatten=spec.flatten)
                stamp = s.get("feed_timestamp")
                s["feed_age_seconds"] = max(0, (now-datetime.fromisoformat(stamp)).total_seconds()) if stamp else None
                s["valuation_stale"] = bool(s["positions"] and (s["feed_age_seconds"] is None or s["feed_age_seconds"] > 10))
            return result

    def _account(self):
        a = self.data["account"]
        a["daily_pnl"] = sum(s["net_pnl"] for s in self.data["sessions"].values())
        equity = a["lifetime_pnl"] + a["daily_pnl"]
        a["peak_pnl"] = max(a["peak_pnl"], equity)
        a["drawdown"] = a["peak_pnl"] - equity
        a['daily_loss_fraction'] = .05 if any(resolve(m, s.get('strategy_id')).version == 3 and s['date'] for m, s in self.data['sessions'].items()) else .01
        if a["daily_pnl"] <= -a['daily_loss_fraction']*a["capital"] or a["drawdown"] >= .05*a["capital"]:
            a["halted"] = True
        if a["halted"]:
            for s in self.data["sessions"].values():
                s["locked"] = True
                s["exit_requested"] = bool(s["positions"])

    def _mark(self, s, quotes, now):
        by_symbol = {q.contract.symbol: q for q in quotes}
        total = 0.
        exit_costs = 0.
        for p in s["positions"]:
            q = by_symbol.get(p["symbol"])
            if q is None or not fresh(q, now):
                raise FeedError("Open position quote is missing/stale; P&L is last known, exit remains pending")
            closing_buy = p["side"] == "SELL"
            size = q.ask_size if closing_buy else q.bid_size
            if size < p["quantity"]:
                raise FeedError("Insufficient displayed depth to close; exposure retained")
            price = q.ask + q.contract.tick_size if closing_buy else max(q.contract.tick_size, q.bid-q.contract.tick_size)
            pnl = (price-p["entry_price"]) * p["quantity"] * (1 if p["side"] == "BUY" else -1)
            p.update(mark_price=price, unrealized_pnl=round(pnl, 4))
            total += pnl
            exit_costs += cost(price, p["quantity"])
        s["unrealized_pnl"] = round(total, 4)
        s["estimated_exit_costs"] = round(exit_costs, 4)
        s["net_pnl"] = round(s["realized_pnl"]+total-s["costs"]-exit_costs, 4)
        if quotes:
            s["feed_timestamp"] = min(q.timestamp for q in quotes).isoformat()

    def _enter(self, market, s, plan, now):
        spec = resolve(market, s.get('strategy_id'))
        naked = spec.id == 'mcxv3' and plan.strategy == 'mcxv3' and plan.max_loss is None and all(l.side == 'SELL' for l in plan.legs)
        limit = (spec.trade_risk_per_unit or 0)*s['multiplier']
        if s["positions"] or (not naked and (plan.max_loss is None or plan.max_loss > limit)):
            raise ValueError("Invalid portfolio risk")
        if not naked and plan.max_loss + self.data["account"]["drawdown"] >= .05*self.data["account"]["capital"]:
            s["reason"] = "Trade would exceed remaining portfolio drawdown budget"
            return
        positions, fills, costs = [], [], 0.
        # Validate entire synthetic basket before recording any fill.
        for leg in sorted(plan.legs, key=lambda leg: leg.side != "BUY"):
            q = leg.quote
            qty = leg.quantity
            if leg.side not in ("BUY", "SELL") or type(qty) is not int or qty <= 0 or qty % q.contract.lot_size:
                raise ValueError("Invalid leg quantity or side")
            if not fresh(q, now) or (q.ask_size if leg.side == "BUY" else q.bid_size) < qty:
                raise FeedError("Entry book is stale or too small")
            price = q.ask + q.contract.tick_size if leg.side == "BUY" else q.bid-q.contract.tick_size
            if price <= 0:
                raise FeedError("Option premium cannot cover slippage")
            fee = cost(price, qty)
            costs += fee
            positions.append({"symbol": q.contract.symbol, "contract": contract_dict(q.contract),
                              "side": leg.side, "quantity": qty, "entry_price": price,
                              "mark_price": price, "unrealized_pnl": 0.})
            fills.append({"timestamp": now.isoformat(), "symbol": q.contract.symbol,
                          "side": leg.side, "quantity": qty, "price": price, "cost": fee,
                          "mode": "paper", "reason": "ENTRY", "simulated": True})
        # Recompute expiration payoff using executable fills, including one-tick
        # slippage and a reserve for both sides of the round trip.
        strikes = sorted({p["contract"]["strike"] for p in positions})
        payoffs = []
        for spot in [0.] + strikes + [max(strikes)*2]:
            payoff = 0.
            for p in positions:
                c = p["contract"]
                intrinsic = max(0., spot-c["strike"]) if c["option_type"] in ("C", "CE", "CALL") else max(0., c["strike"]-spot)
                payoff += (intrinsic-p["entry_price"])*p["quantity"]*(1 if p["side"] == "BUY" else -1)
            payoffs.append(payoff)
        actual_risk = max(0., -min(payoffs)) + costs*2
        if naked:
            actual_risk = None  # A stop trigger does not cap naked-option loss.
        if not naked and actual_risk > limit:
            s["reason"] = "Executable fill and cost model exceed this strategy's maximum loss budget"
            return
        s.update(positions=positions, state="RUNNING", reason=plan.reason, stop_loss=plan.stop_loss,
                 take_profit=plan.take_profit, trade_costs=costs, max_loss=actual_risk,
                 entries=s["entries"]+1, entered_at=now.isoformat(), direction=plan.direction,
                 reversal_count=0, reversal_bar=None)
        s["costs"] += costs
        s["trades"].extend(fills)
        self._mark(s, [leg.quote for leg in plan.legs], now)
        self._event(f"{market}: simulated {plan.strategy} entry; {len(positions)} legs")

    def _exit(self, market, s, quotes, now):
        self._mark(s, quotes, now)
        # In a live executor shorts MUST close before protective longs. Paper
        # preserves that ledger ordering; it does not claim real basket fills.
        for p in sorted(s["positions"], key=lambda p: p["side"] != "SELL"):
            fee = cost(p["mark_price"], p["quantity"])
            s["costs"] += fee
            s["realized_pnl"] += p["unrealized_pnl"]
            s["trades"].append({"timestamp": now.isoformat(), "symbol": p["symbol"],
                                "side": "BUY" if p["side"] == "SELL" else "SELL",
                                "quantity": p["quantity"], "price": p["mark_price"], "cost": fee,
                                "mode": "paper", "reason": s["reason"], "simulated": True})
        s.update(positions=[], unrealized_pnl=0., estimated_exit_costs=0., exit_requested=False,
                 last_exit=now.isoformat(), state="STOPPED" if s["stop_requested"] or s["locked"] else "COOLDOWN")
        s["net_pnl"] = round(s["realized_pnl"]-s["costs"], 4)
        self._event(f"{market}: paper positions flat; session net ₹{s['net_pnl']:.2f}")

    def tick(self):
        with self.lock:
            now = self.clock().astimezone(IST)
            self._roll_day(now)
            for market in MARKETS:
                s = self.data["sessions"][market]
                if s["state"] in ("STOPPED", "SESSION_COMPLETE") and not s["positions"]:
                    continue
                spec = resolve(market, s.get('strategy_id'))
                signal_fn = active_v3.explain_signal if spec.version == 3 else explain_signal
                plan_fn = active_v3.build_plan if spec.version == 3 else build_plan
                deadline = spec.flatten
                ended = s["date"] != now.date().isoformat() or now.strftime("%H:%M") >= deadline or now.weekday() >= 5
                if ended:
                    s.update(stop_requested=True, exit_requested=bool(s["positions"]),
                             reason="Session deadline reached; new authorization required next day")
                    if not s["positions"]:
                        s["state"] = "SESSION_COMPLETE"
                        continue
                try:
                    if s["positions"]:
                        bars, quotes = self._snapshot(market, now, [contract_from(p["contract"]) for p in s["positions"]])
                        self._mark(s, quotes, self.clock().astimezone(IST))
                        trade_net = s["unrealized_pnl"] - s["trade_costs"] - s["estimated_exit_costs"]
                        if spec.version == 3 and bars and not s['exit_requested']:
                            signal = signal_fn(market, bars, self.clock().astimezone(IST))
                            s['signal'] = signal
                            stamp = bars[-1].timestamp.isoformat()
                            if signal.get('eligible') and stamp != s.get('reversal_bar'):
                                s['reversal_bar'] = stamp
                                s['reversal_count'] = s.get('reversal_count', 0)+1 if signal.get('direction') != s.get('direction') else 0
                                if s['reversal_count'] >= 2:
                                    s.update(exit_requested=True, reason='Indicator regime changed for two completed bars')
                        if s["net_pnl"] <= -spec.session_loss_per_unit*s["multiplier"]:
                            s.update(locked=True, exit_requested=True, reason="Session loss limit reached")
                        elif trade_net <= -s["stop_loss"] or trade_net >= s["take_profit"]:
                            s.update(exit_requested=True, reason="Portfolio stop" if trade_net < 0 else "Portfolio take profit")
                        self._account()
                        if s["exit_requested"]:
                            self._exit(market, s, quotes, self.clock().astimezone(IST))
                        continue
                    self._account()
                    if s["locked"] or self.data["account"]["halted"]:
                        s.update(state="STOPPED", reason="Risk lock active")
                        continue
                    if s.get("paused"):
                        s["reason"] = "New entries paused; open positions remain managed"
                        continue
                    if s["entries"] >= spec.max_entries:
                        s.update(state="STOPPED", reason="Session entry limit reached", locked=True)
                        continue
                    if s["last_exit"] and (now-datetime.fromisoformat(s["last_exit"])).total_seconds() < spec.cooldown_seconds:
                        s.update(state="COOLDOWN", reason=f"{spec.cooldown_seconds}-second cooldown after exit")
                        continue
                    if market == "MCX" and spec.version == 2 and now.weekday() == 3:
                        s.update(state="ARMED", reason="Thursday Natural Gas inventory release exclusion; no new MCX entries")
                        continue
                    start, cutoff = spec.entry_start, spec.entry_end
                    if not start <= now.strftime("%H:%M") < cutoff:
                        s.update(state="ARMED", reason=f"Entry window {start}–{cutoff} IST")
                        continue
                    if any(other["positions"] for name, other in self.data["sessions"].items() if name != market):
                        s["reason"] = "Shared account is allocated to another open session"
                        continue
                    bars, quotes = self._snapshot(market, now)
                    fresh_now = self.clock().astimezone(IST)
                    if not bars or (fresh_now-(bars[-1].timestamp+timedelta(minutes=spec.bar_minutes))).total_seconds() > (90 if spec.version == 3 else 360):
                        raise FeedError("Completed underlying bars are missing or stale")
                    if quotes:
                        s["feed_timestamp"] = min(q.timestamp for q in quotes).isoformat()
                    signal = signal_fn(market, bars, fresh_now)
                    s["signal"] = signal
                    s["reason"] = signal.get("reason", "Waiting for a qualifying signal")
                    plan = plan_fn(market, bars, quotes, fresh_now, s["multiplier"], s["capital"])
                    if plan:
                        self._enter(market, s, plan, fresh_now)
                    elif signal.get("eligible"):
                        s["reason"] = "Signal qualifies; no liquid spread meets contract, premium and risk requirements"
                except FeedError as exc:
                    s["reason"] = str(exc)
                    if s["positions"]:
                        s["exit_requested"] = True
                        s["state"] = "EXIT_PENDING"
                except Exception:
                    s.update(reason="Engine validation error; new entries halted, inspect local diagnostics", locked=True)
                    if s["positions"]:
                        s.update(exit_requested=True, state="EXIT_PENDING")
                    else:
                        s["state"] = "STOPPED"
            self._account()
            self._save()

    def start_worker(self):
        if self.worker and self.worker.is_alive():
            return
        def work():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    with self.lock:
                        self.data["account"]["halted"] = True
                        self._event("Controller fault; new entries halted")
                        self._save()
                self._stop.wait(3)
        self.worker = threading.Thread(target=work, name="paper-controller", daemon=True)
        self.worker.start()

    def shutdown(self):
        if self._closed:
            return
        self._stop.set()
        if self.worker:
            self.worker.join(timeout=40)
            if self.worker.is_alive():
                raise RuntimeError("Market-data worker is still finishing; keep controller running")
        with self.lock:
            for s in self.data["sessions"].values():
                s.update(state="RECOVERY_REQUIRED" if s["positions"] else "STOPPED", stop_requested=True,
                         exit_requested=bool(s["positions"]))
            self._save()
            self.db.close()
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._closed = True
