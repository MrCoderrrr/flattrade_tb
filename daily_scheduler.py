"""
================================================================================
📅 DAILY PAPER TRADING SESSION SCHEDULER (NIFTY & MCX)
================================================================================
Automatically manages the lifecycle of paper trading engines in IST:
- NIFTY Engine (nifty_paper_v3.py): Mon-Fri 09:15 - 15:35 IST
- MCX Engine (mcx_paper_v4.py): Sun-Fri 17:00 - 23:25 IST
================================================================================
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent

proc_nifty: subprocess.Popen | None = None
proc_mcx: subprocess.Popen | None = None


def get_ist_now() -> datetime:
    return datetime.now(IST)


def is_nifty_session(now: datetime) -> bool:
    hhmm = now.strftime("%H:%M")
    # Monday to Friday: 09:15 to 15:35 IST
    return now.weekday() < 5 and "09:15" <= hhmm < "15:35"


def is_mcx_session(now: datetime) -> bool:
    hhmm = now.strftime("%H:%M")
    # Sunday to Friday (Saturday closed): 17:00 to 23:25 IST
    return now.weekday() != 5 and "17:00" <= hhmm < "23:25"


def manage_engine(name: str, script_name: str, log_name: str, should_run: bool, current_proc: subprocess.Popen | None) -> subprocess.Popen | None:
    now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
    if should_run:
        if current_proc is None or current_proc.poll() is not None:
            log_path = ROOT / log_name
            log_file = log_path.open("a")
            print(f"[{now_str}] 🚀 Launching {name} ({script_name}) -> {log_name}", flush=True)
            proc = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / script_name)],
                cwd=ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PAPER_MODE": "1", "PYTHONUNBUFFERED": "1"}
            )
            return proc
        return current_proc
    else:
        if current_proc is not None and current_proc.poll() is None:
            print(f"[{now_str}] 🛑 Session closed: Stopping {name} ({script_name})", flush=True)
            current_proc.terminate()
            try:
                current_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                current_proc.kill()
            return None
        return None


def main() -> None:
    global proc_nifty, proc_mcx
    print(f"[{get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')}] Daily Scheduler Active.", flush=True)
    try:
        while True:
            now = get_ist_now()
            nifty_active = is_nifty_session(now)
            mcx_active = is_mcx_session(now)

            proc_nifty = manage_engine("NIFTY", "nifty_paper_v3.py", "nifty.log", nifty_active, proc_nifty)
            proc_mcx = manage_engine("MCX Natural Gas", "mcx_paper_v5.py", "mcx.log", mcx_active, proc_mcx)

            time.sleep(15)
    except KeyboardInterrupt:
        print("\nScheduler interrupted by user. Terminating engines...", flush=True)
        if proc_nifty and proc_nifty.poll() is None:
            proc_nifty.terminate()
        if proc_mcx and proc_mcx.poll() is None:
            proc_mcx.terminate()


if __name__ == "__main__":
    main()
