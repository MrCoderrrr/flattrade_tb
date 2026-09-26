import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from strategy_lab.market_data import FeedError, FlattradeReadOnly, parse_quote
from strategy_lab.runtime import Controller
from test_strategies import nifty_fixture


class Feed:
    def __init__(self, bars, quotes):
        self.bars, self.quotes = bars, quotes
        self.error = False

    def snapshot(self, market, now, held=()):
        if self.error:
            raise FeedError("Test feed unavailable")
        symbols = {c.symbol for c in held}
        return self.bars, [q for q in self.quotes if not held or q.contract.symbol in symbols]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bars, self.quotes, self.now = nifty_fixture()
        self.feed = Feed(self.bars, self.quotes)
        self.c = Controller(Path(self.temp.name), feed=self.feed, clock=lambda: self.now)

    def tearDown(self):
        self.c.shutdown()
        self.temp.cleanup()

    def start(self, multiplier=1, capital=200000):
        if capital != self.c.status()['account']['configured_capital']:
            self.c.configure(capital=capital)
        self.c.start("NIFTY", "paper", multiplier, capital)
        self.c.tick()
        return self.c.status()["sessions"]["NIFTY"]

    def test_paper_never_places_orders_and_requires_explicit_mode(self):
        with self.assertRaises(ValueError):
            self.c.start("NIFTY", None, 1, 200000)
        with self.assertRaisesRegex(ValueError, "live permission"):
            self.c.start("NIFTY", "live", 1, 200000)
        self.c.configure(live_permission=True)
        with self.assertRaisesRegex(ValueError, "no commissioned broker executor"):
            self.c.start("NIFTY", "live", 1, 200000)
        self.assertFalse(self.c.status()["live_enabled"])
        self.assertEqual(len(self.start()["positions"]), 4)

    def test_entry_crosses_book_costs_are_positive_hedges_are_first(self):
        s = self.start()
        self.assertEqual([p["side"] for p in s["positions"]], ["BUY", "BUY", "SELL", "SELL"])
        self.assertEqual(s["positions"][0]["entry_price"], 15.05)
        self.assertGreater(s["costs"], 0)
        self.assertLess(s["net_pnl"], 0)
        self.assertEqual(s["realized_pnl"], 0)
        self.assertLessEqual(s["max_loss"], 2000)

    def test_multiplier_exit_closes_all_units_and_accounts_roundtrip_costs(self):
        s = self.start(2, 400000)
        self.assertEqual({p["quantity"] for p in s["positions"]}, {130})
        entry_cost = s["costs"]
        self.c.stop("NIFTY")
        self.c.tick()
        s = self.c.status()["sessions"]["NIFTY"]
        self.assertFalse(s["positions"])
        self.assertEqual({t["quantity"] for t in s["trades"]}, {130})
        self.assertEqual([t["side"] for t in s["trades"][4:]], ["BUY", "BUY", "SELL", "SELL"])
        self.assertGreater(s["costs"], entry_cost)
        self.assertAlmostEqual(s["net_pnl"], s["realized_pnl"]-s["costs"], places=3)

    def test_stale_stop_keeps_exposure_and_then_flattens_on_fresh_data(self):
        self.start()
        self.now += timedelta(seconds=20)
        self.c.stop("NIFTY")
        self.c.tick()
        s = self.c.status()["sessions"]["NIFTY"]
        self.assertEqual(s["state"], "EXIT_PENDING")
        self.assertEqual(len(s["positions"]), 4)
        self.assertTrue(s["valuation_stale"])
        self.feed.quotes = [replace(q, timestamp=self.now) for q in self.quotes]
        self.c.tick()
        self.assertFalse(self.c.status()["sessions"]["NIFTY"]["positions"])

    def test_insufficient_exit_depth_does_not_fabricate_a_fill(self):
        self.start()
        self.feed.quotes = [replace(q, ask_size=0) for q in self.quotes]
        self.c.stop("NIFTY")
        self.c.tick()
        s = self.c.status()["sessions"]["NIFTY"]
        self.assertEqual(len(s["trades"]), 4)
        self.assertEqual(len(s["positions"]), 4)

    def test_restart_does_not_resume_and_preserves_positions_and_counters(self):
        self.start()
        self.c.shutdown()
        self.c = Controller(Path(self.temp.name), feed=self.feed, clock=lambda: self.now)
        s = self.c.status()["sessions"]["NIFTY"]
        self.assertEqual(s["state"], "RECOVERY_REQUIRED")
        self.assertEqual(s["entries"], 1)
        self.assertEqual(len(s["positions"]), 4)
        self.c.tick()
        self.assertFalse(self.c.status()["sessions"]["NIFTY"]["positions"])

    def test_emergency_stop_lock_survives_restart(self):
        self.start()
        self.c.kill()
        self.c.tick()
        self.c.shutdown()
        self.c = Controller(Path(self.temp.name), feed=self.feed, clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, "Risk limit"):
            self.c.start("NIFTY", "paper", 1, 200000)

    def test_capital_cannot_be_changed_to_reset_limits(self):
        self.start()
        self.c.stop("NIFTY")
        self.c.tick()
        with self.assertRaisesRegex(ValueError, "shared account capital saved"):
            self.c.start("NIFTY", "paper", 2, 400000)

    def test_shared_capital_is_saved_in_settings_and_enforced(self):
        self.c.configure(capital=400000)
        self.assertEqual(self.c.status()['account']['configured_capital'],400000)
        with self.assertRaisesRegex(ValueError,'shared account capital saved'):
            self.c.start('NIFTY','paper',1,200000)
        self.assertEqual(self.start(2,400000)['multiplier'],2)
        with self.assertRaisesRegex(ValueError,'capital is fixed'):
            self.c.configure(capital=600000)

    def test_live_permission_is_user_controlled_and_persistent(self):
        self.c.configure(live_permission=True)
        self.assertTrue(self.c.status()['live_permission'])
        self.assertFalse(self.c.status()['live_enabled'])
        self.c.shutdown()
        self.c=Controller(Path(self.temp.name),feed=self.feed,clock=lambda:self.now)
        self.assertTrue(self.c.status()['live_permission'])
        self.c.configure(live_permission=False)
        self.assertFalse(self.c.status()['live_permission'])

    def test_shared_capital_cannot_back_two_open_sessions(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "Flatten the other"):
            self.c.start("MCX", "paper", 1, 200000)

    def test_daily_cutoff_flattens_and_does_not_reenter(self):
        self.start()
        self.now = self.now.replace(hour=15, minute=30)
        self.feed.quotes = [replace(q, timestamp=self.now) for q in self.quotes]
        self.c.tick()
        self.assertFalse(self.c.status()["sessions"]["NIFTY"]["positions"])
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.c.start("NIFTY", "paper", 1, 200000)

    def test_weekends_and_invalid_allocations_are_rejected(self):
        for multiplier, capital in [(0,200000),(True,200000),(2,200000),(1,float("nan")),(1,float("inf"))]:
            with self.subTest(multiplier=multiplier,capital=capital), self.assertRaises(ValueError):
                self.c.start("NIFTY", "paper", multiplier, capital)
        self.now = self.now.replace(day=26)
        with self.assertRaisesRegex(ValueError, "weekends"):
            self.c.start("NIFTY", "paper", 1, 200000)

    def test_second_controller_cannot_manage_same_ledger(self):
        with self.assertRaisesRegex(RuntimeError, "already running"):
            Controller(Path(self.temp.name), feed=self.feed, clock=lambda: self.now)

    def test_stop_during_slow_feed_is_responsive_and_discards_pending_entry(self):
        entered, release = threading.Event(), threading.Event()
        original = self.feed.snapshot
        def slow(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        self.feed.snapshot = slow
        self.c.start("NIFTY", "paper", 1, 200000)
        worker = threading.Thread(target=self.c.tick)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            self.c.stop("NIFTY")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.c.status()["sessions"]["NIFTY"]["positions"])

    def test_new_day_does_not_erase_unclosed_positions(self):
        self.start()
        self.now += timedelta(days=1)
        self.feed.error = True
        self.c.tick()
        self.assertEqual(len(self.c.status()["sessions"]["NIFTY"]["positions"]), 4)
        self.assertEqual(self.c.status()["account"]["date"], "2026-09-22")

    def test_completed_day_rollup_does_not_double_count_pnl(self):
        self.start()
        self.c.stop("NIFTY")
        self.c.tick()
        expected = self.c.status()["account"]["daily_pnl"]
        self.now += timedelta(days=1)
        self.c.tick()
        a = self.c.status()["account"]
        self.assertEqual(a["daily_pnl"], 0)
        self.assertEqual(a["lifetime_pnl"], expected)
        self.c.tick()
        self.assertEqual(self.c.status()["account"]["lifetime_pnl"], expected)
        rows = self.c.status()['strategy_history']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['strategy_id'],'nfv2')
        self.assertEqual(rows[0]['date'],'2026-09-22')
        self.assertAlmostEqual(rows[0]['net_pnl'],expected)

    def test_current_day_appears_in_strategy_history(self):
        self.start()
        rows=self.c.status()['strategy_history']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['strategy_id'],'nfv2')
        self.assertEqual(rows[0]['entries'],1)


class DataTests(unittest.TestCase):
    def test_only_read_endpoints_exist(self):
        feed = FlattradeReadOnly(Path("."))
        with self.assertRaisesRegex(FeedError,"read-only"):
            feed._call("PlaceOrder")

    def test_missing_exchange_timestamp_is_not_replaced_by_receive_time(self):
        _, quotes, now = nifty_fixture()
        row = {"bp1":30,"sp1":31,"lp":30.5,"bq1":100,"sq1":100,"request_time":str(now)}
        with self.assertRaises(FeedError):
            parse_quote(quotes[0].contract,row)
        row["ft"] = str(int(now.timestamp()))
        self.assertEqual(parse_quote(quotes[0].contract,row).timestamp,now)

    def test_stale_symbol_master_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root,"NFO_symbols_2020-01-01.csv").write_text("Exchange,Token\n")
            _,_,now=nifty_fixture()
            with self.assertRaisesRegex(FeedError,"stale tokens"):
                FlattradeReadOnly(Path(root)).contracts("NIFTY",now)


if __name__ == "__main__":
    unittest.main()

class PauseTests(unittest.TestCase):
    setUp = RuntimeTests.setUp
    tearDown = RuntimeTests.tearDown
    start = RuntimeTests.start
    def test_pause_prevents_new_entry_and_resume_uses_current_authorization(self):
        self.c.start('NIFTY','paper',1,200000)
        self.c.pause('NIFTY',True)
        self.c.tick()
        self.assertFalse(self.c.status()['sessions']['NIFTY']['positions'])
        self.c.pause('NIFTY',False)
        self.c.tick()
        self.assertEqual(len(self.c.status()['sessions']['NIFTY']['positions']),4)

    def test_paused_positions_still_flatten_at_deadline(self):
        self.start()
        self.c.pause('NIFTY',True)
        self.now=self.now.replace(hour=15,minute=30)
        self.feed.quotes=[replace(q,timestamp=self.now) for q in self.quotes]
        self.c.tick()
        self.assertFalse(self.c.status()['sessions']['NIFTY']['positions'])
