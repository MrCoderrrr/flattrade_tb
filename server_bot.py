import os
import time
from datetime import datetime
from auto_login import api

# ==============================================================================
# 1. CORE EXECUTION HELPER FUNCTIONS
# ==============================================================================

def place_leg(api_instance, symbol, buy_or_sell, quantity, product_type="M", price_type="MKT", price=0, exchange="NFO"):
    """
    Places an individual order leg and returns the API response.
    """
    try:
        res = api_instance.place_order(
            buy_or_sell=buy_or_sell,       # 'B' or 'S'
            product_type=product_type,     # 'M' for NRML, 'I' for MIS
            exchange=exchange,             # 'NFO' for options/futures
            tradingsymbol=symbol,          # e.g., 'NIFTY28MAR24C22000'
            quantity=quantity,
            discloseqty=0,
            price_type=price_type,         # 'MKT' or 'LMT'
            price=price,
            trigger_price=0,
            retention='DAY',
            remarks='API_Strategy_Leg'
        )
        if res and res.get('stat') == 'Ok':
            print(f"[SUCCESS] {buy_or_sell} {quantity} x {symbol} | Order No: {res.get('norenordno')}")
            return res
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
        if res and res.get('stat') == 'Ok':
            spot = float(res.get('lp', res.get('ltp', 0)))
            return spot
        else:
            print("Failed to fetch NIFTY spot:", res)
            return None
    except Exception as e:
        print(f"Exception fetching spot: {e}")
        return None

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
    Option type: 'CE' or 'PE'
    Note: For production, you may want to refine the searchtext to target a specific expiry.
    """
    search_text = f"{expiry_prefix} {strike} {option_type}"
    print(f"Searching symbol for: {search_text}")
    res = api_instance.searchscrip(exchange='NFO', searchtext=search_text)
    
    if res and res.get('stat') == 'Ok' and 'values' in res:
        # Take the first matching instrument (usually the nearest expiry if sorted, 
        # but you should verify expiry matching in a robust system)
        return res['values'][0]['tsym']
    else:
        print(f"Could not find symbol for {search_text}: {res}")
        return None

# ==============================================================================
# 3. MAIN EXECUTION FLOW
# ==============================================================================

def run_v2_iron_condor():
    # 1. Ask user for token (generated via pc_login.py)
    print("\n" + "="*50)
    print("FLATTRADE SERVER BOT (NO-SELENIUM MODE)")
    print("="*50)
    access_token = input("Paste your Access Token here: ").strip()
    
    if not access_token:
        print("Aborting: No token provided.")
        return
        
    print("\nAuthenticating session...")
    from creds import USER_ID
    # set_session automatically verifies the token and prepares the NorenApi wrapper
    res = api.set_session(
        userid=str(USER_ID).strip(),
        password='',
        usertoken=access_token
    )
    
    print("Session Active! Getting limits to verify...")
    print(api.get_limits())
    
    # 2. Get Market Data
    spot = get_nifty_spot(api)
    if not spot:
        print("Aborting: Could not retrieve Spot Price.")
        return
        
    print(f"Current NIFTY Spot: {spot}")
    
    # 3. Apply V2 Strategy Logic for Strikes
    # Using defaults representing standard ADX Trend/Chop widths from V2 logic
    atr_value = 35.0  # In a live bot, you'd calculate ATR from 5m candles
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
    # Replace 'NIFTY' with a specific prefix if needed, e.g., 'NIFTY 28 MAR'
    search_prefix = "NIFTY" 
    
    long_pe_sym = find_option_symbol(api, long_pe_strike, 'PE', search_prefix)
    long_ce_sym = find_option_symbol(api, long_ce_strike, 'CE', search_prefix)
    short_pe_sym = find_option_symbol(api, short_pe_strike, 'PE', search_prefix)
    short_ce_sym = find_option_symbol(api, short_ce_strike, 'CE', search_prefix)
    
    if not all([long_pe_sym, long_ce_sym, short_pe_sym, short_ce_sym]):
        print("Aborting: Failed to resolve all option symbols.")
        return
        
    # 5. Define Iron Condor Legs
    # Sequence: Buy Hedges first to reduce margin, then Sell Shorts
    qty = 75 # Lot size
    product = "M" # NRML
    
    iron_condor_legs = [
        {'symbol': long_pe_sym,  'action': 'B', 'qty': qty, 'product': product},
        {'symbol': long_ce_sym,  'action': 'B', 'qty': qty, 'product': product},
        {'symbol': short_pe_sym, 'action': 'S', 'qty': qty, 'product': product},
        {'symbol': short_ce_sym, 'action': 'S', 'qty': qty, 'product': product},
    ]
    
    # 6. Execute Trades
    results = execute_multi_leg_strategy(api, iron_condor_legs)
    print("\nExecution Report:")
    for r in results:
        print(f"Leg: {r['leg']['action']} {r['leg']['symbol']} -> Response: {r['response']}")

if __name__ == "__main__":
    run_v2_iron_condor()
