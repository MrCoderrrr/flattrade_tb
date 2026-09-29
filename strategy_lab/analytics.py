"""Persistent, read-only market context and paper-strategy performance audit.

Market regimes describe completed sessions. They are not entry signals. Strategy
comparisons require observed paper sessions in the same regime; historical spot
data alone cannot establish a strategy's return.
"""
from __future__ import annotations

import csv
import copy
import hashlib
import json
import math
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .catalog import SPECS
from .market_data import FlattradeReadOnly, FeedError, exchange_time, number
from .models import IST

MIN_COMPARISON_DAYS = 10
SESSION_WINDOW = {"NIFTY": ("09:15", "15:34", 250),
                  "MCX": ("16:00", "23:24", 300)}


def quantile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    at = (len(ordered)-1)*fraction
    lo = int(at)
    return ordered[lo] + (ordered[min(lo+1, len(ordered)-1)]-ordered[lo])*(at-lo)


def regime_from_bars(rows, baseline, minimum):
    """Return a descriptive label using only this day and earlier market days."""
    if len(rows) < minimum:
        return {"quality": "partial", "regime": "unknown", "bars": len(rows)}
    closes = [float(r[4]) for r in rows]
    first = float(rows[0][1])
    if first <= 0 or any(x <= 0 or not math.isfinite(x) for x in closes):
        return {"quality": "invalid", "regime": "unknown", "bars": len(rows)}
    path = sum(abs(b-a) for a, b in zip(closes, closes[1:]))
    efficiency = abs(closes[-1]-closes[0])/path if path else 0.0
    rv = math.sqrt(sum(math.log(b/a)**2 for a, b in zip(closes, closes[1:]))) * 10000
    high = max(float(r[2]) for r in rows)
    low = min(float(r[3]) for r in rows)
    result = {"quality": "complete", "bars": len(rows),
              "efficiency": round(efficiency, 6),
              "realized_vol_bps": round(rv, 3),
              "range_bps": round((high-low)/first*10000, 3),
              "move_bps": round((closes[-1]-first)/first*10000, 3)}
    if len(baseline) < 20:
        result['regime'] = 'collecting_baseline'
        return result
    efficiencies = [x[0] for x in baseline]
    volatilities = [x[1] for x in baseline]
    if efficiency >= quantile(efficiencies, .72):
        regime = 'trending'
    elif efficiency <= quantile(efficiencies, .35) and rv >= quantile(volatilities, .45):
        regime = 'choppy'
    elif rv <= quantile(volatilities, .35):
        regime = 'steady'
    elif rv >= quantile(volatilities, .75):
        regime = 'volatile_mixed'
    else:
        regime = 'mixed'
    result['regime'] = regime
    return result


class AnalyticsStore:
    def __init__(self, root: Path, controller, clock=None, feed=None):
        self.root = Path(root)
        self.controller = controller
        self.clock = clock or (lambda: datetime.now(IST))
        self.feed = feed or FlattradeReadOnly(self.root, auto_refresh=True)
        directory = self.root / 'data' / 'strategy_lab'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'analytics.sqlite3'
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.execute('''CREATE TABLE IF NOT EXISTS market_minutes(
            market TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL, vix REAL,
            source TEXT NOT NULL, PRIMARY KEY(market,ts))''')
        self.db.execute('CREATE INDEX IF NOT EXISTS market_minutes_day ON market_minutes(market,day)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS market_days(
            market TEXT NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL,
            quality TEXT NOT NULL, regime TEXT NOT NULL, bars INTEGER NOT NULL,
            efficiency REAL, realized_vol_bps REAL, range_bps REAL,
            move_bps REAL, vix_open REAL, vix_close REAL,
            PRIMARY KEY(market,day))''')
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
        self.market_errors = {}
        self._last_market_minute = None
        self._last_summary_minute = None
        self._closed = False
        self._finalized_previous_days = False

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
                    if not self._finalized_previous_days:
                        self.finalize_previous_days(now.date().isoformat())
                        self._finalized_previous_days = True
                    self.capture_strategies(now)
                    minute = now.strftime('%Y-%m-%d %H:%M')
                    if minute != self._last_market_minute:
                        self._last_market_minute = minute
                        self.capture_markets(now)
                    if minute != self._last_summary_minute:
                        self._last_summary_minute = minute
                        self.update_strategy_days()
                    self.last_capture = now.isoformat()
                except Exception as exc:
                    self.market_errors['analytics'] = type(exc).__name__ + ': ' + str(exc)[:120]
                self.stop_event.wait(10)
        self.worker = threading.Thread(target=work, name='paper-analytics', daemon=True)
        self.worker.start()

    def _vix_bars(self, now):
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        response = self.feed._call('TPSeries', exch='NSE', token='26017',
                                   st=str(int(start.timestamp())),
                                   et=str(int(now.timestamp())), intrv='1')
        if not isinstance(response, list):
            raise FeedError('India VIX one-minute bars unavailable')
        result = {}
        for row in response:
            stamp = exchange_time(row['time']).astimezone(IST)
            if stamp + timedelta(minutes=1) <= now:
                result[stamp.isoformat()] = number(row['intc'])
        return result

    def capture_markets(self, now):
        if now.weekday() >= 5:
            return
        minute = now.strftime('%H:%M')
        for market, (start, end, _) in SESSION_WINDOW.items():
            if end < minute <= (datetime.strptime(end,'%H:%M')+timedelta(minutes=5)).strftime('%H:%M'):
                self.update_market_day(market, now.date().isoformat(), finalize=True)
                continue
            if not start <= minute <= end:
                continue
            try:
                bars = self.feed.bars(market, now, 1)
                vix = {}
                if market == 'NIFTY':
                    try:
                        vix = self._vix_bars(now)
                    except (FeedError, ValueError, KeyError) as exc:
                        self.market_errors['VIX'] = str(exc)[:120]
                rows = []
                for bar in bars:
                    stamp = bar.timestamp.astimezone(IST)
                    if stamp.date() != now.date() or not start <= stamp.strftime('%H:%M') <= end:
                        continue
                    rows.append((market, stamp.isoformat(), stamp.date().isoformat(),
                                 bar.open, bar.high, bar.low, bar.close, bar.volume,
                                 vix.get(stamp.isoformat()), 'broker_live'))
                with self.lock, self.db:
                    self.db.executemany('''INSERT INTO market_minutes VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(market,ts) DO UPDATE SET vix=COALESCE(excluded.vix,market_minutes.vix)''', rows)
                self.update_market_day(market, now.date().isoformat(), finalize=minute>=end)
                self.market_errors.pop(market, None)
            except (FeedError, ValueError, KeyError, OSError) as exc:
                self.market_errors[market] = str(exc)[:120]

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
                # The intraday chart needs only P&L and leg count. Repeating
                # every leg's option premiums on each tick wastes disk space.
                leg_count = len(session.get('positions', []))
                with self.lock, self.db:
                    self.db.execute('''INSERT OR REPLACE INTO strategy_ticks VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (strategy_id, now.isoformat(), day, session['state'],
                         float(session['net_pnl'])-float(session.get('pnl_reset_offset') or 0), float(session['realized_pnl']),
                         float(session['unrealized_pnl'])-sum(float(p.get('pnl_reset_unrealized') or 0) for p in session.get('positions', [])), float(session['capital']),
                         int(session['multiplier']), leg_count, None, None,
                         None, None))
                    for trade in session.get('trades', []):
                        ts = str(trade.get('timestamp') or now.isoformat())
                        payload = json.dumps([strategy_id, ts, trade.get('symbol'),
                                              trade.get('side'), trade.get('quantity'),
                                              trade.get('price')], separators=(',',':'))
                        key = hashlib.sha256(payload.encode()).hexdigest()
                        self.db.execute('''INSERT OR IGNORE INTO strategy_trades VALUES
                            (?,?,?,?,?,?,?,?,?,?)''',
                            (key, strategy_id, ts[:10], ts, str(trade.get('symbol') or ''),
                             str(trade.get('side') or ''), int(trade.get('quantity') or 0),
                             float(trade.get('price') or 0), float(trade.get('cost') or 0),
                             str(trade.get('reason') or '')[:240]))

    def update_market_day(self, market, day, finalize=False):
        with self.lock:
            rows = self.db.execute('''SELECT ts,open,high,low,close,vix,source
                FROM market_minutes WHERE market=? AND day=? ORDER BY ts''', (market, day)).fetchall()
            baseline = self.db.execute('''SELECT efficiency,realized_vol_bps FROM market_days
                WHERE market=? AND day<? AND quality='complete' AND efficiency IS NOT NULL
                ORDER BY day DESC LIMIT 60''', (market, day)).fetchall()
        if not rows:
            return
        result = regime_from_bars(rows, baseline, SESSION_WINDOW[market][2])
        if not finalize and result['quality'] == 'complete':
            result['quality'] = 'partial'
        values = [r[5] for r in rows if r[5] is not None]
        source = 'broker_live' if any(r[6] == 'broker_live' for r in rows) else 'flattrade_history'
        with self.lock, self.db:
            self.db.execute('''INSERT OR REPLACE INTO market_days VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (market, day, source, result['quality'], result['regime'], result['bars'],
                 result.get('efficiency'), result.get('realized_vol_bps'),
                 result.get('range_bps'), result.get('move_bps'),
                 values[0] if values else None, values[-1] if values else None))

    def finalize_previous_days(self, today):
        with self.lock:
            pending = self.db.execute('''SELECT market,day FROM market_days
                WHERE day<? AND quality='partial' ORDER BY day''', (today,)).fetchall()
        for market, day in pending:
            self.update_market_day(market, day, finalize=True)

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
                market = SPECS[sid].market
                market_row = self.db.execute('''SELECT regime,quality FROM market_days
                    WHERE market=? AND day=?''', (market, day)).fetchone()
                regime, market_quality = market_row if market_row else ('unknown','missing')
                capital = float(row.get('capital') or 0)
                pnl = float(row.get('net_pnl') or 0)
                self.db.execute('''INSERT OR REPLACE INTO strategy_days VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (sid, day, market, row.get('mode') or 'paper', pnl, capital,
                     round(100*pnl/capital, 5) if capital > 0 else 0.0,
                     int(row.get('entries') or 0), round(drawdown,4) if curve else None,
                     len(curve), regime, market_quality))

    def backfill_nifty_history(self):
        base = self.root / 'data' / 'market_history' / 'nifty_1y'
        spot_dir = base / 'nifty_spot_1m'
        vix_dir = base / 'india_vix_1m'
        imported = 0
        for spot_file in sorted(spot_dir.glob('*.csv')):
            day = spot_file.stem[-10:]
            vix_file = vix_dir / ('india_vix_1m_' + day + '.csv')
            vix = {}
            if vix_file.exists():
                with vix_file.open(newline='') as handle:
                    for row in csv.DictReader(handle):
                        try:
                            vix[row['Timestamp']] = float(row['Close'])
                        except (KeyError, ValueError):
                            pass
            rows = []
            with spot_file.open(newline='') as handle:
                for row in csv.DictReader(handle):
                    try:
                        stamp = datetime.fromisoformat(row['Timestamp']).replace(tzinfo=IST)
                        if stamp.date().isoformat() != day or not '09:15' <= stamp.strftime('%H:%M') <= '15:34':
                            continue
                        values = [float(row[k]) for k in ('Open','High','Low','Close','Volume')]
                        if not all(math.isfinite(x) for x in values) or values[3] <= 0:
                            continue
                        rows.append(('NIFTY', stamp.isoformat(), day, *values,
                                     vix.get(row['Timestamp']), 'flattrade_history'))
                    except (KeyError, ValueError):
                        continue
            with self.lock, self.db:
                self.db.executemany('''INSERT OR IGNORE INTO market_minutes VALUES
                    (?,?,?,?,?,?,?,?,?,?)''', rows)
            if rows:
                self.update_market_day('NIFTY', day, finalize=True)
                imported += 1
        if self.controller is not None:
            self.update_strategy_days()
        return imported

    def _regime_stats(self, strategy_id=None, days=60):
        cutoff = (self.clock().astimezone(IST).date()-timedelta(days=days)).isoformat()
        params = [cutoff]
        where = ''
        if strategy_id:
            where = ' AND strategy_id=?'
            params.append(strategy_id)
        with self.lock:
            rows = self.db.execute('''SELECT strategy_id,market_regime,return_pct,pnl,max_drawdown,day
                FROM strategy_days WHERE day>=? AND market_quality='complete'
                AND market_regime NOT IN ('unknown','collecting_baseline')
                AND sampled_points>=3''' + where, params).fetchall()
        groups = {}
        for sid, regime, pct, pnl, drawdown, day in rows:
            groups.setdefault((sid, regime), []).append((pct,pnl,drawdown,day))
        result = []
        for (sid, regime), samples in sorted(groups.items()):
            returns = [x[0] for x in samples]
            result.append({'strategy_id':sid,'regime':regime,'days':len(samples),
                           'market':SPECS[sid].market,
                           'mean_return_pct':round(sum(returns)/len(returns),3),
                           'median_return_pct':round(quantile(returns,.5),3),
                           'win_rate_pct':round(100*sum(x>0 for x in returns)/len(returns),1),
                           'worst_return_pct':round(min(returns),3),
                           'mean_drawdown':round(sum(x[2] or 0 for x in samples)/len(samples),2),
                           'evidence':'comparable' if len(samples)>=MIN_COMPARISON_DAYS else 'too_few_days'})
        return result

    def summary(self):
        with self.lock:
            coverage = [dict(market=m, days=n, latest=latest)
                        for m,n,latest in self.db.execute('''SELECT market,COUNT(*),MAX(day)
                            FROM market_days WHERE quality='complete' GROUP BY market''')]
            market_days = [dict(market=m,day=d,regime=r,quality=q,bars=b,
                                realized_vol_bps=v,efficiency=e,move_bps=move)
                           for m,d,r,q,b,v,e,move in self.db.execute('''SELECT market,day,regime,quality,bars,
                               realized_vol_bps,efficiency,move_bps FROM market_days
                               ORDER BY day DESC,market LIMIT 14''')]
            daily = [dict(day=d,pnl=round(p,2),allocation=round(c,2),
                          return_pct=round(100*p/c,3) if c else 0.0,
                          strategies=n)
                     for d,p,c,n in self.db.execute('''SELECT day,SUM(pnl),SUM(capital),COUNT(*)
                         FROM strategy_days WHERE mode='paper' GROUP BY day
                         ORDER BY day DESC LIMIT 31''')]
            cutoff = (self.clock().astimezone(IST).date()-timedelta(days=60)).isoformat()
            rollup = [dict(strategy_id=sid,days=n,total_pnl=round(pnl,2),
                           mean_daily_return_pct=round(avg_pct,3),
                           win_days=w,latest_day=latest)
                      for sid,n,pnl,avg_pct,w,latest in self.db.execute('''SELECT strategy_id,
                          COUNT(*),SUM(pnl),AVG(return_pct),SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),MAX(day)
                          FROM strategy_days WHERE day>=? AND mode='paper' GROUP BY strategy_id''', (cutoff,))]
        stats = self._regime_stats()
        leaders = {}
        leader_scores = {}
        comparison_days = {}
        for market, regime in sorted(set((x['market'],x['regime']) for x in stats)):
            eligible_ids = [x['strategy_id'] for x in stats if x['market']==market and
                            x['regime']==regime and x['days']>=MIN_COMPARISON_DAYS]
            if len(eligible_ids)<2:
                continue
            with self.lock:
                matched_rows = self.db.execute('''SELECT strategy_id,day,return_pct FROM strategy_days
                    WHERE market=? AND market_regime=? AND market_quality='complete'
                    AND sampled_points>=3 AND day>=?''', (market,regime,cutoff)).fetchall()
            by_strategy = {sid:{} for sid in eligible_ids}
            for sid,day,pct in matched_rows:
                if sid in by_strategy:
                    by_strategy[sid][day] = pct
            shared = set.intersection(*(set(days) for days in by_strategy.values()))
            comparison_days[market+':'+regime] = len(shared)
            if len(shared)>=MIN_COMPARISON_DAYS:
                scores = {sid:sum(by_strategy[sid][day] for day in shared)/len(shared)
                          for sid in eligible_ids}
                winner = max(eligible_ids,key=lambda sid:scores[sid])
                leaders[market+':'+regime] = winner
                leader_scores[market+':'+regime] = round(scores[winner],3)
        return {'coverage':coverage,'market_days':market_days,'daily':daily,
                'regime_stats':stats,'leaders':leaders,'leader_scores':leader_scores,
                'comparison_days':comparison_days,
                'strategy_rollup':rollup,
                'minimum_comparison_days':MIN_COMPARISON_DAYS,
                'last_capture':self.last_capture,'collector_running':bool(self.worker and self.worker.is_alive()),
                'market_errors':dict(self.market_errors),
                'note':'Regimes are descriptive after the day closes. A leader requires at least ten same-market, same-regime days shared by at least two paper strategies. It is not a forecast.'}

    def detail(self, strategy_id, month=None, day=None):
        if strategy_id not in SPECS:
            raise ValueError('Unknown strategy')
        month = month or self.clock().astimezone(IST).strftime('%Y-%m')
        if len(month)!=7 or month[4]!='-' or not month[:4].isdigit() or not month[5:].isdigit() or not 1<=int(month[5:])<=12:
            raise ValueError('Month must be YYYY-MM')
        if day is not None and (len(day)!=10 or not day.startswith(month+'-') or not day[8:].isdigit()):
            raise ValueError('Day must belong to selected month')
        with self.lock:
            rows = [dict(day=d,pnl=p,return_pct=pct,entries=entries,
                         max_drawdown=dd,sampled_points=n,regime=regime,market_quality=q)
                    for d,p,pct,entries,dd,n,regime,q in self.db.execute('''SELECT day,pnl,return_pct,entries,
                        max_drawdown,sampled_points,market_regime,market_quality
                        FROM strategy_days WHERE strategy_id=? AND day LIKE ? ORDER BY day''',
                        (strategy_id, month+'%'))]
            chosen = day or (rows[-1]['day'] if rows else None)
            if chosen:
                curve = [dict(ts=ts,pnl=pnl,state=state,legs=legs,spot=spot,vix=vix)
                         for ts,pnl,state,legs,spot,vix in self.db.execute('''SELECT ts,net_pnl,state,legs,spot,vix
                             FROM strategy_ticks WHERE strategy_id=? AND day=? ORDER BY ts''',
                             (strategy_id,chosen))]
                trades = [dict(ts=ts,symbol=symbol,side=side,quantity=qty,price=price,cost=cost,reason=reason)
                          for ts,symbol,side,qty,price,cost,reason in self.db.execute('''SELECT ts,symbol,side,
                              quantity,price,cost,reason FROM strategy_trades
                              WHERE strategy_id=? AND day=? ORDER BY ts''', (strategy_id,chosen))]
                market_row = self.db.execute('''SELECT regime,quality,bars,efficiency,realized_vol_bps,
                    range_bps,move_bps,vix_open,vix_close FROM market_days WHERE market=? AND day=?''',
                    (SPECS[strategy_id].market,chosen)).fetchone()
            else:
                curve,trades,market_row = [],[],None
        if len(curve)>600:
            step = math.ceil(len(curve)/599)
            curve = curve[::step] + ([curve[-1]] if curve[-1] != curve[::step][-1] else [])
        market = dict(zip(('regime','quality','bars','efficiency','realized_vol_bps',
                           'range_bps','move_bps','vix_open','vix_close'),market_row)) if market_row else None
        return {'strategy_id':strategy_id,'market':SPECS[strategy_id].market,
                'month':month,'day':chosen,'daily':rows,'curve':curve,'trades':trades,
                'market_day':market,'regime_stats':self._regime_stats(strategy_id),
                'intraday_available':bool(curve)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Import already-downloaded NIFTY spot/VIX minutes for descriptive market regimes')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    store = AnalyticsStore(args.root, None)
    try:
        count = store.backfill_nifty_history()
        print(f'Imported {count} NIFTY market days; no strategy returns were inferred.')
    finally:
        store.close()


if __name__ == '__main__':
    main()
