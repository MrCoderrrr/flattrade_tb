import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

# 1. Define missing constants
if "TSL_STRANGLE_PCT" not in code or "TSL_STRANGLE_PCT = 0.05" not in code:
    code = code.replace(
        'PREM_SL_DEBOUNCE_BARS   = 1           # 1-minute candle closes needed to trigger SL',
        'PREM_SL_DEBOUNCE_BARS   = 1           # 1-minute candle closes needed to trigger premium SL\\nTSL_STRANGLE_PCT        = 0.05        # 5% SL when both legs active\\nTSL_TREND_ORPHAN_PCT    = 0.08        # 8% SL when one leg active in TREND'
    )
    # If the first replace didn't work because it was already PREM_SL_DEBOUNCE_BARS...
    if "TSL_STRANGLE_PCT = 0.05" not in code:
        code = code.replace(
            'PREM_SL_DEBOUNCE_BARS   = 1',
            'PREM_SL_DEBOUNCE_BARS   = 1           # 1-minute candle closes needed to trigger premium SL\\nTSL_STRANGLE_PCT        = 0.05        # 5% SL when both legs active\\nTSL_TREND_ORPHAN_PCT    = 0.08        # 8% SL when one leg active in TREND'
        )
        
# 2. Fix dashboard header
old_hdr = "hdr = f\"  {'LEG':<10} {VS} {'STRIKE':>7} {VS} {'SIDE':<5} {VS} {'QTY':>3} {VS} {'ENTRY':>7} {VS} {'LTP':>7} {VS} {'ENTRY SPOT':>10} {VS} {'SPOT SL':>10} {VS} {'DEBOUNCE':>8} {VS} {'PNL':>10}  \""
new_hdr = "hdr = f\"  {'LEG':<10} {VS} {'STRIKE':>7} {VS} {'SIDE':<5} {VS} {'QTY':>3} {VS} {'ENTRY':>7} {VS} {'LTP':>7} {VS} {'BEST PREM':>10} {VS} {'PREM SL':>10} {VS} {'DEBOUNCE':>8} {VS} {'PNL':>10}  \""
code = code.replace(old_hdr, new_hdr)
code = code.replace("'SPOT SL':>10}", "'PREM SL':>10}") # fallback

with open('algo_v2.py', 'w') as f:
    f.write(code)
