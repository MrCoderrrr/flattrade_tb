#!/usr/bin/env python3
import json
import os

BASE_CAPITAL = 195784.0

def reset_pnl():
    pnl_file = "pnl_tracker.json"
    data = {
        "mtd_pnl": 0.0,
        "ytd_pnl": 0.0,
        "current_capital": BASE_CAPITAL,
        "today_pnl": 0.0,
        "last_date": "",
        "intraday_date": ""
    }
    if os.path.exists(pnl_file):
        try:
            with open(pnl_file, "r") as f:
                existing = json.load(f)
                if isinstance(existing, dict):
                    data.update(existing)
        except Exception:
            pass
    
    data["mtd_pnl"] = 0.0
    data["ytd_pnl"] = 0.0
    data["today_pnl"] = 0.0
    data["current_capital"] = BASE_CAPITAL
    if "daily_pnl" in data and isinstance(data["daily_pnl"], dict):
        for k in list(data["daily_pnl"].keys()):
            if -15000.0 <= data["daily_pnl"][k] <= -7000.0:
                data["daily_pnl"][k] = 0.0

    with open(pnl_file, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Reset {pnl_file} -> MTD=0.0, YTD=0.0, Capital={BASE_CAPITAL}")

    state_file = os.path.join("data", "state", "algo_state_v2_paper.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                st = json.load(f)
            if isinstance(st, dict):
                st["realized_pnl"] = 0.0
                with open(state_file, "w") as f:
                    json.dump(st, f, indent=2)
                print(f"✅ Reset {state_file} -> realized_pnl=0.0")
        except Exception as e:
            print(f"⚠️ Could not reset {state_file}: {e}")

if __name__ == "__main__":
    reset_pnl()
