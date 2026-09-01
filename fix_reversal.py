import re

with open('algo_v2.py', 'r') as f:
    code = f.read()

code = code.replace('KAMA_REVERSAL_REQUIRED = 7.5', 'KAMA_REVERSAL_REQUIRED = 1.0')
code = code.replace('Requiring 7.5 pt KAMA reversal', 'Requiring 1.0 pt KAMA reversal')
code = code.replace('Reversal of 7.5+ pts achieved', 'Reversal of 1.0+ pts achieved')

with open('algo_v2.py', 'w') as f:
    f.write(code)
