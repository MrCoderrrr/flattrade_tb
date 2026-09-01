import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

old_loop_start = """                elif self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN", "HEDGES_ONLY"):
                    if not self.positions:
                        log_info("No active positions in RUNNING mode. Resetting to WAIT_DATA...")
                        self.mode = "WAIT_DATA"
                        self._save_state()
                        continue"""

new_loop_start = """                elif self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN", "HEDGES_ONLY"):
                    has_short = any(p.get("side") == "SELL" for p in self.positions.values())
                    if not has_short:
                        log_info("All short legs stopped out. Resetting to WAIT_DATA to re-center new Strangle...")
                        self.mode = "WAIT_DATA"
                        self.cooldown_tracker.clear()
                        self._save_state()
                        continue"""

code = code.replace(old_loop_start, new_loop_start)

with open('algo_v2.py', 'w') as f:
    f.write(code)
