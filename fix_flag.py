import re

with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

old_main = '''if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:'''

new_main = '''if __name__ == "__main__":
    import os
    if os.path.exists(KILL_SWITCH_FILE):
        try: os.remove(KILL_SWITCH_FILE)
        except: pass

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:'''
    
code = code.replace(old_main, new_main)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

