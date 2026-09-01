with open('algo_v2.py', 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "def _check_cooldown_and_reenter" in line:
        # Insert check after the docstring (which ends around i+3)
        # Actually let's just insert it before KAMA_REVERSAL_REQUIRED
        pass
        
with open('algo_v2.py', 'r') as f:
    code = f.read()

import re
code = re.sub(r'(def _check_cooldown_and_reenter.*?:.*?\"\"\".*?\"\"\")', r'\1\n        if regime == "TREND":\n            return  # NEVER re-enter opposing legs while a strong trend is active!', code, flags=re.DOTALL)

with open('algo_v2.py', 'w') as f:
    f.write(code)
