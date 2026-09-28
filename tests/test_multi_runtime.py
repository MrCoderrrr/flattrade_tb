"""Paper strategies may share a market without sharing a lifecycle or risk lock."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from strategy_lab.multi_runtime import MultiController
from test_fixtures import Feed
from test_fixtures import nifty_fixture


class MultiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        bars, quotes, self.now = nifty_fixture()
        self.controller = MultiController(Path(self.temp.name), feed=Feed(bars, quotes),
                                          clock=lambda: self.now)
        self.assertIs(self.controller.feed, self.controller.children['nfv5'].feed)

    def tearDown(self):
        self.controller.shutdown()
        self.temp.cleanup()

    def test_same_market_paper_sessions_stop_independently(self):
        self.controller.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv1')
        self.controller.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv3')
        before = self.controller.status()
        self.assertIn('nfv1', before['sessions'])
        self.assertIn('nfv3', before['sessions'])
        self.assertIsNot(self.controller.children['nfv1'].worker,
                         self.controller.children['nfv3'].worker)
        other_state = before['sessions']['nfv3']['state']
        self.controller.stop('NIFTY', strategy_id='nfv1')
        after = self.controller.status()
        self.assertTrue(after['sessions']['nfv1']['stop_requested'])
        self.assertEqual(after['sessions']['nfv3']['state'], other_state)
        self.assertFalse(after['sessions']['nfv3']['stop_requested'])
        self.assertEqual(after['paper_capital_basis'], 400000)

    def test_chain_tracks_v1_expiry_when_v5_is_flat(self):
        self.controller.children['nfv1'].nifty_position_expiry = lambda: '2026-10-06'
        self.assertEqual(self.controller.nifty_position_expiry(), '2026-10-06')

    def test_scheduled_sessions_survive_restart_and_cancel_separately(self):
        self.now += timedelta(hours=13)
        self.controller.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv1')
        self.controller.start('NIFTY', 'paper', 1, 200000, strategy_id='nfv3')
        self.assertEqual(set(self.controller.status()['schedules']), {'nfv1', 'nfv3'})
        self.controller.shutdown()
        self.controller = MultiController(Path(self.temp.name), feed=Feed(*nifty_fixture()[:2]),
                                          clock=lambda: self.now)
        self.assertEqual(set(self.controller.status()['schedules']), {'nfv1', 'nfv3'})
        self.controller.stop('NIFTY', strategy_id='nfv1')
        self.assertEqual(set(self.controller.status()['schedules']), {'nfv3'})


if __name__ == '__main__':
    unittest.main()
