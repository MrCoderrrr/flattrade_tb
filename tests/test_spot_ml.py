"""Causality checks for the read-only spot/VIX research model."""
import unittest
from datetime import datetime, timedelta

from strategy_lab.spot_ml import samples_for_day


class SpotMLTests(unittest.TestCase):
    def test_future_bars_change_label_not_decision_features(self):
        start = datetime(2026, 8, 25, 9, 15)
        rows = []
        for i in range(100):
            stamp = start+timedelta(minutes=i)
            spot = 24000+i*0.1
            rows.append({"Timestamp":stamp.isoformat(sep=" "),
                         "Spot_Open":str(spot),"Spot_High":str(spot+1),
                         "Spot_Low":str(spot-1),"Spot_Close":str(spot),
                         "VIX_Open":"14","VIX_High":"14.1",
                         "VIX_Low":"13.9","VIX_Close":"14"})
        original = {row["timestamp"]:row for row in samples_for_day(rows)}
        target = (start+timedelta(minutes=40)).isoformat(sep=" ")
        for i in range(41,46):
            for field in ("Spot_Open","Spot_High","Spot_Low","Spot_Close"):
                rows[i][field] = str(float(rows[i][field])+50)
        changed = {row["timestamp"]:row for row in samples_for_day(rows)}
        self.assertEqual(original[target]["features"],changed[target]["features"])
        self.assertNotEqual(original[target]["label"],changed[target]["label"])
        self.assertNotEqual(original[target]["future_excursion_bps"],
                            changed[target]["future_excursion_bps"])

    def test_gap_prevents_overlapping_feature_and_label_window(self):
        start = datetime(2026, 8, 25, 9, 15)
        rows = []
        for i in range(100):
            stamp = start+timedelta(minutes=i+(1 if i>=44 else 0))
            spot = 24000+i*0.1
            rows.append({"Timestamp":stamp.isoformat(sep=" "),
                         "Spot_Open":str(spot),"Spot_High":str(spot+1),
                         "Spot_Low":str(spot-1),"Spot_Close":str(spot),
                         "VIX_Open":"14","VIX_High":"14.1",
                         "VIX_Low":"13.9","VIX_Close":"14"})
        stamps = {row["timestamp"] for row in samples_for_day(rows)}
        self.assertNotIn((start+timedelta(minutes=40)).isoformat(sep=" "),stamps)


if __name__ == "__main__":
    unittest.main()
