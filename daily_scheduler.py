import subprocess
import time
from datetime import datetime, timezone, timedelta
import os
import signal

# Define IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# Define process variables
process_upv2 = None
process_mcx = None

def get_python_exec():
    # Detect virtual environment python
    candidates = [
        os.path.abspath("venv/bin/python3"),
        os.path.abspath("../venv/bin/python3"),
        "/home/ubuntu/flattrade_tb/venv/bin/python3",
        "/home/ubuntu/flattrade_tb/flattrade_tb/venv/bin/python3",
        "/home/ubuntu/venv/bin/python3",
        sys.executable
    ]
    for c in candidates:
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return "python3"

def start_upv2():
    global process_upv2
    # Start if not running
    if process_upv2 is None or process_upv2.poll() is not None:
        py_bin = get_python_exec()
        print(f"[{datetime.now(IST)}] Starting upv2_paper.py using {py_bin}")
        with open("paper.log", "a") as out:
            process_upv2 = subprocess.Popen(
                [py_bin, "-u", "upv2_paper.py"],
                stdout=out,
                stderr=subprocess.STDOUT
            )

def stop_upv2():
    global process_upv2
    # Stop if running
    if process_upv2 is not None and process_upv2.poll() is None:
        print(f"[{datetime.now(IST)}] Stopping upv2_paper.py")
        process_upv2.terminate()
        try:
            process_upv2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process_upv2.kill()
        process_upv2 = None

def start_mcx():
    global process_mcx
    # Start if not running
    if process_mcx is None or process_mcx.poll() is not None:
        py_bin = get_python_exec()
        print(f"[{datetime.now(IST)}] Starting mcx_naturalgas_paper.py using {py_bin}")
        with open("natgas_paper.log", "a") as out:
            process_mcx = subprocess.Popen(
                [py_bin, "-u", "mcx_naturalgas_paper.py"],
                stdout=out,
                stderr=subprocess.STDOUT
            )

def stop_mcx():
    global process_mcx
    # Stop if running
    if process_mcx is not None and process_mcx.poll() is None:
        print(f"[{datetime.now(IST)}] Stopping mcx_naturalgas_paper.py")
        process_mcx.terminate()
        try:
            process_mcx.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process_mcx.kill()
        process_mcx = None

def main():
    print(f"[{datetime.now(IST)}] Scheduler started...")
    try:
        while True:
            now = datetime.now(IST)
            current_time = now.strftime("%H:%M")
            
            # Check if it's a weekday (Monday=0, Sunday=6)
            # You can remove `and now.weekday() < 5` if you want it to run on weekends too.
            is_weekday = now.weekday() < 5

            # upv2_paper.py schedule: 09:15 to 15:35
            if is_weekday and "09:15" <= current_time < "15:35":
                start_upv2()
            else:
                stop_upv2()

            # mcx_naturalgas_paper.py schedule: 18:00 to 23:25
            if is_weekday and "18:00" <= current_time < "23:25":
                start_mcx()
            else:
                stop_mcx()

            # Wait 30 seconds before checking again
            time.sleep(30)
            
    except KeyboardInterrupt:
        print(f"[{datetime.now(IST)}] Scheduler stopping... shutting down active processes.")
        stop_upv2()
        stop_mcx()
        print("Done.")

if __name__ == "__main__":
    main()
