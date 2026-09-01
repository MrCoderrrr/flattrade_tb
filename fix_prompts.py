import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

# Fix globals
code = code.replace(
    'global PREM_SL_DEBOUNCE_BARS, BASE_MIN_WIDTH_PTS, BASE_MAX_WIDTH_PTS, SPOT_SL_TRAIL_RATIO',
    'global PREM_SL_DEBOUNCE_BARS, BASE_MIN_WIDTH_PTS, BASE_MAX_WIDTH_PTS, TSL_STRANGLE_PCT, TSL_TREND_ORPHAN_PCT'
)

# Fix prompts
old_prompt = '''        width = ask("Strangle Width (pts)", 0, int)
        BASE_MIN_WIDTH_PTS = width
        BASE_MAX_WIDTH_PTS = width
        tsl_pct = ask("Spot Trail % (TSL)", 55.0, float)
        SPOT_SL_TRAIL_RATIO = tsl_pct / 100.0 if tsl_pct > 1.0 else tsl_pct'''

new_prompt = '''        width = ask("Strangle Width (pts)", 0, int)
        BASE_MIN_WIDTH_PTS = width
        BASE_MAX_WIDTH_PTS = width
        # TSL inputs removed to use hardcoded 5% / 8% logic'''

code = code.replace(old_prompt, new_prompt)

with open('algo_v2.py', 'w') as f:
    f.write(code)
