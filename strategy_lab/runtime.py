"""Persistent paper controller; live execution is locked pending independent validation.

No legacy engine is imported and no order submission API is reachable. Paper
fills cross the displayed book plus one tick, include estimated costs, and
refuse stale/insufficient depth. Synthetic fills are explicitly labelled.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Contract, IST
from .market_data import FlattradeReadOnly, FeedError
from .strategies import _option_type
from .catalog import resolve, catalog
from . import active_v3, legacy_v1, nifty_v5
from .nifty_flow import decision as flow_decision
from .risk import adaptive_short_stop

MARKETS = ("NIFTY", "MCX")
LIVE_REASON = "Real orders are unavailable: this controller has no commissioned broker executor or partial-fill reconciliation."


def risk_code_fingerprint():
    """Identify the strategy rules used to calculate persisted paper stops."""
    digest = hashlib.sha256()
    for name in ("runtime.py", "legacy_v1.py", "active_v3.py", "nifty_flow.py",
                 "nifty_v5.py", "risk.py", "strategies.py", "catalog.py", "opening_trend.py"):
        digest.update(name.encode())
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


def fresh(quote, now):
    try:
        book_time = quote.book_observed_at or quote.timestamp
        return (0 <= (now-book_time).total_seconds() <= 10 and
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


# The configured paper sessions finish at 15:34 (NIFTY) and 23:24 (MCX).
# Bought protection is retained through the preceding minute, including
# manual stops, portfolio stops, and early version-specific flatten times.
HEDGE_RELEASE = {"NIFTY": "15:33", "MCX": "23:23"}


def hedge_release_reached(market, session_day, now):
    return (not session_day or session_day != now.date().isoformat()
            or now.weekday() >= 5 or now.strftime("%H:%M") >= HEDGE_RELEASE[market])


def blank_session():
    return {"state": "STOPPED", "mode": None, "multiplier": 1, "capital": 200000,
            "date": None, "reason": "Choose a mode and start this session",
            "realized_pnl": 0., "unrealized_pnl": 0., "costs": 0., "net_pnl": 0.,
            "positions": [], "trades": [], "feed_timestamp": None,
            "entries": 0, "last_exit": None, "stop_loss": 0., "take_profit": 0.,
            "trade_costs": 0., "exit_requested": False, "stop_requested": False,
            "locked": False,
            "feed_error_count": 0, "last_feed_error_at": None}


def next_session_date(now, spec):
    """Next weekday with an entry window still ahead, in exchange local time."""
    day = now.date()
    if now.weekday() >= 5 or now.strftime("%H:%M") >= spec.entry_end:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


class Controller:
    def __init__(self, root: Path, feed=None, clock=None, storage_directory: Path | None = None):
        self.root = Path(root)
        self.directory = Path(storage_directory) if storage_directory is not None else self.root / "data" / "strategy_lab"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._file = (self.directory / "controller.lock").open("a+")
        try:
            fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file.close()
            raise RuntimeError("Another strategy controller is already running") from None
        self.lock = threading.RLock()
        self.clock = clock or (lambda: datetime.now(IST))
        self.feed = feed or FlattradeReadOnly(self.root, auto_refresh=True)
        self.nifty_stream = None
        self.nifty_observer = None
        self.v5_flow_history = []
        self.record_market_data = False  # Keep trade ledgers, not raw option-book snapshots.
        self._risk_code_fingerprint = risk_code_fingerprint()
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
        self.data.setdefault('strategy_authorizations', {})
        # A pending start was already an explicit user request in older
        # releases. Promote it to a persistent authorization on upgrade.
        for market, planned in self.data['schedules'].items():
            self.data['strategy_authorizations'].setdefault(market, {
                key: planned[key] for key in ('strategy_id','mode','multiplier','capital')
                if key in planned
            })
        today = self.clock().astimezone(IST).date().isoformat()
        for market, session in self.data["sessions"].items():
            session.pop('flow_history',None)  # Prior releases stored oversized per-second histories.
            if (market not in self.data['strategy_authorizations'] and
                    session.get('mode') == 'paper' and session.get('date') == today and
                    not session.get('stop_requested') and not session.get('exit_requested') and
                    not session.get('locked') and session.get('strategy_id') and
                    session.get('state') not in ('STOPPED','SESSION_COMPLETE')):
                self.data['strategy_authorizations'][market] = {
                    **{key: session[key] for key in ('strategy_id','mode','multiplier','capital')},
                    'auto_restart':True,
                }
            if session["positions"]:
                if (session.get("mode") == "paper" and session.get("date") == today
                        and not session.get("stop_requested") and not session.get("exit_requested")
                        and not session.get("locked")):
                    if session.get("risk_code_fingerprint") != self._risk_code_fingerprint:
                        # Keep the original fills and best premium. Only the
                        # old rule's stop is discarded; the first fresh quote
                        # passes the leg through the new rule before an exit.
                        for position in session["positions"]:
                            if position["side"] == "SELL":
                                position["leg_stop"] = None
                        session["risk_code_fingerprint"] = self._risk_code_fingerprint
                    session.update(state="DATA_WAIT", reason="Recovered paper legs; refreshing quotes and stop rules")
                else:
                    session.update(state="RECOVERY_REQUIRED", exit_requested=True, stop_requested=True,
                                   reason="Recovered paper positions; fresh quotes required to flatten")
            elif (session.get("mode") == "paper" and session.get("date") == today
                  and session.get("state") in ("ARMED", "COOLDOWN", "RUNNING")
                  and not session.get("stop_requested")
                  and not session.get("locked")):
                session.update(state="ARMED", reason="Recovered active paper session; waiting for current market data")
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

    def nifty_position_expiry(self):
        with self.lock:
            positions = self.data['sessions']['NIFTY']['positions']
            return positions[0]['contract']['expiry'] if positions else None

    def _v5_observation(self, s, bars, now):
        if self.nifty_observer:
            observation,history = self.nifty_observer.snapshot(now)
            self.v5_flow_history = history
            s['signal'] = observation
            return observation
        ticks = self.nifty_stream.latest(now)['ticks'] if self.nifty_stream else []
        observation = nifty_v5.flow(bars, ticks, now)
        if observation.get('eligible'):
            history = self.v5_flow_history
            stamp = observation['timestamp']
            if not history or history[-1]['timestamp'] != stamp:
                history.append(observation)
                self.v5_flow_history = history[-90:]
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
        # The one-second ML collector already stores NIFTY v5's spot and
        # pinned option books; duplicating full seven-day bar arrays on every
        # valuation would grow this uncompressed ledger by gigabytes.
        if self.record_market_data and strategy_id != 'nfv5':
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
                                     market, session.get('mode') or 'paper',
                                     float(session['net_pnl']) - float(session.get('pnl_reset_offset') or 0),
                                     float(session['capital']), int(session['entries'])))
            visible_daily_pnl = sum(
                float(session["net_pnl"]) - float(session.get("pnl_reset_offset") or 0)
                for session in self.data["sessions"].values()
                if session.get("date") == account["date"])
            self.data["history"].append({"date": account["date"], "net_pnl": visible_daily_pnl,
                                          "capital": account["capital"]})
            account["lifetime_pnl"] += visible_daily_pnl
        uncapped_v3 = any(s.get('date') and s.get('strategy_id') in ('nfv1', 'nfv3', 'mcxv1', 'mcxv3')
                          for s in self.data['sessions'].values())
        account.update(date=now.date().isoformat(), daily_pnl=0.,
                       halted=False if uncapped_v3 else account["drawdown"] >= .05 * account["capital"])
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
                    (spec.id not in ('nfv1', 'nfv3', 'mcxv1', 'mcxv3') and
                     self.data["account"]["drawdown"] >= .05*capital)):
                raise ValueError("Risk limit reached; restarting cannot clear the lock")
            if any(x["date"] for x in self.data["sessions"].values()) and capital != self.data["account"]["capital"]:
                raise ValueError("Both sessions share one account; capital is fixed for the trading day")
            other = self.data["sessions"]["MCX" if market == "NIFTY" else "NIFTY"]
            if immediate and other["positions"]:
                raise ValueError("Flatten the other session before reusing account capital")
            self.data["account"]["capital"] = float(capital)
            authorization = {"strategy_id":spec.id, "mode":mode,
                             "multiplier":multiplier, "capital":float(capital),
                             "auto_restart":True}
            self.data['strategy_authorizations'][market] = authorization
            self._revision += 1
            if immediate:
                s.update(state="ARMED", mode=mode, multiplier=multiplier, capital=float(capital), strategy_id=spec.id,
                         date=now.date().isoformat(), reason="Paper session armed; waiting for valid market data and signal",
                         stop_requested=False, exit_requested=False, paused=False)
                self._event(f"{market}: PAPER session authorized at {multiplier}×; shared capital ₹{capital:,.0f}")
            else:
                self.data['schedules'][market] = {**authorization, "scheduled_for":target.isoformat()}
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
            self.data['strategy_authorizations'].pop(market, None)
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
                                        net_pnl=round(float(session['net_pnl']) - float(session.get('pnl_reset_offset') or 0), 4),
                                        capital=session['capital'], entries=session['entries']))
            result['strategy_history'] = history
            for market, s in result["sessions"].items():
                s["net_pnl"] = round(float(s["net_pnl"]) - float(s.get("pnl_reset_offset") or 0), 2)
                open_gross = float(s["unrealized_pnl"]) - sum(
                    float(p.get("pnl_reset_unrealized") or 0) for p in s["positions"])
                open_entry_costs = sum(cost(p["entry_price"], p["quantity"]) for p in s["positions"])
                s["unrealized_pnl"] = round(open_gross - open_entry_costs -
                                            float(s.get("estimated_exit_costs") or 0), 2)
                s["realized_pnl"] = round(s["net_pnl"] - s["unrealized_pnl"], 2)
                for position in s["positions"]:
                    position["unrealized_pnl"] = round(
                        float(position["unrealized_pnl"]) - float(position.get("pnl_reset_unrealized") or 0), 4)
            result['account']['daily_pnl'] = round(sum(
                s['net_pnl'] for s in result['sessions'].values()
                if s.get('date') == now.date().isoformat()), 4)
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
        uncapped = any(s.get('date') and s.get('strategy_id') in ('nfv1', 'nfv3', 'mcxv1', 'mcxv3')
                       for s in self.data['sessions'].values())
        a['daily_loss_fraction'] = (None if uncapped else .05 if any(
            resolve(m, s.get('strategy_id')).version >= 3 and s['date']
            for m, s in self.data['sessions'].items()) else .01)
        if not uncapped and (a["daily_pnl"] <= -a['daily_loss_fraction']*a["capital"] or
                                a["drawdown"] >= .05*a["capital"]):
            a["halted"] = True
        if a["halted"]:
            for s in self.data["sessions"].values():
                s["locked"] = True
                s["exit_requested"] = bool(s["positions"])

    def _mark(self, s, quotes, now):
        by_symbol = {q.contract.symbol: q for q in quotes}
        total = 0.
        exit_costs = 0.
        missing = False
        thin = False
        for p in s["positions"]:
            q = by_symbol.get(p["symbol"])
            if q is None or not fresh(q, now):
                missing = True
                continue
            closing_buy = p["side"] == "SELL"
            size = q.ask_size if closing_buy else q.bid_size
            if size < p["quantity"]:
                thin = True
                continue
            price = q.ask + q.contract.tick_size if closing_buy else max(q.contract.tick_size, q.bid-q.contract.tick_size)
            pnl = (price-p["entry_price"]) * p["quantity"] * (1 if p["side"] == "BUY" else -1)
            mark_stamp = (q.book_observed_at or q.timestamp).isoformat()
            p.update(mark_price=price, unrealized_pnl=round(pnl, 4), mark_timestamp=mark_stamp)
            if p["side"] == "SELL":
                samples = p.setdefault("premium_marks", [])
                if not samples or samples[-1].get("timestamp") != mark_stamp:
                    samples.append({"timestamp": mark_stamp, "price": round(price, 4)})
                    del samples[:-31]
            if p["side"] == "BUY":
                p["best_mark"] = max(p.get("best_mark", p["entry_price"]), price)
            else:
                p["best_mark"] = min(p.get("best_mark", p["entry_price"]), price)
            total += pnl
            exit_costs += cost(price, p["quantity"])
        # Refresh every available leg even when another leg has no executable
        # depth. Basket P&L and all trading decisions still require every leg.
        if missing:
            raise FeedError("Open position quote is missing/stale; P&L is last known, exit remains pending")
        if thin:
            raise FeedError("Insufficient displayed depth to close; exposure retained")
        s["unrealized_pnl"] = round(total, 4)
        s["estimated_exit_costs"] = round(exit_costs, 4)
        s["net_pnl"] = round(s["realized_pnl"]+total-s["costs"]-exit_costs, 4)
        if quotes:
            s["feed_timestamp"] = min(q.book_observed_at or q.timestamp for q in quotes).isoformat()

    def _enter(self, market, s, plan, now):
        spec = resolve(market, s.get('strategy_id'))
        naked = spec.id in ('mcxv1', 'mcxv3') and plan.strategy == spec.id and plan.max_loss is None and all(l.side == 'SELL' for l in plan.legs)
        if market == 'MCX':
            legs = list(plan.legs)
            if (not naked or len(legs) != 2 or
                    {_option_type(leg.quote.contract) for leg in legs} != {'CE', 'PE'} or
                    len({(leg.quote.contract.exchange, leg.quote.contract.expiry,
                          leg.quote.contract.strike, leg.quote.contract.lot_size,
                          leg.quote.contract.symbol.upper().startswith('NATGASMINI'))
                         for leg in legs}) != 1):
                raise ValueError('MCX entry must sell one call and one put at the same strike, expiry and contract family')
        limit = (spec.trade_risk_per_unit or 0)*s['multiplier']
        if any(p['side'] != 'BUY' for p in s['positions']) or (not naked and (plan.max_loss is None or plan.max_loss > limit)):
            raise ValueError("Invalid portfolio risk")
        if not naked and spec.id not in ('nfv1', 'nfv3', 'nfv5') and plan.max_loss + self.data["account"]["drawdown"] >= .05*self.data["account"]["capital"]:
            s["reason"] = "Trade would exceed remaining portfolio drawdown budget"
            return
        held = list(s['positions'])
        held_quotes = []
        if held:
            revision = self._revision
            _, held_quotes = self._snapshot(market, now, [contract_from(p['contract']) for p in held])
            now = self.clock().astimezone(IST)
            if revision != self._revision or s['stop_requested'] or s['exit_requested'] or s['locked']:
                raise FeedError('Control request changed during hedge refresh; entry discarded')
            self._mark(s, held_quotes, now)
        # A new ATM strike may need a different 1000-point hedge pair. Buy
        # replacements in the plan and retire only the older, superseded
        # wings; otherwise successive baskets accumulate four or six longs.
        planned_wings = {leg.quote.contract.symbol for leg in plan.legs if leg.side == "BUY"}
        retiring = ([p for p in held if p["side"] == "BUY" and p["symbol"] not in planned_wings]
                    if spec.id in ("nfv1", "nfv3") else [])
        active_held = [p for p in held if p not in retiring]
        positions, risk_positions, fills, costs = list(active_held), [], [], 0.
        reused = set()
        reused_exit_cost = 0.
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
            existing = next((p for p in active_held if leg.side == 'BUY' and p['symbol'] == q.contract.symbol
                             and p['quantity'] == qty and p['symbol'] not in reused), None)
            if existing is not None:
                reused.add(existing['symbol'])
                risk_positions.append({**existing, 'entry_price': price})
                reused_exit_cost += cost(existing['mark_price'], qty)
                continue
            fee = cost(price, qty)
            costs += fee
            position = {"symbol": q.contract.symbol, "contract": contract_dict(q.contract),
                        "side": leg.side, "quantity": qty, "entry_price": price,
                        "mark_price": price, "unrealized_pnl": 0.}
            if spec.id in ("nfv1", "mcxv1"):
                position["entry_signal_bar"] = s.get("signal", {}).get("indicators", {}).get("last_bar_open")
            positions.append(position)
            risk_positions.append(position)
            fills.append({"timestamp": now.isoformat(), "symbol": q.contract.symbol,
                          "side": leg.side, "quantity": qty, "price": price, "cost": fee,
                          "mode": "paper", "reason": "ENTRY", "simulated": True})
        held_by_symbol = {q.contract.symbol: q for q in held_quotes}
        for existing in active_held:
            if existing['symbol'] not in reused:
                q = held_by_symbol[existing['symbol']]
                risk_positions.append({**existing, 'entry_price': q.ask + q.contract.tick_size})
                reused_exit_cost += cost(existing['mark_price'], existing['quantity'])
        # Recompute expiration payoff using executable fills, including one-tick
        # slippage and a reserve for both sides of the round trip.
        strikes = sorted({p["contract"]["strike"] for p in risk_positions})
        payoffs = []
        for spot in [0.] + strikes + [max(strikes)*2]:
            payoff = 0.
            for p in risk_positions:
                c = p["contract"]
                intrinsic = max(0., spot-c["strike"]) if c["option_type"] in ("C", "CE", "CALL") else max(0., c["strike"]-spot)
                payoff += (intrinsic-p["entry_price"])*p["quantity"]*(1 if p["side"] == "BUY" else -1)
            payoffs.append(payoff)
        actual_risk = max(0., -min(payoffs)) + costs*2 + reused_exit_cost
        if naked:
            actual_risk = None  # A stop trigger does not cap naked-option loss.
        if not naked and actual_risk > limit:
            s["reason"] = "Executable fill and cost model exceed this strategy's maximum loss budget"
            return
        retire_fills, retire_costs, retire_realized = [], 0.0, 0.0
        for old in retiring:
            quote = held_by_symbol.get(old["symbol"])
            if (quote is None or not fresh(quote, now) or
                    quote.bid_size < old["quantity"]):
                raise FeedError("Superseded hedge cannot be retired on fresh executable depth")
            price = max(quote.contract.tick_size, quote.bid-quote.contract.tick_size)
            fee = cost(price, old["quantity"])
            retire_costs += fee
            retire_realized += (price-old["entry_price"])*old["quantity"]
            retire_fills.append({"timestamp":now.isoformat(), "symbol":old["symbol"],
                                 "side":"SELL", "quantity":old["quantity"],
                                 "price":price, "cost":fee, "mode":"paper",
                                 "reason":"Replaced superseded hedge after new protection",
                                 "simulated":True})
        # A basket stop is measured from this entry, while the session ledger
        # continues to include every earlier closed leg and its costs.
        basket_start_net = s["net_pnl"]
        s.update(positions=positions, state="RUNNING", reason=plan.reason, stop_loss=plan.stop_loss,
                 take_profit=plan.take_profit, trade_costs=costs, max_loss=actual_risk,
                 entries=s["entries"]+1, entered_at=now.isoformat(), direction=plan.direction,
                 reversal_count=0, reversal_bar=None,
                 risk_code_fingerprint=self._risk_code_fingerprint)
        if spec.id == "nfv3":
            s["cycle_start_net"] = basket_start_net
        if spec.version == 1:
            for p in positions:
                if p["side"] == "SELL":
                    p.update(best_mark=p["entry_price"], leg_stop=None, trail_armed=False)
        if spec.id == "mcxv3":
            s.update(cycle_start_net=s["net_pnl"], trend_count=0,
                     trend_bar=s.get("signal", {}).get("indicators", {}).get("last_bar_open"),
                     reentry_count=0, reentry_bar=None, leg_reentries=s.get("leg_reentries", 0))
            for p in positions:
                p.update(best_mark=p["entry_price"], leg_stop=None, trail_armed=False)
        if spec.id == "nfv3":
            s['v3_anchor'] = {_option_type(contract_from(p['contract'])):p['contract']
                              for p in positions if p['side'] == 'SELL'}
            s.update(v3_trend_bar=None, v3_trend_count=0, v3_trend_direction=0)
            for p in positions:
                if p['side'] == 'SELL':
                    p.update(best_mark=p['entry_price'], leg_stop=None, trail_armed=False)
        if spec.id == "nfv5":
            s['v5_anchor'] = { _option_type(contract_from(p['contract'])): p['contract']
                               for p in positions if p['side'] == 'SELL' }
            s['v5_state'] = 'DUAL'
            for p in positions:
                if p['side'] == 'SELL':
                    p.update(best_mark=p['entry_price'], leg_stop=None, trail_armed=False)
        s["costs"] += costs + retire_costs
        s["realized_pnl"] += retire_realized
        s["trades"].extend(fills + retire_fills)
        self._mark(s, [leg.quote for leg in plan.legs] + held_quotes, now)
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

    def _mcx_reentry_quote(self, held, missing, now):
        """Fetch only the original straddle strike's missing option contract."""
        if not hasattr(self.feed, 'contracts'):
            raise FeedError('MCX contract master is unavailable for same-strike re-entry')
        anchor = contract_from(held['contract'])
        if anchor.exchange != 'MCX' or _option_type(anchor) == missing:
            raise FeedError('MCX held leg is not a valid opposite-side anchor')
        self.lock.release()
        try:
            contracts = self.feed.contracts('MCX', now)
        finally:
            self.lock.acquire()
        mini = anchor.symbol.upper().startswith('NATGASMINI')
        candidates = [c for c in contracts if c.exchange == 'MCX' and
                      _option_type(c) == missing and c.expiry == anchor.expiry and
                      c.strike == anchor.strike and c.lot_size == anchor.lot_size and
                      c.symbol.upper().startswith('NATGASMINI') == mini]
        if len(candidates) != 1:
            raise FeedError('Matching MCX opposite leg is unavailable or ambiguous')
        _, quotes = self._snapshot('MCX', now, [candidates[0]])
        quote = next((q for q in quotes if q.contract.token == candidates[0].token), None)
        if quote is None or not fresh(quote, self.clock().astimezone(IST)) or quote.bid_size < held['quantity']:
            raise FeedError('Matching MCX opposite leg has no fresh executable bid')
        return quote

    def _restructure_held_short(self, s, held, stop, now):
        """Keep the original fill/P&L, but begin a fresh stop cycle at this mark."""
        mark = held['mark_price']
        held.update(risk_entry_price=mark, best_mark=mark, leg_stop=round(stop, 4),
                    trail_armed=False, premium_marks=[{'timestamp':now.isoformat(), 'price':mark}],
                    last_restructured_at=now.isoformat())
        s['restructures'] = s.get('restructures', 0) + 1

    def _restructure_attempt_due(self, s, now):
        """Limit repeated missing-leg lookups while still marking every second."""
        previous = s.get('last_restructure_attempt')
        if previous and (now-datetime.fromisoformat(previous)).total_seconds() < 3:
            return False
        s['last_restructure_attempt'] = now.isoformat()
        return True

    def _reenter_v1_leg(self, market, s, held, missing, signal, quotes, now, reason):
        spec = resolve(market, s['strategy_id'])
        if (s.get('paused') or s.get('locked') or s.get('exit_requested') or
                s.get('stop_requested') or s['entries'] >= spec.max_entries or
                not spec.entry_start <= now.strftime('%H:%M') < spec.entry_end):
            return False
        try:
            if market == 'MCX':
                chain = [self._mcx_reentry_quote(held, missing, now)]
            else:
                _, chain = self._snapshot(market, now)
        except FeedError:
            s['reason'] = 'V1 restore waiting for fresh opposite-leg depth'
            return False
        if s.get('exit_requested') or s.get('stop_requested'):
            return False
        candidates = [q for q in chain if _option_type(q.contract) == missing
                      and q.contract.expiry.isoformat() == held['contract']['expiry']
                      and q.contract.lot_size == held['contract']['lot_size']
                      and (market != 'MCX' or q.contract.strike == held['contract']['strike'])
                      and q.bid_size >= held['quantity'] and fresh(q, self.clock().astimezone(IST))]
        if market == 'NIFTY':
            wing = next((p for p in s['positions'] if p['side'] == 'BUY'
                         and _option_type(contract_from(p['contract'])) == missing), None)
            if wing is None:
                return False
            strike = wing['contract']['strike']
            candidates = [q for q in candidates if
                          (q.contract.strike <= strike-1000 if missing == 'CE' else
                           q.contract.strike >= strike+1000)]
        if not candidates:
            s['reason'] = 'V1 restore waiting for protected opposite-leg depth'
            return False
        spot = signal.get('indicators', {}).get('close') or held['contract']['strike']
        q = min(candidates, key=lambda item: abs(item.contract.strike-spot))
        price = q.bid-q.contract.tick_size
        if price <= 0:
            return False
        fee = cost(price, held['quantity'])
        s['positions'].append({'symbol': q.contract.symbol, 'contract': contract_dict(q.contract),
                               'side': 'SELL', 'quantity': held['quantity'], 'entry_price': price,
                               'mark_price': price, 'unrealized_pnl': 0., 'best_mark': price,
                               'leg_stop': None, 'trail_armed': False,
                               'entry_signal_bar': signal.get('indicators', {}).get('last_bar_open')})
        s['costs'] += fee
        s['trades'].append({'timestamp': now.isoformat(), 'symbol': q.contract.symbol,
                            'side': 'SELL', 'quantity': held['quantity'], 'price': price,
                            'cost': fee, 'mode': 'paper', 'reason': reason,
                            'simulated': True})
        s['entries'] += 1
        s['reason'] = 'V1 paper strangle restored'
        self._mark(s, quotes+[q], now)
        self._event(f'{market}: simulated v1 {missing} re-entry')
        return True

    def _manage_v1(self, market, s, signal, quotes, now):
        """Manage the dashboard v1 paper port without invoking legacy broker code."""
        shorts = [p for p in s['positions'] if p['side'] == 'SELL']
        if not shorts:
            return
        slope = signal.get('indicators', {}).get('kama_slope', 0.) if signal.get('eligible') else 0.
        direction = signal.get('direction', 0) if signal.get('eligible') else 0
        signal_bar = signal.get('indicators', {}).get('last_bar_open')
        initial = .15 if market == 'NIFTY' else .10
        efficiency = signal.get('indicators', {}).get('efficiency5')
        efficiency = efficiency if isinstance(efficiency, (int, float)) else .5
        solo_trail = .09 if market == 'NIFTY' else min(.14, .08 + .06*(1-efficiency))
        volatility_ratio = signal.get('indicators', {}).get('volatility_ratio', 1.0)
        for p in list(shorts):
            if not p.get('entry_signal_bar'):
                p['entry_signal_bar'] = signal_bar
            entry, mark = p.get('risk_entry_price', p['entry_price']), p['mark_price']
            p['best_mark'] = min(p.get('best_mark', entry), mark)
            solo = len([x for x in s['positions'] if x['side'] == 'SELL']) == 1
            stop_pct, trail_pct = adaptive_short_stop(
                entry, p.get('premium_marks'), volatility_ratio, initial, solo_trail,
                solo=solo, stop_bounds=(.07, .20 if market == 'NIFTY' else .16),
                trail_bounds=(.04, .12 if market == 'NIFTY' else .16))
            hard_stop = entry*(1+stop_pct)
            stop = min(hard_stop, p['best_mark']*(1+trail_pct) if solo else hard_stop)
            p['leg_stop'] = min(p.get('leg_stop') or stop, stop)
            p['trail_armed'] = solo
            option = _option_type(contract_from(p['contract']))
            impulse = ((direction > 0 and option == 'CE' or direction < 0 and option == 'PE')
                       and signal_bar and p.get('entry_signal_bar') and
                       signal_bar > p['entry_signal_bar'])
            trail_hit = solo and mark >= p['leg_stop'] and mark < hard_stop
            if trail_hit:
                missing = 'CE' if option == 'PE' else 'PE'
                if (self._restructure_attempt_due(s, now) and
                        self._reenter_v1_leg(market, s, p, missing, signal, quotes, now,
                                             'V1 trailing-stop strangle restructure')):
                    reset_mark = p['mark_price']
                    dual_stop, _ = adaptive_short_stop(
                        reset_mark, None, volatility_ratio, initial, solo_trail, solo=False,
                        stop_bounds=(.07, .20 if market == 'NIFTY' else .16),
                        trail_bounds=(.04, .12 if market == 'NIFTY' else .16))
                    self._restructure_held_short(s, p, reset_mark*(1+dual_stop), now)
                    s['reason'] = 'V1 strangle restructured; held short retained at original fill'
                return
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
            # Close stale wings, then allow the normal cooldown path to open
            # another protected ATM strangle while the session is authorized.
            if market == 'NIFTY':
                session_limit = resolve(market, s['strategy_id']).session_loss_per_unit
                if session_limit is not None and s['net_pnl'] <= -session_limit*s['multiplier']:
                    s.update(locked=True, exit_requested=True,
                             reason='NIFTY v1 session loss cap reached; closing protective wings')
                else:
                    s.update(exit_requested=True, last_exit=now.isoformat(),
                             reason='Both v1 shorts exited; next protected strangle after cooldown')
            else:
                s.update(state='COOLDOWN', last_exit=now.isoformat())
            return
        if (len(shorts) != 1 or s.get('paused') or not signal.get('eligible') or
                now.strftime('%H:%M') >= resolve(market, s['strategy_id']).entry_end or
                s['entries'] >= resolve(market, s['strategy_id']).max_entries or
                (market != 'MCX' and resolve(market, s['strategy_id']).session_loss_per_unit is not None and
                 s['net_pnl'] <= -resolve(market, s['strategy_id']).session_loss_per_unit*s['multiplier']) or
                not s.get('last_leg_exit') or
                (now-datetime.fromisoformat(s['last_leg_exit'])).total_seconds() < 60):
            return
        held = shorts[0]
        missing = 'CE' if _option_type(contract_from(held['contract'])) == 'PE' else 'PE'
        if direction != (-1 if missing == 'CE' else 1):
            return
        self._reenter_v1_leg(market, s, held, missing, signal, quotes, now,
                             'V1 KAMA reversal re-entry')

    def _manage_mcx_v3(self, s, signal, quotes, now):
        if s["exit_requested"] or s["stop_requested"]:
            return
        indicators = signal.get("indicators", {}) if signal.get("eligible") else {}
        er, atr, spot = indicators.get("efficiency", .5), indicators.get("atr14", 0.), indicators.get("close", 1.)
        base_stop = min(.28, max(.12, .16 + .08*(1-er) + min(.04, 2*atr/spot)))
        base_trail = min(.16, max(.05, .05 + .08*(1-er) + min(.03, atr/spot)))
        volatility_ratio = indicators.get("volatility_ratio", 1.0)
        for p in list(s["positions"]):
            entry, mark = p.get("risk_entry_price", p["entry_price"]), p["mark_price"]
            p["best_mark"] = min(p.get("best_mark", entry), mark)
            if len(s["positions"]) == 1 or mark <= .92*entry:
                p["trail_armed"] = True
            # A lone short remains trailed, but its cushion expands in noisy
            # MCX moves. The separate adaptive premium stop still caps loss.
            leg_trail_pct = (min(.18, max(base_trail, .10 + .08*(1-er)))
                             if len(s["positions"]) == 1 else base_trail)
            stop_pct, trail_pct = adaptive_short_stop(
                entry, p.get("premium_marks"), volatility_ratio, base_stop, leg_trail_pct,
                solo=len(s["positions"]) == 1, stop_bounds=(.10, .28), trail_bounds=(.04, .18))
            distance = max(p["contract"]["tick_size"], entry*trail_pct)
            hard_stop = entry*(1+stop_pct)
            stop = hard_stop
            if p["trail_armed"]:
                stop = min(stop, p["best_mark"]+distance)
            p["leg_stop"] = round(min(p.get("leg_stop") or stop, stop), 4)
            p["stop_pct_cap"] = .28
            if len(s["positions"]) == 1 and mark >= p["leg_stop"] and mark < hard_stop:
                missing = 'CE' if _option_type(contract_from(p['contract'])) == 'PE' else 'PE'
                if (self._restructure_attempt_due(s, now) and
                        self._reenter_mcx_v3_leg(s, p, missing, quotes, now,
                                                 'MCX trailing-stop straddle restructure',
                                                 indicators.get('last_bar_open'))):
                    reset_mark = p['mark_price']
                    dual_stop, _ = adaptive_short_stop(
                        reset_mark, None, volatility_ratio, base_stop, base_trail,
                        solo=False, stop_bounds=(.10, .28), trail_bounds=(.04, .18))
                    self._restructure_held_short(s, p, reset_mark*(1+dual_stop), now)
                    s['reason'] = 'MCX straddle restructured; held short retained at original fill'
                return
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
        score = indicators["flow_score"]
        if not s.get("missing_armed"):
            if (score >= .35 if missing == "CE" else score <= -.35):
                s["missing_armed"] = True
            return
        # Re-entry uses the same fully confirmed direction as leg exits.
        # A weak KAMA turn during a bounce no longer restores the losing side.
        reversal = signal["direction"] == (-1 if missing == "CE" else 1)
        if bar != s.get("reentry_bar"):
            s["reentry_bar"] = bar
            s["reentry_count"] = s.get("reentry_count", 0)+1 if reversal else 0
        if not reversal or s["reentry_count"] < 2 or s.get("leg_reentries", 0) >= 12:
            return
        if not s.get("last_leg_exit") or (now-datetime.fromisoformat(s["last_leg_exit"])).total_seconds() < 60:
            return
        self._reenter_mcx_v3_leg(s, held, missing, quotes, now,
                                  'KAMA/EMA reversal re-entry', bar)

    def _reenter_mcx_v3_leg(self, s, held, missing, quotes, now, reason, bar):
        spec = resolve('MCX', 'mcxv3')
        if (s.get('paused') or s.get('exit_requested') or s.get('stop_requested') or
                s['entries'] >= spec.max_entries or s.get('leg_reentries', 0) >= 12 or
                not spec.entry_start <= now.strftime('%H:%M') < spec.entry_end):
            return False
        self._account()
        if s['locked'] or self.data['account']['halted']:
            return False
        try:
            q = self._mcx_reentry_quote(held, missing, now)
        except FeedError:
            s['reason'] = 'MCX restore waiting for original-strike opposite-leg depth'
            return False
        if s.get('exit_requested') or s.get('stop_requested'):
            return False
        price = q.bid-q.contract.tick_size
        if price <= 0:
            return False
        fee = cost(price, held['quantity'])
        s['positions'].append({'symbol':q.contract.symbol, 'contract':contract_dict(q.contract),
                               'side':'SELL', 'quantity':held['quantity'], 'entry_price':price,
                               'mark_price':price, 'unrealized_pnl':0., 'best_mark':price,
                               'leg_stop':None, 'trail_armed':False})
        s['costs'] += fee
        s['trades'].append({'timestamp':now.isoformat(), 'symbol':q.contract.symbol,
                            'side':'SELL', 'quantity':held['quantity'], 'price':price,
                            'cost':fee, 'mode':'paper', 'reason':reason, 'simulated':True})
        s['entries'] += 1
        s['leg_reentries'] = s.get('leg_reentries', 0)+1
        s['reentry_count'] = 0
        s['trend_count'] = 0
        s['trend_bar'] = bar
        s['missing_armed'] = False
        s['reason'] = 'MCX ATM straddle restored'
        self._mark(s, quotes+[q], now)
        self._event(f'MCX: simulated ATM {missing} re-entry')
        return True

    def _close_nifty_v3_leg(self, s, position, quotes, now, reason):
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
        self._event(f'NIFTY v3: simulated {_option_type(contract_from(position["contract"]))} short exit — {reason}')

    def _reenter_nifty_v3_leg(self, s, kind, quotes, now,
                               reason='V3 trend pause/reversal re-entry'):
        spec = resolve('NIFTY', 'nfv3')
        anchor = s.get('v3_anchor', {}).get(kind)
        wing = next((p for p in s['positions'] if p['side'] == 'BUY' and
                     _option_type(contract_from(p['contract'])) == kind), None)
        if (not anchor or not wing or s.get('paused') or s['entries'] >= spec.max_entries or
                s['stop_requested'] or s['exit_requested'] or s['locked'] or
                now.strftime('%H:%M') >= spec.entry_end or
                (spec.session_loss_per_unit is not None and
                 s['net_pnl'] <= -spec.session_loss_per_unit*s['multiplier'])):
            return False
        width = (wing['contract']['strike'] - anchor['strike'] if kind == 'CE'
                 else anchor['strike'] - wing['contract']['strike'])
        if width != 1000 or wing['quantity'] != anchor['lot_size']*s['multiplier']:
            return False
        try:
            _, candidates = self._snapshot('NIFTY', now, [contract_from(anchor)])
        except FeedError:
            s['reason'] = 'V3 restore waiting for fresh original-strike depth'
            return False
        if s.get('stop_requested') or s.get('exit_requested'):
            return False
        q = next((q for q in candidates if q.contract.symbol == anchor['symbol']), None)
        quantity = anchor['lot_size']*s['multiplier']
        if q is None or not fresh(q, self.clock().astimezone(IST)) or q.bid_size < quantity:
            return False
        price = q.bid - q.contract.tick_size
        if price <= 0:
            return False
        fee = cost(price, quantity)
        s['positions'].append({'symbol':q.contract.symbol, 'contract':anchor, 'side':'SELL',
                               'quantity':quantity, 'entry_price':price, 'mark_price':price,
                               'unrealized_pnl':0., 'best_mark':price, 'leg_stop':None,
                               'trail_armed':False})
        s['costs'] += fee
        s['entries'] += 1
        s['leg_reentries'] = s.get('leg_reentries', 0) + 1
        s['trades'].append({'timestamp':now.isoformat(), 'symbol':q.contract.symbol,
                            'side':'SELL', 'quantity':quantity, 'price':price, 'cost':fee,
                            'mode':'paper', 'reason':reason,
                            'simulated':True})
        self._mark(s, quotes+[q], now)
        s['reason'] = 'V3 ATM straddle restored at the protected anchor'
        self._event(f'NIFTY v3: simulated {kind} short re-entry')
        return True

    def _manage_nifty_v3(self, s, signal, quotes, now):
        shorts = [p for p in s['positions'] if p['side'] == 'SELL']
        if not shorts or s['stop_requested'] or s['exit_requested']:
            return
        indicators = signal.get('indicators', {}) if signal.get('eligible') else {}
        direction = signal.get('direction', 0) if indicators else 0
        bar = indicators.get('last_bar_open')
        if bar and bar != s.get('v3_trend_bar'):
            s['v3_trend_count'] = (s.get('v3_trend_count', 0) + 1
                                   if direction == s.get('v3_trend_direction') else 1)
            s['v3_trend_bar'] = bar
            s['v3_trend_direction'] = direction
        confirmed = s.get('v3_trend_count', 0) >= 2
        atr = indicators.get('atr14', 0.)
        spot = indicators.get('close', 0.)
        volatility = atr/spot if spot and atr else 0.
        base_stop = min(.30, max(.15, .18 + 8*volatility))
        base_trail = min(.16, max(.05, .07 + 4*volatility))
        volatility_ratio = indicators.get('volatility_ratio', 1.0)
        regime = (float(volatility_ratio) if isinstance(volatility_ratio, (int, float))
                  and math.isfinite(volatility_ratio) else 1.0)
        for p in list(shorts):
            mark, entry = p['mark_price'], p.get('risk_entry_price', p['entry_price'])
            p['best_mark'] = min(p.get('best_mark', entry), mark)
            solo = len(shorts) == 1
            gain_fraction = max(0., (entry-p['best_mark'])/entry) if entry > 0 else 0.
            arm_fraction = min(.03, max(.015, .02 + .005*(regime-1)))
            p['trail_armed'] = solo and gain_fraction >= arm_fraction
            stop_pct, trail_pct = adaptive_short_stop(
                entry, p.get('premium_marks'), volatility_ratio, base_stop, base_trail,
                solo=solo, stop_bounds=(.10, .27), trail_bounds=(.04, .14))
            hard_stop = entry*(1+stop_pct)
            trail = hard_stop
            if p['trail_armed']:
                # The adaptive trail alone can allow most of a profitable
                # premium decay to reverse. Bound that distance and lock an
                # increasing share of the best observed gain.
                max_trail_pct = min(.05, max(.025, .035 + .01*(regime-1)))
                lock_share = min(.75, .45 + 1.5*max(0., gain_fraction-.02))
                trail = min(p['best_mark']*(1+min(trail_pct, max_trail_pct)),
                            entry-(entry-p['best_mark'])*lock_share)
            p['leg_stop'] = round(min(p.get('leg_stop') or min(hard_stop, trail), hard_stop, trail), 4)
            kind = _option_type(contract_from(p['contract']))
            trend_exit = len(shorts) == 2 and confirmed and ((direction == 1 and kind == 'CE') or
                                        (direction == -1 and kind == 'PE'))
            if solo and mark >= p['leg_stop'] and mark < hard_stop:
                missing = 'CE' if kind == 'PE' else 'PE'
                if (self._restructure_attempt_due(s, now) and
                        self._reenter_nifty_v3_leg(s, missing, quotes, now,
                                                    'V3 trailing-stop straddle restructure')):
                    reset_mark = p['mark_price']
                    dual_stop, _ = adaptive_short_stop(
                        reset_mark, None, volatility_ratio, base_stop, base_trail,
                        solo=False, stop_bounds=(.10, .27), trail_bounds=(.04, .14))
                    self._restructure_held_short(s, p, reset_mark*(1+dual_stop), now)
                    s['reason'] = 'V3 straddle restructured; held short retained at original fill'
                return
            if mark >= p['leg_stop'] or trend_exit:
                self._close_nifty_v3_leg(s, p, quotes, now,
                                         'V3 premium stop/trail' if mark >= p['leg_stop']
                                         else 'V3 confirmed trend; close losing short')
                shorts = [x for x in s['positions'] if x['side'] == 'SELL']
                if not shorts:
                    s.update(exit_requested=True, reason='V3 shorts exited; protective wings retained')
                return
        if len(shorts) == 1 and confirmed:
            held_kind = _option_type(contract_from(shorts[0]['contract']))
            missing = 'CE' if held_kind == 'PE' else 'PE'
            reversal = (held_kind == 'PE' and direction == -1) or (held_kind == 'CE' and direction == 1)
            last_exit = s.get('last_leg_exit')
            cooldown_done = (not last_exit or
                             (now-datetime.fromisoformat(last_exit)).total_seconds() >= 60)
            if reversal and cooldown_done:
                self._reenter_nifty_v3_leg(s, missing, quotes, now)

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

    def _reenter_v5(self, s, missing, quotes, now, restructure=False):
        spec = resolve('NIFTY','nfv5')
        if (s.get('paused') or s.get('exit_requested') or s.get('stop_requested') or
                s['entries'] >= spec.max_entries or now.strftime('%H:%M') >= spec.entry_end or
                s['net_pnl'] <= -spec.session_loss_per_unit*s['multiplier'] or
                (not restructure and (not s.get('last_leg_exit') or
                 (now-datetime.fromisoformat(s['last_leg_exit'])).total_seconds() < 30))):
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
        if q is None or not fresh(q,self.clock().astimezone(IST)) or q.bid_size < quantity:
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
                            'mode':'paper','reason':('V5 trailing-stop straddle restructure' if restructure
                                                    else 'V5 flow re-entry at protected anchor'),
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
        action = flow_decision(self.v5_flow_history,state) if observation.get('eligible') else None
        volatility_ratio = observation.get('volatility_ratio', 1.0)
        for p in list(shorts):
            mark,entry = p['mark_price'],p.get('risk_entry_price',p['entry_price'])
            p['best_mark'] = min(p.get('best_mark',entry),mark)
            stop_pct,trail_pct = nifty_v5.stop_parameters(
                observation, len(shorts)==1, entry, p.get('premium_marks'))
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
            if len(shorts)==1 and stop_hit and mark < entry*(1+stop_pct):
                missing = 'CE' if kind=='PE' else 'PE'
                if self._restructure_attempt_due(s,now) and self._reenter_v5(s,missing,quotes,now,restructure=True):
                    reset_mark = p['mark_price']
                    dual_stop,_ = nifty_v5.stop_parameters(observation,False,reset_mark,None)
                    self._restructure_held_short(s,p,reset_mark*(1+dual_stop),now)
                    s['reason'] = 'V5 straddle restructured; held short retained at original fill'
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
        release_hedges = hedge_release_reached(market, s.get("date"), now)
        # A risk, stop, or signal exit may close shorts now. Bought protection
        # stays in the paper ledger until the market's penultimate minute.
        closing = [p for p in s["positions"] if p["side"] == "SELL" or release_hedges]
        for p in sorted(closing, key=lambda p: p["side"] != "SELL"):
            fee = cost(p["mark_price"], p["quantity"])
            s["costs"] += fee
            s["realized_pnl"] += p["unrealized_pnl"]
            s["trades"].append({"timestamp": now.isoformat(), "symbol": p["symbol"],
                                "side": "BUY" if p["side"] == "SELL" else "SELL",
                                "quantity": p["quantity"], "price": p["mark_price"], "cost": fee,
                                "mode": "paper", "reason": s["reason"], "simulated": True})
        s["positions"] = [p for p in s["positions"] if p not in closing]
        s["exit_requested"] = False
        if closing:
            s["last_exit"] = now.isoformat()
        if s["positions"]:
            self._mark(s, quotes, now)
            s["state"] = ("HEDGE_HOLD" if s["stop_requested"] or s["locked"] or
                          now.strftime("%H:%M") >= resolve(market, s.get("strategy_id")).flatten
                          else "COOLDOWN")
            s["reason"] = (f"Protective buys held until {HEDGE_RELEASE[market]} IST; "
                           "short exposure closed")
            if closing:
                self._event(f"{market}: paper shorts flat; protective buys retained")
        else:
            s.update(unrealized_pnl=0., estimated_exit_costs=0.,
                     state="STOPPED" if s["stop_requested"] or s["locked"] else "COOLDOWN")
            s["net_pnl"] = round(s["realized_pnl"]-s["costs"], 4)
            self._event(f"{market}: paper positions flat; session net ₹{s['net_pnl']:.2f}")

    def _paper_close_at_last_marks(self, market, s, now):
        """End a paper session from recorded marks when executable depth is gone.

        This is accounting only. It never represents a broker fill or a price
        that could necessarily have been executed after the session cutoff.
        """
        marks = []
        for p in s["positions"]:
            try:
                price = float(p["mark_price"])
                entry = float(p["entry_price"])
                quantity = int(p["quantity"])
            except (KeyError, TypeError, ValueError, OverflowError):
                price = 0.
                try:
                    entry = float(p["entry_price"])
                    quantity = int(p["quantity"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    entry, quantity = 0., 0
            if not (math.isfinite(entry) and entry > 0 and quantity > 0):
                s.update(state="EXIT_PENDING", reason="Paper EOD close blocked by invalid leg entry data")
                return
            has_mark = math.isfinite(price) and price > 0
            if not has_mark:
                # A missing final quote must not leave a simulated leg open.
                # Entry price is a neutral fallback, explicitly labeled below.
                price = entry
            marks.append((p, price, entry, quantity, has_mark))
        for p, price, entry, quantity, has_mark in marks:
            pnl = (price-entry)*quantity*(1 if p["side"] == "BUY" else -1)
            fee = cost(price, quantity)
            s["realized_pnl"] += pnl
            s["costs"] += fee
            s["trades"].append({"timestamp": now.isoformat(), "symbol": p["symbol"],
                                "side": "BUY" if p["side"] == "SELL" else "SELL",
                                "quantity": quantity, "price": price, "cost": fee,
                                "mode": "paper", "simulated": True,
                                "last_mark_timestamp": p.get("mark_timestamp") or s.get("feed_timestamp"),
                                "reason": ("EOD paper close at last observed mark; not an executable broker fill"
                                           if has_mark else
                                           "EOD paper close; no mark available, entry-price accounting fallback; not executable")})
        count = len(marks)
        s["positions"] = []
        s.update(unrealized_pnl=0., estimated_exit_costs=0.,
                 net_pnl=round(s["realized_pnl"]-s["costs"], 4),
                 state="SESSION_COMPLETE", stop_requested=True, exit_requested=False,
                 last_exit=now.isoformat(),
                 reason="Paper session closed at last observed marks; prices may be stale")
        self._account()
        self._event(f"{market}: {count} paper legs closed at last observed marks after session cutoff")

    def _activate_schedules(self, now):
        self._queue_authorized_schedules(now)
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
                self.data['strategy_authorizations'].pop(market, None)
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

    def _queue_authorized_schedules(self, now):
        """Re-arm an explicitly enabled strategy for its next session.

        Session flattening is daily; the user's On authorization is durable
        until stop() removes it. Schedule the following exchange weekday once
        the runtime is flat and has stopped for the day.
        """
        for market, authorization in list(self.data.get('strategy_authorizations', {}).items()):
            if market in self.data['schedules']:
                continue
            session = self.data['sessions'][market]
            if session['positions'] or session['state'] not in ('STOPPED','SESSION_COMPLETE'):
                continue
            try:
                spec = resolve(market, authorization.get('strategy_id'))
            except ValueError:
                self.data['strategy_authorizations'].pop(market, None)
                continue
            # End-of-session, risk-stop, and a recovered prior-day session all
            # resume at the next session start, never immediately re-enter.
            base_time = (now.replace(hour=23, minute=59, second=59)
                         if session.get('date') == now.date().isoformat() else now)
            target = next_session_date(base_time, spec)
            self.data['schedules'][market] = {**authorization, 'scheduled_for':target.isoformat()}
            session['reason'] = f"Strategy remains ON; next session scheduled for {target.isoformat()} {spec.entry_start} IST"
            self._event(f"{market}: {spec.id} remains ON; next PAPER session scheduled for {target.isoformat()} {spec.entry_start} IST")

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
                signal_fn = legacy_v1.explain_signal if spec.version == 1 else active_v3.explain_signal
                plan_fn = legacy_v1.build_plan if spec.version == 1 else active_v3.build_plan
                deadline = spec.flatten
                ended = s["date"] != now.date().isoformat() or now.strftime("%H:%M") >= deadline or now.weekday() >= 5
                if ended:
                    s.update(stop_requested=True, exit_requested=bool(s["positions"]),
                             reason="Session deadline reached; new authorization required next day")
                    if not s["positions"]:
                        s["state"] = "SESSION_COMPLETE"
                        continue
                # Allow one minute for a normal fresh-quote exit. If it still
                # cannot fill, close paper legs at their last recorded marks.
                # Never use this accounting fallback for real broker positions.
                minute_now = now.hour*60+now.minute
                # Use the configured cutoff itself, never a minute after it.
                minute_limit = int(deadline[:2])*60+int(deadline[3:])
                if (ended and s["positions"] and s.get("mode") == "paper" and
                        (s.get("date") != now.date().isoformat() or
                         now.weekday() >= 5 or minute_now >= minute_limit)):
                    self._paper_close_at_last_marks(market, s, now)
                    continue
                if market == "NIFTY" and (now.weekday() >= 5 or
                        now.strftime("%H:%M") < "09:15" or
                        now.strftime("%H:%M") >= "15:40"):
                    # A closed exchange cannot provide executable NIFTY depth.
                    # Repeated reads here can exhaust the broker quote budget
                    # needed to manage the still-open MCX paper positions.
                    if s["positions"]:
                        s.update(state="EXIT_PENDING",
                                 reason="NIFTY market closed; paper exit awaits fresh session quotes")
                    continue
                try:
                    release_due = (any(p["side"] == "BUY" for p in s["positions"])
                                   and hedge_release_reached(market, s.get("date"), now))
                    if release_due and not ended:
                        s.update(stop_requested=True, exit_requested=True,
                                 reason="Protective hedge release cutoff reached")
                    shorts_open = any(p["side"] == "SELL" for p in s["positions"])
                    if (s["positions"] and (shorts_open or ended or release_due or
                                             s["stop_requested"] or s["exit_requested"] or
                                             s["locked"] or s["state"] == "RECOVERY_REQUIRED")):
                        bars, quotes = self._snapshot(market, now, [contract_from(p["contract"]) for p in s["positions"]])
                        self._mark(s, quotes, self.clock().astimezone(IST))
                        if (not s["exit_requested"] and
                                (s.pop("feed_waiting", False) or s["state"] == "DATA_WAIT" or
                                 s.get("reason", "").startswith("Recovered paper positions"))):
                            s.update(state="RUNNING", reason="Market data recovered; managing paper positions")
                        # Report the whole-session net MTM, but apply a basket
                        # stop to movement since that basket opened. Comparing
                        # cumulative loss with a fresh basket stop caused v3 to
                        # exit and re-enter every few seconds after one loss.
                        baseline = s.get("cycle_start_net")
                        if spec.id in ("mcxv3", "nfv3"):
                            trade_net = s["net_pnl"] - (baseline if isinstance(baseline, (int, float))
                                                        and math.isfinite(baseline) else s["net_pnl"])
                        else:
                            trade_net = s["net_pnl"]
                        signal = {}
                        if spec.id == 'nfv5':
                            signal = self._v5_observation(s,bars,self.clock().astimezone(IST))
                        if spec.version in (1, 3) and bars and not s['exit_requested']:
                            signal = signal_fn(market, bars, self.clock().astimezone(IST))
                            s['signal'] = signal
                            if spec.version == 3 and spec.id not in ("mcxv3", "nfv3"):
                                stamp = bars[-1].timestamp.isoformat()
                                if signal.get('eligible') and stamp != s.get('reversal_bar'):
                                    s['reversal_bar'] = stamp
                                    s['reversal_count'] = s.get('reversal_count', 0)+1 if signal.get('direction') != s.get('direction') else 0
                                    if s['reversal_count'] >= 2:
                                        s.update(exit_requested=True, reason='Indicator regime changed for two completed bars')
                        if (spec.id not in ('mcxv1', 'mcxv3') and spec.session_loss_per_unit is not None and
                                s["net_pnl"] <= -spec.session_loss_per_unit*s["multiplier"]):
                            s.update(locked=True, exit_requested=True, reason="Session loss limit reached")
                        elif spec.version != 1 and (trade_net <= -s["stop_loss"] or (spec.id != "mcxv3" and trade_net >= s["take_profit"])):
                            s.update(exit_requested=True, reason="Portfolio stop" if trade_net < 0 else "Portfolio take profit")
                        self._account()
                        if spec.id == "mcxv3" and not s["exit_requested"]:
                            self._manage_mcx_v3(s, signal, quotes, self.clock().astimezone(IST))
                            self._account()
                        if spec.id == 'nfv3' and not s['exit_requested']:
                            self._manage_nifty_v3(s, signal, quotes, self.clock().astimezone(IST))
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
                    long_only_v3 = False
                    if s["positions"]:
                        # Long-only protection survives a prior short exit. Keep
                        # marking it while allowing the next paper short cycle.
                        _, held_quotes = self._snapshot(
                            market, now, [contract_from(p["contract"]) for p in s["positions"]])
                        self._mark(s, held_quotes, self.clock().astimezone(IST))
                        if s["state"] == "DATA_WAIT":
                            s.update(state="COOLDOWN", reason="Market data recovered; protective buys retained")
                        long_only_v3 = (spec.id == 'nfv3' and
                                        all(p['side'] == 'BUY' for p in s['positions']))
                        if long_only_v3:
                            # The previous shorts are gone. Start a fresh ATM
                            # pair with 1000-point wings; matching wings are
                            # reused, while older protection remains held.
                            s['reason'] = 'V3 shorts flat; seeking a new protected ATM straddle'
                    self._account()
                    if s["locked"] or self.data["account"]["halted"]:
                        s.update(state="HEDGE_HOLD" if s["positions"] else "STOPPED",
                                 reason="Risk lock active; protective buys held until cutoff"
                                 if s["positions"] else "Risk lock active")
                        continue
                    if s.get("paused"):
                        s["reason"] = "New entries paused; open positions remain managed"
                        continue
                    if s["entries"] >= spec.max_entries:
                        s.update(state="STOPPED", reason="Session entry limit reached", locked=True)
                        continue
                    if (not long_only_v3 and s["last_exit"] and
                            (now-datetime.fromisoformat(s["last_exit"])).total_seconds() < spec.cooldown_seconds):
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
                                not nifty_v5.ready_to_open(self.v5_flow_history)):
                            continue
                        bars,quotes = self._snapshot(market,self.clock().astimezone(IST))
                        fresh_now = self.clock().astimezone(IST)
                        observation = self._v5_observation(s,bars,fresh_now)
                        if (not bars or not quotes or not observation.get('eligible') or
                                not nifty_v5.ready_to_open(self.v5_flow_history)):
                            raise FeedError('NIFTY flow or option-chain depth stale before entry')
                        plan = nifty_v5.build_plan(bars,quotes,fresh_now,s['multiplier'],s['capital'],observation)
                        if plan:
                            self._enter(market,s,plan,fresh_now)
                        else:
                            s['reason'] = 'Flow is balanced; protected ATM basket lacks fresh depth or risk capacity'
                        continue
                    bars, quotes = self._snapshot(market, now)
                    if s["state"] == "DATA_WAIT":
                        # A recovered broker read resumes the same paper
                        # session; it never consumes a fresh authorization.
                        s["state"] = "ARMED"
                    fresh_now = self.clock().astimezone(IST)
                    if not bars or (fresh_now-(bars[-1].timestamp+timedelta(minutes=spec.bar_minutes))).total_seconds() > (90 if spec.version in (1, 3) else 360):
                        raise FeedError("Completed underlying bars are missing or stale")
                    if quotes:
                        s["feed_timestamp"] = min(q.book_observed_at or q.timestamp for q in quotes).isoformat()
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
                    s["feed_waiting"] = True
                    s["feed_error_count"] = s.get("feed_error_count", 0) + 1
                    s["last_feed_error_at"] = self.clock().astimezone(IST).isoformat()
                    if s["positions"]:
                        # A failed data read is not a trading signal. Preserve
                        # the existing basket and retry; explicit stops and
                        # risk exits remain pending for fresh executable quotes.
                        s["state"] = "EXIT_PENDING" if s["exit_requested"] else "RUNNING"
                    else:
                        s["state"] = "ARMED"
                except Exception:
                    s.update(reason="Engine validation error; new entries halted, inspect local diagnostics", locked=True)
                    if s["positions"]:
                        s.update(exit_requested=True, state="EXIT_PENDING")
                    else:
                        s["state"] = "STOPPED"
            self._account()
            self._queue_authorized_schedules(now)
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
                if (s.get("mode") == "paper" and s.get("date") == self.clock().astimezone(IST).date().isoformat()
                        and not s.get("stop_requested") and not s.get("exit_requested")
                        and not s.get("locked")):
                    # A deployment is not a trading instruction. The durable
                    # ledger retains entry, best mark, fills and trail state.
                    if s["positions"]:
                        s.update(state="DATA_WAIT", reason="Paper worker restarting; open legs preserved")
                    elif s["state"] not in ("STOPPED", "SESSION_COMPLETE"):
                        s.update(state="ARMED", reason="Paper worker restarting; session remains armed")
                else:
                    s.update(state="RECOVERY_REQUIRED" if s["positions"] else "STOPPED",
                             stop_requested=True, exit_requested=bool(s["positions"]))
            self._save()
            self.db.close()
            fcntl.flock(self._file, fcntl.LOCK_UN)
            self._file.close()
            self._closed = True
