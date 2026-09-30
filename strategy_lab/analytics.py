"""Persistent paper-strategy performance ledger (no market-regime collection)."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .catalog import SPECS
from .models import IST
from .runtime import cost


class AnalyticsStore:
    """Capture strategy P&L and trades without polling spot/VIX market history."""

    def __init__(self, root: Path, controller, clock=None, feed=None):
        self.root = Path(root)
        self.controller = controller
        self.clock = clock or (lambda: datetime.now(IST))
        # `feed` remains accepted for caller compatibility; analytics no longer
        # requests market bars or option/volatility history.
        directory = self.root / 'data' / 'strategy_lab'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'analytics.sqlite3'
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.execute('''CREATE TABLE IF NOT EXISTS strategy_ticks(
            strategy_id TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,
            state TEXT NOT NULL, net_pnl REAL NOT NULL, realized_pnl REAL NOT NULL,
            unrealized_pnl REAL NOT NULL, capital REAL NOT NULL, multiplier INTEGER NOT NULL,
            legs INTEGER NOT NULL, spot REAL, vix REAL, signal TEXT, positions TEXT,
            PRIMARY KEY(strategy_id,ts))''')
        self.db.execute('CREATE INDEX IF NOT EXISTS strategy_ticks_day ON strategy_ticks(strategy_id,day,ts)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS strategy_trades(
            event_key TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, day TEXT NOT NULL,
            ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            quantity INTEGER NOT NULL, price REAL NOT NULL, cost REAL NOT NULL,
            reason TEXT NOT NULL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS strategy_trades_day ON strategy_trades(strategy_id,day,ts)')
        # Legacy regime columns are retained in the existing schema so prior
        # P&L rows remain readable; new rows are explicitly marked disabled.
        self.db.execute('''CREATE TABLE IF NOT EXISTS strategy_days(
            strategy_id TEXT NOT NULL, day TEXT NOT NULL, market TEXT NOT NULL,
            mode TEXT NOT NULL, pnl REAL NOT NULL, capital REAL NOT NULL,
            return_pct REAL NOT NULL, entries INTEGER NOT NULL,
            max_drawdown REAL, sampled_points INTEGER NOT NULL,
            market_regime TEXT NOT NULL, market_quality TEXT NOT NULL,
            PRIMARY KEY(strategy_id,day))''')
        self.db.commit()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.last_capture = None
        self._last_summary_minute = None
        self._closed = False

    def close(self):
        if self._closed:
            return
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=40)
            if self.worker.is_alive():
                raise RuntimeError('Analytics worker is still using its ledger')
        with self.lock:
            self.db.close()
            self._closed = True

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()

        def work():
            while not self.stop_event.is_set():
                now = self.clock().astimezone(IST).replace(microsecond=0)
                try:
                    self.capture_strategies(now)
                    minute = now.strftime('%Y-%m-%d %H:%M')
                    if minute != self._last_summary_minute:
                        self._last_summary_minute = minute
                        self.update_strategy_days()
                    self.last_capture = now.isoformat()
                except Exception:
                    # Avoid exposing request data or broker details in status.
                    pass
                self.stop_event.wait(10)

        self.worker = threading.Thread(target=work, name='paper-analytics', daemon=True)
        self.worker.start()

    def capture_strategies(self, now):
        day = now.date().isoformat()
        targets = [('legacy', self.controller.legacy)] + list(self.controller.children.items())
        for sid, child in targets:
            with child.lock:
                sessions = [(market, copy.deepcopy(session))
                            for market, session in child.data['sessions'].items()
                            if session.get('date') == day]
            for market, session in sessions:
                strategy_id = session.get('strategy_id') or (sid if sid != 'legacy' else None)
                if strategy_id not in SPECS:
                    continue
                leg_count = len(session.get('positions', []))
                net_pnl = float(session['net_pnl'])-float(session.get('pnl_reset_offset') or 0)
                open_gross = float(session['unrealized_pnl'])-sum(
                    float(p.get('pnl_reset_unrealized') or 0)
                    for p in session.get('positions', []))
                open_net = (open_gross-sum(cost(p['entry_price'], p['quantity'])
                                           for p in session.get('positions', []))-
                            float(session.get('estimated_exit_costs') or 0))
                with self.lock, self.db:
                    self.db.execute('''INSERT OR REPLACE INTO strategy_ticks VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (strategy_id, now.isoformat(), day, session['state'],
                         net_pnl, net_pnl-open_net, open_net,
                         float(session['capital']), int(session['multiplier']), leg_count,
                         None, None, None, None))
                    for trade in session.get('trades', []):
                        ts = str(trade.get('timestamp') or now.isoformat())
                        payload = json.dumps([strategy_id, ts, trade.get('symbol'),
                                              trade.get('side'), trade.get('quantity'),
                                              trade.get('price')], separators=(',', ':'))
                        key = hashlib.sha256(payload.encode()).hexdigest()
                        self.db.execute('''INSERT OR IGNORE INTO strategy_trades VALUES
                            (?,?,?,?,?,?,?,?,?,?)''',
                            (key, strategy_id, ts[:10], ts, str(trade.get('symbol') or ''),
                             str(trade.get('side') or ''), int(trade.get('quantity') or 0),
                             float(trade.get('price') or 0), float(trade.get('cost') or 0),
                             str(trade.get('reason') or '')[:240]))

    def update_strategy_days(self):
        states = [self.controller.legacy.status()] + [c.status() for c in self.controller.children.values()]
        records = {}
        for state in states:
            for row in state.get('strategy_history', []):
                sid = row.get('strategy_id')
                if sid in SPECS and row.get('date'):
                    records[(sid, row['date'])] = row
        with self.lock, self.db:
            for (sid, day), row in records.items():
                curve = self.db.execute('''SELECT net_pnl FROM strategy_ticks
                    WHERE strategy_id=? AND day=? ORDER BY ts''', (sid, day)).fetchall()
                peak = 0.0
                drawdown = 0.0
                for (pnl,) in curve:
                    peak = max(peak, pnl)
                    drawdown = max(drawdown, peak-pnl)
                capital = float(row.get('capital') or 0)
                pnl = float(row.get('net_pnl') or 0)
                self.db.execute('''INSERT OR REPLACE INTO strategy_days VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (sid, day, SPECS[sid].market, row.get('mode') or 'paper', pnl, capital,
                     round(100*pnl/capital, 5) if capital > 0 else 0.0,
                     int(row.get('entries') or 0), round(drawdown, 4) if curve else None,
                     len(curve), 'disabled', 'disabled'))

    def summary(self):
        with self.lock:
            daily = [dict(day=d, pnl=round(p, 2), allocation=round(c, 2),
                          return_pct=round(100*p/c, 3) if c else 0.0, strategies=n)
                     for d, p, c, n in self.db.execute('''SELECT day,SUM(pnl),SUM(capital),COUNT(*)
                         FROM strategy_days WHERE mode='paper' GROUP BY day
                         ORDER BY day DESC LIMIT 31''')]
            cutoff = (self.clock().astimezone(IST).date()-timedelta(days=60)).isoformat()
            rollup = [dict(strategy_id=sid, days=n, total_pnl=round(pnl, 2),
                           mean_daily_return_pct=round(avg_pct, 3), win_days=w, latest_day=latest)
                      for sid, n, pnl, avg_pct, w, latest in self.db.execute('''SELECT strategy_id,
                          COUNT(*),SUM(pnl),AVG(return_pct),SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),MAX(day)
                          FROM strategy_days WHERE day>=? AND mode='paper' GROUP BY strategy_id''', (cutoff,))]
        return {'daily': daily, 'strategy_rollup': rollup,
                'last_capture': self.last_capture,
                'collector_running': bool(self.worker and self.worker.is_alive())}

    def detail(self, strategy_id, month=None, day=None):
        if strategy_id not in SPECS:
            raise ValueError('Unknown strategy')
        month = month or self.clock().astimezone(IST).strftime('%Y-%m')
        if (len(month) != 7 or month[4] != '-' or not month[:4].isdigit()
                or not month[5:].isdigit() or not 1 <= int(month[5:]) <= 12):
            raise ValueError('Month must be YYYY-MM')
        if day is not None and (len(day) != 10 or not day.startswith(month+'-') or not day[8:].isdigit()):
            raise ValueError('Day must belong to selected month')
        with self.lock:
            rows = [dict(day=d, pnl=p, return_pct=pct, entries=entries,
                         max_drawdown=dd, sampled_points=n)
                    for d, p, pct, entries, dd, n in self.db.execute('''SELECT day,pnl,return_pct,entries,
                        max_drawdown,sampled_points FROM strategy_days
                        WHERE strategy_id=? AND day LIKE ? ORDER BY day''', (strategy_id, month+'%'))]
            chosen = day or (rows[-1]['day'] if rows else None)
            if chosen:
                curve = [dict(ts=ts, pnl=pnl, state=state, legs=legs)
                         for ts, pnl, state, legs in self.db.execute('''SELECT ts,net_pnl,state,legs
                             FROM strategy_ticks WHERE strategy_id=? AND day=? ORDER BY ts''',
                             (strategy_id, chosen))]
                trades = [dict(ts=ts, symbol=symbol, side=side, quantity=qty,
                               price=price, cost=cost, reason=reason)
                          for ts, symbol, side, qty, price, cost, reason in self.db.execute('''SELECT ts,symbol,side,
                              quantity,price,cost,reason FROM strategy_trades
                              WHERE strategy_id=? AND day=? ORDER BY ts''', (strategy_id, chosen))]
            else:
                curve, trades = [], []
        if len(curve) > 600:
            step = (len(curve)+598)//599
            curve = curve[::step] + ([curve[-1]] if curve[-1] != curve[::step][-1] else [])
        return {'strategy_id': strategy_id, 'market': SPECS[strategy_id].market,
                'month': month, 'day': chosen, 'daily': rows, 'curve': curve,
                'trades': trades, 'intraday_available': bool(curve)}
