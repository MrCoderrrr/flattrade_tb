import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

# 1. Constants
code = code.replace(
    'SPOT_SL_DEBOUNCE_BARS   = 1           # 1-minute candle closes needed to trigger SL',
    'PREM_SL_DEBOUNCE_BARS   = 1           # 1-minute candle closes needed to trigger premium SL\nTSL_STRANGLE_PCT        = 0.05        # 5% SL when both legs active\nTSL_TREND_ORPHAN_PCT    = 0.08        # 8% SL when one leg active in TREND'
)

# 2. Risk Manager
old_risk_manager = re.search(r'class RiskManager:.*?# ══════════════════════════════════════════════════════════════════════════════\n# MODULE 4: EXECUTION STATE MACHINE & STRATEGY ENGINE', code, re.DOTALL).group(0)

new_risk_manager = """class RiskManager:
    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1.0 * (capital * PORTFOLIO_CIRCUIT_PCT / 100.0)
        self.circuit_breaker_triggered = False

    def check_portfolio_circuit_breaker(self, unrealized_pnl: float, realized_pnl: float) -> getattr(typing, 'Tuple', tuple):
        net_mtm = realized_pnl + unrealized_pnl
        if net_mtm <= self.circuit_breaker_loss_limit:
            self.circuit_breaker_triggered = True
            return True, f"PORTFOLIO CIRCUIT BREAKER HIT: Net MTM {net_mtm:.2f} breached limit {self.circuit_breaker_loss_limit:.2f}"
        return False, ""

    def init_premium_sl(self, leg: str, entry_premium: float) -> getattr(typing, 'Dict', dict):
        \"\"\"Initializes Premium Trailing Stop Loss tracker.\"\"\"
        return {
            "entry_premium": round(entry_premium, 2),
            "best_premium": round(entry_premium, 2),
            "current_sl": round(entry_premium * 1.05, 2),
            "breach_count": 0
        }

    def update_premium_sl_and_check(self, leg: str, pos_data: getattr(typing, 'Dict', dict), current_premium: float, is_strangle: bool, regime: str, is_new_1m_bar: bool) -> getattr(typing, 'Tuple', tuple):
        sl_state = pos_data.get("premium_sl_state")
        if not sl_state:
            return False, ""
        
        best_premium = float(sl_state.get("best_premium", pos_data["entry_price"]))
        current_sl = float(sl_state["current_sl"])
        breach_count = int(sl_state.get("breach_count", 0))
        
        # Determine trailing percentage based on strategy rules
        if is_strangle:
            tsl_pct = TSL_STRANGLE_PCT
        else:
            if regime == "TREND":
                tsl_pct = TSL_TREND_ORPHAN_PCT
            else:
                tsl_pct = TSL_STRANGLE_PCT
                
        # Update best premium if it dropped (favorable for sellers)
        if current_premium < best_premium:
            best_premium = current_premium
            sl_state["best_premium"] = round(best_premium, 2)
            
        # Calculate trailing SL
        candidate_sl = best_premium * (1.0 + tsl_pct)
        
        # Only tighten SL, never loosen unless pct changed (e.g. 5% to 8% expansion)
        if is_strangle:
            sl_state["current_sl"] = min(current_sl, round(candidate_sl, 2))
        else:
            sl_state["current_sl"] = round(candidate_sl, 2)
            
        is_breaching = (current_premium >= sl_state["current_sl"])
        
        if is_breaching:
            if PREM_SL_DEBOUNCE_BARS <= 0:
                return True, f"⛔ Premium SL Triggered for {leg} | Premium: ₹{current_premium:.2f} breached SL: ₹{sl_state['current_sl']:.2f}"
            
            if is_new_1m_bar:
                breach_count += 1
                sl_state["breach_count"] = breach_count
                if breach_count >= PREM_SL_DEBOUNCE_BARS:
                    return True, f"⛔ Premium SL Triggered for {leg} | Premium: ₹{current_premium:.2f} breached SL: ₹{sl_state['current_sl']:.2f} (Confirmed over {PREM_SL_DEBOUNCE_BARS} 1-min bars)"
                else:
                    log_warn(f"⚠️ {leg} Premium SL Warning: ₹{current_premium:.2f} >= ₹{sl_state['current_sl']:.2f}. Need {PREM_SL_DEBOUNCE_BARS - breach_count} more closes.")
        else:
            if breach_count > 0:
                log_info(f"🛡️ {leg} recovered. Premium SL threat cleared.")
            sl_state["breach_count"] = 0
            
        return False, ""

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: EXECUTION STATE MACHINE & STRATEGY ENGINE"""

code = code.replace(old_risk_manager, new_risk_manager)

# 3. Fix references in _enter_leg and _load_state
code = code.replace('pos_info["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, spot, atr)', 'pos_info["premium_sl_state"] = self.risk_manager.init_premium_sl(leg, ltp)')
code = code.replace('if "spot_sl_state" not in pos or not pos["spot_sl_state"]:', 'if "premium_sl_state" not in pos or not pos["premium_sl_state"]:')
code = code.replace('pos["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, entry_spot, atr_val)', 'pos["premium_sl_state"] = self.risk_manager.init_premium_sl(leg, float(pos.get("entry_price", 100.0)))')

# 4. Fix update call in main loop
old_update_call = '''                            is_stopped, reason = self.risk_manager.update_spot_sl_and_check(
                                leg, self.positions[leg], spot, is_new_1m_bar
                            )'''
new_update_call = '''                            is_strangle = ("CE" in self.positions and "PE" in self.positions)
                            ltp_premium = self._get_ltp(self.positions[leg]["strike"], self.positions[leg]["base"])
                            is_stopped, reason = self.risk_manager.update_premium_sl_and_check(
                                leg, self.positions[leg], ltp_premium, is_strangle, regime, is_new_1m_bar
                            )'''
code = code.replace(old_update_call, new_update_call)

# 5. Fix Dashboard
code = code.replace('PREM_SL_DEBOUNCE_BARS   = 1           # 1-minute candle closes needed to trigger premium SL', 'PREM_SL_DEBOUNCE_BARS   = 1') # ensure clean replacement
code = code.replace('SPOT-TSL ACTIVE', 'PREM-TSL (5%/8%) ACTIVE')
code = code.replace('║  LEG        │  STRIKE │ SIDE  │ QTY │   ENTRY │     LTP │ ENTRY SPOT │    SPOT SL │ DEBOUNCE │        PNL        ║', '║  LEG        │  STRIKE │ SIDE  │ QTY │   ENTRY │     LTP │  BEST PREM │    PREM SL │ DEBOUNCE │        PNL        ║')

# 6. Fix Dashboard row rendering
old_dash_row = '''                if "spot_sl_state" in pos:
                    entry_spot_str = f"{pos['spot_sl_state'].get('entry_spot', 0):.1f}"
                    spot_sl_str = f"{pos['spot_sl_state'].get('current_sl', 0):.1f}"
                    debounce_str = f"{pos['spot_sl_state'].get('breach_count', 0)}/{SPOT_SL_DEBOUNCE_BARS}"
                else:
                    entry_spot_str = "—"
                    spot_sl_str = "—"
                    debounce_str = "—"
                    
                table_lines.append(f"║  {leg:10s} │ {strike:7d} │ {side:5s} │ {qty:3d} │ {entry:7.2f} │ {ltp:7.2f} │ {entry_spot_str:>10s} │ {spot_sl_str:>10s} │ {debounce_str:>8s} │ {pnl_col}{pnl_sign}₹{abs(pnl):8.2f}{Style.RESET_ALL}        ║")'''

new_dash_row = '''                if "premium_sl_state" in pos:
                    best_prem_str = f"{pos['premium_sl_state'].get('best_premium', 0):.2f}"
                    prem_sl_str = f"{pos['premium_sl_state'].get('current_sl', 0):.2f}"
                    debounce_str = f"{pos['premium_sl_state'].get('breach_count', 0)}/{PREM_SL_DEBOUNCE_BARS}"
                else:
                    best_prem_str = "—"
                    prem_sl_str = "—"
                    debounce_str = "—"
                    
                table_lines.append(f"║  {leg:10s} │ {strike:7d} │ {side:5s} │ {qty:3d} │ {entry:7.2f} │ {ltp:7.2f} │ {best_prem_str:>10s} │ {prem_sl_str:>10s} │ {debounce_str:>8s} │ {pnl_col}{pnl_sign}₹{abs(pnl):8.2f}{Style.RESET_ALL}        ║")'''

code = code.replace(old_dash_row, new_dash_row)

with open('algo_v2.py', 'w') as f:
    f.write(code)

print("All patched")
