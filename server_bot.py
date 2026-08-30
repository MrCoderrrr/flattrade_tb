import os
import time
from datetime import datetime, timedelta
from api_helper import NorenApiPy
import pandas as pd

api = NorenApiPy()

# ==============================================================================
# 1. CORE EXECUTION HELPER FUNCTIONS
# ==============================================================================

def place_leg(api_instance, symbol, buy_or_sell, quantity, product_type="M", price_type="MKT", price=0, exchange="NFO"):
    """
    Places an individual order leg and returns the API response.
    """
    try:
        res = api_instance.place_order(
            buy_or_sell=str(buy_or_sell),       # 'B' or 'S'
            product_type=str(product_type),     # 'M' for NRML, 'I' for MIS
            exchange=str(exchange),             # 'NFO' for options/futures
            tradingsymbol=str(symbol),          # e.g., 'NIFTY28MAR24C22000'
            quantity=str(quantity),
            discloseqty="0",
            price_type=str(price_type),         # 'MKT' or 'LMT'
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
                print(f"[FAILED] {symbol}: No response from API (res is None). Market might be closed.")
            else:
                print(f"[FAILED] {symbol}: {res.get('emsg', 'Unknown Error')}")
            return None
    except Exception as e:
        print(f"[ERROR] Exception placing leg {symbol}: {e}")
        return None

def execute_multi_leg_strategy(api_instance, legs):
    """
    Executes a list of strategy legs sequentially.
    Hedges (Buys) should generally be placed before Shorts (Sells) for margin benefits.
    """
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
        
        # Safety Guard: If a Hedge (Buy) fails, abort placing the naked shorts!
        if leg['action'] == 'B' and (res is None or res.get('stat') != 'Ok'):
            print(f"[CRITICAL] Hedge leg {leg['symbol']} failed to place! Aborting the rest of the strategy to prevent naked shorts.")
            break
            
        # Brief pause between order requests to prevent API rate limiting
        time.sleep(0.2)
        
    return results

# ==============================================================================
# 2. V2 STRATEGY LOGIC (Strike Selection)
# ==============================================================================

def get_nifty_spot(api_instance):
    """Fetches the latest NIFTY 50 spot price."""
    print("Fetching NIFTY spot price...")
    try:
        # 26000 is the token for NIFTY 50 on NSE
        res = api_instance.get_quotes(exchange='NSE', token='26000')
        if res and isinstance(res, dict) and res.get('stat') == 'Ok':
            spot = float(res.get('lp', res.get('ltp', 0)))
            return spot
        else:
            print("Failed to fetch NIFTY spot:", res)
            return None
    except Exception as e:
        print(f"Exception fetching spot: {e}")
        return None

def get_recent_atr(api_instance, default=35.0):
    """Fetches recent 5m candles to calculate a basic ATR, falls back to default on failure."""
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(days=5) # Get a few days of data
        res = api_instance.get_time_price_series(
            exchange='NSE', 
            token='26000', 
            starttime=start_time.timestamp(), 
            endtime=end_time.timestamp(), 
            interval=5
        )
        if res and isinstance(res, list) and len(res) > 0:
            df = pd.DataFrame(res)
            df['intc'] = pd.to_numeric(df['intc'])
            df['inth'] = pd.to_numeric(df['inth'])
            df['intl'] = pd.to_numeric(df['intl'])
            # Simple ATR approx (High - Low average over last 14 candles)
            recent_candles = df.head(14)
            tr = recent_candles['inth'] - recent_candles['intl']
            calculated_atr = tr.mean()
            print(f"Dynamically Calculated 14-period 5m ATR: {calculated_atr:.2f}")
            return max(15.0, calculated_atr) # Floor of 15
    except Exception as e:
        print(f"Could not calculate dynamic ATR ({e}). Falling back to default: {default}")
    
    return default

def get_safe_strikes(spot, atr=35.0, min_width=0, max_width=0, atr_multiplier=1.0):
    """
    V2 Strategy Mathematical Strike-Collision Barrier
    CE_Strike = round_50(ATM_Spot + max(MIN_WIDTH, ATR_Width))
    PE_Strike = round_50(ATM_Spot - max(MIN_WIDTH, ATR_Width))
    """
    atm_spot = int(round(spot / 50.0) * 50)
    
    calculated_width = max(min_width, min(max_width if max_width > 0 else float('inf'), atr_multiplier * atr))
    
    # Round width to nearest 50-point strike interval
    stride_50 = int(round(calculated_width / 50.0) * 50)
    
    ce_strike = atm_spot + stride_50
    pe_strike = atm_spot - stride_50
    
    # Mathematical verification assert (Fixes ATM Straddle Collapse)
    if ce_strike <= atm_spot or pe_strike >= atm_spot or ce_strike == pe_strike:
        ce_strike = atm_spot + 50
        pe_strike = atm_spot - 50
        
    return atm_spot, ce_strike, pe_strike

def find_option_symbol(api_instance, strike, option_type, expiry_prefix="NIFTY"):
    """
    Searches Flattrade for the exact option trading symbol for a given strike.
    Returns a dictionary with 'tsym' and 'ls' (Lot Size), or None.
    """
    search_text = f"{expiry_prefix} {strike} {option_type}"
    print(f"Searching symbol for: {search_text}")
    res = api_instance.searchscrip(exchange='NFO', searchtext=search_text)
    
    if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
        opt_char = option_type[0] # 'C' or 'P'
        expected_suffix = f"{opt_char}{strike}"
        
        valid_candidates = []
        for item in res['values']:
            if item['tsym'].endswith(expected_suffix) and 'exd' in item:
                # Parse date to sort safely
                try:
                    dt = datetime.strptime(item['exd'], "%d-%b-%Y")
                    valid_candidates.append({'item': item, 'dt': dt})
                except ValueError:
                    continue
                    
        if valid_candidates:
            # Sort by nearest expiry date
            valid_candidates.sort(key=lambda x: x['dt'])
            best_match = valid_candidates[0]['item']
            print(f"  -> Selected Nearest Expiry: {best_match['tsym']} (Lot Size: {best_match['ls']})")
            return {'tsym': best_match['tsym'], 'ls': int(best_match['ls'])}
            
        print(f"  -> No valid candidates ended with {expected_suffix} and had 'exd'.")
        return None
    else:
        print(f"Could not find symbol for {search_text}: {res}")
        return None

# ==============================================================================
# 3. MAIN EXECUTION FLOW
# ==============================================================================

def authenticate_with_request_code(request_code):
    import hashlib
    import requests
    from creds import API_KEY, API_SECRET
    
    raw_token_str = f"{str(API_KEY).strip()}{request_code}{str(API_SECRET).strip()}"
    token_hash = hashlib.sha256(raw_token_str.encode('utf-8')).hexdigest()
    
    url = "https://authapi.flattrade.in/trade/apitoken"
    payload = {
        "api_key": str(API_KEY).strip(),
        "request_code": request_code,
        "api_secret": token_hash
    }
    
    print("Exchanging request_code for access_token via Flattrade API...")
    auth_resp = requests.post(url, json=payload)
    if auth_resp.status_code == 200:
        access_token = auth_resp.json().get("token")
        if not access_token:
            print("Failed to get token:", auth_resp.text)
            return None
        return access_token
    else:
        print(f"Error authenticating: {auth_resp.text}")
        return None

def check_market_hours():
    """Returns True if the market is currently open."""
    now = datetime.now()
    if now.weekday() >= 5: # 5=Saturday, 6=Sunday
        print("[WARNING] Today is a weekend. The NSE market is closed.")
        return False
        
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    if now < market_open or now > market_close:
        print(f"[WARNING] Outside market hours (Current time: {now.strftime('%H:%M')}). Orders may be rejected.")
        return False
        
    return True

def run_v2_iron_condor():
    from creds import API_KEY, USER_ID
    # 1. Ask user for request_code (generated by manually logging in on PC)
    print("\n" + "="*50)
    print("FLATTRADE SERVER BOT (MANUAL PC-LOGIN MODE)")
    print("="*50)
    
    if not check_market_hours():
        proceed = input("Market appears closed. Do you still want to proceed for testing? (y/n): ")
        if proceed.lower() != 'y':
            return
            
    print("\nStep 1: On your local PC browser, click this exact link and log in:")
    print(f"        https://auth.flattrade.in/?app_key={str(API_KEY).strip()}")
    print("\nStep 2: After logging in, you will be redirected to a blank/error page.")
    print("Step 3: Look at the URL in your browser, and copy ALL the characters after 'code='")
    
    request_code = input("\nPaste your request_code here: ").strip()
    
    if not request_code:
        print("Aborting: No request_code provided.")
        return
        
    access_token = authenticate_with_request_code(request_code)
    
    if not access_token:
        print("Aborting: Could not generate access token.")
        return
        
    print("\nSession Authenticated Successfully!")
    from creds import USER_ID
    # set_session automatically verifies the token and prepares the NorenApi wrapper
    res = api.set_session(
        userid=str(USER_ID).strip(),
        password='',
        usertoken=access_token
    )
    
    print("Session Active! Getting limits to verify...")
    limits = api.get_limits()
    if not limits or not isinstance(limits, dict) or limits.get('stat') != 'Ok':
        print("[ERROR] Failed to fetch account limits. Session might be invalid or market is completely offline.")
        print("Limits response:", limits)
        return
    print("Limits verified.")
    
    # 2. Get Market Data
    spot = get_nifty_spot(api)
    if not spot or spot < 10000:
        print(f"Aborting: Could not retrieve a valid NIFTY Spot Price (Got: {spot}).")
        return
        
    print(f"Current NIFTY Spot: {spot}")
    
    # 3. Apply V2 Strategy Logic for Strikes
    # Dynamically calculate ATR
    atr_value = get_recent_atr(api, default=35.0)
    hedge_width = 1000 # 1000 pts OTM for protective legs
    
    atm, short_ce_strike, short_pe_strike = get_safe_strikes(
        spot=spot, 
        atr=atr_value, 
        min_width=0, 
        atr_multiplier=1.0
    )
    
    long_ce_strike = atm + hedge_width
    long_pe_strike = atm - hedge_width
    
    print(f"--- V2 Strategy Strike Calculation ---")
    print(f"ATM: {atm}")
    print(f"Short PE Strike: {short_pe_strike} | Short CE Strike: {short_ce_strike}")
    print(f"Long PE Strike (Hedge): {long_pe_strike} | Long CE Strike (Hedge): {long_ce_strike}")
    
    # 4. Resolve exact trading symbols from Flattrade
    search_prefix = "NIFTY" 
    
    long_pe_data = find_option_symbol(api, long_pe_strike, 'PE', search_prefix)
    long_ce_data = find_option_symbol(api, long_ce_strike, 'CE', search_prefix)
    short_pe_data = find_option_symbol(api, short_pe_strike, 'PE', search_prefix)
    short_ce_data = find_option_symbol(api, short_ce_strike, 'CE', search_prefix)
    
    if not all([long_pe_data, long_ce_data, short_pe_data, short_ce_data]):
        print("Aborting: Failed to resolve all option symbols and lot sizes.")
        return
        
    # 5. Define Iron Condor Legs
    # Sequence: Buy Hedges first to reduce margin, then Sell Shorts
    product = "M" # NRML
    
    iron_condor_legs = [
        {'symbol': long_pe_data['tsym'],  'action': 'B', 'qty': long_pe_data['ls'], 'product': product},
        {'symbol': long_ce_data['tsym'],  'action': 'B', 'qty': long_ce_data['ls'], 'product': product},
        {'symbol': short_pe_data['tsym'], 'action': 'S', 'qty': short_pe_data['ls'], 'product': product},
        {'symbol': short_ce_data['tsym'], 'action': 'S', 'qty': short_ce_data['ls'], 'product': product},
    ]
    
    # 6. Execute Trades
    results = execute_multi_leg_strategy(api, iron_condor_legs)
    print("\nExecution Report:")
    for r in results:
        print(f"Leg: {r['leg']['action']} {r['leg']['symbol']} -> Response: {r['response']}")

if __name__ == "__main__":
    run_v2_iron_condor()
