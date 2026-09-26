import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from strategy_lab.replay import DataError, Snapshot, load_snapshots, run_replay, evaluate
from test_strategies import nifty_fixture


def encode(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError()


class ReplayTests(unittest.TestCase):
    def test_real_controller_entries_and_unresolved_exposure_are_reported(self):
        bars, quotes, now = nifty_fixture()
        result = run_replay([Snapshot(now,"NIFTY",bars,quotes)])
        self.assertEqual(result["entries"],1)
        self.assertEqual(result["fill_count"],4)
        self.assertEqual(len(result["unresolved_positions"]),1)
        self.assertIsNone(result["monthly"][0]["calendar_month_return_pct"])
        self.assertEqual(result["complete_observed_days"],0)

    def test_deadline_exits_and_missing_data_are_not_hidden(self):
        bars,quotes,now=nifty_fixture()
        end=now.replace(hour=15,minute=30)
        result=run_replay([Snapshot(now,"NIFTY",bars,quotes),
                           Snapshot(end,"NIFTY",bars,[replace(q,timestamp=end) for q in quotes])])
        self.assertEqual(result["closed_trades"],1)
        self.assertFalse(result["unresolved_positions"])
        self.assertTrue(result["drawdown_is_lower_bound_with_missing_ticks"])
        self.assertLess(result["booked_net_pnl"],0)

    def test_stale_quote_cannot_close_position_at_end(self):
        bars,quotes,now=nifty_fixture()
        end=now.replace(hour=15,minute=30)
        result=run_replay([Snapshot(now,"NIFTY",bars,quotes),Snapshot(end,"NIFTY",bars,quotes)])
        self.assertEqual(result["fill_count"],4)
        self.assertTrue(result["unresolved_positions"])

    def test_schema_chronology_and_duplicates_are_rejected(self):
        bars,quotes,now=nifty_fixture()
        row={"timestamp":now.isoformat(),"market":"NIFTY","bars":[asdict(b) for b in bars],"quotes":[asdict(q) for q in quotes]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sample.jsonl'
            text=json.dumps(row,default=encode)
            path.write_text(text+'\n'+text+'\n')
            with self.assertRaises(DataError): load_snapshots(path)
            row["timestamp"]=now.replace(tzinfo=None).isoformat()
            path.write_text(json.dumps(row,default=encode))
            with self.assertRaises(DataError): load_snapshots(path)

    def test_single_day_does_not_claim_holdout_validation(self):
        bars,quotes,now=nifty_fixture()
        row={"timestamp":now.isoformat(),"market":"NIFTY","bars":[asdict(b) for b in bars],"quotes":[asdict(q) for q in quotes]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sample.jsonl'
            path.write_text(json.dumps(row,default=encode)+'\n')
            result=evaluate(path)
            self.assertFalse(result["split"]["available"])
            self.assertEqual(result["all_data"]["entries"],1)


if __name__ == '__main__': unittest.main()
