with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

old_ret = '''        return {
            "entry_premium": entry_prem,
            "lowest_premium_seen": lowest_seen,
            "is_armed": armed,
            "active_stop": active_stop,
            "breach_count": current_state.get("breach_count", 0)
        }'''

new_ret = '''        return {
            "entry_premium": entry_prem,
            "lowest_premium_seen": lowest_seen,
            "is_armed": armed,
            "active_stop": active_stop,
            "breach_count": current_state.get("breach_count", 0),
            "is_reentry": is_reentry
        }'''
        
code = code.replace(old_ret, new_ret)

# Also fix the call site to use .get just in case:
old_call = 'new_tsl = self.risk_manager.update_premium_tsl(tsl_state, ltp, tsl_state["is_reentry"])'
new_call = 'new_tsl = self.risk_manager.update_premium_tsl(tsl_state, ltp, tsl_state.get("is_reentry", False))'
code = code.replace(old_call, new_call)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

