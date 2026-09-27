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
from .strategies import build_plan, explain_signal, _option_type
from .catalog import resolve, catalog
from . import active_v3, legacy_v1, pattern_v4, nifty_v5
from .nifty_flow import decision as flow_decision

MARKETS = ("NIFTY", "MCX")
LIVE_REASON = "Real orders are unavailable: this controller has no commissioned broker executor or partial-fill reconciliation."


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


def next_session_date(now, spec):
    """Next weekday with an entry window still ahead, in exchange local time."""
    day = now.date()
    if now.weekday() >= 5 or now.strftime("%H:%M") >= spec.entry_end:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


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
        self.nifty_stream = None
        self.nifty_observer = None
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
        self.data['account'].setdefault('configured_capital',float(self.data['account']['capital']))
        self.data['account'].setdefault('live_permission',False)
        self.data.setdefault('schedules', {})
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

    def attach_nifty_stream(self, stream):
        """Share the dashboard's read-only index stream; never grants order access."""
        with self.lock:
            self.nifty_stream = stream

    def attach_nifty_observer(self, observer):
        with self.lock:
            self.nifty_observer = observer

    def nifty_pin_strikes(self):
        """Keep held wings and original short strikes in the research stream."""
        with self.lock:
            s = self.data['sessions']['NIFTY']
            return ([p['contract']['strike'] for p in s['positions']] + [
                c['strike'] for c in s.get('v5_anchor',{}).values()]) if s['positions'] else []

    def _v5_observation(self, s, bars, now):
        if self.nifty_observer:
            observation,history = self.nifty_observer.snapshot(now)
            s['flow_history'] = history
            s['signal'] = observation
            return observation
        ticks = self.nifty_stream.latest(now)['ticks'] if self.nifty_stream else []
        observation = nifty_v5.flow(bars, ticks, now)
        if observation.get('eligible'):
            history = s.setdefault('flow_history', [])
            stamp = observation['timestamp']
            if not history or history[-1]['timestamp'] != stamp:
                history.append(observation)
                s['flow_history'] = history[-90:]
        s['signal'] = observation
        return observation

    def _snapshot(self, market, now, held=()):
        # Broker I/O must never hold the control lock: Stop/Kill remain responsive.
        revision = self._revision
        strategy_id = self.data['sessions'][market].get('strategy_id')
        spot_override = (self.data['sessions'][market].get('signal') or {}).get('spot') if strategy_id == 'nfv5' and not held else None
        self.lock.release()
        try:
            if isinstance(self.feed, FlattradeReadOnly):
                bars, quotes = self.feed.snapshot(market, now, held, strategy_id=strategy_id,
                                                   spot_override=spot_override)
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

    def _v5_bars(self, now):
        # In the flat state, do not request an entire option basket every
        # second. The broker's completed-minute bar cache costs one read/minute.
        if not isinstance(self.feed, FlattradeReadOnly):
            return self._snapshot('NIFTY', now)[0]
        revision = self._revision
        self.lock.release()
        try:
            bars = self.feed.bars('NIFTY', now, 1)
        finally:
            self.lock.acquire()
        if revision != self._revision:
            raise FeedError('Control changed during NIFTY flow read')
        return bars

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
                if not self.data['account']['live_permission']:
                    raise ValueError('Enable account-wide live permission in Settings first')
                raise ValueError(LIVE_REASON)
            spec = resolve(market, strategy_id)
            if not spec.enabled:
                raise ValueError('This strategy is unavailable in the paper controller')
            if type(multiplier) is not int or not 1 <= multiplier <= 100:
                raise ValueError("Multiplier must be an integer from 1 to 100")
            if isinstance(capital, bool) or not isinstance(capital, (int, float)) or not math.isfinite(capital) or capital < 200000 * multiplier:
                raise ValueError("Each multiplier requires at least ₹200,000 of declared capital")
            if float(capital) != float(self.data['account']['configured_capital']):
                raise ValueError('Use the shared account capital saved in Settings')
            self._roll_day(now)
            s = self.data["sessions"][market]
            target = next_session_date(now, spec)
            immediate = target == now.date() and now.strftime("%H:%M") >= spec.entry_start
            if market in self.data['schedules']:
                raise ValueError('Strategy already scheduled; switch it off before changing settings')
            if (target == now.date() and s.get('date') == now.date().isoformat() and
                    resolve(market, s.get('strategy_id')).id != spec.id):
                raise ValueError('Strategy version is fixed for this trading day to preserve its risk ledger')
            if s["positions"] or s["state"] not in ("STOPPED", "SESSION_COMPLETE"):
                raise ValueError("Stop and flatten the existing session before starting")
            if (immediate and (s["locked"] or self.data["account"]["halted"]) or
                    self.data["account"]["drawdown"] >= .05*capital):
                raise ValueError("Risk limit reached; restarting cannot clear the lock")
            if any(x["date"] for x in self.data["sessions"].values()) and capital != self.data["account"]["capital"]:
                raise ValueError("Both sessions share one account; capital is fixed for the trading day")
            other = self.data["sessions"]["MCX" if market == "NIFTY" else "NIFTY"]
            if immediate and other["positions"]:
                raise ValueError("Flatten the other session before reusing account capital")
            self.data["account"]["capital"] = float(capital)
            self._revision += 1
            if immediate:
                s.update(state="ARMED", mode=mode, multiplier=multiplier, capital=float(capital), strategy_id=spec.id,
                         date=now.date().isoformat(), reason="Paper session armed; waiting for valid market data and signal",
                         stop_requested=False, exit_requested=False, paused=False)
                self._event(f"{market}: PAPER session authorized at {multiplier}×; shared capital ₹{capital:,.0f}")
            else:
                self.data['schedules'][market] = {"strategy_id":spec.id, "mode":mode,
                    "multiplier":multiplier, "capital":float(capital), "scheduled_for":target.isoformat()}
                self._event(f"{market}: PAPER {spec.id} scheduled for {target.isoformat()} {spec.entry_start} IST at {multiplier}×")
            self._save()
            return self.status()

    def configure(self, *, capital=None, live_permission=None):
        with self.lock:
            if (capital is None) == (live_permission is None):
                raise ValueError('Change either account capital or live permission')
            account = self.data['account']
            if capital is not None:
                if (type(capital) not in (int,float) or not math.isfinite(capital)
                        or capital < 200000):
                    raise ValueError('Account capital must be at least ₹200,000')
                now = self.clock().astimezone(IST).date().isoformat()
                if any(s.get('date') == now or s['positions'] for s in self.data['sessions'].values()):
                    raise ValueError('Account capital is fixed after a strategy starts for the day')
                if self.data['schedules']:
                    raise ValueError('Stop scheduled strategies before changing account capital')
                account['configured_capital'] = float(capital)
                account['capital'] = float(capital)
                self._event(f"Account capital set to ₹{capital:,.0f}")
            else:
                if type(live_permission) is not bool:
                    raise ValueError('Live permission must be true or false')
                account['live_permission'] = live_permission
                if not live_permission:
                    for market,session in self.data['sessions'].items():
                        if session.get('mode') == 'live' and session.get('state') not in ('STOPPED','SESSION_COMPLETE'):
                            self.stop(market)
                self._event('Account live permission '+('enabled' if live_permission else 'disabled'))
            self._revision += 1
            self._save()
            return self.status()

    def stop(self, market):
        with self.lock:
            if market not in MARKETS:
                raise ValueError("Unknown market")
            scheduled = self.data['schedules'].pop(market, None)
            s = self.data["sessions"][market]
            self._revision += 1
            if s["positions"] or s["state"] not in ("STOPPED", "SESSION_COMPLETE"):
                s.update(exit_requested=bool(s["positions"]), stop_requested=True,
                         state="EXIT_PENDING" if s["positions"] else "STOPPED",
                         reason="Stop requested; flattening on fresh executable quotes" if s["positions"] else "Stopped; no open positions")
            elif scheduled:
                s["reason"] = "Scheduled start canceled"
            self._event(f"{market}: {'scheduled start canceled' if scheduled else 'stop requested'}")
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
                          live_permission=bool(self.data['account']['live_permission']),
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
        a['daily_loss_fraction'] = .05 if any(resolve(m, s.get('strategy_id')).version >= 3 and s['date'] for m, s in self.data['sessions'].items()) else .01
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
        naked = spec.id in ('mcxv1', 'mcxv3') and plan.strategy == spec.id and plan.max_loss is None and all(l.side == 'SELL' for l in plan.legs)
        limit = (spec.trade_risk_per_unit or 0)*s['multiplier']
        if s["positions"] or (not naked and (plan.max_loss is None or plan.max_loss > limit)):
            raise ValueError("Invalid portfolio risk")
        if not naked and spec.version not in (1, 5) and plan.max_loss + self.data["account"]["drawdown"] >= .05*self.data["account"]["capital"]:
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
        if spec.version == 4:
            s.update(underlying_stop=plan.underlying_stop,
                     underlying_target=plan.underlying_target,
                     pattern=plan.pattern, pattern_score=plan.pattern_score,
                     last_signal_bar=s.get('signal', {}).get('indicators', {}).get('last_bar_open'))
        if spec.version == 1:
            for p in positions:
                if p["side"] == "SELL":
                    p.update(best_mark=p["entry_price"], leg_stop=None, trail_armed=False)
        if spec.id == "mcxv3":
            s.update(cycle_start_net=s["net_pnl"], trend_count=0, trend_bar=None,
                     reentry_count=0, reentry_bar=None, leg_reentries=s.get("leg_reentries", 0))
            for p in positions:
                p.update(best_mark=p["entry_price"], leg_stop=None, trail_armed=False)
        if spec.id == "nfv5":
            s['v5_anchor'] = { _option_type(contract_from(p['contract'])): p['contract']
                               for p in positions if p['side'] == 'SELL' }
            s['v5_state'] = 'DUAL'
            for p in positions:
                if p['side'] == 'SELL':
                    p.update(best_mark=p['entry_price'], leg_stop=None, trail_armed=False)
        s["costs"] += costs
        s["trades"].extend(fills)
        self._mark(s, [leg.quote for leg in plan.legs], now)
        self._event(f"{market}: simulated {plan.strategy} entry; {len(positions)} legs")

    def _close_mcx_leg(self, s, position, quotes, now, reason):
        fee = cost(position["mark_price"], position["quantity"])
        s["costs"] += fee
        s["realized_pnl"] += position["unrealized_pnl"]
        s["trades"].append({"timestamp":now.isoformat(), "symbol":position["symbol"],
                            "side":"BUY", "quantity":position["quantity"],
                            "price":position["mark_price"], "cost":fee,
                            "mode":"paper", "reason":reason, "simulated":True})
        s["positions"].remove(position)
        s["last_leg_exit"] = now.isoformat()
        s["reason"] = reason
        self._mark(s, quotes, now)
        if not s["positions"]:
            s.update(state="COOLDOWN", last_exit=now.isoformat())
        else:
            s["missing_armed"] = False
        self._event(f"MCX: simulated {position['contract']['option_type']} leg exit — {reason}")

    def _manage_v1(self, market, s, signal, quotes, now):
        """Manage the dashboard v1 paper port without invoking legacy broker code."""
        shorts = [p for p in s['positions'] if p['side'] == 'SELL']
        if not shorts:
            return
        slope = signal.get('indicators', {}).get('kama_slope', 0.) if signal.get('eligible') else 0.
        direction = signal.get('direction', 0) if signal.get('eligible') else 0
        initial = .15 if market == 'NIFTY' else .10
        solo_trail = .09 if market == 'NIFTY' else .05
        for p in list(shorts):
            entry, mark = p['entry_price'], p['mark_price']
            p['best_mark'] = min(p.get('best_mark', entry), mark)
            solo = len([x for x in s['positions'] if x['side'] == 'SELL']) == 1
            stop = min(entry*(1+initial), p['best_mark']*(1+solo_trail) if solo else entry*(1+initial))
            p['leg_stop'] = min(p.get('leg_stop') or stop, stop)
            p['trail_armed'] = solo
            option = _option_type(contract_from(p['contract']))
            impulse = (direction > 0 and option == 'CE' or direction < 0 and option == 'PE')
            if mark < p['leg_stop'] and not (impulse and abs(slope) >= (.5 if market == 'NIFTY' else .05)):
                continue
            fee = cost(mark, p['quantity'])
            s['costs'] += fee
            s['realized_pnl'] += p['unrealized_pnl']
            s['trades'].append({'timestamp': now.isoformat(), 'symbol': p['symbol'],
                                'side': 'BUY', 'quantity': p['quantity'], 'price': mark,
                                'cost': fee, 'mode': 'paper', 'simulated': True,
                                'reason': 'V1 KAMA impulse' if impulse and mark < p['leg_stop'] else 'V1 premium stop'})
            s['positions'].remove(p)
            s['last_leg_exit'] = now.isoformat()
            s['reason'] = 'V1 paper leg exited on KAMA impulse or premium stop'
            self._mark(s, quotes, now)
            self._event(f"{market}: simulated v1 {option} short exit")
        shorts = [p for p in s['positions'] if p['side'] == 'SELL']
        if not shorts:
            # Original NIFTY keeps only wings after both shorts are gone. This
            # controlled adaptation exits them too, so the dashboard is flat.
            if market == 'NIFTY':
                s.update(locked=True, exit_requested=True,
                         reason='Both v1 shorts exited; closing protective wings')
            else:
                s.update(state='COOLDOWN', last_exit=now.isoformat())
            return
        if (len(shorts) != 1 or s.get('paused') or not signal.get('eligible') or
                now.strftime('%H:%M') >= resolve(market, s['strategy_id']).entry_end or
                s['entries'] >= resolve(market, s['strategy_id']).max_entries or
                s['net_pnl'] <= -resolve(market, s['strategy_id']).session_loss_per_unit*s['multiplier'] or
                not s.get('last_leg_exit') or
                (now-datetime.fromisoformat(s['last_leg_exit'])).total_seconds() < 60):
            return
        held = shorts[0]
        missing = 'CE' if _option_type(contract_from(held['contract'])) == 'PE' else 'PE'
        if direction != (-1 if missing == 'CE' else 1):
            return
        try:
            _, chain = self._snapshot(market, now)
        except FeedError:
            s['reason'] = 'KAMA reversal; waiting for fresh option-chain depth'
            return
        if s['exit_requested'] or s['stop_requested']:
            return
        spot = signal['indicators']['close']
        candidates = [q for q in chain if _option_type(q.contract) == missing
                      and q.contract.expiry.isoformat() == held['contract']['expiry']
                      and q.contract.lot_size == held['contract']['lot_size']
                      and q.bid_size >= held['quantity'] and fresh(q, now)]
        if market == 'NIFTY':
            wing = next((p for p in s['positions'] if p['side'] == 'BUY'
                         and _option_type(contract_from(p['contract'])) == missing), None)
            if wing is None:
                return
            strike = wing['contract']['strike']
            candidates = [q for q in candidates if
                          (q.contract.strike <= strike-1000 if missing == 'CE' else
                           q.contract.strike >= strike+1000)]
        if not candidates:
            s['reason'] = 'V1 reversal confirmed; suitable ATM option or protection unavailable'
            return
        q = min(candidates, key=lambda item: abs(item.contract.strike-spot))
        price = q.bid-q.contract.tick_size
        if price <= 0:
            return
        fee = cost(price, held['quantity'])
        s['positions'].append({'symbol': q.contract.symbol, 'contract': contract_dict(q.contract),
                               'side': 'SELL', 'quantity': held['quantity'], 'entry_price': price,
                               'mark_price': price, 'unrealized_pnl': 0., 'best_mark': price,
                               'leg_stop': None, 'trail_armed': False})
        s['costs'] += fee
        s['trades'].append({'timestamp': now.isoformat(), 'symbol': q.contract.symbol,
                            'side': 'SELL', 'quantity': held['quantity'], 'price': price,
                            'cost': fee, 'mode': 'paper', 'reason': 'V1 KAMA reversal re-entry',
                            'simulated': True})
        s['entries'] += 1
        s['reason'] = 'V1 KAMA reversal; paper strangle restored'
        self._mark(s, quotes+[q], now)
        self._event(f"{market}: simulated v1 {missing} re-entry")

    def _manage_mcx_v3(self, s, signal, quotes, now):
        if s["exit_requested"] or s["stop_requested"]:
            return
        indicators = signal.get("indicators", {}) if signal.get("eligible") else {}
        er, atr, spot = indicators.get("efficiency", .5), indicators.get("atr14", 0.), indicators.get("close", 1.)
        stop_pct = min(.28, max(.12, .16 + .08*(1-er) + min(.04, 2*atr/spot)))
        trail_pct = min(.16, max(.05, .05 + .08*(1-er) + min(.03, atr/spot)))
        for p in list(s["positions"]):
            entry, mark = p["entry_price"], p["mark_price"]
            p["best_mark"] = min(p.get("best_mark", entry), mark)
            if len(s["positions"]) == 1 or mark <= .92*entry:
                p["trail_armed"] = True
            distance = max(p["contract"]["tick_size"], entry*(trail_pct-(.02 if len(s["positions"]) == 1 else 0)))
            stop = entry*(1+stop_pct)
            if p["trail_armed"]:
                stop = min(stop, p["best_mark"]+distance)
            p["leg_stop"] = round(min(p.get("leg_stop") or stop, stop), 4)
            p["stop_pct_cap"] = .28
            if mark >= p["leg_stop"]:
                self._close_mcx_leg(s, p, quotes, now,
                                    "MCX premium trailing stop" if p["trail_armed"] else "MCX premium stop")
                if not s["positions"]:
                    return
                remaining_type = _option_type(contract_from(s["positions"][0]["contract"]))
                s["missing_armed"] = (indicators.get("flow_score", 0.) >= .35 if remaining_type == "PE"
                                      else indicators.get("flow_score", 0.) <= -.35)
        if not signal.get("eligible"):
            return
        bar = indicators["last_bar_open"]
        direction = signal["direction"]
        if len(s["positions"]) == 2:
            if bar != s.get("trend_bar"):
                s["trend_bar"] = bar
                s["trend_count"] = s.get("trend_count", 0)+1 if direction and direction == s.get("trend_direction") else 1 if direction else 0
                s["trend_direction"] = direction
            if direction and s["trend_count"] >= 2:
                losing_type = "CE" if direction > 0 else "PE"
                losing = next((p for p in s["positions"] if _option_type(contract_from(p["contract"])) == losing_type), None)
                if losing is not None:
                    self._close_mcx_leg(s, losing, quotes, now, "Confirmed KAMA/EMA trend impulse")
                    s["missing_armed"] = True
            return
        if len(s["positions"]) != 1 or s.get("paused") or s["entries"] >= resolve("MCX", "mcxv3").max_entries:
            return
        held = s["positions"][0]
        missing = "CE" if _option_type(contract_from(held["contract"])) == "PE" else "PE"
        score, slope = indicators["flow_score"], indicators["kama_slope"]
        if not s.get("missing_armed"):
            if (score >= .35 if missing == "CE" else score <= -.35):
                s["missing_armed"] = True
            return
        reversal = ((score <= -.15 and slope <= 0 and indicators["ema8"] <= indicators["ema21"])
                    if missing == "CE" else
                    (score >= .15 and slope >= 0 and indicators["ema8"] >= indicators["ema21"]))
        if bar != s.get("reentry_bar"):
            s["reentry_bar"] = bar
            s["reentry_count"] = s.get("reentry_count", 0)+1 if reversal else 0
        if not reversal or s["reentry_count"] < 2 or s.get("leg_reentries", 0) >= 12:
            return
        if not s.get("last_leg_exit") or (now-datetime.fromisoformat(s["last_leg_exit"])).total_seconds() < 60:
            return
        if not resolve("MCX", "mcxv3").entry_start <= now.strftime("%H:%M") < resolve("MCX", "mcxv3").entry_end:
            return
        self._account()
        if s["locked"] or self.data["account"]["halted"] or s["net_pnl"] <= -6000*s["multiplier"]:
            return
        try:
            _, chain = self._snapshot("MCX", now)
        except FeedError:
            s["reason"] = "KAMA reversal confirmed; waiting for a fresh option chain"
            return
        if s["exit_requested"] or s["stop_requested"]:
            return
        candidates = [q for q in chain if _option_type(q.contract) == missing and
                      q.contract.expiry.isoformat() == held["contract"]["expiry"] and
                      q.contract.lot_size == held["contract"]["lot_size"] and
                      q.bid_size >= held["quantity"] and fresh(q, now)]
        if not candidates:
            s["reason"] = "KAMA reversal confirmed; waiting for fresh ATM opposite-leg depth"
            return
        q = min(candidates, key=lambda item:abs(item.contract.strike-spot))
        price = q.bid-q.contract.tick_size
        if price <= 0:
            return
        fee = cost(price, held["quantity"])
        s["positions"].append({"symbol":q.contract.symbol, "contract":contract_dict(q.contract),
                               "side":"SELL", "quantity":held["quantity"], "entry_price":price,
                               "mark_price":price, "unrealized_pnl":0., "best_mark":price,
                               "leg_stop":None, "trail_armed":False})
        s["costs"] += fee
        s["trades"].append({"timestamp":now.isoformat(), "symbol":q.contract.symbol,
                            "side":"SELL", "quantity":held["quantity"], "price":price,
                            "cost":fee, "mode":"paper", "reason":"KAMA/EMA reversal re-entry",
                            "simulated":True})
        s["entries"] += 1
        s["leg_reentries"] += 1
        s["reentry_count"] = 0
        s["trend_count"] = 0
        s["missing_armed"] = False
        s["reason"] = "KAMA/EMA reversal; ATM straddle restored"
        self._mark(s, quotes+[q], now)
        self._event(f"MCX: simulated ATM {missing} re-entry after confirmed KAMA/EMA reversal")

    def _close_v5_leg(self, s, position, quotes, now, reason):
        fee = cost(position['mark_price'], position['quantity'])
        s['costs'] += fee
        s['realized_pnl'] += position['unrealized_pnl']
        s['trades'].append({'timestamp':now.isoformat(), 'symbol':position['symbol'],
                            'side':'BUY', 'quantity':position['quantity'],
                            'price':position['mark_price'], 'cost':fee,
                            'mode':'paper', 'reason':reason, 'simulated':True})
        s['positions'].remove(position)
        s['last_leg_exit'] = now.isoformat()
        s['reason'] = reason
        self._mark(s, quotes, now)
        self._event(f"NIFTY v5: simulated {_option_type(contract_from(position['contract']))} short exit — {reason}")

    def _reenter_v5(self, s, missing, quotes, now):
        spec = resolve('NIFTY','nfv5')
        if (s.get('paused') or s.get('exit_requested') or s.get('stop_requested') or
                s['entries'] >= spec.max_entries or now.strftime('%H:%M') >= spec.entry_end or
                s['net_pnl'] <= -spec.session_loss_per_unit*s['multiplier'] or
                not s.get('last_leg_exit') or
                (now-datetime.fromisoformat(s['last_leg_exit'])).total_seconds() < 5):
            return False
        anchor = s.get('v5_anchor',{}).get(missing)
        if not anchor:
            return False
        wing = next((p for p in s['positions'] if p['side'] == 'BUY' and
                     _option_type(contract_from(p['contract'])) == missing),None)
        if (wing is None or wing['quantity'] != s['multiplier']*anchor['lot_size'] or
                (wing['contract']['strike']-anchor['strike'] if missing == 'CE' else
                 anchor['strike']-wing['contract']['strike']) < 1000):
            return False
        try:
            _, candidate = self._snapshot('NIFTY',now,[contract_from(anchor)])
        except FeedError:
            s['reason'] = 'V5 re-entry waiting for fresh original-strike depth'
            return False
        if s.get('exit_requested') or s.get('stop_requested'):
            return False
        q = next((q for q in candidate if q.contract.symbol == anchor['symbol']),None)
        quantity = s['multiplier']*anchor['lot_size']
        if q is None or not fresh(q,now) or q.bid_size < quantity:
            s['reason'] = 'V5 re-entry waiting for executable original-strike bid'
            return False
        price = q.bid-q.contract.tick_size
        if price <= 0:
            return False
        fee = cost(price,quantity)
        s['positions'].append({'symbol':q.contract.symbol,'contract':anchor,'side':'SELL',
                               'quantity':quantity,'entry_price':price,'mark_price':price,
                               'unrealized_pnl':0.,'best_mark':price,'leg_stop':None,
                               'trail_armed':False})
        s['costs'] += fee
        s['entries'] += 1
        s['v5_state'] = 'DUAL'
        s['trades'].append({'timestamp':now.isoformat(),'symbol':q.contract.symbol,
                            'side':'SELL','quantity':quantity,'price':price,'cost':fee,
                            'mode':'paper','reason':'V5 flow re-entry at protected anchor',
                            'simulated':True})
        self._mark(s,quotes+candidate,now)
        s['reason'] = 'V5 protected ATM straddle restored at original strike'
        self._event(f'NIFTY v5: simulated {missing} short re-entry')
        return True

    def _manage_nifty_v5(self, s, observation, quotes, now):
        shorts = [p for p in s['positions'] if p['side'] == 'SELL']
        if not shorts or s.get('exit_requested') or s.get('stop_requested'):
            if not shorts:
                s.update(exit_requested=True,reason='V5 no short remains; release protective wings')
            return
        state = ('DUAL' if len(shorts) == 2 else
                 'SOLO_PE' if _option_type(contract_from(shorts[0]['contract'])) == 'PE' else 'SOLO_CE')
        s['v5_state'] = state
        action = flow_decision(s.get('flow_history',[]),state) if observation.get('eligible') else None
        stop_pct,trail_pct = nifty_v5.stop_parameters(observation,len(shorts)==1)
        for p in list(shorts):
            mark,entry = p['mark_price'],p['entry_price']
            p['best_mark'] = min(p.get('best_mark',entry),mark)
            p['trail_armed'] = len(shorts)==1 or p.get('trail_armed',False) or mark <= .90*entry
            stop = entry*(1+stop_pct)
            if p['trail_armed']:
                stop = min(stop,p['best_mark']*(1+trail_pct))
            p['leg_stop'] = round(min(p.get('leg_stop') or stop,stop),4)
            p['stop_pct_cap'] = .30
            kind = _option_type(contract_from(p['contract']))
            signal_exit = action == 'EXIT_'+kind
            stop_hit = mark >= p['leg_stop']
            if not (signal_exit or stop_hit):
                continue
            # If a profitable solo leg trails out just as the trend stalls,
            # restore the protected straddle and reset its trail once. A hard
            # premium stop still takes precedence over this optimization.
            if (len(shorts)==1 and stop_hit and mark < entry*(1+stop_pct) and
                    action == 'REENTER_'+('CE' if kind=='PE' else 'PE')):
                missing = 'CE' if kind=='PE' else 'PE'
                if self._reenter_v5(s,missing,quotes,now):
                    p['best_mark'] = mark
                    p['leg_stop'] = round(entry*(1+stop_pct),4)
                    p['trail_armed'] = False
                    return
            self._close_v5_leg(s,p,quotes,now,'V5 flow exit' if signal_exit else 'V5 premium stop/trail')
            shorts = [x for x in s['positions'] if x['side']=='SELL']
            if not shorts:
                s.update(exit_requested=True,reason='V5 shorts exited; closing wings')
                return
            break  # One directional change per observed second.
        shorts = [p for p in s['positions'] if p['side']=='SELL']
        if len(shorts)==1 and observation.get('eligible'):
            kind = _option_type(contract_from(shorts[0]['contract']))
            s['v5_state'] = 'SOLO_'+kind
            missing = 'CE' if kind=='PE' else 'PE'
            if action == 'REENTER_'+missing:
                self._reenter_v5(s,missing,quotes,now)

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

    def _activate_schedules(self, now):
        for market, planned in list(self.data['schedules'].items()):
            spec = resolve(market, planned['strategy_id'])
            if now.date().isoformat() < planned['scheduled_for'] or now.weekday() >= 5:
                continue
            minute = now.strftime("%H:%M")
            if minute < spec.entry_start:
                continue
            if minute >= spec.entry_end:
                planned['scheduled_for'] = next_session_date(now, spec).isoformat()
                continue
            s = self.data['sessions'][market]
            if s['positions'] or s['state'] not in ('STOPPED', 'SESSION_COMPLETE'):
                continue
            if ((s.get('date') == now.date().isoformat() and s['entries']) or
                    self.data['account']['halted'] or s['locked']):
                planned['scheduled_for'] = next_session_date(
                    now.replace(hour=23, minute=59), spec).isoformat()
                continue
            if any(other['positions'] for name, other in self.data['sessions'].items() if name != market):
                continue
            if (planned['capital'] != self.data['account']['configured_capital'] or
                    planned['capital'] < 200000*planned['multiplier']):
                del self.data['schedules'][market]
                self._event(f"{market}: scheduled start canceled; account capital changed")
                continue
            s.update(state='ARMED', mode=planned['mode'], multiplier=planned['multiplier'],
                     capital=planned['capital'], strategy_id=spec.id,
                     date=now.date().isoformat(),
                     reason='Scheduled paper session armed; waiting for valid market data and signal',
                     stop_requested=False, exit_requested=False, paused=False)
            del self.data['schedules'][market]
            self._revision += 1
            self._event(f"{market}: scheduled PAPER {spec.id} activated at {planned['multiplier']}×")

    def tick(self):
        with self.lock:
            now = self.clock().astimezone(IST)
            self._roll_day(now)
            self._activate_schedules(now)
            for market in MARKETS:
                s = self.data["sessions"][market]
                if s["state"] in ("STOPPED", "SESSION_COMPLETE") and not s["positions"]:
                    continue
                spec = resolve(market, s.get('strategy_id'))
                signal_fn = (legacy_v1.explain_signal if spec.version == 1 else
                             active_v3.explain_signal if spec.version == 3 else
                             pattern_v4.explain_signal if spec.version == 4 else explain_signal)
                plan_fn = (legacy_v1.build_plan if spec.version == 1 else
                           active_v3.build_plan if spec.version == 3 else
                           pattern_v4.build_plan if spec.version == 4 else build_plan)
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
                        trade_net = (s["net_pnl"] - s.get("cycle_start_net", 0.)) if spec.id == "mcxv3" else s["unrealized_pnl"] - s["trade_costs"] - s["estimated_exit_costs"]
                        signal = {}
                        if spec.id == 'nfv5':
                            signal = self._v5_observation(s,bars,self.clock().astimezone(IST))
                        if spec.version in (1, 3, 4) and bars and not s['exit_requested']:
                            signal = signal_fn(market, bars, self.clock().astimezone(IST))
                            s['signal'] = signal
                            if spec.version == 3 and spec.id != "mcxv3":
                                stamp = bars[-1].timestamp.isoformat()
                                if signal.get('eligible') and stamp != s.get('reversal_bar'):
                                    s['reversal_bar'] = stamp
                                    s['reversal_count'] = s.get('reversal_count', 0)+1 if signal.get('direction') != s.get('direction') else 0
                                    if s['reversal_count'] >= 2:
                                        s.update(exit_requested=True, reason='Indicator regime changed for two completed bars')
                        if s["net_pnl"] <= -spec.session_loss_per_unit*s["multiplier"]:
                            s.update(locked=True, exit_requested=True, reason="Session loss limit reached")
                        elif spec.version != 1 and (trade_net <= -s["stop_loss"] or (spec.id != "mcxv3" and trade_net >= s["take_profit"])):
                            s.update(exit_requested=True, reason="Portfolio stop" if trade_net < 0 else "Portfolio take profit")
                        if spec.version == 4 and bars and not s['exit_requested']:
                            bar = bars[-1]
                            entered = datetime.fromisoformat(s['entered_at'])
                            if (bar.interval_minutes == 1 and bar.timestamp.astimezone(IST).date() == now.date()
                                    and entered < bar.timestamp+timedelta(minutes=1) <= now):
                                stop, target = s['underlying_stop'], s['underlying_target']
                                hit_stop = bar.low <= stop if s['direction'] > 0 else bar.high >= stop
                                hit_target = bar.high >= target if s['direction'] > 0 else bar.low <= target
                                if hit_stop or hit_target:
                                    s.update(exit_requested=True,
                                             reason='V4 underlying invalidation' if hit_stop else 'V4 underlying target')
                        self._account()
                        if spec.id == "mcxv3" and not s["exit_requested"]:
                            self._manage_mcx_v3(s, signal, quotes, self.clock().astimezone(IST))
                            if s["net_pnl"] <= -spec.session_loss_per_unit*s["multiplier"]:
                                s.update(locked=True, exit_requested=bool(s["positions"]), reason="Session loss limit reached")
                            self._account()
                        if spec.id == 'nfv5' and not s['exit_requested']:
                            self._manage_nifty_v5(s,signal,quotes,self.clock().astimezone(IST))
                            self._account()
                        if spec.version == 1 and not s["exit_requested"]:
                            self._manage_v1(market, s, signal, quotes, self.clock().astimezone(IST))
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
                    if spec.id == 'nfv5':
                        bars = [] if self.nifty_observer else self._v5_bars(now)
                        observation = self._v5_observation(s,bars,self.clock().astimezone(IST))
                        s['reason'] = observation.get('reason','Waiting for NIFTY flow')
                        if (not observation.get('eligible') or
                                flow_decision(s.get('flow_history',[]),'FLAT') != 'OPEN_BOTH'):
                            continue
                        bars,quotes = self._snapshot(market,self.clock().astimezone(IST))
                        fresh_now = self.clock().astimezone(IST)
                        observation = self._v5_observation(s,bars,fresh_now)
                        if (not bars or not quotes or not observation.get('eligible') or
                                flow_decision(s.get('flow_history',[]),'FLAT') != 'OPEN_BOTH'):
                            raise FeedError('NIFTY flow or option-chain depth stale before entry')
                        plan = nifty_v5.build_plan(bars,quotes,fresh_now,s['multiplier'],s['capital'],observation)
                        if plan:
                            self._enter(market,s,plan,fresh_now)
                        else:
                            s['reason'] = 'Flow is balanced; protected ATM basket lacks fresh depth or risk capacity'
                        continue
                    bars, quotes = self._snapshot(market, now)
                    fresh_now = self.clock().astimezone(IST)
                    if not bars or (fresh_now-(bars[-1].timestamp+timedelta(minutes=spec.bar_minutes))).total_seconds() > (90 if spec.version in (1, 3, 4) else 360):
                        raise FeedError("Completed underlying bars are missing or stale")
                    if quotes:
                        s["feed_timestamp"] = min(q.timestamp for q in quotes).isoformat()
                    signal = signal_fn(market, bars, fresh_now)
                    s["signal"] = signal
                    s["reason"] = signal.get("reason", "Waiting for a qualifying signal")
                    if (spec.version == 4 and signal.get('indicators', {}).get('last_bar_open')
                            == s.get('last_signal_bar')):
                        s['reason'] = 'V4 waits for a new completed candle after its last entry'
                        continue
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
                active_v5 = self.data['sessions']['NIFTY'].get('strategy_id') == 'nfv5' and self.data['sessions']['NIFTY']['state'] not in ('STOPPED','SESSION_COMPLETE')
                self._stop.wait(1 if active_v5 else 3)
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
