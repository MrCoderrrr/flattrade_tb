with open('algo_v2.py', 'r') as f:
    code = f.read()

code = code.replace('sl_state = pos.get("spot_sl_state")', 'sl_state = pos.get("premium_sl_state")')
code = code.replace('entry_spot_str = f"{sl_state[\'entry_spot\']:.1f}"', 'entry_spot_str = f"{sl_state[\'best_premium\']:.2f}"')
code = code.replace('spot_sl_str = f"{sl_state[\'current_sl\']:.1f}"', 'spot_sl_str = f"{sl_state[\'current_sl\']:.2f}"')

with open('algo_v2.py', 'w') as f:
    f.write(code)
