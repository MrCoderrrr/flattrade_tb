"""Offline ML data-boundary checks; no broker or order methods are used."""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from strategy_lab.ml_research import MLResearch
from strategy_lab.models import IST


class MLResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.research = MLResearch(self.root)

    def tearDown(self):
        self.research.close()
        self.temp.cleanup()

    def test_status_exposes_spot_report_without_enabling_orders(self):
        path = self.research.spot_report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"paper_only": True, "test": {"accuracy": 0.33}}))
        status = self.research.status()
        self.assertTrue(status["spot_model"]["paper_only"])
        self.assertEqual(status["spot_model"]["test"]["accuracy"], 0.33)
        self.assertFalse(status["model_controls_orders"])

    def test_capture_preserves_stale_quote_status_and_second_timestamp(self):
        stamp = "2026-09-28T10:01:02.345678+05:30"
        rows = [{"strike": 24000, "ce": {"bid": 100, "ask": 101, "bid_size": 65,
                                            "ask_size": 130, "age_seconds": 2},
                 "pe": {"bid": 99, "ask": 100, "age_seconds": 2}}]
        self.research.capture({"as_of": stamp, "ready": True, "spot": 24001,
                               "expiry": "2026-09-29", "rows": rows})
        rows[0]["ce"]["age_seconds"] = 15
        self.research.capture({"as_of": stamp.replace("02.345678", "03.345678"),
                               "ready": True, "spot": 24001,
                               "expiry": "2026-09-29", "rows": rows})
        stored = self.research.db.execute(
            "SELECT timestamp,quality FROM snapshots ORDER BY timestamp").fetchall()
        self.assertEqual(stored[0], ("2026-09-28T10:01:02+05:30", "live_observed"))
        self.assertEqual(stored[1][1], "live_stale")
        payload = self.research.db.execute(
            "SELECT payload FROM snapshots ORDER BY timestamp LIMIT 1").fetchone()[0]
        import zlib
        self.assertEqual(json.loads(zlib.decompress(payload))['rows'][0]['ce']['bid_size'],65)
        self.assertEqual(len(self.research._minutes()["2026-09-28"]), 1)

    def test_constant_legacy_fields_are_quarantined(self):
        folder = self.root / "option_chain"
        folder.mkdir()
        path = folder / "nifty_oc_2026-08-19.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "Timestamp", "Spot", "ATM", "Strike", "Expiry", "DTE",
                "CE_IV", "CE_OI", "CE_Bid", "CE_Ask", "PE_IV", "PE_OI",
                "PE_Bid", "PE_Ask"])
            writer.writeheader()
            for minute in range(10):
                writer.writerow({"Timestamp": f"2026-08-19 09:{15+minute:02d}:00",
                                 "Spot": 24000, "ATM": 24000, "Strike": 24000,
                                 "Expiry": "2026-08-25", "DTE": 6,
                                 "CE_IV": 13.5, "CE_OI": 150000,
                                 "CE_Bid": 100, "CE_Ask": 101,
                                 "PE_IV": 13.5, "PE_OI": 150000,
                                 "PE_Bid": 100, "PE_Ask": 101})
        result = self.research.import_directory(folder)
        self.assertEqual(result[path.name]["quality"], "quarantined_static_fields")
        self.assertEqual(self.research._minutes(), {})

    def test_latest_day_is_held_out_and_candidate_refits_after_test(self):
        for day_index, day in enumerate((25, 26, 27)):
            base = datetime(2026, 8, day, 9, 15, tzinfo=IST)
            for minute in range(155):
                stamp = base + timedelta(minutes=minute)
                spot = 24000 + 9 * math.sin(minute / 9 + day_index)
                ce = 100 + (spot - 24000) / 2
                pe = 100 - (spot - 24000) / 2
                rows = [
                    {"strike": 23000, "ce": {"bid": 1001, "ask": 1002},
                     "pe": {"bid": 2, "ask": 2.1}},
                    {"strike": 24000, "ce": {"bid": ce, "ask": ce + 0.5,
                     "iv": 14 + minute / 100, "oi": 100000 + minute},
                     "pe": {"bid": pe, "ask": pe + 0.5,
                     "iv": 15 + minute / 100, "oi": 110000 + minute}},
                    {"strike": 25000, "ce": {"bid": 2, "ask": 2.1},
                     "pe": {"bid": 1001, "ask": 1002}},
                ]
                self.research._put(stamp, "test_fixture", "historical_variable",
                                   spot, 24000, "2026-08-28",
                                   {"rows": rows, "dte": 3})
        report = self.research.train()
        self.assertEqual(report["train_days"], ["2026-08-25", "2026-08-26"])
        self.assertEqual(report["test_day"], "2026-08-27")
        self.assertGreater(report["test"]["samples"], 100)
        self.assertFalse(report["promotion_ready"])
        self.assertEqual(report['action_model']['training_days'],['2026-08-25','2026-08-26'])
        self.assertEqual(report['action_model']['test_day'],'2026-08-27')
        self.assertGreater(report['action_model']['test_samples'],100)
        self.assertTrue(report['action_model']['paper_only'])
        candidate = json.loads(self.research.db.execute(
            "SELECT value FROM artifacts WHERE key='candidate'").fetchone()[0])
        self.assertEqual(candidate["trained_days"][-1], "2026-08-27")


if __name__ == "__main__":
    unittest.main()
