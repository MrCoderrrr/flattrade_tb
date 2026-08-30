import os
import sys
import time
from datetime import datetime, timedelta
from api_helper import NorenApiPy
import pandas as pd
from creds import USER_ID

# ==============================================================================
# CONFIGURATION
# ==============================================================================
ENTRY_TIME = "09:20"
EXIT_TIME = "15:15"
TOKEN_FILE = "token.txt"

api = NorenApiPy()

# ==============================================================================
# 1. CORE EXECUTION HELPER FUNCTIONS
# ==============================================================================

def place_leg(api_instance, symbol, buy_or_sell, quantity, product_type="M", price_type="MKT", price=0, exchange="NFO"):
    try:
        res = api_instance.place_order(
            buy_or_sell=str(buy_or_sell),
            product_type=str(product_type),
            exchange=str(exchange),
            tradingsymbol=str(symbol),
            quantity=str(quantity),
            discloseqty="0",
            price_type=str(price_type),
            price=str(price),
            trigger_price="0",
            retention='DAY',
            remarks='API_Strategy_Leg'
        )
        if res and isinstance(res, dict) and res.get('stat') == 'Ok':
            print(f"[SUCCESS] {buy_or_sell} {quantity} x {symbol} | Order No: {res.get('norenordno')}")
            return res
        else:
            if res is None:
                print(f"[FAILED] {symbol}: No response from API (res is None).")
            else:
                print(f"[FAILED] {symbol}: {res.get('emsg', 'Unknown Error')}")
            return None
    except Exception as e:
        print(f"[ERROR] Exception placing leg {symbol}: {e}")
        return None

def execute_multi_leg_strategy(api_instance, legs):
    print(f"\n--- EXECUTING {len(legs)}-LEG STRATEGY ---")
    results = []
    
    for idx, leg in enumerate(legs, start=1):
        print(f"\nProcessing Leg {idx}/{len(legs)}...")
        res = place_leg(
            api_instance=api_instance,
            symbol=leg['symbol'],
            buy_or_sell=leg['action'],     
            quantity=leg['qty'],           
            product_type=leg.get('product', 'M'),
            price_type=leg.get('price_type', 'MKT'),
            price=leg.get('price', 0),
            exchange=leg.get('exchange', 'NFO')
        )
        results.append({'leg': leg, 'response': res})
        
        if leg['action'] == 'B' and (res is None or res.get('stat') != 'Ok'):
            print(f"[CRITICAL] Hedge leg {leg['symbol']} failed! Aborting to prevent naked shorts.")
            break
        time.sleep(0.2)
    return results

def exit_all_positions(api_instance):
    print("\n" + "="*50)
    print("INITIATING SQUARE OFF OF ALL POSITIONS")
    print("="*50)
    pos_res = api_instance.get_positions()
    
    if not pos_res or not isinstance(pos_res, list):
        print("No open positions found or failed to fetch positions.")
        return
        
    for pos in pos_res:
        net_qty = int(pos.get('netqty', 0))
        if net_qty == 0:
            continue
            
        symbol = pos.get('tsym')
        exchange = pos.get('exch')
        product = pos.get('prd')
        
        # Long position -> Sell to close. Short position -> Buy to close.
        action = 'S' if net_qty > 0 else 'B'
        square_qty = abs(net_qty)
        
        print(f"Squaring off: {action} {square_qty} x {symbol}")
        place_leg(
            api_instance=api_instance,
            symbol=symbol,
            buy_or_sell=action,
            quantity=square_qty,
            product_type=product,
            price_type="MKT",
            price=0,
            exchange=exchange
        )
        time.sleep(0.2)
    print("Square off complete.")

# ==============================================================================
# 2. V2 STRATEGY LOGIC (Strike Selection)
# ==============================================================================

def get_nifty_spot(api_instance):
    try:
        res = api_instance.get_quotes(exchange='NSE', token='26000')
        if res and isinstance(res, dict) and res.get('stat') == 'Ok':
            return float(res.get('lp', res.get('ltp', 0)))
    except:
        pass
    return None

def get_recent_atr(api_instance, default=35.0):
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(days=5)
        res = api_instance.get_time_price_series(
            exchange='NSE', token='26000', 
            starttime=start_time.timestamp(), endtime=end_time.timestamp(), interval=5
        )
        if res and isinstance(res, list) and len(res) > 0:
            df = pd.DataFrame(res)
            df['intc'] = pd.to_numeric(df['intc'])
            df['inth'] = pd.to_numeric(df['inth'])
            df['intl'] = pd.to_numeric(df['intl'])
            recent = df.head(14)
            tr = recent['inth'] - recent['intl']
            return max(15.0, tr.mean())
    except:
        pass
    return default

def get_safe_strikes(spot, atr=35.0, min_width=0, max_width=0, atr_multiplier=1.0):
    atm_spot = int(round(spot / 50.0) * 50)
    calculated_width = max(min_width, min(max_width if max_width > 0 else float('inf'), atr_multiplier * atr))
    stride_50 = int(round(calculated_width / 50.0) * 50)
    
    ce_strike = atm_spot + stride_50
    pe_strike = atm_spot - stride_50
    
    if ce_strike <= atm_spot or pe_strike >= atm_spot or ce_strike == pe_strike:
        ce_strike = atm_spot + 50
        pe_strike = atm_spot - 50
        
    return atm_spot, ce_strike, pe_strike

def find_option_symbol(api_instance, strike, option_type, expiry_prefix="NIFTY"):
    search_text = f"{expiry_prefix} {strike} {option_type}"
    res = api_instance.searchscrip(exchange='NFO', searchtext=search_text)
    
    if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
        opt_char = option_type[0]
        expected_suffix = f"{opt_char}{strike}"
        valid_candidates = []
        
        for item in res['values']:
            if item['tsym'].endswith(expected_suffix) and 'exd' in item:
                try:
                    dt = datetime.strptime(item['exd'], "%d-%b-%Y")
                    valid_candidates.append({'item': item, 'dt': dt})
                except ValueError:
                    continue
                    
        if valid_candidates:
            valid_candidates.sort(key=lambda x: x['dt'])
            best_match = valid_candidates[0]['item']
            return {'tsym': best_match['tsym'], 'ls': int(best_match['ls'])}
            
    return None

def enter_iron_condor():
    print("\n" + "="*50)
    print("INITIATING IRON CONDOR ENTRY")
    print("="*50)
    
    spot = get_nifty_spot(api)
    if not spot or spot < 10000:
        print(f"Aborting: Invalid NIFTY Spot Price ({spot}).")
        return
        
    print(f"Current NIFTY Spot: {spot}")
    
    atr_value = get_recent_atr(api, default=35.0)
    hedge_width = 1000 
    
    atm, short_ce_strike, short_pe_strike = get_safe_strikes(spot=spot, atr=atr_value)
    long_ce_strike = atm + hedge_width
    long_pe_strike = atm - hedge_width
    
    print(f"ATM: {atm} | Shorts: PE {short_pe_strike} / CE {short_ce_strike} | Hedges: PE {long_pe_strike} / CE {long_ce_strike}")
    
    prefix = "NIFTY"
    long_pe_data = find_option_symbol(api, long_pe_strike, 'PE', prefix)
    long_ce_data = find_option_symbol(api, long_ce_strike, 'CE', prefix)
    short_pe_data = find_option_symbol(api, short_pe_strike, 'PE', prefix)
    short_ce_data = find_option_symbol(api, short_ce_strike, 'CE', prefix)
    
    if not all([long_pe_data, long_ce_data, short_pe_data, short_ce_data]):
        print("Aborting: Failed to resolve option symbols.")
        return
        
    iron_condor_legs = [
        {'symbol': long_pe_data['tsym'],  'action': 'B', 'qty': long_pe_data['ls'], 'product': 'M'},
        {'symbol': long_ce_data['tsym'],  'action': 'B', 'qty': long_ce_data['ls'], 'product': 'M'},
        {'symbol': short_pe_data['tsym'], 'action': 'S', 'qty': short_pe_data['ls'], 'product': 'M'},
        {'symbol': short_ce_data['tsym'], 'action': 'S', 'qty': short_ce_data['ls'], 'product': 'M'},
    ]
    
    execute_multi_leg_strategy(api, iron_condor_legs)


# ==============================================================================
# 3. BACKGROUND AUTOMATION LOOP
# ==============================================================================

def check_market_hours():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return True

def wait_until(target_time_str):
    target_hour, target_minute = map(int, target_time_str.split(':'))
    while True:
        now = datetime.now()
        if now.hour > target_hour or (now.hour == target_hour and now.minute >= target_minute):
            break
        print(f"[{now.strftime('%H:%M:%S')}] Waiting for {target_time_str}...")
        time.sleep(30)

def main():
    print("\n" + "="*50)
    print("FLATTRADE BACKGROUND AUTOBOT")
    print("="*50)
    
    if not check_market_hours():
        print("[WARNING] Today is a weekend. Exiting.")
        sys.exit(0)
        
    # 1. Load Token Silently
    if not os.path.exists(TOKEN_FILE):
        print(f"[FATAL] {TOKEN_FILE} not found. Run login.py first to generate the token.")
        sys.exit(1)
        
    with open(TOKEN_FILE, "r") as f:
        access_token = f.read().strip()
        
    res = api.set_session(userid=str(USER_ID).strip(), password='', usertoken=access_token)
    limits = api.get_limits()
    
    if not limits or not isinstance(limits, dict) or limits.get('stat') != 'Ok':
        print(f"[FATAL] Token in {TOKEN_FILE} is invalid or expired. Run login.py to refresh it.")
        sys.exit(1)
        
    print("[SUCCESS] Session Authenticated! Limits Verified.")
    
    # 2. Schedule Logic
    now = datetime.now()
    current_mins = now.hour * 60 + now.minute
    
    entry_h, entry_m = map(int, ENTRY_TIME.split(':'))
    exit_h, exit_m = map(int, EXIT_TIME.split(':'))
    entry_mins = entry_h * 60 + entry_m
    exit_mins = exit_h * 60 + exit_m
    
    if current_mins < entry_mins:
        print(f"Server booted early. Waiting until Entry Time ({ENTRY_TIME})...")
        wait_until(ENTRY_TIME)
        enter_iron_condor()
    elif current_mins >= entry_mins and current_mins < exit_mins:
        print(f"Server booted during market hours. Entering strategy immediately!")
        enter_iron_condor()
        
    if current_mins < exit_mins:
        print(f"Strategy active. Sleeping until Exit Time ({EXIT_TIME})...")
        wait_until(EXIT_TIME)
        
    # 3. Square Off
    exit_all_positions(api)
    print("\n[SUCCESS] Day complete. Shutting down autobot.")

if __name__ == "__main__":
    main()
