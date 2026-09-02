import re

with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

old_dash = '''    def _render_dashboard(self, spot: float, atm: int, indicators: dict, unrealized: float):
        try:
            os.system('cls' if os.name == 'nt' else 'clear')
            
            # Save external snapshot'''

new_dash = '''    def _render_dashboard(self, spot: float, atm: int, indicators: dict, unrealized: float):
        try:
            # We explicitly removed os.system('clear') per user request so the terminal scrolls naturally
            
            # Save external snapshot'''

code = code.replace(old_dash, new_dash)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

