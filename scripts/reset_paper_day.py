"""Archive and remove one day's *paper strategy* results while service is stopped.

Market minutes, market snapshots, option-chain archives, and broker credentials
are intentionally untouched. Run only with an explicit day and backup path.
"""

import argparse
import fcntl
import json
import sqlite3
from datetime import date
from pathlib import Path

from strategy_lab.runtime import MARKETS, blank_session


def backup(db, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as archived:
        db.backup(archived)


def reset_ledger(path, day, backup_path):
    lock_path = path.parent / "controller.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Controller still owns {path}; stop the service first") from None
        db = sqlite3.connect(path)
        try:
            backup(db, backup_path)
            row = db.execute("SELECT payload FROM state WHERE id=1").fetchone()
            if row:
                state = json.loads(row[0])
                for session in state.get("sessions", {}).values():
                    if session.get("date") == day and session.get("mode") != "paper":
                        raise RuntimeError(f"Non-paper session found in {path}; refusing reset")
                for market in MARKETS:
                    if state["sessions"][market].get("date") == day:
                        state["sessions"][market] = blank_session()
                state["schedules"] = {market: item for market, item in
                                      state.get("schedules", {}).items()
                                      if item.get("scheduled_for") != day}
                state["events"] = [item for item in state.get("events", [])
                                   if not str(item.get("timestamp", "")).startswith(day)]
                state["history"] = [item for item in state.get("history", [])
                                    if item.get("date") != day]
                account = state["account"]
                if account.get("date") == day:
                    account.update(daily_pnl=0.0, drawdown=0.0, halted=False,
                                   peak_pnl=account.get("lifetime_pnl", 0.0))
                with db:
                    db.execute("UPDATE state SET payload=? WHERE id=1", (json.dumps(state),))
            with db:
                db.execute("DELETE FROM journal WHERE substr(timestamp,1,10)=?", (day,))
                db.execute("DELETE FROM strategy_daily WHERE day=?", (day,))
        finally:
            db.close()


def reset_analytics(path, day, backup_path):
    with sqlite3.connect(path) as db:
        backup(db, backup_path)
        with db:
            for table in ("strategy_ticks", "strategy_trades", "strategy_days"):
                db.execute(f"DELETE FROM {table} WHERE day=?", (day,))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--day", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--confirm-paper-only", action="store_true", required=True)
    args = parser.parse_args()
    date.fromisoformat(args.day)
    data = args.root / "data" / "strategy_lab"
    ledgers = [data / "ledger.sqlite3", *sorted((data / "instances").glob("*/ledger.sqlite3"))]
    for path in ledgers:
        if path.is_file():
            name = "legacy" if path == ledgers[0] else path.parent.name
            reset_ledger(path, args.day, args.backup_dir / f"{name}-ledger.sqlite3")
            print(f"cleared paper strategy day in {name}")
    analytics = data / "analytics.sqlite3"
    if analytics.is_file():
        reset_analytics(analytics, args.day, args.backup_dir / "analytics.sqlite3")
        print("cleared today's strategy analytics; market data retained")


if __name__ == "__main__":
    main()
