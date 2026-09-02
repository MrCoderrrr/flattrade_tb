import re

with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

# 1. Fix _init_spot_token
old_init = '''    def _init_spot_token(self):
        if self.broker.paper_trading or not self.broker.api:
            self.spot_token = "SIMULATED"
            self.latest_spot = 220.0 # Example
            self.latest_atm = 220
            return'''
            
new_init = '''    def _init_spot_token(self):
        if not getattr(self.broker, 'api', None):
            self.spot_token = "SIMULATED"
            self.latest_spot = 220.0 # Example
            self.latest_atm = 220
            return'''
code = code.replace(old_init, new_init)

# 2. Fix get_spot_and_atm
old_get_spot = '''    def get_spot_and_atm(self) -> Tuple[float, int, bool]:
        if self.broker.paper_trading:
            return self.latest_spot, self.latest_atm, False
            
        if not self.spot_token: return self.latest_spot, self.latest_atm, True'''
        
new_get_spot = '''    def get_spot_and_atm(self) -> Tuple[float, int, bool]:
        if not getattr(self.broker, 'api', None) or self.spot_token == "SIMULATED":
            return self.latest_spot, self.latest_atm, False
            
        if not self.spot_token: return self.latest_spot, self.latest_atm, True'''
code = code.replace(old_get_spot, new_get_spot)

# 3. Fix get_live_quote
old_quote = '''    def get_live_quote(self, strike: int, option_type: str) -> Dict[str, Any]:
        if self.broker.paper_trading or not self.broker.api:
            # Paper mode deterministic pricing
            dist = abs(self.latest_spot - strike)
            return {"lp": max(0.1, 15.0 - (dist * 0.5)), "tsym": f"NATGAS{strike}{option_type}"}'''
            
new_quote = '''    def get_live_quote(self, strike: int, option_type: str) -> Dict[str, Any]:
        if not getattr(self.broker, 'api', None) or self.spot_token == "SIMULATED":
            # Fallback deterministic pricing only if API is completely unavailable
            dist = abs(self.latest_spot - strike)
            return {"lp": max(0.1, 15.0 - (dist * 0.5)), "tsym": f"NATGAS{strike}{option_type}"}'''
code = code.replace(old_quote, new_quote)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

