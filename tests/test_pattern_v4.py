"""NIFTY v4 closed-candle selection and paper execution checks."""
import tempfile
import unittest
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from strategy_lab.models import Bar, IST
from strategy_lab.market_data import FlattradeReadOnly
from strategy_lab.pattern_v4 import build_plan, explain_signal
from strategy_lab.replay import Snapshot, load_snapshots, run_replay
from strategy_lab.runtime import Controller
from test_runtime import Feed
from test_strategies import quote


def breakout(direction=1):
    start = datetime(2026, 9, 22, 9, 15, tzinfo=IST)
    bars = [Bar(start+timedelta(minutes=i), 25000, 25005, 24995, 25000, 0, 1)
            for i in range(15)]
    close = 25009 if direction > 0 else 24991
    bars.append(Bar(start+timedelta(minutes=15), 25000,
                    max(25001, close), min(24999, close), close, 0, 1))
    now = start+timedelta(minutes=16)
    option = 'PE' if direction > 0 else 'CE'
    short, wing = (25000, 24900) if direction > 0 else (25000, 25100)
    quotes = [quote('NIFTY', short, option, 50, 51, now),
              quote('NIFTY', wing, option, 19, 20, now)]
    return bars, quotes, now


class PatternV4Tests(unittest.TestCase):
    def test_both_opening_break_directions_have_structural_stops(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                bars, quotes, now = breakout(direction)
                signal = explain_signal('NIFTY', bars, now)
                self.assertTrue(signal['eligible'], signal)
                self.assertEqual(signal['direction'], direction)
                self.assertEqual(signal['pattern'], 'opening_break')
                self.assertGreaterEqual(signal['score'], .70)
                self.assertGreater((signal['target_level']-signal['indicators']['close'])*direction, 0)
                self.assertGreater((signal['indicators']['close']-signal['stop_level'])*direction, 0)
                plan = build_plan('NIFTY', bars, quotes, now, 1, 200000)
                self.assertEqual(plan.strategy, 'nfv4')
                self.assertEqual([leg.side for leg in plan.legs], ['BUY', 'SELL'])
                self.assertLessEqual(plan.max_loss, 8000)
                self.assertEqual(plan.pattern, 'opening_break')

    def test_incomplete_bar_missing_history_and_bad_books_cannot_trigger(self):
        bars, quotes, now = breakout()
        expected = explain_signal('NIFTY', bars, now)
        incomplete = Bar(now, 25009, 30000, 1, 30000, 0, 1)
        self.assertEqual(explain_signal('NIFTY', bars+[incomplete], now), expected)
        self.assertFalse(explain_signal('NIFTY', bars[:7]+bars[8:], now)['eligible'])
        self.assertIsNone(build_plan('NIFTY', bars, quotes[:1], now, 1, 200000))
        self.assertIsNone(build_plan('NIFTY', bars,
                                      [replace(q, timestamp=now-timedelta(seconds=11)) for q in quotes],
                                      now, 1, 200000))

    def test_broker_adapter_requests_only_signaled_option_side(self):
        bars, quotes, now = breakout()
        quotes.append(quote('NIFTY', 25000, 'CE', 50, 51, now))
        feed = FlattradeReadOnly(Path('.'))
        feed.bars = lambda market, stamp, interval: bars
        feed.contracts = lambda market, stamp: [q.contract for q in quotes]
        requested = []
        def read_quotes(contracts, stamp, *, strict=True):
            requested.extend(contracts)
            return [q for q in quotes if q.contract in contracts]
        feed.quotes = read_quotes
        _, selected = feed.snapshot('NIFTY', now, strategy_id='nfv4')
        self.assertEqual({q.contract.option_type for q in selected}, {'PE'})
        self.assertEqual(len(selected), 2)
        requested.clear()
        flat = bars[:-1]+[Bar(bars[-1].timestamp, 25000, 25005, 24995, 25000, 0, 1)]
        feed.bars = lambda market, stamp, interval: flat
        self.assertEqual(feed.snapshot('NIFTY', now, strategy_id='nfv4')[1], [])
        self.assertFalse(requested)

    def test_failed_break_beats_continuation_and_rejection_uses_support(self):
        bars, _, now = breakout()
        bars[:15] = [replace(bar, low=24980) for bar in bars[:15]]
        bars.append(Bar(now, 25009, 25010, 25002, 25003, 0, 1))
        failed = explain_signal('NIFTY', bars, now+timedelta(minutes=1))
        self.assertEqual((failed['pattern'], failed['direction']), ('failed_break', -1))
        self.assertEqual(failed['matches'][0]['pattern'], 'failed_break')

        start = datetime(2026, 9, 22, 9, 15, tzinfo=IST)
        bars = [Bar(start+timedelta(minutes=i), 25000, 25025, 24995, 25000, 0, 1)
                for i in range(15)]
        bars.append(Bar(start+timedelta(minutes=15), 25000, 25006, 24993, 25003, 0, 1))
        rejected = explain_signal('NIFTY', bars, start+timedelta(minutes=16))
        self.assertEqual(rejected['pattern'], 'level_rejection')
        self.assertEqual(rejected['direction'], 1)
        self.assertEqual(rejected['matches'][0]['level'], 24995)

    def test_runtime_records_paper_and_underlying_target_exit(self):
        bars, quotes, now = breakout()
        clock = [now]
        feed = Feed(bars, quotes)
        with tempfile.TemporaryDirectory() as root:
            c = Controller(Path(root), feed=feed, clock=lambda: clock[0])
            try:
                c.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv4')
                c.tick()
                session = c.status()['sessions']['NIFTY']
                self.assertEqual(session['pattern'], 'opening_break')
                self.assertEqual(len(session['positions']), 2)
                self.assertTrue(all(fill['simulated'] for fill in session['trades']))
                target = session['underlying_target']
                clock[0] = now+timedelta(minutes=1)
                feed.bars.append(Bar(now, 25009, target+1, 25008, target, 0, 1))
                feed.quotes = [replace(q, timestamp=clock[0]) for q in quotes]
                c.tick()
                session = c.status()['sessions']['NIFTY']
                self.assertEqual(session['trades'][-1]['reason'], 'V4 underlying target')
                self.assertEqual([p['side'] for p in session['positions']], ['BUY'])
                self.assertEqual(len(session['trades']), 3)
            finally:
                c.shutdown()

    def test_same_candle_stop_and_target_takes_stop_first(self):
        bars, quotes, now = breakout()
        clock = [now]
        feed = Feed(bars, quotes)
        with tempfile.TemporaryDirectory() as root:
            c = Controller(Path(root), feed=feed, clock=lambda: clock[0])
            try:
                c.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv4')
                c.tick()
                session = c.status()['sessions']['NIFTY']
                clock[0] = now+timedelta(minutes=1)
                feed.bars.append(Bar(now, 25009, session['underlying_target']+1,
                                     session['underlying_stop']-1, 25010, 0, 1))
                feed.quotes = [replace(q, timestamp=clock[0]) for q in quotes]
                c.tick()
                self.assertEqual(c.status()['sessions']['NIFTY']['trades'][-1]['reason'],
                                 'V4 underlying invalidation')
            finally:
                c.shutdown()

    def test_same_breakout_bar_cannot_reopen_after_option_stop(self):
        bars, quotes, now = breakout()
        clock = [now]
        feed = Feed(bars, quotes)
        with tempfile.TemporaryDirectory() as root:
            c = Controller(Path(root), feed=feed, clock=lambda: clock[0])
            try:
                c.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv4')
                c.tick()
                clock[0] = now+timedelta(seconds=3)
                feed.quotes = [replace(quotes[0], bid=92, ask=93, timestamp=clock[0]),
                               replace(quotes[1], timestamp=clock[0])]
                c.tick()
                self.assertEqual([p['side'] for p in c.status()['sessions']['NIFTY']['positions']], ['BUY'])
                clock[0] = now+timedelta(seconds=64)
                feed.quotes = [replace(q, timestamp=clock[0]) for q in quotes]
                c.tick()
                session = c.status()['sessions']['NIFTY']
                self.assertEqual(session['entries'], 1)
                self.assertIn('new completed candle', session['reason'])
            finally:
                c.shutdown()

    def test_named_replay_runs_only_nifty_v4(self):
        bars, quotes, now = breakout()
        target = explain_signal('NIFTY', bars, now)['target_level']
        next_time = now+timedelta(minutes=1)
        next_bar = Bar(now, 25009, target+1, 25008, target, 0, 1)
        recorded = [Snapshot(now, 'NIFTY', bars, quotes),
                    Snapshot(next_time, 'NIFTY', bars+[next_bar],
                             [replace(q, timestamp=next_time) for q in quotes])]
        report = run_replay(recorded, strategy_id='nfv4')
        self.assertEqual(report['strategy_id'], 'nfv4')
        self.assertEqual(report['entries'], 1)
        self.assertEqual({row['market'] for row in report['sessions']}, {'NIFTY'})
        self.assertEqual(report['performance_status'], 'UNVALIDATED')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'ledger.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE market_snapshots (id INTEGER PRIMARY KEY, timestamp TEXT, market TEXT, payload TEXT)')
                for row in recorded:
                    db.execute('INSERT INTO market_snapshots(timestamp,market,payload) VALUES (?,?,?)',
                               (row.timestamp.isoformat(), row.market,
                                json.dumps(asdict(row), default=str)))
                db.commit()
            self.assertEqual(load_snapshots(path), recorded)
