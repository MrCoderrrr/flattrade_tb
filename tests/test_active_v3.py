"""Synthetic execution checks, not evidence of profitability."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from strategy_lab.models import Bar, IST
from strategy_lab.active_v3 import build_plan, explain_signal
from strategy_lab.catalog import catalog
from strategy_lab.runtime import Controller
from test_runtime import Feed
from test_strategies import quote


def fixture(market='NIFTY', direction=1):
    now = datetime(2026, 9, 22, 9, 20, tzinfo=IST) if market == 'NIFTY' else datetime(2026, 9, 22, 16, 5, tzinfo=IST)
    start = now-timedelta(minutes=5)
    previous = (start-timedelta(days=1)).replace(hour=14, minute=0)
    stamps = [previous+timedelta(minutes=i) for i in range(30)]+[start+timedelta(minutes=i) for i in range(5)]
    base, step = (25000, 1) if market == 'NIFTY' else (250, .1)
    bars = [Bar(t, base+direction*i*step, base+direction*i*step+step, base+direction*i*step-step, base+direction*i*step, 100, 1) for i,t in enumerate(stamps)]
    strike = round(bars[-1].close/50)*50 if market == 'NIFTY' else round(bars[-1].close/5)*5
    lot = 65 if market == 'NIFTY' else 250
    quotes = [quote(market, strike, side, 50, 51, now, lot=lot) for side in ('PE','CE')]
    if market == 'NIFTY':
        quotes += [quote(market, strike-100, 'PE', 19, 20, now), quote(market, strike+100, 'CE', 19, 20, now)]
    return bars, quotes, now


class ActiveTests(unittest.TestCase):
    def test_early_warmup_and_no_lookahead(self):
        bars, quotes, now = fixture()
        expected = explain_signal('NIFTY', bars, now)
        self.assertTrue(expected['eligible'])
        self.assertEqual(expected['direction'], 1)
        self.assertEqual(explain_signal('NIFTY', bars+[Bar(now,1,999999,1,999999,0,1)], now), expected)
        self.assertFalse(explain_signal('NIFTY', bars[-5:], now)['eligible'])
        self.assertFalse(explain_signal('NIFTY', [replace(b,interval_minutes=5) for b in bars], now)['eligible'])

    def test_nifty_hedges_and_mcx_starts_atm_straddle_in_both_directions(self):
        for market in ('NIFTY','MCX'):
            for direction in (1,-1):
                bars,quotes,now=fixture(market,direction)
                plan=build_plan(market,bars,quotes,now,2,400000)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.direction,direction)
                self.assertEqual([l.quote.contract.option_type for l in plan.legs if l.side=='SELL'],
                                 ['PE' if direction==1 else 'CE'] if market=='NIFTY' else ['PE','CE'])
                self.assertEqual(len(plan.legs),2 if market=='NIFTY' else 2)
                self.assertEqual(plan.max_loss is None,market=='MCX')
                if market == 'MCX':
                    self.assertEqual(len({l.quote.contract.strike for l in plan.legs}),1)
                self.assertTrue(all(l.quantity==l.quote.contract.lot_size*2 for l in plan.legs))

    def test_neutral_sells_both_and_missing_hedge_rejected(self):
        bars,quotes,now=fixture(direction=0)
        plan=build_plan('NIFTY',bars,quotes,now,1,200000)
        self.assertEqual(len(plan.legs),4)
        self.assertIsNone(build_plan('NIFTY',bars,quotes[:2],now,1,200000))

    def test_stale_and_missing_history_rejected(self):
        bars,quotes,now=fixture()
        self.assertIsNone(build_plan('NIFTY',bars,[replace(q,timestamp=now-timedelta(seconds=11)) for q in quotes],now,1,200000))
        self.assertFalse(explain_signal('NIFTY',bars[:10]+bars[11:],now)['eligible'])

    def test_runtime_naked_accounting_stop_and_version_lock(self):
        bars,quotes,now=fixture('MCX')
        with tempfile.TemporaryDirectory() as root:
            c=Controller(Path(root),feed=Feed(bars,quotes),clock=lambda:now)
            try:
                c.start('MCX','paper',1,200000,strategy_id='mcxv3'); c.tick()
                s=c.status()['sessions']['MCX']
                self.assertEqual(len(s['positions']),2)
                self.assertIsNone(s['max_loss'])
                self.assertLess(s['net_pnl'],0)
                c.stop('MCX'); c.tick()
                self.assertFalse(c.status()['sessions']['MCX']['positions'])
                with self.assertRaisesRegex(ValueError,'fixed'):
                    c.start('MCX','paper',1,200000,strategy_id='mcxv2')
            finally:c.shutdown()

    def test_mcx_kama_impulse_closes_one_leg_then_reenters_on_reversal(self):
        bars, quotes, now = fixture('MCX', 1)
        clock = [now]
        feed = Feed(bars, quotes)
        with tempfile.TemporaryDirectory() as root:
            c = Controller(Path(root), feed=feed, clock=lambda:clock[0])
            try:
                c.start('MCX', 'paper', 1, 200000, strategy_id='mcxv3')
                c.tick()
                self.assertEqual(len(c.status()['sessions']['MCX']['positions']), 2)
                for minute in range(1, 7):
                    clock[0] = now + timedelta(minutes=minute)
                    old = feed.bars[-1].close
                    delta = .1 if minute < 3 else -1.5
                    close = old + delta
                    feed.bars.append(Bar(clock[0]-timedelta(minutes=1), old,
                                         max(old,close)+.2, min(old,close)-.2, close, 100, 1))
                    feed.quotes = [replace(q,timestamp=clock[0]) for q in feed.quotes]
                    c.tick()
                    session = c.status()['sessions']['MCX']
                    if minute == 2:
                        self.assertEqual([p['contract']['option_type'] for p in session['positions']], ['PE'])
                    if minute == 6:
                        self.assertEqual(len(session['positions']), 2)
                        self.assertEqual(session['leg_reentries'], 1)
                        self.assertTrue(any(t['reason'] == 'KAMA/EMA reversal re-entry' for t in session['trades']))
            finally:
                c.shutdown()

    def test_mcx_profit_trail_closes_leg_without_immediate_reentry(self):
        bars, quotes, now = fixture('MCX', 1)
        clock = [now]
        feed = Feed(bars, quotes)
        with tempfile.TemporaryDirectory() as root:
            c = Controller(Path(root), feed=feed, clock=lambda:clock[0])
            try:
                c.start('MCX','paper',1,200000,strategy_id='mcxv3')
                c.tick()
                clock[0] = now+timedelta(seconds=3)
                feed.quotes = [replace(q, bid=39, ask=40, last=39.5, timestamp=clock[0]) if q.contract.option_type == 'PE'
                               else replace(q,timestamp=clock[0]) for q in feed.quotes]
                c.tick()
                pe = next(p for p in c.status()['sessions']['MCX']['positions'] if p['contract']['option_type'] == 'PE')
                self.assertTrue(pe['trail_armed'])
                self.assertLessEqual(pe['leg_stop'], pe['entry_price']*1.28)
                clock[0] += timedelta(seconds=3)
                feed.quotes = [replace(q, bid=48, ask=49, last=48.5, timestamp=clock[0]) if q.contract.option_type == 'PE'
                               else replace(q,timestamp=clock[0]) for q in feed.quotes]
                c.tick()
                session = c.status()['sessions']['MCX']
                self.assertEqual([p['contract']['option_type'] for p in session['positions']], ['CE'])
                self.assertFalse(session['missing_armed'])
                self.assertEqual(session.get('leg_reentries',0),0)
            finally:
                c.shutdown()

    def test_mcx_session_loss_trigger_locks_after_gap(self):
        bars, quotes, now = fixture('MCX', 0)
        clock = [now]
        feed = Feed(bars, quotes)
        with tempfile.TemporaryDirectory() as root:
            c = Controller(Path(root), feed=feed, clock=lambda:clock[0])
            try:
                c.start('MCX','paper',1,200000,strategy_id='mcxv3')
                c.tick()
                clock[0] += timedelta(seconds=3)
                feed.quotes = [replace(q,bid=79,ask=80,last=79.5,timestamp=clock[0]) if q.contract.option_type == 'CE'
                               else replace(q,timestamp=clock[0]) for q in feed.quotes]
                c.tick()
                session = c.status()['sessions']['MCX']
                self.assertTrue(session['locked'])
                self.assertFalse(session['positions'])
                self.assertLess(session['net_pnl'], -6000)
            finally:
                c.shutdown()

    def test_nifty_flatten_at_1534_and_stop_during_pause(self):
        bars,quotes,now=fixture()
        feed=Feed(bars,quotes)
        with tempfile.TemporaryDirectory() as root:
            c=Controller(Path(root),feed=feed,clock=lambda:now)
            try:
                c.start('NIFTY','paper',1,200000,strategy_id='nfv3'); c.tick()
                self.assertTrue(c.status()['sessions']['NIFTY']['positions'])
                c.pause('NIFTY')
                now=now.replace(hour=15,minute=34)
                feed.quotes=[replace(q,timestamp=now) for q in quotes]
                c.tick()
                self.assertFalse(c.status()['sessions']['NIFTY']['positions'])
                self.assertTrue(c.status()['sessions']['NIFTY']['stop_requested'])
            finally:c.shutdown()

    def test_six_unique_catalog_names(self):
        self.assertEqual({s['id'] for s in catalog()},{'nfv1','nfv2','nfv3','mcxv1','mcxv2','mcxv3'})
