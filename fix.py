with open('algo_v2.py', 'r') as f:
    code = f.read()
    
# Find the exact line and replace it with proper newlines
import re
code = re.sub(r'PREM_SL_DEBOUNCE_BARS.*?= 1.*?\\nTSL_STRANGLE_PCT.*?TREND_ORPHAN_PCT.*?TREND.*?$', 
'PREM_SL_DEBOUNCE_BARS   = 1\\nTSL_STRANGLE_PCT        = 0.05\\nTSL_TREND_ORPHAN_PCT    = 0.08', code, flags=re.MULTILINE)

with open('algo_v2.py', 'w') as f:
    f.write(code)
