import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

# 1. Remove IV Gate
iv_block = '''        # IV Gate
        current_iv = 15.0
        if hasattr(self, 'session_em_1sd') and self.session_em_1sd > 0:
            current_iv = (self.session_em_1sd / spot) * 19.1 * 100.0
        ivr = update_and_get_ivr(current_iv)
        
        if side == "SELL" and ivr < IVR_THRESHOLD_PCT and IVR_ACTION == "SKIP":
            log_warn(f"IVR {ivr:.1f}% < {IVR_THRESHOLD_PCT}%. Skipping {leg} entry.")
            return False'''

code = code.replace(iv_block, '        # IV Gate Removed per user request')

# 2. Hardcode 1 lot in _calculate_lot_quantity
old_calc = '''    def _calculate_lot_quantity(self) -> int:
        capital = CAPITAL
        try:
            api = getattr(self.broker, "api", None)
            if not getattr(self.broker, "paper_trading", False) and api and hasattr(api, "get_limits"):
                limits = api.get_limits()
                if limits and isinstance(limits, dict) and limits.get('stat') == 'Ok':
                    cash = float(limits.get('cash', 0.0))
                    payin = float(limits.get('payin', 0.0))
                    margin_used = float(limits.get('margin', 0.0))
                    live_avail = (cash + payin) - margin_used
                    if live_avail > 0:
                        capital = min(capital, live_avail)
        except Exception as e:
            log_warn(f"Failed to fetch live margin for lot sizing: {e}")
            
        allowed_cap = capital * CAPITAL_FRACTION_LIVE
        max_lots_by_cap = int(allowed_cap // MARGIN_IRON_CONDOR)
        if max_lots_by_cap <= 0: max_lots_by_cap = 1
        return min(MAX_LOTS_PER_LEG * LOT_SIZE, max_lots_by_cap * LOT_SIZE)'''

new_calc = '''    def _calculate_lot_quantity(self) -> int:
        return LOT_SIZE # Hardcoded to exactly 1 lot per user request'''

code = code.replace(old_calc, new_calc)

with open('algo_v2.py', 'w') as f:
    f.write(code)
