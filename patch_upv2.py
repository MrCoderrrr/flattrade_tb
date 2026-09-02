import re

with open('upv2.py', 'r') as f:
    code = f.read()

# 1. Update Global Defaults
code = re.sub(r'KAMA_PERIOD\s*=\s*\d+', 'KAMA_PERIOD             = 12', code, count=1)
code = re.sub(r'KAMA_FAST_EMA\s*=\s*\d+', 'KAMA_FAST_EMA           = 3', code, count=1)
code = re.sub(r'KAMA_SLOW_EMA\s*=\s*\d+', 'KAMA_SLOW_EMA           = 30', code, count=1)
code = re.sub(r'ADX_PERIOD\s*=\s*\d+', 'ADX_PERIOD              = 7', code, count=1)
code = re.sub(r'ADX_CHOP_THRESHOLD\s*=\s*[\d\.]+', 'ADX_CHOP_THRESHOLD      = 25.0', code, count=1)
code = re.sub(r'ADX_TREND_THRESHOLD\s*=\s*[\d\.]+', 'ADX_TREND_THRESHOLD     = 25.0', code, count=1)
code = re.sub(r'PREM_SL_DEBOUNCE_BARS\s*=\s*\d+', 'PREM_SL_DEBOUNCE_BARS   = 1', code, count=1)

# 2. Update Prompts
code = code.replace('ask("KAMA Lookback", 13, int)', 'ask("KAMA Lookback", 12, int)')
code = code.replace('ask("KAMA Fast EMA", 2, int)', 'ask("KAMA Fast EMA", 3, int)')
code = code.replace('ask("ADX Period (5m)", 9, int)', 'ask("ADX Period (5m)", 7, int)')
code = code.replace('ask("ADX Regime Gate", 20.0, float)', 'ask("ADX Regime Gate", 25.0, float)')

# 3. Update Entry Logic
old_entry_logic = """                        ce_s_ok = True
                        pe_s_ok = True
                        if "PE" not in self.positions:
                            pe_s_ok = self._enter_leg("PE", pe_strike, "SELL", spot, atr, dte_days)
                        if "CE" not in self.positions:
                            ce_s_ok = self._enter_leg("CE", ce_strike, "SELL", spot, atr, dte_days)"""

new_entry_logic = """                        ce_s_ok = True
                        pe_s_ok = True
                        k_trend = self.current_indicators.get("trend", 0)
                        
                        if regime == "TREND":
                            log_info(f"TREND Regime detected. Selling single directional leg. KAMA Trend: {k_trend}")
                            if k_trend == 1:
                                if "PE" not in self.positions: pe_s_ok = self._enter_leg("PE", pe_strike, "SELL", spot, atr, dte_days)
                            elif k_trend == -1:
                                if "CE" not in self.positions: ce_s_ok = self._enter_leg("CE", ce_strike, "SELL", spot, atr, dte_days)
                            else:
                                log_info("TREND but KAMA is flat. Entering full strangle.")
                                if "PE" not in self.positions: pe_s_ok = self._enter_leg("PE", pe_strike, "SELL", spot, atr, dte_days)
                                if "CE" not in self.positions: ce_s_ok = self._enter_leg("CE", ce_strike, "SELL", spot, atr, dte_days)
                        else:
                            # CHOP Regime -> Sell Both
                            if "PE" not in self.positions: pe_s_ok = self._enter_leg("PE", pe_strike, "SELL", spot, atr, dte_days)
                            if "CE" not in self.positions: ce_s_ok = self._enter_leg("CE", ce_strike, "SELL", spot, atr, dte_days)"""

code = code.replace(old_entry_logic, new_entry_logic)

with open('upv2.py', 'w') as f:
    f.write(code)

