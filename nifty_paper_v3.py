"""
================================================================================
🚀 NIFTY 50 PAPER TRADING ENGINE (VERSION 3.0)
================================================================================
Production-Ready Algorithmic Paper Trading Engine for NIFTY 50 Options.
Core Architecture: Adaptive KAMA-ADX Hedged Strangle (upv2_paper engine).

Key Rules:
1. Strict 1-Minute Execution Cadence with 1-second continuous tick evaluation.
2. 15% Initial SL and Dynamic Trailing SL on short legs.
3. Solo short leg anchor when one side exits.
4. Protective Hedges bought OTM and sold ONLY when session closes at 15:34 IST.
5. Live Option Chain and per-leg PnL calculated every second.
6. Time strictly in Indian Standard Time (IST = UTC+5:30).
7. Session Auto Square-off strictly at 15:34 IST.
================================================================================
"""
import fcntl
import os
import sys
from upv2_paper import ExecutionEngine, prompt_user_variables

_lock_file = None


def acquire_engine_lock() -> bool:
    global _lock_file
    try:
        _lock_file = open("/tmp/nifty_paper_engine.lock", "a+")
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.seek(0)
        _lock_file.truncate()
        _lock_file.write(f"{os.getpid()}\n")
        _lock_file.flush()
        return True
    except (IOError, BlockingIOError):
        return False


def main():
    if not acquire_engine_lock():
        print("\n❌ [FATAL] Another instance of NIFTY Paper Trading Engine is already running!", flush=True)
        print("   Aborting this instance immediately to prevent duplicate orders and dual Telegram messages.\n", flush=True)
        sys.exit(0)

    prompt_user_variables()
    engine = ExecutionEngine()
    engine.run()


if __name__ == "__main__":
    main()
