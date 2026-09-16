"""Weekday session launcher for the paper-only runtime."""
from __future__ import annotations
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
process_runtime: subprocess.Popen | None = None


def in_session(now: datetime) -> bool:
    hhmm = now.strftime("%H:%M")
    # NSE runs Monday-Friday. MCX's evening session opens Sunday and runs
    # through Friday; Saturday is closed. Boundaries are half-open so the
    # scheduler never starts a process after the session has ended.
    nse_open = now.weekday() < 5 and "09:15" <= hhmm < "15:15"
    mcx_open = now.weekday() != 5 and "18:00" <= hhmm < "23:25"
    return nse_open or mcx_open


def start_runtime() -> None:
    global process_runtime
    if process_runtime is not None and process_runtime.poll() is None:
        return
    log = (ROOT / "paper.log").open("a")
    print(f"[{datetime.now(IST)}] Starting PAPER runtime (no live orders)", flush=True)
    process_runtime = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "runtime.py")],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PAPER_MODE": "1"})


def stop_runtime() -> None:
    global process_runtime
    if process_runtime is not None and process_runtime.poll() is None:
        print(f"[{datetime.now(IST)}] Stopping PAPER runtime", flush=True)
        process_runtime.terminate()
        try:
            process_runtime.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process_runtime.kill()
    process_runtime = None


def main() -> None:
    print(f"[{datetime.now(IST)}] Paper scheduler started", flush=True)
    try:
        while True:
            if in_session(datetime.now(IST)):
                start_runtime()
            else:
                stop_runtime()
            time.sleep(30)
    except KeyboardInterrupt:
        stop_runtime()


if __name__ == "__main__":
    main()
