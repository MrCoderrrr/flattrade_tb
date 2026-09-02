import re

with open('natgas_strategy_v1.py', 'r') as f:
    code = f.read()

old_broker_init = '''    def __init__(self, paper_trading: bool):
        self.paper_trading = paper_trading
        self.api = None
        self.order_counter = 1000
        
        if not self.paper_trading:
            from creds import USER_ID
            try:
                from api_helper import NorenApiPy
                self.api = NorenApiPy()
                log_info("Using NorenApiPy() factory.")
            except ImportError:
                from api_helper import get_norenapi
                self.api = get_norenapi()
                log_warn("NorenApiPy not found, used get_norenapi() fallback.") # Bug 2 fix
            token_file = "token.txt" if os.path.exists("token.txt") else os.path.join(PROJECT_ROOT, "token.txt")
            if not os.path.exists(token_file):
                raise RuntimeError("Token missing. Run login.py first.")
            with open(token_file, "r") as f:
                token = f.read().strip()
            self.api.set_session(userid=str(USER_ID).strip(), password="", usertoken=token)
            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
                raise RuntimeError("Token invalid or expired. Login again.")
            
            cash = float(limits.get('cash', 0.0))
            payin = float(limits.get('payin', 0.0))
            self.live_capital = cash + payin
            log_info(f"Flattrade MCX session authenticated successfully. (Live Capital: {self.live_capital})")
        else:
            log_warn("PAPER TRADING MODE — Fills are simulated.")'''

new_broker_init = '''    def __init__(self, paper_trading: bool):
        self.paper_trading = paper_trading
        self.api = None
        self.order_counter = 1000
        
        # Always authenticate to fetch live data, even in paper mode
        from creds import USER_ID
        try:
            from api_helper import NorenApiPy
            self.api = NorenApiPy()
            log_info("Using NorenApiPy() factory.")
        except ImportError:
            from api_helper import get_norenapi
            self.api = get_norenapi()
            log_warn("NorenApiPy not found, used get_norenapi() fallback.") # Bug 2 fix
            
        token_file = "token.txt" if os.path.exists("token.txt") else os.path.join(PROJECT_ROOT, "token.txt")
        if not os.path.exists(token_file):
            log_warn("Token missing. Live data will be unavailable.")
            if not self.paper_trading: raise RuntimeError("Token missing for LIVE mode.")
        else:
            with open(token_file, "r") as f:
                token = f.read().strip()
            self.api.set_session(userid=str(USER_ID).strip(), password="", usertoken=token)
            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
                if not self.paper_trading: raise RuntimeError("Token invalid or expired. Login again.")
                else: log_warn("Paper mode: Token invalid. Falling back to simulated data.")
            else:
                cash = float(limits.get('cash', 0.0))
                payin = float(limits.get('payin', 0.0))
                self.live_capital = cash + payin
                log_info(f"Flattrade MCX session authenticated. (Live Capital: {self.live_capital})")
                
        if self.paper_trading:
            log_warn("PAPER TRADING MODE — Execution fills are simulated (but data is live if authenticated).")'''

code = code.replace(old_broker_init, new_broker_init)

with open('natgas_strategy_v1.py', 'w') as f:
    f.write(code)

