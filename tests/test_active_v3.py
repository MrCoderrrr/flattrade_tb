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

    def test_nifty_hedges_and_mcx_naked_in_both_directions(self):
        for market in ('NIFTY','MCX'):
            for direction in (1,-1):
                bars,quotes,now=fixture(market,direction)
                plan=build_plan(market,bars,quotes,now,2,400000)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.direction,direction)
                self.assertEqual([l.quote.contract.option_type for l in plan.legs if l.side=='SELL'], ['PE' if direction==1 else 'CE'])
                self.assertEqual(len(plan.legs),2 if market=='NIFTY' else 1)
                self.assertEqual(plan.max_loss is None,market=='MCX')
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
                self.assertEqual(len(s['positions']),1)
                self.assertIsNone(s['max_loss'])
                self.assertLess(s['net_pnl'],0)
                c.stop('MCX'); c.tick()
                self.assertFalse(c.status()['sessions']['MCX']['positions'])
                with self.assertRaisesRegex(ValueError,'fixed'):
                    c.start('MCX','paper',1,200000,strategy_id='mcxv2')
            finally:c.shutdown()

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
