import pandas as pd
import glob
import os
import numpy as np
from datetime import datetime

INITIAL_SL_PCT = 0.25
TRAIL_ACTIVATION_PCT = 0.25
TRAIL_DISTANCE_PCT = 0.15
LOT_SIZE = 25

def get_pnl(day_file):
    df = pd.read_csv(day_file)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    df = df.sort_values(['Timestamp', 'Strike'])
    
    times = df['Timestamp'].unique()
    # Filter to only times >= 09:16
    times = [t for t in times if pd.to_datetime(t).time() >= pd.to_datetime("09:16").time()]
    if len(times) == 0: return 0.0
    
    entry_time = times[0]
    df_entry = df[df['Timestamp'] == entry_time]
    
    spot = df_entry['Spot'].iloc[0]
    atm = int(round(spot / 50.0) * 50)
    
    entry_ce_row = df_entry[df_entry['Strike'] == atm]
    entry_pe_row = df_entry[df_entry['Strike'] == atm]
    
    if len(entry_ce_row) == 0: return 0.0
    
    ce_entry_price = entry_ce_row['CE_LTP'].iloc[0]
    pe_entry_price = entry_pe_row['PE_LTP'].iloc[0]
    
    if ce_entry_price <= 0 or pe_entry_price <= 0:
        return 0.0
        
    pos = {
        'CE': {'price': ce_entry_price, 'lowest': ce_entry_price, 'armed': False, 'stop': ce_entry_price * (1 + INITIAL_SL_PCT), 'active': True},
        'PE': {'price': pe_entry_price, 'lowest': pe_entry_price, 'armed': False, 'stop': pe_entry_price * (1 + INITIAL_SL_PCT), 'active': True}
    }
    
    pnl = 0.0
    
    for t in times[1:]:
        df_t = df[df['Timestamp'] == t]
        if pd.to_datetime(t).hour >= 15 and pd.to_datetime(t).minute >= 15:
            for leg in ['CE', 'PE']:
                if pos[leg]['active']:
                    row = df_t[df_t['Strike'] == atm]
                    if len(row) > 0:
                        exit_price = row[f'{leg}_LTP'].iloc[0]
                        pnl += (pos[leg]['price'] - exit_price) * LOT_SIZE
                        pos[leg]['active'] = False
            break
            
        for leg in ['CE', 'PE']:
            if not pos[leg]['active']: continue
            row = df_t[df_t['Strike'] == atm]
            if len(row) == 0: continue
            
            ltp = row[f'{leg}_LTP'].iloc[0]
            if ltp <= 0: continue
            p = pos[leg]
            
            if ltp < p['lowest']:
                p['lowest'] = ltp
            if not p['armed'] and p['lowest'] <= p['price'] * (1 - TRAIL_ACTIVATION_PCT):
                p['armed'] = True
            if p['armed']:
                new_stop = p['lowest'] * (1 + TRAIL_DISTANCE_PCT)
                if new_stop < p['stop']:
                    p['stop'] = new_stop
                    
            if ltp >= p['stop']:
                pnl += (p['price'] - ltp) * LOT_SIZE
                p['active'] = False
                
    return pnl

files = sorted(glob.glob("/Users/vanshilpatel/Desktop/ai trading tradingview/tradingbot/data/option_chain/nifty_oc_*.csv"))
total_pnl = 0.0
print("=== DAY-WISE PNL (Naked ATM Straddle at 09:16 + Premium TSL) ===")
for f in files:
    date_str = f.split("_")[-1].replace(".csv", "")
    pnl = get_pnl(f)
    total_pnl += pnl
    print(f"{date_str}: ₹{pnl:,.2f}")
print(f"-----------------------------")
print(f"Total PnL: ₹{total_pnl:,.2f}")
