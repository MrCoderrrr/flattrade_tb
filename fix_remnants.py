with open('algo_v2.py', 'r') as f:
    code = f.read()

# Replace debounce everywhere
code = code.replace('SPOT_SL_DEBOUNCE_BARS', 'PREM_SL_DEBOUNCE_BARS')
code = code.replace('SPOT_TSL_HIT', 'PREM_TSL_HIT')
code = code.replace('Spot SL', 'Premium SL')

with open('algo_v2.py', 'w') as f:
    f.write(code)
