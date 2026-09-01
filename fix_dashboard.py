import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

old_dash = """                sl_state = pos.get("spot_sl_state")
                if sl_state and is_short:
                    entry_spot_str = f"{sl_state['entry_spot']:.1f}"
                    spot_sl_str = f"{sl_state['current_sl']:.1f}"
                    debounce_str = f"{sl_state.get('breach_count', 0)}/{PREM_SL_DEBOUNCE_BARS}"
                else:
                    entry_spot_str = "—"
                    spot_sl_str = "—"
                    debounce_str = "—"

                row = (f"  {c_white}{leg:<10}{res} {VS} {c_white}{pos['strike']:>7}{res} {VS} {side_col}{pos['side']:<5}{res} {VS} "
                       f"{pos['qty']:>3} {VS} {pos['entry_price']:>7.2f} {VS} {c_yellow}{ltp:>7.2f}{res} {VS} "
                       f"{entry_spot_str:>10} {VS} {c_mag}{spot_sl_str:>10}{res} {VS} {debounce_str:>8} {VS} "
                       f"{pnl_col}{sign}₹{abs(pnl):>8.2f}{res}  ")"""

new_dash = """                sl_state = pos.get("premium_sl_state")
                if sl_state and is_short:
                    entry_spot_str = f"{sl_state['best_premium']:.2f}"
                    spot_sl_str = f"{sl_state['current_sl']:.2f}"
                    debounce_str = f"{sl_state.get('breach_count', 0)}/{PREM_SL_DEBOUNCE_BARS}"
                else:
                    entry_spot_str = "—"
                    spot_sl_str = "—"
                    debounce_str = "—"

                row = (f"  {c_white}{leg:<10}{res} {VS} {c_white}{pos['strike']:>7}{res} {VS} {side_col}{pos['side']:<5}{res} {VS} "
                       f"{pos['qty']:>3} {VS} {pos['entry_price']:>7.2f} {VS} {c_yellow}{ltp:>7.2f}{res} {VS} "
                       f"{entry_spot_str:>10} {VS} {c_mag}{spot_sl_str:>10}{res} {VS} {debounce_str:>8} {VS} "
                       f"{pnl_col}{sign}₹{abs(pnl):>8.2f}{res}  ")"""

code = code.replace(old_dash, new_dash)

with open('algo_v2.py', 'w') as f:
    f.write(code)
