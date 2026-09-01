import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

# Replace TSL constants
code = code.replace('TSL_STRANGLE_PCT        = 0.05', 'TSL_STRANGLE_PCT        = 0.08')
code = code.replace('TSL_TREND_ORPHAN_PCT    = 0.08', 'TSL_TREND_ORPHAN_PCT    = 0.10')

# Update dashboard UI
code = code.replace('PREM-TSL (5%/8%) ACTIVE', 'PREM-TSL (8%/10%) ACTIVE')

# Update init_premium_sl multiplier (1.05 -> 1.08)
code = code.replace('round(entry_premium * 1.05, 2)', 'round(entry_premium * 1.08, 2)')

with open('algo_v2.py', 'w') as f:
    f.write(code)
