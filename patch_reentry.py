import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

old_trigger = """    def _trigger_leg_cooldown(self, stopped_leg: str, current_spot: float):
        \"\"\"
        Enters cooldown tracker without 3-minute delay.
        Re-entry is immediately eligible on the very next 1-minute bar if trend is safe.
        \"\"\"
        self.cooldown_tracker[stopped_leg] = {
            "stopped_time": time.time(),
            "stopped_spot": current_spot,
            "active": True
        }
        log_alert(f"⏳ {stopped_leg} stopped out. 3-minute cooldown removed: Re-entry evaluated on next 1-min candle.")
        self.mode = "COOLDOWN"
        self._save_state()"""

new_trigger = """    def _trigger_leg_cooldown(self, stopped_leg: str, current_spot: float):
        \"\"\"
        Enters cooldown tracker.
        Requires a strict KAMA reversal of 7.5 points to re-enter.
        \"\"\"
        current_kama = float(self.current_indicators.get("kama", current_spot) or current_spot)
        self.cooldown_tracker[stopped_leg] = {
            "stopped_time": time.time(),
            "stopped_spot": current_spot,
            "extreme_kama": current_kama,
            "active": True
        }
        log_alert(f"⏳ {stopped_leg} stopped out. Requiring 7.5 pt KAMA reversal from {current_kama:.2f} to re-enter.")
        self.mode = "COOLDOWN"
        self._save_state()"""

code = code.replace(old_trigger, new_trigger)

old_check = """    def _check_cooldown_and_reenter(self, spot: float, atm: int, atr: float, regime: str, trend: int, dte_days: float = 2.0):
        \"\"\"
        Evaluates immediate re-entry on 1-min candle closes without 3-minute waiting timer.
        If re-entry order fails after 3 tries -> ONLY HEDGES LEFT!
        \"\"\"
        for leg in ("PE", "CE"):
            cd = self.cooldown_tracker.get(leg)
            if not cd or not cd.get("active", False):
                continue
            
            # Trend direction safety guard: do not re-enter against strong directional momentum
            trend_safe = True
            if leg == "CE" and regime == "TREND" and trend == 1:
                trend_safe = False
            elif leg == "PE" and regime == "TREND" and trend == -1:
                trend_safe = False
                
            if trend_safe:
                log_info(f"✅ Dynamic clearance met for {leg} (Trend safe: {trend}). Attempting re-shorting...")"""

new_check = """    def _check_cooldown_and_reenter(self, spot: float, atm: int, atr: float, regime: str, trend: int, dte_days: float = 2.0):
        \"\"\"
        Requires KAMA to reverse by >= 7.5 points from its extreme before re-entering.
        \"\"\"
        KAMA_REVERSAL_REQUIRED = 7.5
        current_kama = float(self.current_indicators.get("kama", spot) or spot)
        
        for leg in ("PE", "CE"):
            cd = self.cooldown_tracker.get(leg)
            if not cd or not cd.get("active", False):
                continue
                
            extreme = cd.get("extreme_kama", current_kama)
            reversal_met = False
            
            if leg == "CE":
                # CE stopped out means market rallied. Track highest KAMA.
                if current_kama > extreme:
                    cd["extreme_kama"] = current_kama
                    extreme = current_kama
                
                # To re-enter CE, market must fall (KAMA must drop 7.5 points from highest KAMA)
                if current_kama <= extreme - KAMA_REVERSAL_REQUIRED:
                    reversal_met = True
            else:
                # PE stopped out means market crashed. Track lowest KAMA.
                if current_kama < extreme:
                    cd["extreme_kama"] = current_kama
                    extreme = current_kama
                
                # To re-enter PE, market must bounce (KAMA must rise 7.5 points from lowest KAMA)
                if current_kama >= extreme + KAMA_REVERSAL_REQUIRED:
                    reversal_met = True
                    
            if reversal_met:
                log_info(f"✅ KAMA Reversal of 7.5+ pts achieved for {leg}! Extreme: {extreme:.2f}, Current: {current_kama:.2f}. Attempting re-shorting...")"""

code = code.replace(old_check, new_check)

with open('algo_v2.py', 'w') as f:
    f.write(code)
