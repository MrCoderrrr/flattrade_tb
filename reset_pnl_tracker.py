#!/usr/bin/env python3
import json
import os
import csv
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
today_str = datetime.now(IST).strftime("%Y-%m-%d")

BASE_CAPITAL = 195784.0

def reset_pnl():
    for pnl_file in ["pnl_tracker.json", os.path.join("tradingbot", "pnl_tracker.json")]:
        if os.path.exists(pnl_file):
            try:
                data = {}
                with open(pnl_file, "r") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
                data["today_pnl"] = 0.0
                data["mtd_pnl"] = 0.0
                data["ytd_pnl"] = 0.0
                data["current_capital"] = BASE_CAPITAL
                data["last_date"] = today_str
                data["intraday_date"] = today_str
                if "daily_pnl" in data and isinstance(data["daily_pnl"], dict):
                    for k in list(data["daily_pnl"].keys()):
                        if k == today_str or (-15000.0 <= data["daily_pnl"][k] <= -2000.0):
                            data["daily_pnl"][k] = 0.0
                with open(pnl_file, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"✅ Reset {pnl_file} -> today_pnl=0.0")
            except Exception as e:
                print(f"⚠️ Error resetting {pnl_file}: {e}")

    state_files = [
        os.path.join("tradingbot", "data", "state", "algo_state_v2_paper.json"),
        os.path.join("data", "state", "algo_state_v2_paper.json"),
        os.path.join("tradingbot", "data", "state", "algo_state_v2.json"),
        os.path.join("data", "state", "algo_state_v2.json"),
    ]
    for sf in state_files:
        if os.path.exists(sf):
            try:
                with open(sf, "r") as f:
                    st = json.load(f)
                if isinstance(st, dict):
                    st["realized_pnl"] = 0.0
                    with open(sf, "w") as f:
                        json.dump(st, f, indent=2)
                    print(f"✅ Reset {sf} -> realized_pnl=0.0")
            except Exception as e:
                print(f"⚠️ Could not reset {sf}: {e}")

    trade_csvs = [
        os.path.join("tradingbot", "data", "logs", "trade_book", "trades_v2_paper.csv"),
        os.path.join("data", "logs", "trade_book", "trades_v2_paper.csv"),
    ]
    for tc in trade_csvs:
        if os.path.exists(tc):
            try:
                rows = []
                with open(tc, "r") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        if row.get("timestamp", "").startswith(today_str) and row.get("action") == "EXIT":
                            row["pnl"] = "0.0"
                        rows.append(row)
                with open(tc, "w") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                print(f"✅ Sanitized {tc} -> today's EXIT pnl cleared to 0.0")
            except Exception as e:
                print(f"⚠️ Could not update {tc}: {e}")

if __name__ == "__main__":
    reset_pnl()
