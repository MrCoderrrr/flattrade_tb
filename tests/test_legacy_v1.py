"""V1 paper cards must use the simulated controller, not legacy launchers."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from strategy_lab.catalog import catalog
from strategy_lab.legacy_v1 import build_plan, explain_signal
from strategy_lab.runtime import Controller
from test_active_v3 import fixture
from test_runtime import Feed
from test_strategies import quote


class LegacyV1PaperTests(unittest.TestCase):
    def test_nifty_has_far_wings_and_mcx_has_no_hedges(self):
        bars, quotes, now = fixture('NIFTY')
        quotes += [quote('NIFTY', 24050, 'PE', 5.5, 6, now),
                   quote('NIFTY', 26050, 'CE', 5.5, 6, now)]
        signal = explain_signal('NIFTY', bars, now)
        self.assertTrue(signal['eligible'])
        plan = build_plan('NIFTY', bars, quotes, now, 1, 200000)
        self.assertEqual(plan.strategy, 'nfv1')
        self.assertEqual([leg.side for leg in plan.legs], ['BUY', 'BUY', 'SELL', 'SELL'])
        self.assertGreaterEqual(plan.max_loss, 0)
        self.assertTrue(all(abs(leg.quote.contract.strike-25050) >= 1000
                            for leg in plan.legs[:2]))
        bars, quotes, now = fixture('MCX')
        plan = build_plan('MCX', bars, quotes, now, 1, 200000)
        self.assertEqual(plan.strategy, 'mcxv1')
        self.assertEqual([leg.side for leg in plan.legs], ['SELL', 'SELL'])
        self.assertIsNone(plan.max_loss)

    def test_v1_can_schedule_and_run_only_paper(self):
        self.assertTrue(all(spec['enabled'] for spec in catalog() if spec['version'] == 1))
        bars, quotes, now = fixture('MCX')
        with tempfile.TemporaryDirectory() as directory:
            clock = [(now-timedelta(days=1)).replace(hour=23, minute=30)]
            c = Controller(Path(directory), feed=Feed(bars, quotes), clock=lambda: clock[0])
            try:
                result = c.start('MCX', 'paper', 1, 200000, strategy_id='mcxv1')
                self.assertEqual(result['schedules']['MCX']['strategy_id'], 'mcxv1')
                clock[0] = now
                c.tick()
                session = c.status()['sessions']['MCX']
                self.assertEqual(session['strategy_id'], 'mcxv1')
                self.assertEqual(session['mode'], 'paper')
                self.assertEqual(len(session['positions']), 2)
                self.assertTrue(all(row['simulated'] for row in session['trades']))
                c.stop('MCX')
                c.tick()
                self.assertFalse(c.status()['sessions']['MCX']['positions'])
                with self.assertRaisesRegex(ValueError, 'live permission'):
                    c.start('MCX', 'live', 1, 200000, strategy_id='mcxv1')
            finally:
                c.shutdown()

    def test_nifty_runtime_has_wings_and_stop_flattens_paper_positions(self):
        bars, quotes, now = fixture('NIFTY')
        quotes += [quote('NIFTY', 24050, 'PE', 5.5, 6, now),
                   quote('NIFTY', 26050, 'CE', 5.5, 6, now)]
        with tempfile.TemporaryDirectory() as directory:
            c = Controller(Path(directory), feed=Feed(bars, quotes), clock=lambda: now)
            try:
                c.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv1')
                c.tick()
                session = c.status()['sessions']['NIFTY']
                self.assertEqual(session['strategy_id'], 'nfv1')
                self.assertEqual([p['side'] for p in session['positions']],
                                 ['BUY', 'BUY', 'SELL', 'SELL'])
                self.assertTrue(all(row['simulated'] for row in session['trades']))
                c.stop('NIFTY')
                c.tick()
                self.assertFalse(c.status()['sessions']['NIFTY']['positions'])
            finally:
                c.shutdown()
