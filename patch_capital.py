import re

with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

# 1. Update FlattradeBroker to capture live capital
old_limits = '''            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
                raise RuntimeError("Token invalid or expired. Login again.")
            log_info("Flattrade MCX session authenticated successfully.")'''
new_limits = '''            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
                raise RuntimeError("Token invalid or expired. Login again.")
            
            cash = float(limits.get('cash', 0.0))
            payin = float(limits.get('payin', 0.0))
            self.live_capital = cash + payin
            log_info(f"Flattrade MCX session authenticated successfully. (Live Capital: {self.live_capital})")'''
code = code.replace(old_limits, new_limits)

# 2. Update prompt_user_variables to remove capital prompt
old_prompt = '''    if not PAPER_TRADING_MODE:
        cap_in = input(f"Enter Allocated Capital for Circuit Breaker (Default: 250000): ").strip()
        if cap_in.isdigit():
            CAPITAL = float(cap_in)'''
code = code.replace(old_prompt, '')

# 3. Update main setup to override CAPITAL if live
old_setup = '''        broker = FlattradeBroker(paper_trading=PAPER_TRADING_MODE)
        market_data = MarketData(broker)
        risk_manager = RiskManager(capital=CAPITAL)'''
new_setup = '''        broker = FlattradeBroker(paper_trading=PAPER_TRADING_MODE)
        
        if not PAPER_TRADING_MODE and hasattr(broker, 'live_capital') and broker.live_capital > 0:
            CAPITAL = broker.live_capital
            log_info(f"Capital auto-fetched from Flattrade: ₹{CAPITAL:,.2f}")
            
        market_data = MarketData(broker)
        risk_manager = RiskManager(capital=CAPITAL)'''
code = code.replace(old_setup, new_setup)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

