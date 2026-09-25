"""
================================================================================
📅 DAILY PAPER TRADING SESSION SCHEDULER (NIFTY & MCX)
================================================================================
Automatically manages the lifecycle of paper trading engines in IST:
- NIFTY Engine (upv2_paper.py): Mon-Fri 09:15 - 15:35 IST
- MCX Engine (mcx_paper_v5.py): Mon-Fri 15:30 - 23:25 IST
Includes:
- Single-instance process lock (/tmp/trading_daily_scheduler.lock)
- Process-group aware clean termination on SIGTERM & SIGINT
- Orphaned process cleanup before engine launch
================================================================================
"""
from __future__ import annotations
import fcntl
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent

proc_nifty: subprocess.Popen | None = None
proc_mcx: subprocess.Popen | None = None
_scheduler_lock_file = None


def acquire_scheduler_lock(replace_existing: bool = True) -> bool:
    global _scheduler_lock_file
    lock_path = "/tmp/trading_daily_scheduler.lock"

    if replace_existing:
        try:
            # Cleanly terminate any duplicate or older daily_scheduler instances
            out = subprocess.check_output(["pgrep", "-f", "daily_scheduler.py"], text=True).strip()
            pids = [int(p) for p in out.splitlines() if int(p) != os.getpid()]
            if pids:
                now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
                print(f"[{now_str}] ⚠️ Terminating {len(pids)} duplicate/older scheduler instance(s): {pids}", flush=True)
                for p in pids:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except Exception:
                        pass
                time.sleep(1)
                for p in pids:
                    try:
                        os.kill(p, signal.SIGKILL)
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        _scheduler_lock_file = open(lock_path, "a+")
        fcntl.flock(_scheduler_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _scheduler_lock_file.seek(0)
        _scheduler_lock_file.truncate()
        _scheduler_lock_file.write(f"{os.getpid()}\n")
        _scheduler_lock_file.flush()
        return True
    except (IOError, BlockingIOError):
        return False


def get_ist_now() -> datetime:
    return datetime.now(IST)


def is_nifty_session(now: datetime) -> bool:
    hhmm = now.strftime("%H:%M")
    # Monday to Friday: 09:15 to 15:35 IST
    return now.weekday() < 5 and "09:15" <= hhmm < "15:35"


def is_mcx_session(now: datetime) -> bool:
    hhmm = now.strftime("%H:%M")
    # Monday to Friday: 15:30 to 23:25 IST
    return now.weekday() < 5 and "15:30" <= hhmm < "23:25"


def cleanup_orphans(script_name: str) -> None:
    """Terminates any stray or orphaned processes for the given script."""
    try:
        out = subprocess.check_output(["pgrep", "-f", script_name], text=True).strip()
        pids = [int(p) for p in out.splitlines() if int(p) != os.getpid()]
        if pids:
            now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
            print(f"[{now_str}] ⚠️ Terminating {len(pids)} orphaned process(es) of {script_name} (PIDs: {pids})...", flush=True)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(1)
            try:
                out_after = subprocess.check_output(["pgrep", "-f", script_name], text=True).strip()
                rem_pids = [int(p) for p in out_after.splitlines() if int(p) != os.getpid()]
                for pid in rem_pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass


def kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass


def manage_engine(name: str, script_name: str, log_name: str, should_run: bool, current_proc: subprocess.Popen | None) -> subprocess.Popen | None:
    now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
    if should_run:
        if current_proc is None or current_proc.poll() is not None:
            cleanup_orphans(script_name)
            log_path = ROOT / log_name
            log_file = log_path.open("a")
            print(f"[{now_str}] 🚀 Launching {name} ({script_name}) -> {log_name}", flush=True)
            proc = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / script_name)],
                cwd=ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PAPER_MODE": "1", "PYTHONUNBUFFERED": "1"},
                preexec_fn=os.setsid
            )
            return proc
        return current_proc
    else:
        if current_proc is not None and current_proc.poll() is None:
            print(f"[{now_str}] 🛑 Session closed: Stopping {name} ({script_name})", flush=True)
            kill_process_tree(current_proc)
            return None
        return None


def cleanup_and_exit(signum=None, frame=None) -> None:
    global proc_nifty, proc_mcx
    now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"\n[{now_str}] Scheduler signal received ({signum}). Terminating engines...", flush=True)
    if proc_nifty and proc_nifty.poll() is None:
        kill_process_tree(proc_nifty)
    if proc_mcx and proc_mcx.poll() is None:
        kill_process_tree(proc_mcx)
    sys.exit(0)


def main() -> None:
    global proc_nifty, proc_mcx
    now_str = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")

    if not acquire_scheduler_lock():
        print(f"[{now_str}] ⚠️ Another daily_scheduler is already running. Exiting duplicate instance.", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)

    print(f"[{now_str}] Daily Scheduler Active (Single Instance Locked PID: {os.getpid()}).", flush=True)
    try:
        while True:
            now = get_ist_now()
            nifty_active = is_nifty_session(now)
            mcx_active = is_mcx_session(now)

            proc_nifty = manage_engine("NIFTY", "upv2_paper.py", "nifty.log", nifty_active, proc_nifty)
            proc_mcx = manage_engine("MCX Natural Gas", "mcx_paper_v5.py", "mcx.log", mcx_active, proc_mcx)

            time.sleep(15)
    except KeyboardInterrupt:
        cleanup_and_exit(signal.SIGINT)


if __name__ == "__main__":
    main()
