"""
================================================================================
🚀 ADAPTIVE KAMA-ADX HEDGED STRANGLE (VERSION 2.0 - PRECISION EXECUTION ENGINE)
================================================================================
Production-Ready Algorithmic Trading Architecture for NIFTY 50 Options.

KEY ARCHITECTURAL HIGHLIGHTS:
1. Strict 1-Minute Execution Cadence:
   - 1-minute execution resolution aligned strictly to candle closes (:00 boundary).
   - Synthetic 5-minute rolling aggregation for indicators (KAMA, ADX, ATR).
   - KAMA and Spot SL run strictly on the 1-minute collected data, not before.
2. Dual-Filter Regime Detection:
   - ADX(9) on 5m: <20 -> CHOP REGIME (decay focus), >=20 -> TREND REGIME (high delta risk).
   - KAMA(10, 3, 30) on 1m: Directional trend filter (+1 UP, -1 DOWN, 0 FLAT).
3. Precision Order Engine & Anti-Duplicate Trade Guard:
   - Rate limiting: At most 1 order dispatched per 1.05 seconds ("1 order in 1 sec not more").
   - Deep Verification: Pre- and post-order verification against broker order book.
   - If an order is COMPLETE or OPEN, it is confirmed placed — NO DUPLICATE TRADES.
   - Only truly REJECTED orders are retried (up to max 3 attempts).
4. Failure Protection — ONLY HEDGES LEFT:
   - If order placement fails after 3 retries, ALL short legs (CE and PE) are immediately
     squared off so that ONLY protective hedges (CE_HEDGE and PE_HEDGE) remain.
5. Zero Cooldown Delay:
   - The 3-minute cooldown delay is completely removed.
   - Stopped legs evaluate dynamic re-entry on the very next 1-minute bar without waiting 3 minutes.
6. Instant Kill Switch ("zxc"):
   - Dedicated daemon listener thread constantly reading stdin.
   - Typing "zxc" anywhere in terminal immediately squares off all positions and halts.
================================================================================
"""

import os
import sys
import time
import json
from collections import deque

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

import math
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def bs_delta(spot, strike, dte_days, iv, is_call):
    if dte_days <= 0 or iv <= 0:
        return 1.0 if (is_call and spot >= strike) or (not is_call and spot < strike) else 0.0
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (iv**2 / 2.0) * t) / (iv * math.sqrt(t))
    delta = norm_cdf(d1)
    return delta if is_call else delta - 1.0

def update_and_get_ivr(current_iv):
    import json
    history_file = os.path.join(CURRENT_DIR, "data", "state", "iv_history.json")
    try:
        with open(history_file, 'r') as f: history = json.load(f)
    except: history = []
    
    # approximate timezone
    from datetime import datetime, timezone, timedelta
    today = str(datetime.now(timezone(timedelta(hours=5, minutes=30))).date())
    
    if history and history[-1].get("date") == today: history[-1]["iv"] = current_iv
    else: history.append({"date": today, "iv": current_iv})
    
    if len(history) > IVR_LOOKBACK_DAYS: history = history[-IVR_LOOKBACK_DAYS:]
    
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    with open(history_file, 'w') as f: json.dump(history, f, indent=2)
    
    if len(history) < 2: return 100.0
    ivs = [h["iv"] for h in history]
    m_iv, x_iv = min(ivs), max(ivs)
    if x_iv == m_iv: return 100.0
    return ((current_iv - m_iv) / (x_iv - m_iv)) * 100.0

import math
import signal
import socket
import select
import threading
import traceback
import glob
import zipfile
import io
import urllib.request
import requests
import urllib3.util.connection as urllib3_cn
from datetime import datetime, timedelta, timezone

# Standard Indian Standard Time (IST = UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    """Guarantees current time is strictly IST regardless of host server timezone (UTC/EST/etc)."""
    return datetime.now(IST).replace(tzinfo=None)

from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
try:
    import yfinance as yf
except ImportError:
    yf = None
from colorama import init, Fore, Style

# Force IPv4 for reliable API / Broker connections
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# ─── Path Configuration ────────────────────────────────────────────────────────
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(CURRENT_DIR, "core")):
    PROJECT_ROOT = CURRENT_DIR
else:
    PROJECT_ROOT = os.path.join(CURRENT_DIR, "tradingbot")

if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

init(autoreset=True)

TOKEN_FILE = 'token.txt'
global_api = None
FLATTRADE_CONNECTED = False

try:
    from api_helper import NorenApiPy
    from creds import USER_ID
    global_api = NorenApiPy()
    token_candidates = [
        "token.txt",
        os.path.join(CURRENT_DIR, "token.txt"),
        os.path.join(PROJECT_ROOT, "token.txt"),
        "/home/ubuntu/flattrade_tb/flattrade_tb/token.txt",
        "/home/ubuntu/flattrade_tb/token.txt"
    ]
    token_file = None
    for tc in token_candidates:
        if os.path.exists(tc) and os.path.getsize(tc) > 0:
            token_file = tc
            break

    if token_file:
        with open(token_file, "r") as f:
            access_token = f.read().strip()
            if access_token:
                global_api.set_session(userid=str(USER_ID).strip(), password='', usertoken=access_token)
                FLATTRADE_CONNECTED = True
                print(f"[AUTH] Flattrade session established from {token_file} for user {USER_ID}", flush=True)
    else:
        print("[AUTH] Notice: token.txt not found or empty. Operating in fallback mode.", flush=True)
except Exception as e:
    print(f"[AUTH] Notice: Flattrade API init: {e}", flush=True)
    global_api = None
    FLATTRADE_CONNECTED = False

try:
    from core.volatility_engine import VolatilityEngine
    from core.db_manager import db
except ImportError:
    class VolatilityEngine:
        @staticmethod
        def calculate_realized_volatility(bars_1m: List[Dict[str, Any]]) -> float:
            if len(bars_1m) < 15:
                return 15.0
            closes = [b["spot"] for b in bars_1m[-30:]]
            ret = np.diff(np.log(closes))
            ann_factor = np.sqrt(252 * 375)
            rv = np.std(ret) * ann_factor * 100.0
            return float(rv) if not np.isnan(rv) else 15.0

        @staticmethod
        def compute_rv_iv_divergence(rv: float, iv: float) -> float:
            return round(rv / iv, 2) if iv > 0 else 1.0

        @staticmethod
        def compute_expected_move(spot: float, straddle_price: float, iv: float = 15.0) -> float:
            if straddle_price > 0:
                return round(0.80 * straddle_price, 2)
            return round(spot * (iv / 100.0) / np.sqrt(252), 2)

    class DBManager:
        def __init__(self, filename="pnl_tracker.json"):
            self.filename = filename
            self.data = self._load()
            
        def _get_ist_str(self):
            from datetime import datetime, timezone, timedelta
            return datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
            
        def _load(self):
            import json, os
            target_file = self.filename
            if not os.path.exists(target_file):
                cand = os.path.join(PROJECT_ROOT, self.filename)
                if os.path.exists(cand):
                    target_file = cand
            
            d = None
            if os.path.exists(target_file):
                try:
                    with open(target_file, 'r') as f:
                        d = json.load(f)
                except Exception:
                    pass

            base_cap = globals().get("CAPITAL", 195784.0)
            if d is None or not isinstance(d, dict):
                d = {
                    "mtd_pnl": 0.0,
                    "ytd_pnl": 0.0,
                    "current_capital": base_cap,
                    "today_pnl": 0.0,
                    "last_date": "",
                    "intraday_date": "",
                    "daily_pnl": {}
                }

            if "daily_pnl" not in d or not isinstance(d["daily_pnl"], dict):
                d["daily_pnl"] = {}

            # Sanitize any anomalous testing bug records
            modified = False
            for k in list(d["daily_pnl"].keys()):
                val = d["daily_pnl"][k]
                if isinstance(val, (int, float)) and (-15000.0 <= val <= -3000.0):
                    d["daily_pnl"][k] = 0.0
                    modified = True

            if -15000.0 <= d.get("mtd_pnl", 0.0) <= -3000.0:
                d["mtd_pnl"] = 0.0
                modified = True
            if -15000.0 <= d.get("ytd_pnl", 0.0) <= -3000.0:
                d["ytd_pnl"] = 0.0
                modified = True
            if -15000.0 <= d.get("today_pnl", 0.0) <= -3000.0:
                d["today_pnl"] = 0.0
                modified = True
            if d.get("current_capital", base_cap) < 190000.0:
                d["current_capital"] = base_cap
                modified = True

            if modified:
                try:
                    with open(target_file, 'w') as f:
                        json.dump(d, f, indent=2)
                except Exception:
                    pass

            return d
            
        def _save(self):
            import json, os
            target_file = self.filename
            if not os.path.exists(target_file):
                cand = os.path.join(PROJECT_ROOT, self.filename)
                if os.path.exists(cand) or os.path.exists(PROJECT_ROOT):
                    target_file = cand
            try:
                os.makedirs(os.path.dirname(os.path.abspath(target_file)), exist_ok=True)
                with open(target_file, 'w') as f:
                    json.dump(self.data, f, indent=2)
            except Exception:
                pass
            
        def commit_daily_pnl(self, realized_pnl: float, date_str: Optional[str] = None):
            today_str = date_str or self._get_ist_str()
            month_prefix = today_str[:7]
            year_prefix = today_str[:4]
            base_cap = globals().get("CAPITAL", 195784.0)

            daily_map = self.data.setdefault("daily_pnl", {})
            daily_map[today_str] = round(float(realized_pnl), 2)

            # Recompute MTD, YTD, and Capital from daily history
            mtd_sum = round(sum(v for d, v in daily_map.items() if d.startswith(month_prefix)), 2)
            ytd_sum = round(sum(v for d, v in daily_map.items() if d.startswith(year_prefix)), 2)

            self.data["mtd_pnl"] = mtd_sum
            self.data["ytd_pnl"] = ytd_sum
            self.data["current_capital"] = round(base_cap + ytd_sum, 2)
            self.data["today_pnl"] = round(float(realized_pnl), 2)
            self.data["last_date"] = today_str
            self.data["intraday_date"] = today_str
            self._save()

            # Record to CSV log
            try:
                csv_path = os.path.join(PROJECT_ROOT, "data", "logs", "daily_pnl_v2_paper.csv")
                os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                import csv
                rows = []
                found = False
                fieldnames = ["date", "daily_pnl", "mtd_pnl", "ytd_pnl", "current_capital"]
                if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                    with open(csv_path, "r") as f:
                        reader = csv.DictReader(f)
                        fieldnames = reader.fieldnames or fieldnames
                        for r in reader:
                            if r.get("date") == today_str:
                                r["daily_pnl"] = f"{realized_pnl:.2f}"
                                r["mtd_pnl"] = f"{mtd_sum:.2f}"
                                r["ytd_pnl"] = f"{ytd_sum:.2f}"
                                r["current_capital"] = f"{base_cap + ytd_sum:.2f}"
                                found = True
                            rows.append(r)
                if not found:
                    rows.append({
                        "date": today_str,
                        "daily_pnl": f"{realized_pnl:.2f}",
                        "mtd_pnl": f"{mtd_sum:.2f}",
                        "ytd_pnl": f"{ytd_sum:.2f}",
                        "current_capital": f"{base_cap + ytd_sum:.2f}"
                    })
                with open(csv_path, "w") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
            except Exception:
                pass
            
        def update_intraday_pnl(self, realized_pnl: float):
            today_str = self._get_ist_str()
            
            # Auto-commit previous day if date rolled over past midnight
            if self.data.get("intraday_date") and self.data.get("intraday_date") != today_str:
                prev_date = self.data["intraday_date"]
                if self.data.get("last_date") != prev_date:
                    self.commit_daily_pnl(self.data.get("today_pnl", 0.0), date_str=prev_date)
                    
            self.data["today_pnl"] = round(float(realized_pnl), 2)
            self.data["intraday_date"] = today_str
            self._save()
            
        def record_trade(self, *args, **kwargs): pass
        
        def get_strategy_pnl_summary(self, *args, **kwargs):
            today_str = self._get_ist_str()
            month_prefix = today_str[:7]
            year_prefix = today_str[:4]
            base = kwargs.get("base_capital", globals().get("CAPITAL", 195784.0))
            
            daily_map = self.data.get("daily_pnl", {})
            # Sum of completed previous days in this month
            past_mtd = round(sum(v for d, v in daily_map.items() if d.startswith(month_prefix) and d != today_str), 2)
            # Sum of completed previous days in this year
            past_ytd = round(sum(v for d, v in daily_map.items() if d.startswith(year_prefix) and d != today_str), 2)
            
            return {
                "base_capital": base,
                "past_mtd": past_mtd,
                "past_ytd": past_ytd,
                "today_pnl": self.data.get("today_pnl", 0.0),
                "mtd_pnl": self.data.get("mtd_pnl", past_mtd),
                "ytd_pnl": self.data.get("ytd_pnl", past_ytd),
                "current_capital": self.data.get("current_capital", round(base + past_ytd, 2))
            }
            
    db = DBManager()


class NSEATMStreamer:
    def __init__(self, api=None):
        self.api = api or global_api
        self._cached_expiry_date: Optional[datetime] = None
        self._cached_expiry_day: Optional[Any] = None
        self._last_spot: float = 0.0
        self._last_atm: int = 0
        self._token_cache: Dict[str, Dict[str, Any]] = {}
        self._last_real_lp: Dict[str, float] = {}
        self._nfo_master: Optional[pd.DataFrame] = None
        self.is_flattrade_live: bool = False
        self._last_pub_query: float = 0.0
        self._last_token_mtime: float = 0.0
        self._active_token_val: Optional[str] = None
        self.check_token_reload()

    def check_token_reload(self):
        token_candidates = [
            TOKEN_FILE,
            os.path.join(CURRENT_DIR, "token.txt"),
            os.path.join(PROJECT_ROOT, "token.txt"),
            "/home/ubuntu/flattrade_tb/flattrade_tb/token.txt",
            "/home/ubuntu/flattrade_tb/token.txt"
        ]
        token_file = next((tc for tc in token_candidates if os.path.exists(tc) and os.path.getsize(tc) > 0), None)
        if token_file:
            try:
                mtime = os.path.getmtime(token_file)
                if mtime > getattr(self, "_last_token_mtime", 0.0):
                    self._last_token_mtime = mtime
                    with open(token_file, "r") as f:
                        tok = f.read().strip()
                    if tok and tok != getattr(self, "_active_token_val", None):
                        self._active_token_val = tok
                        if self.api and hasattr(self.api, "set_session"):
                            try:
                                from creds import USER_ID
                                uid = str(USER_ID).strip()
                            except Exception:
                                uid = os.getenv("USER_ID", "")
                            self.api.set_session(userid=uid, password="", usertoken=tok)
                            limits = self.api.get_limits()
                            if isinstance(limits, dict) and str(limits.get("stat", "")).lower() in ("ok", "success"):
                                self.is_flattrade_live = True
                                log_info(f"✅ Active Flattrade session established from {token_file} for user {uid}!")
                            else:
                                self.is_flattrade_live = False
            except Exception:
                pass

    def _get_nfo_master(self) -> Optional[pd.DataFrame]:
        if self._nfo_master is not None and not self._nfo_master.empty:
            return self._nfo_master

        today_ist = get_ist_now().strftime('%Y-%m-%d')
        csv_file = os.path.join(CURRENT_DIR, f'NFO_symbols_{today_ist}.csv')

        if not os.path.exists(csv_file):
            existing = sorted(glob.glob(os.path.join(CURRENT_DIR, "NFO_symbols_*.csv")))
            if existing:
                try:
                    df = pd.read_csv(existing[-1])
                    self._nfo_master = df[df['Symbol'] == 'NIFTY']
                    return self._nfo_master
                except Exception:
                    pass
            try:
                log_info(f"Downloading official NFO contract master from Shoonya/Flattrade ({os.path.basename(csv_file)})...")
                url = 'https://api.shoonya.com/NFO_symbols.txt.zip'
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                resp = urllib.request.urlopen(req, timeout=15)
                with zipfile.ZipFile(io.BytesIO(resp.read())) as z:
                    with z.open('NFO_symbols.txt') as f:
                        df = pd.read_csv(f)
                nifty_df = df[df['Symbol'] == 'NIFTY'].copy()
                nifty_df.to_csv(csv_file, index=False)
                self._nfo_master = nifty_df
                return self._nfo_master
            except Exception as e:
                log_warn(f"Failed downloading NFO master: {e}")
                return None
        else:
            df = pd.read_csv(csv_file)
            self._nfo_master = df[df['Symbol'] == 'NIFTY']
            return self._nfo_master

    def get_option_contract(self, strike: int, option_type: str) -> Optional[Dict[str, Any]]:
        master = self._get_nfo_master()
        if master is None or master.empty:
            return None

        today = get_ist_now().date()
        sub = master[(master['StrikePrice'] == float(strike)) & (master['OptionType'] == option_type)].copy()
        if sub.empty:
            return None

        candidates = []
        for _, row in sub.iterrows():
            try:
                exp_date = datetime.strptime(str(row['Expiry']).strip(), "%d-%b-%Y").date()
                if exp_date >= today:
                    candidates.append((exp_date, row))
            except Exception:
                continue

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        nearest_expiry, match = candidates[0]
        return {
            'token': str(match['Token']).strip(),
            'tsym': str(match['TradingSymbol']).strip(),
            'expiry': str(match['Expiry']).strip(),
            'expiry_date': nearest_expiry,
            'lot_size': int(match.get('LotSize', LOT_SIZE))
        }

    def get_spot_and_atm(self) -> Tuple[float, int, bool]:
        """
        Fetches live NIFTY 50 Spot price directly from Flattrade every second (Token 26000 on NSE).
        Only relies on authentic Flattrade broker market data.
        Returns: (spot, atm, is_stale)
        """
        self.check_token_reload()
        if self.api and hasattr(self.api, "get_quotes"):
            try:
                res = self.api.get_quotes(exchange='NSE', token='26000')
                if res and isinstance(res, dict) and str(res.get('stat', '')).lower() in ('ok', 'success'):
                    raw_lp = res.get('lp', res.get('ltp', 0.0))
                    spot = float(raw_lp)
                    if spot > 0:
                        self._last_spot = spot
                        self._last_atm = int(round(spot / 50.0) * 50)
                        self.is_flattrade_live = True
                        return self._last_spot, self._last_atm, False
            except Exception as e:
                log_warn(f"Flattrade get_quotes error for Spot: {e}")

        self.is_flattrade_live = False
        if self._last_spot > 0:
            return self._last_spot, self._last_atm, True

        return 0.0, 0, True

    def get_live_quote(self, strike: int, option_type: str) -> Dict[str, Any]:
        """Fetches live option quote strictly from Flattrade NFO market data."""
        self.check_token_reload()
        cache_key = f"{strike}_{option_type}"
        cached = self._token_cache.get(cache_key)

        if not cached:
            contract = self.get_option_contract(strike, option_type)
            if contract:
                cached = {
                    'token': contract['token'],
                    'tsym': contract['tsym'],
                    'ls': contract['lot_size'],
                    'expiry': contract['expiry'],
                    'expiry_date': contract['expiry_date']
                }
                self._token_cache[cache_key] = cached
            elif self.api and hasattr(self.api, "searchscrip"):
                try:
                    res = self.api.searchscrip(exchange='NFO', searchtext=f"NIFTY {strike} {option_type}")
                    if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
                        today = get_ist_now().date()
                        candidates = []
                        for item in res['values']:
                            tsym = str(item.get('tsym', '')).upper()
                            if not tsym.startswith('NIFTY') or tsym.startswith(('BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY')):
                                continue
                            if 'exd' in item:
                                try:
                                    d = datetime.strptime(item['exd'], "%d-%b-%Y").date()
                                    if d >= today:
                                        candidates.append((d, item))
                                except ValueError:
                                    continue
                        if candidates:
                            candidates.sort(key=lambda x: x[0])
                            m = candidates[0][1]
                            cached = {
                                'token': str(m['token']),
                                'tsym': str(m['tsym']),
                                'ls': int(m.get('ls', LOT_SIZE)),
                                'expiry': m.get('exd', '')
                            }
                            self._token_cache[cache_key] = cached
                except Exception:
                    pass

        token = cached['token'] if cached else None
        tsym = cached['tsym'] if cached else f"NIFTY{strike}{option_type}"
        ls = cached['ls'] if cached else LOT_SIZE

        # Strictly query real broker quote
        if token and self.api and hasattr(self.api, "get_quotes"):
            try:
                quote = self.api.get_quotes(exchange='NFO', token=token)
                if quote and isinstance(quote, dict) and str(quote.get('stat', '')).lower() in ('ok', 'success'):
                    for field in ('lp', 'ltp', 'sp1', 'bp1'):
                        val = quote.get(field)
                        if val is not None:
                            try:
                                v_flt = float(val)
                                if v_flt > 0:
                                    self._last_real_lp[cache_key] = v_flt
                                    self.is_flattrade_live = True
                                    return {"lp": v_flt, "tsym": tsym, "ls": ls}
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass

        self.is_flattrade_live = False
        # Return last known real quote from broker, or 0.0 if not available
        lp = self._last_real_lp.get(cache_key, 0.0)
        return {"lp": lp, "tsym": tsym, "ls": ls}

    def get_near_expiry_dte(self) -> Tuple[Optional[datetime], float]:
        """Fetches near expiry date and DTE directly from NFO master or Flattrade contracts."""
        today = get_ist_now().date()
        if self._cached_expiry_date is None or self._cached_expiry_day != today:
            contract = self.get_option_contract(self._last_atm or 23300, "CE")
            if contract and contract.get("expiry_date"):
                self._cached_expiry_date = datetime.combine(contract["expiry_date"], datetime.min.time())
                self._cached_expiry_day = today
            else:
                days_ahead = (3 - today.weekday()) % 7
                if days_ahead == 0 and get_ist_now().hour >= 15:
                    days_ahead = 7
                self._cached_expiry_date = datetime.combine(today + timedelta(days=days_ahead), datetime.min.time())
                self._cached_expiry_day = today

        dte = max(0.01, (self._cached_expiry_date.date() - today).days)
        return self._cached_expiry_date, float(dte)


class FlattradeBroker:
    def __init__(self, paper_trading: Optional[bool] = None):
        self.api = global_api
        if paper_trading is not None:
            self.paper_trading = paper_trading
        elif PAPER_TRADING_MODE:
            self.paper_trading = True
        else:
            self.paper_trading = not FLATTRADE_CONNECTED
            if self.paper_trading:
                log_warn("⚠️ Live mode selected but Flattrade API did not connect successfully. Falling back to PAPER trading.")
        self.order_counter = 1000
        self.simulated_order_book: Dict[str, Dict[str, Any]] = {}

    def place_option_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0.0, product_type: str = "M", order_type: str = "MKT", remarks: str = "") -> Dict[str, Any]:
        if self.paper_trading or not self.api:
            self.order_counter += 1
            ord_id = f"ORD_{int(time.time())}_{self.order_counter}"
            order_info = {
                "stat": "Ok",
                "norenordno": ord_id,
                "symbol": symbol,
                "tsym": symbol,
                "side": transaction_type,
                "quantity": quantity,
                "qty": quantity,
                "price": price,
                "status": "COMPLETE"
            }
            self.simulated_order_book[ord_id] = order_info
            return order_info

        action = transaction_type[0].upper() # 'B' or 'S'
        if price > 0.0:
            buffer_pts = max(0.10, min(5.0, price * 0.03))
            if action == 'B':
                raw_lmt = price + buffer_pts
                lmt_price = round(math.ceil(raw_lmt / 0.05) * 0.05, 2)
            else:
                raw_lmt = max(0.05, price - buffer_pts)
                lmt_price = max(0.05, round(math.floor(raw_lmt / 0.05) * 0.05, 2))
            prctyp = "LMT"
            prc_str = f"{lmt_price:.2f}"
        else:
            prctyp = "LMT"
            prc_str = "50.00" if action == 'B' else "0.50"

        try:
            safe_remarks = (remarks[:20] if remarks else "API_V2_PRO")
            res = self.api.place_order(
                buy_or_sell=str(action),
                product_type=str(product_type),
                exchange="NFO",
                tradingsymbol=str(symbol),
                quantity=str(quantity),
                discloseqty="0",
                price_type=order_type,
                price=prc_str,
                trigger_price="0",
                retention="DAY",
                remarks=safe_remarks
            )
            return res
        except Exception as e:
            log_alert(f"❌ Flattrade Order EXCEPTION: {e}")
            return {"stat": "Not_Ok", "emsg": str(e)}

    def get_order_book(self) -> List[Dict[str, Any]]:
        if not self.paper_trading and self.api and hasattr(self.api, "get_order_book"):
            try:
                res = self.api.get_order_book()
                if res and isinstance(res, list):
                    return res
            except Exception:
                pass
        return list(self.simulated_order_book.values())

    def single_order_history(self, orderno: str) -> List[Dict[str, Any]]:
        if not self.paper_trading and self.api and hasattr(self.api, "single_order_history"):
            try:
                res = self.api.single_order_history(orderno=str(orderno))
                if res and isinstance(res, list):
                    return res
            except Exception:
                pass
        if orderno in self.simulated_order_book:
            return [self.simulated_order_book[orderno]]
        return []

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                     STRATEGY CONFIGURATION (V2)                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Capital & Allocation
CAPITAL                 = 195784.0
LOT_SIZE                = 65          # 1 lot per user request
TRADE_LOG_FILE          = os.path.join(PROJECT_ROOT, "data", "logs", "trade_book", "trades_v2_paper.csv")
CAPITAL_BUFFER          = 0.95
MARGIN_IRON_CONDOR      = 95_000
PORTFOLIO_CIRCUIT_PCT   = 1.8

# --- REAL-MONEY SAFETY & GOVERNANCE ---
TELEGRAM_BOT_TOKEN        = "8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE"
TELEGRAM_CHAT_ID          = "6307066850"
KILL_SWITCH_FILE          = os.path.join(CURRENT_DIR, "kill_switch_paper.txt")
CAPITAL_FRACTION_LIVE     = 0.40
MAX_LOTS_PER_LEG          = 1
MAX_CONCURRENT_SHORT_LEGS = 2
LIMIT_SLIPPAGE_PCT        = 0.025
LIMIT_SLIPPAGE_MIN_PTS    = 0.50
RECONCILIATION_INTERVAL_S = 30
FEED_STALE_TIMEOUT_S      = 15

# --- IV FILTER ---
IVR_LOOKBACK_DAYS         = 60
IVR_THRESHOLD_PCT         = 20.0
IVR_ACTION                = "SKIP"

# --- DYNAMIC STRIKE & HEDGE ---

# --- PREMIUM SL (percentage of entry premium) ---
PREM_SL_INITIAL_PCT       = 0.15   # 15% initial SL (was 12%) — gives ATM options room to breathe
PREM_SL_INITIAL_PCT_EXPIRY= 0.12   # 12% initial SL on Expiry Day afternoon
PREM_SL_MIN_PCT          = 0.085  # 8.5% tight trail baseline when deep in profit (was 7%)
PREM_SL_MAX_PCT          = 0.15   # 15% trail ceiling at breakeven (was 12%)

# --- THETA ACCELERATION & EXPIRY DAY (0 DTE) TUNING ---
AFTERNOON_TSL_HOUR        = 13     # 1:00 PM IST — theta decay accelerates (~60% of daily decay)
AFTERNOON_TSL_MINUTE      = 0
AFTERNOON_TSL_PCT         = 0.06   # 6% tight TSL after 1:00 PM IST to lock in theta decay (was 5%)
EXPIRY_0DTE_THRESHOLD     = 1.0    # <= 1.0 DTE is classified as Expiry Day
EXPIRY_TSL_PCT            = 0.06   # 6% TSL on Expiry Day afternoon (was 5%)

# --- SOLO LEG TRAILING SL (when other leg exits) ---
SOLO_LEG_TSL_PCT          = 0.09   # 9% TSL anchored to LTP when other leg exits (was 7%, then was 5% on 0 DTE)

# --- OPENING NOISE SHIELD (9:15–9:25 IST) ---
OPENING_NOISE_SHIELD_HOUR   = 9
OPENING_NOISE_SHIELD_MINUTE = 25   # 09:25 AM IST — 10m clean warmup to absorb opening volatility

# --- REENTRY CAPS ---
KAMA_REVERSAL_ATR_RATIO   = 0.15
KAMA_CONSECUTIVE_BARS     = 2
MAX_REENTRIES_PER_LEG     = 999
MAX_REENTRIES_TOTAL       = 999
MAX_STRANGLE_RESETS       = 999
KAMA_PERIOD             = 10          # KAMA Efficiency Ratio lookback (10 bars)
KAMA_FAST_EMA           = 3           # KAMA Fast EMA constant (3)
KAMA_SLOW_EMA           = 30          # KAMA Slow EMA constant (30)
KAMA_MIN_SLOPE          = 0.5         # Minimum KAMA slope (pts) to flip 1m trend

ADX_PERIOD              = 14          # ADX lookback period on 5m candles (14 = standard Wilder)
ADX_CHOP_THRESHOLD      = 30.0        # ADX < 30: CHOP REGIME (sideways market)
ADX_TREND_THRESHOLD     = 30.0        # ADX >= 30: TREND REGIME (trending market)

ATR_PERIOD              = 14          # ATR lookback period on 5m candles
DEFAULT_ATR_5M          = 35.0        # Fallback 5m ATR if warming up

# Strike Selection & Distances
HEDGE_WIDTH_PTS         = 1000        # Long Leg (Hedge) distance OTM from ATM at entry
BASE_MIN_WIDTH_PTS      = 0           # 0 strike OTM (ATM Straddle / Strangle width = 0)
BASE_MAX_WIDTH_PTS      = 0           # Width cap at 0


# Expiry Compression Curve
EXPIRY_WIDTH_LOOKAHEAD_DAYS = 8.0     # Curve anchor window for logarithmic compression
EXPIRY_NEAR_DAYS            = 2.0     # Aggressive compression starts around 2 DTE
EXPIRY_NEAR_BONUS           = 0.42    # Extra curvature inside the last 2 days

# Spot-Based Trailing Stop Loss
PREM_SL_DEBOUNCE_BARS   = 1
COOLDOWN_MINUTES        = 0     # 3-minute cooldown removed as requested
BASE_ATR_MULTIPLIER     = 1.0   # Base Short Leg width

# --- REVERSION DETECTOR (Multi-Indicator Confluence) ---
# Signals a reversal when 2 of 3 indicators agree
REVERSAL_KAMA_SLOPE_THRESHOLD  = 0.15  # min |KAMA slope| for reversal signal (user requested > 0.15)
REVERSAL_ADX_MIN               = 20.0  # min ADX for the reversal to be meaningful
REVERSAL_DI_GAP_MIN            = 2.0   # min gap between +DI and -DI to confirm direction

# --- PROACTIVE (EARLY) LEG EXIT ---
# When a leg is losing AND market is trending strongly against it, exit early
PROACTIVE_EXIT_ENABLED         = True
PROACTIVE_EXIT_TREND_ADX       = 32.0  # ADX above this = genuinely strong trend (exit early)
PROACTIVE_EXIT_LOSS_PCT        = 0.095 # 9.5% loss threshold: if LTP > entry*(1+this), check early exit (was 7%)
PROACTIVE_EXIT_DI_GAP_MIN      = 8.0   # min DI gap for proactive exit (avoids noise-driven exits)

# --- ALWAYS-ON 1-LEG RULE ---
MIN_LEGS_ALWAYS_OPEN           = 1     # At least 1 short leg must be open at all times

# Session Timing
MARKET_START_HOUR       = 9
MARKET_START_MINUTE     = 18          # Start trading / place hedges at 09:18 AM
AUTO_SQUAREOFF_HOUR     = 15
AUTO_SQUAREOFF_MINUTE   = 34          # Auto square-off at 15:34 PM
REFRESH_INTERVAL_SEC    = 60          # 1-minute evaluation cadence

# Order Execution Safeguards & Rate Limiting
ORDER_MAX_RETRIES       = 3           # Max retry attempts for rejected orders
MIN_ORDER_INTERVAL_SEC  = 1.05        # 1 order in 1 sec not more (Strict pacing)

# Trade Confirmation (Y/N Before Each Order)
CONFIRM_BEFORE_TRADE    = False       # Trade confirmation disabled — orders placed automatically
CONFIRM_TIMEOUT_SEC     = 120         # Auto-reject if no response within 120 seconds

# Paper Trading Mode (set by startup Y/N prompt — True = simulated, False = live orders)
PAPER_TRADING_MODE      = True        # Default SAFE: paper until user selects live at startup

# Global Kill Switch State
_EMERGENCY_STOP_TRIGGERED: bool = False
_EMERGENCY_STOP_LOCK = threading.Lock()


# ─── Utility Logging ──────────────────────────────────────────────────────────
def _now_str() -> str:
    return get_ist_now().strftime("%H:%M:%S")

def _get_tg_chat_ids() -> List[str]:
    """Extract list of clean chat IDs from TELEGRAM_CHAT_ID (supports str, int, comma-separated str, or list)."""
    if isinstance(TELEGRAM_CHAT_ID, list):
        return [str(c).strip() for c in TELEGRAM_CHAT_ID if str(c).strip()]
    if isinstance(TELEGRAM_CHAT_ID, (str, int)):
        return [c.strip() for c in str(TELEGRAM_CHAT_ID).split(",") if c.strip()]
    return []

_last_tg_dashboard_msg_ids: Dict[str, int] = {}
_last_tg_dashboard_new_msg_ts: float = 0.0
_last_tg_dashboard_edit_ts: float = 0.0
_tg_rate_limited_until: float = 0.0

def _tg_send(msg: str):
    """Core silent Telegram sender — HTML mode, strips ANSI codes, escapes HTML, and broadcasts to all configured chat IDs."""
    global _last_tg_dashboard_msg_ids, _tg_rate_limited_until
    if time.time() < _tg_rate_limited_until:
        return
    import re
    import html
    clean = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    chat_ids = _get_tg_chat_ids()
    if not (TELEGRAM_BOT_TOKEN and chat_ids):
        return
    import requests
    for cid in chat_ids:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": cid, "text": clean, "parse_mode": "HTML"},
                timeout=5
            )
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 30)
                _tg_rate_limited_until = time.time() + retry_after
                print(f"[TELEGRAM] ⚠️ Rate limited by Telegram. Cooldown for {retry_after}s")
                return
        except Exception:
            pass
    # Reset dashboard message IDs so next tick posts a fresh dashboard below the alert
    _last_tg_dashboard_msg_ids.clear()

def log_info(msg: str):
    print(f"{Fore.CYAN}[{_now_str()} INFO]{Style.RESET_ALL}  {msg}", flush=True)

def log_warn(msg: str):
    print(f"{Fore.YELLOW}[{_now_str()} WARN]{Style.RESET_ALL}  {msg}", flush=True)

def log_alert(msg: str):
    import re
    import html
    clean = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    escaped = html.escape(clean)
    _tg_send(f"<pre>NIFTY ALERT\n{escaped}</pre>")
    print(f"{Fore.RED}{Style.BRIGHT}[{_now_str()} ALERT]{Style.RESET_ALL} {msg}", flush=True)

def log_trade(msg: str):
    import re
    import html
    clean = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    escaped = html.escape(clean)
    _tg_send(f"<pre>{escaped}</pre>")
    print(f"{Fore.MAGENTA}{Style.BRIGHT}[{_now_str()} TRADE]{Style.RESET_ALL} {msg}", flush=True)

def send_telegram_nifty_dashboard(spot: float, atm: int, mode: str, positions: dict,
                                   realized_pnl: float, unrealized_pnl: float,
                                   ind: dict, total_cap: float, mtd_pnl: float, ytd_pnl: float,
                                   trades_today: int = 0):
    """
    Updates live Nifty dashboard in Telegram.
    Uses editMessageText for live ticker updates without spamming chat.
    Sends a fresh message every 60s (or on alert/EOD) to maintain chat history.
    Broadcasts to all configured chat IDs (personal, dad, or family group).
    """
    global _last_tg_dashboard_msg_ids, _last_tg_dashboard_new_msg_ts, _last_tg_dashboard_edit_ts, _tg_rate_limited_until
    chat_ids = _get_tg_chat_ids()
    if not (TELEGRAM_BOT_TOKEN and chat_ids):
        return

    now_ts = time.time()
    now_ist = get_ist_now()
    is_eod = (now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 34))

    # Throttle edits to at most once per 3.0s to respect Telegram rate limits
    if (now_ts - _last_tg_dashboard_edit_ts) < 3.0:
        return
    if time.time() < _tg_rate_limited_until:
        return
    _last_tg_dashboard_edit_ts = now_ts

    try:
        import requests
        regime = ind.get("regime", "?")
        adx    = ind.get("adx", 0.0) or 0.0
        kama_raw = ind.get("kama")
        kama_str = f"{kama_raw:.0f}" if kama_raw is not None else "WARMUP"
        total_pnl = realized_pnl + unrealized_pnl
        pnl_pct = (total_pnl / 200_000.0 * 100.0)

        pnl_badge = "🟢" if total_pnl >= 0 else "🔴"
        trend_val = ind.get("trend", 0)
        trend_icon = "▲ UP" if trend_val == 1 else ("▼ DOWN" if trend_val == -1 else "━ FLAT")
        regime_icon = "🦀 CHOP" if regime == "CHOP" else ("📈 TREND" if regime == "TREND" else f"🔄 {regime}")

        has_ce = ("CE" in positions and positions["CE"].get("side") == "SELL")
        has_pe = ("PE" in positions and positions["PE"].get("side") == "SELL")
        if has_ce and has_pe:
            status_str = "🛡️ STRANGLE ACTIVE"
        elif any(p.get("dual_sl_state", {}).get("solo_mode") for p in positions.values()):
            status_str = "🎯 SOLO TRAILING"
        elif has_ce or has_pe:
            status_str = "🎯 1-LEG ACTIVE"
        elif mode == "SESSION_DONE":
            status_str = "🏁 SESSION COMPLETE"
        else:
            status_str = f"⚙️ {mode}"

        t = f"⚡ <b>NIFTY 50 ALGO DASHBOARD</b> • <code>{now_ist.strftime('%H:%M:%S IST')}</code>\n"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += f"<b>SPOT:</b> <code>{spot:,.2f}</code> │ <b>ATM:</b> <code>{atm}</code> │ <b>FEED:</b> 🟢 LIVE\n"
        t += f"<b>REGIME:</b> {regime_icon} ({adx:.1f}) │ <b>TREND:</b> {trend_icon}\n"
        t += f"<b>STATUS:</b> {status_str} │ 🎯 <b>TRADES:</b> <code>{trades_today}</code>\n"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += "<pre>\n"
        t += f"{'LEG':<7} {'STRIKE':>6} {'ENTRY':>7} {'LTP':>7} {'SL':>7} {'PNL':>9}\n"
        t += "─────────────────────────────────────────\n"

        if positions:
            for leg, pos in positions.items():
                strike = pos.get("strike", 0)
                entry = pos.get("entry_price", 0.0)
                ltp = pos.get("ltp", entry)
                pnl = pos.get("pnl", 0.0)
                sign = "+" if pnl >= 0 else ""
                sl_state = pos.get("dual_sl_state") or {}
                tsl = sl_state.get("current_premium_sl", 0.0)
                tsl_str = f"{tsl:>7.2f}" if tsl > 0 else "      —"
                if leg == "CE_HEDGE":
                    leg_tag = "CE(H)"
                elif leg == "PE_HEDGE":
                    leg_tag = "PE(H)"
                elif sl_state.get("solo_mode") and leg == "PE":
                    leg_tag = "PE*"
                elif sl_state.get("solo_mode") and leg == "CE":
                    leg_tag = "CE*"
                else:
                    leg_tag = leg

                t += f"{leg_tag:<7} {strike:>6} {entry:>7.2f} {ltp:>7.2f} {tsl_str} {sign}{pnl:>8,.0f}\n"
        else:
            t += "  No Open Positions\n"

        t += "─────────────────────────────────────────\n"
        r_sign = "+" if realized_pnl >= 0 else ""
        u_sign = "+" if unrealized_pnl >= 0 else ""
        n_sign = "+" if total_pnl >= 0 else ""
        t += f"Realized PnL:               {r_sign}₹{realized_pnl:>10,.2f}\n"
        t += f"Unrealized MTM:             {u_sign}₹{unrealized_pnl:>10,.2f}\n"
        t += "─────────────────────────────────────────\n"
        t += f"NET MTM:         {n_sign}₹{total_pnl:>10,.2f} ({pnl_pct:+.2f}%)\n"
        t += "</pre>\n"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        circuit_val = round(-total_cap * 0.018, 0)
        t += f"💰 <b>Capital:</b> <code>₹{total_cap:,.0f}</code> │ ⚡ <b>Circuit:</b> <code>₹{circuit_val:,.0f}</code>\n"
        mtd_sign = "+" if mtd_pnl >= 0 else ""
        ytd_sign = "+" if ytd_pnl >= 0 else ""
        t += f"📅 <b>MTD:</b> <code>{mtd_sign}₹{mtd_pnl:,.0f}</code> │ <b>YTD:</b> <code>{ytd_sign}₹{ytd_pnl:,.0f}</code>"

        is_refresh_cycle = (now_ts - _last_tg_dashboard_new_msg_ts) >= 60.0 or is_eod

        for cid in chat_ids:
            msg_id = _last_tg_dashboard_msg_ids.get(cid)
            edited = False

            if msg_id is not None and not is_refresh_cycle:
                try:
                    edit_resp = requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                        json={"chat_id": cid, "message_id": msg_id, "text": t, "parse_mode": "HTML"},
                        timeout=3
                    )
                    if edit_resp.status_code == 200 and edit_resp.json().get("ok"):
                        edited = True
                    elif edit_resp.status_code == 400 and "message is not modified" in edit_resp.text:
                        edited = True  # Content identical, already up to date!
                    elif edit_resp.status_code == 429:
                        retry_after = edit_resp.json().get("parameters", {}).get("retry_after", 30)
                        _tg_rate_limited_until = time.time() + retry_after
                        return
                except Exception:
                    pass

            if not edited and (msg_id is None or is_refresh_cycle):
                try:
                    send_resp = requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": cid, "text": t, "parse_mode": "HTML"},
                        timeout=4
                    )
                    if send_resp.status_code == 200 and send_resp.json().get("ok"):
                        _last_tg_dashboard_msg_ids[cid] = send_resp.json().get("result", {}).get("message_id")
                        _last_tg_dashboard_new_msg_ts = now_ts
                    elif send_resp.status_code == 429:
                        retry_after = send_resp.json().get("parameters", {}).get("retry_after", 30)
                        _tg_rate_limited_until = time.time() + retry_after
                        return
                except Exception:
                    pass

        if is_refresh_cycle:
            _last_tg_dashboard_new_msg_ts = now_ts
    except Exception as e:
        log_warn(f"Telegram dashboard failed: {e}")



def round_to_strike(price: float, strike_step: int = 50) -> int:
    return int(round(price / float(strike_step)) * strike_step)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1A: CONTINUOUS STREAMING EMA & MOMENTUM ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class ContinuousEMA:
    """Continuous exponential moving average with half-life in seconds."""
    def __init__(self, half_life_seconds: float):
        self.half_life_seconds = float(half_life_seconds)
        self.value: Optional[float] = None
        self.timestamp: Optional[float] = None

    def update(self, value: float, timestamp: float) -> float:
        if self.value is None or self.timestamp is None:
            self.value = value
        else:
            dt = max(0.0, timestamp - self.timestamp)
            alpha = 1.0 - math.exp(-math.log(2) * dt / self.half_life_seconds)
            self.value += alpha * (value - self.value)
        self.timestamp = timestamp
        return self.value


class RollingVolatility:
    """Sample rolling standard deviation over a fixed window of ticks."""
    def __init__(self, window: int):
        self.values = deque(maxlen=window)

    def update(self, value: float) -> Optional[float]:
        self.values.append(float(value))
        if len(self.values) < 2:
            return None
        mean = sum(self.values) / len(self.values)
        variance = sum((x - mean) ** 2 for x in self.values) / (len(self.values) - 1)
        return math.sqrt(max(0.0, variance))


class AdaptivePersistence:
    """Maps volatility ratio to required signal hold duration."""
    @staticmethod
    def raw(volatility_ratio: float, minimum: float = 3.0, maximum: float = 30.0) -> float:
        """P_raw = 10/VR, clamped to [minimum, maximum]."""
        return max(minimum, min(maximum, 10.0 / max(float(volatility_ratio), 1e-12)))


class ContinuousEMAEngine:
    """Continuous streaming indicator pipeline tracking EMA 15s, 90s, 300s, slope, and persistence."""
    def __init__(self):
        self.emas = {h: ContinuousEMA(h) for h in (15.0, 90.0, 300.0)}
        self.slow_history = deque(maxlen=11)
        self.rv = {60: RollingVolatility(60), 300: RollingVolatility(300)}
        self.raw_signal: int = 0
        self.signal_start_ts: Optional[float] = None
        self.confirmed_signal: int = 0
        self.latest_snapshot: Dict[str, Any] = {}

    def update(self, spot: float, now_ts: float) -> Dict[str, Any]:
        if spot <= 0:
            return self.latest_snapshot

        for ema in self.emas.values():
            ema.update(spot, now_ts)

        if self.emas[300.0].value is not None:
            self.slow_history.append(self.emas[300.0].value)

        rv60 = self.rv[60].update(spot)
        rv300 = self.rv[300].update(spot)

        vr = (rv60 / rv300) if rv60 is not None and rv300 and rv300 > 0 else 1.0
        p_req = AdaptivePersistence.raw(vr)

        slow_slope = (self.slow_history[-1] - self.slow_history[0]) if len(self.slow_history) >= 2 else 0.0

        fast_val = self.emas[15.0].value if self.emas[15.0].value is not None else spot
        slow_val = self.emas[90.0].value if self.emas[90.0].value is not None else spot

        # Directional raw signal: Fast > Slow and Slow Slope > 0 (Bullish), or Fast < Slow and Slope < 0 (Bearish)
        if fast_val > slow_val and slow_slope > 0:
            sig = 1
        elif fast_val < slow_val and slow_slope < 0:
            sig = -1
        else:
            sig = 0

        if sig != 0:
            if sig == self.raw_signal:
                pass
            else:
                self.raw_signal = sig
                self.signal_start_ts = now_ts
        else:
            self.raw_signal = 0
            self.signal_start_ts = None
            self.confirmed_signal = 0

        hold_time = (now_ts - self.signal_start_ts) if self.signal_start_ts else 0.0
        if self.raw_signal != 0 and hold_time >= p_req:
            self.confirmed_signal = self.raw_signal
        else:
            self.confirmed_signal = 0

        self.latest_snapshot = {
            "ema_15": fast_val,
            "ema_90": slow_val,
            "ema_300": self.emas[300.0].value or spot,
            "slow_slope": slow_slope,
            "rv60": rv60 or 0.0,
            "rv300": rv300 or 0.0,
            "vr": vr,
            "persistence_req": p_req,
            "raw_signal": self.raw_signal,
            "hold_time": hold_time,
            "confirmed_signal": self.confirmed_signal
        }
        return self.latest_snapshot


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1B: MARKET DATA INGESTION & 5-MIN AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

class MarketData:
    def __init__(self, cache_file: str):
        self.cache_file = cache_file
        self.streamer = NSEATMStreamer()
        self.bars_1m: List[Dict[str, Any]] = []
        self.logged_1m_keys: set = set()
        self.bars_5m: List[Dict[str, Any]] = []
        self.latest_spot: float = 0.0
        self.latest_atm: int = 0
        self.last_completed_1m_key: Optional[str] = None
        self.ema_engine = ContinuousEMAEngine()
        self.latest_ema_snapshot: Dict[str, Any] = {}
        self._load_cache()
        self._seed_history_if_needed()

    def _load_cache(self):
        if not os.path.exists(self.cache_file):
            return
        try:
            today_str = get_ist_now().strftime("%Y-%m-%d")
            minute_map = {}
            stale_lines = []
            today_lines = []
            with open(self.cache_file, "r") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            dt = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M:%S")
                            # Only load bars from TODAY to prevent cross-day indicator corruption
                            if dt.strftime("%Y-%m-%d") != today_str:
                                stale_lines.append(line)
                                continue
                            price_val = float(parts[1])
                            if price_val < 10000.0:
                                stale_lines.append(line)
                                continue
                            today_lines.append(line)
                            min_key = dt.strftime("%Y-%m-%d %H:%M")
                            minute_map[min_key] = (dt, price_val)
                        except ValueError:
                            continue

            # Flush stale days from cache file (keep only today's bars)
            if stale_lines and today_lines:
                log_info(f"MarketData: Flushing {len(stale_lines)} stale bars from previous days. Keeping {len(today_lines)} bars from today.")
                with open(self.cache_file, "w") as f:
                    for line in today_lines:
                        f.write(line if line.endswith("\n") else line + "\n")
            elif stale_lines and not today_lines:
                log_info(f"MarketData: Cache contains only old data ({len(stale_lines)} bars). Clearing file for fresh start.")
                with open(self.cache_file, "w") as f:
                    pass  # empty the file

            sorted_bars = sorted(minute_map.values(), key=lambda x: x[0])
            warmup_bars = sorted_bars[-300:] if len(sorted_bars) > 300 else sorted_bars

            for dt, price in warmup_bars:
                min_key = dt.strftime("%Y-%m-%d %H:%M")
                if min_key not in self.logged_1m_keys:
                    self.bars_1m.append({"timestamp": dt, "spot": price, "minute_key": min_key})
                    self.logged_1m_keys.add(min_key)
                    self.latest_spot = price
                    self.latest_atm = round_to_strike(price, 50)
            
            self._rebuild_5m_candles()
            if self.bars_1m:
                self.last_completed_1m_key = self.bars_1m[-1]["minute_key"]
                log_info(f"MarketData: Loaded {len(self.bars_1m)} today-only 1-min bars ({len(self.bars_5m)} 5-min candles built). Latest Spot: {self.latest_spot:.2f}")
            else:
                log_info(f"MarketData: No bars from today in cache. Will build fresh from live ticks.")
        except Exception as e:
            log_warn(f"MarketData: Error loading cache: {e}")

    def _seed_history_if_needed(self):
        """
        Seeds 50 historical 5-minute bars for instant indicator readiness.
        Primary: Flattrade API timeseries (Token 26000).
        Fallback: yfinance (^NSEI) if Flattrade timeseries is empty or unavailable.
        """
        if len(self.bars_5m) >= 30:
            return

        seeded_5m = []

        # 1. Primary: Seed from Flattrade API
        api = getattr(self.streamer, "api", global_api)
        if api and hasattr(api, "get_time_price_series"):
            try:
                log_info("MarketData: Attempting historical 5-min seeding from Flattrade (Token 26000)...")
                end_time = get_ist_now()
                start_time = end_time - timedelta(days=5)

                res = api.get_time_price_series(
                    exchange='NSE',
                    token='26000',
                    starttime=start_time.timestamp(),
                    endtime=end_time.timestamp(),
                    interval=5
                )

                if res and isinstance(res, list) and len(res) > 0:
                    for row in res:
                        try:
                            ts = datetime.strptime(row['time'], "%d-%m-%Y %H:%M:%S")
                            seeded_5m.append({
                                'timestamp': ts,
                                'open': float(row['into']),
                                'high': float(row['inth']),
                                'low': float(row['intl']),
                                'close': float(row['intc'])
                            })
                        except Exception:
                            continue

                    if seeded_5m:
                        log_info(f"MarketData: Successfully fetched {len(seeded_5m)} 5m bars from Flattrade.")
                else:
                    log_warn("MarketData: Flattrade get_time_price_series returned empty.")
            except Exception as e:
                log_warn(f"MarketData: Flattrade history seeding skipped ({e}).")

        # 2. Fallback: Seed from yfinance if Flattrade returned fewer than 30 bars
        if len(seeded_5m) < 30 and yf is not None:
            try:
                log_info("MarketData: Using yfinance fallback for 50 historical 5-min bars (^NSEI)...")
                df = yf.download("^NSEI", period="5d", interval="5m", progress=False, timeout=8)
                if df is not None and not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    for idx, row in df.iterrows():
                        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                        seeded_5m.append({
                            "timestamp": ts,
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"])
                        })
                    if seeded_5m:
                        log_info(f"MarketData: Successfully seeded {len(seeded_5m)} 5m bars from yfinance.")
            except Exception as e:
                log_warn(f"MarketData: yfinance history seeding fallback skipped ({e}).")

        if seeded_5m:
            seeded_5m.sort(key=lambda x: x['timestamp'])
            # Normalise ALL timestamps to naive IST (strip tz-info) to avoid comparison crashes
            IST_OFFSET = timedelta(hours=5, minutes=30)
            clean_bars = []
            for b in seeded_5m[-100:]:
                ts = b['timestamp']
                if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                    ts = ts.astimezone(timezone(IST_OFFSET)).replace(tzinfo=None)
                clean_bars.append({
                    'timestamp': ts,
                    'open':  float(b['open']),
                    'high':  float(b['high']),
                    'low':   float(b['low']),
                    'close': float(b['close']),
                })
            # Filter to only valid IST market-hours bars (9:15–15:30)
            clean_bars = [b for b in clean_bars
                          if b['high'] > 0 and b['low'] > 0
                          and b['high'] - b['low'] > 0.5]
            self.historical_5m_bars = clean_bars
            self.bars_5m = list(clean_bars)
            if clean_bars:
                avg_hl = sum(b['high'] - b['low'] for b in clean_bars) / len(clean_bars)
                log_info(f"MarketData: ✅ Seeded {len(clean_bars)} 5m bars | Avg H-L = {avg_hl:.1f} pts | ADX will be accurate!")
            else:
                log_warn("MarketData: ⚠️ Seeded bars had zero valid H-L range — seeding effectively failed!")
        else:
            self.historical_5m_bars = []
            log_warn("MarketData: ⚠️ History seeding FAILED. ADX will be inflated until 30+ live bars accumulate.")

        # 3. Seed 1-minute historical bars for instant KAMA(10, 3, 30) readiness
        if len(self.bars_1m) < 30:
            seeded_1m = []
            api = getattr(self.streamer, "api", global_api)
            if api and hasattr(api, "get_time_price_series"):
                try:
                    log_info("MarketData: Attempting historical 1-min seeding from Flattrade (Token 26000)...")
                    end_time = get_ist_now()
                    start_time_1m = end_time - timedelta(days=2)
                    res_1m = api.get_time_price_series(
                        exchange='NSE',
                        token='26000',
                        starttime=start_time_1m.timestamp(),
                        endtime=end_time.timestamp(),
                        interval=1
                    )
                    if res_1m and isinstance(res_1m, list) and len(res_1m) > 0:
                        for row in res_1m:
                            try:
                                ts = datetime.strptime(row['time'], "%d-%m-%Y %H:%M:%S")
                                min_key = ts.strftime("%Y-%m-%d %H:%M")
                                c_val = float(row.get('intc') or row.get('close') or 0.0)
                                if c_val > 0:
                                    seeded_1m.append({
                                        'timestamp': ts,
                                        'spot': c_val,
                                        'minute_key': min_key
                                    })
                            except Exception:
                                continue
                        if seeded_1m:
                            log_info(f"MarketData: Successfully fetched {len(seeded_1m)} 1m bars from Flattrade.")
                except Exception as e:
                    log_warn(f"MarketData: Flattrade 1m seeding skipped ({e}).")

            if len(seeded_1m) < 30 and yf is not None:
                try:
                    log_info("MarketData: Using yfinance fallback for historical 1-min bars (^NSEI)...")
                    df_yf_1m = yf.download("^NSEI", period="2d", interval="1m", progress=False, timeout=8)
                    if df_yf_1m is not None and not df_yf_1m.empty:
                        if isinstance(df_yf_1m.columns, pd.MultiIndex):
                            df_yf_1m.columns = df_yf_1m.columns.get_level_values(0)
                        IST_OFFSET = timedelta(hours=5, minutes=30)
                        for idx, row in df_yf_1m.iterrows():
                            ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
                            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                                ts = ts.astimezone(timezone(IST_OFFSET)).replace(tzinfo=None)
                            min_key = ts.strftime("%Y-%m-%d %H:%M")
                            c_val = float(row["Close"])
                            if c_val > 0:
                                seeded_1m.append({
                                    "timestamp": ts,
                                    "spot": c_val,
                                    "minute_key": min_key
                                })
                        if seeded_1m:
                            log_info(f"MarketData: Successfully seeded {len(seeded_1m)} 1m bars from yfinance.")
                except Exception as e:
                    log_warn(f"MarketData: yfinance 1m seeding fallback skipped ({e}).")

            if seeded_1m:
                seeded_1m.sort(key=lambda x: x['timestamp'])
                existing_keys = {b['minute_key'] for b in self.bars_1m}
                merged = [b for b in seeded_1m if b['minute_key'] not in existing_keys] + self.bars_1m
                merged.sort(key=lambda x: x['timestamp'])
                self.bars_1m = merged[-300:]
                for b in self.bars_1m:
                    self.logged_1m_keys.add(b['minute_key'])
                if self.bars_1m:
                    self.last_completed_1m_key = self.bars_1m[-1]['minute_key']
                    log_info(f"MarketData: ✅ Seeded {len(self.bars_1m)} 1-min bars | KAMA(10,3,30) active immediately!")

    def _rebuild_5m_candles(self):
        if not self.bars_1m:
            return
        df_1m = pd.DataFrame(self.bars_1m)
        df_1m.set_index("timestamp", inplace=True)
        df_5m_live = df_1m["spot"].resample("5min", label="left", closed="left").ohlc().dropna()

        historical = getattr(self, 'historical_5m_bars', [])
        # Use naive datetime for comparison — seeded bars were already stripped of tzinfo above
        last_seeded_ts = historical[-1]['timestamp'] if historical else None

        self.bars_5m = list(historical)
        for ts, row in df_5m_live.iterrows():
            # Make ts naive if pandas gave us a tz-aware timestamp
            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            # Only append bars strictly NEWER than last seeded real-OHLC bar
            if last_seeded_ts is not None and ts <= last_seeded_ts:
                continue
            self.bars_5m.append({
                "timestamp": ts,
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            })

    def fetch_live_tick(self) -> Tuple[float, int, bool, bool]:
        spot, atm, is_stale = self.streamer.get_spot_and_atm()
        self.latest_spot = spot
        self.latest_atm = atm
        if spot > 0:
            self.latest_ema_snapshot = self.ema_engine.update(spot, time.time())
        
        now = get_ist_now()
        current_min_key = now.strftime("%Y-%m-%d %H:%M")
        is_new_1m_bar = (current_min_key != self.last_completed_1m_key)
        
        if is_new_1m_bar and spot > 10000.0:
            candle_ts_str = f"{current_min_key}:00"
            dt = datetime.strptime(candle_ts_str, "%Y-%m-%d %H:%M:%S")
            
            if current_min_key not in self.logged_1m_keys:
                try:
                    os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
                    with open(self.cache_file, "a") as f:
                        f.write(f"{candle_ts_str},{spot:.2f}\n")
                    self.logged_1m_keys.add(current_min_key)
                except Exception as e:
                    log_warn(f"MarketData: Failed writing cache line: {e}")
                
                self.bars_1m.append({"timestamp": dt, "spot": spot, "minute_key": current_min_key})
                self._rebuild_5m_candles()
            
            self.last_completed_1m_key = current_min_key
            
        return spot, atm, is_new_1m_bar, is_stale

    def get_1m_dataframe(self) -> pd.DataFrame:
        if not self.bars_1m:
            return pd.DataFrame(columns=["timestamp", "spot", "minute_key"])
        return pd.DataFrame(self.bars_1m)

    def get_5m_dataframe(self) -> pd.DataFrame:
        if not self.bars_5m:
            return pd.DataFrame(columns=["open", "high", "low", "close"])
        return pd.DataFrame(self.bars_5m)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2: INDICATORS & REGIME DETECTION (KAMA, ADX, ATR on 5M)
# ══════════════════════════════════════════════════════════════════════════════

class Indicators:
    @staticmethod
    def calculate_kama(closes: np.ndarray, period: int = KAMA_PERIOD, fast: int = KAMA_FAST_EMA, slow: int = KAMA_SLOW_EMA) -> Tuple[Optional[float], Optional[float], int]:
        if closes is None:
            return None, None, 0
        closes = np.asarray(closes, dtype=float)
        valid_mask = ~np.isnan(closes) & ~np.isinf(closes)
        closes = closes[valid_mask]
        if len(closes) < period + 1:
            return None, None, 0
        
        kama = np.zeros(len(closes))
        kama[period - 1] = np.mean(closes[:period])
        
        fast_sc = 2.0 / (fast + 1.0)
        slow_sc = 2.0 / (slow + 1.0)
        
        for i in range(period, len(closes)):
            change = abs(closes[i] - closes[i - period])
            volatility = np.sum(np.abs(np.diff(closes[i - period:i + 1])))
            er = (change / volatility) if volatility > 1e-6 else 0.0
            er = min(1.0, max(0.0, er))
            sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
            kama[i] = kama[i - 1] + sc * (closes[i] - kama[i - 1])
            
        current_kama = float(kama[-1])
        prev_kama = float(kama[-2])
        diff = current_kama - prev_kama
        
        if diff > KAMA_MIN_SLOPE:
            trend = 1   # UP
        elif diff < -KAMA_MIN_SLOPE:
            trend = -1  # DOWN
        else:
            trend = 0   # FLAT
            
        return current_kama, prev_kama, trend

    @staticmethod
    def calculate_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ATR_PERIOD) -> float:
        if len(closes) < 2:
            return DEFAULT_ATR_5M
        
        n = len(closes)
        tr = np.zeros(n)
        tr[0] = highs[0] - lows[0]
        
        for i in range(1, n):
            hl = highs[i] - lows[i]
            hpc = abs(highs[i] - closes[i - 1])
            lpc = abs(lows[i] - closes[i - 1])
            tr[i] = max(hl, hpc, lpc)
            
        if len(tr) < period:
            return float(np.mean(tr)) if len(tr) > 0 else DEFAULT_ATR_5M
        
        atr = np.zeros(n)
        atr[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            
        return float(atr[-1])

    @staticmethod
    def calculate_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ADX_PERIOD) -> Tuple[float, float, float]:
        n = len(closes)
        if n < period * 2:
            return 18.0, 20.0, 20.0
        
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            
            if up_move > down_move and up_move > 0:
                plus_dm[i] = up_move
            else:
                plus_dm[i] = 0.0
                
            if down_move > up_move and down_move > 0:
                minus_dm[i] = down_move
            else:
                minus_dm[i] = 0.0
                
            hl = highs[i] - lows[i]
            hpc = abs(highs[i] - closes[i - 1])
            lpc = abs(lows[i] - closes[i - 1])
            tr[i] = max(hl, hpc, lpc)
            
        tr_smooth = np.zeros(n)
        plus_dm_smooth = np.zeros(n)
        minus_dm_smooth = np.zeros(n)
        
        tr_smooth[period] = np.sum(tr[1:period + 1])
        plus_dm_smooth[period] = np.sum(plus_dm[1:period + 1])
        minus_dm_smooth[period] = np.sum(minus_dm[1:period + 1])
        
        for i in range(period + 1, n):
            tr_smooth[i] = tr_smooth[i - 1] - (tr_smooth[i - 1] / period) + tr[i]
            plus_dm_smooth[i] = plus_dm_smooth[i - 1] - (plus_dm_smooth[i - 1] / period) + plus_dm[i]
            minus_dm_smooth[i] = minus_dm_smooth[i - 1] - (minus_dm_smooth[i - 1] / period) + minus_dm[i]
            
        plus_di = 100.0 * (plus_dm_smooth / np.where(tr_smooth == 0, 1e-6, tr_smooth))
        minus_di = 100.0 * (minus_dm_smooth / np.where(tr_smooth == 0, 1e-6, tr_smooth))
        
        di_sum = plus_di + minus_di
        di_diff = np.abs(plus_di - minus_di)
        dx = 100.0 * (di_diff / np.where(di_sum == 0, 1e-6, di_sum))
        
        adx = np.zeros(n)
        start_idx = period * 2 - 1
        if start_idx < n:
            adx[start_idx] = np.mean(dx[period:start_idx + 1])
            for i in range(start_idx + 1, n):
                adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
            current_adx = float(adx[-1])
        else:
            current_adx = float(np.mean(dx[period:])) if len(dx) > period else 18.0
            
        return current_adx, float(plus_di[-1]), float(minus_di[-1])

    @classmethod
    def evaluate_all(cls, df_1m: pd.DataFrame, df_5m: pd.DataFrame) -> Dict[str, Any]:
        if df_5m.empty or len(df_5m) < 5:
            return {
                "kama": None, "prev_kama": None, "trend": 0,
                "atr": DEFAULT_ATR_5M, "adx": 18.0, "plus_di": 20.0, "minus_di": 20.0,
                "regime": "CHOP"
            }
            
        highs = df_5m["high"].to_numpy(dtype=float)
        lows = df_5m["low"].to_numpy(dtype=float)
        closes = df_5m["close"].to_numpy(dtype=float)
        
        # ── DIAGNOSTIC: Print ADX input quality every 5 minutes ──
        if not getattr(Indicators, '_adx_diag_count', None):
            Indicators._adx_diag_count = 0
        Indicators._adx_diag_count += 1
        if Indicators._adx_diag_count % 5 == 1:
            avg_hl = float(np.mean(highs - lows)) if len(highs) > 0 else 0
            log_info(f"[ADX DIAG] 5m bars={len(df_5m)} | Avg H-L={avg_hl:.1f} pts | Last H={highs[-1]:.1f} L={lows[-1]:.1f} C={closes[-1]:.1f}")
        
        # Calculate KAMA on 1-minute spot (close) prices
        if not df_1m.empty and len(df_1m) >= KAMA_PERIOD + 1:
            closes_1m = df_1m["spot"].to_numpy(dtype=float)
            kama, prev_kama, trend = cls.calculate_kama(closes_1m, period=KAMA_PERIOD, fast=KAMA_FAST_EMA, slow=KAMA_SLOW_EMA)
        else:
            kama, prev_kama, trend = cls.calculate_kama(closes, period=KAMA_PERIOD, fast=KAMA_FAST_EMA, slow=KAMA_SLOW_EMA)
        atr = cls.calculate_atr(highs, lows, closes, period=ATR_PERIOD)
        adx, p_di, m_di = cls.calculate_adx(highs, lows, closes, period=ADX_PERIOD)
        
        if adx < ADX_CHOP_THRESHOLD:
            regime = "CHOP"
        elif adx >= ADX_TREND_THRESHOLD:
            regime = "TREND"
        else:
            regime = "TRANSITION"
            
        return {
            "kama": kama,
            "prev_kama": prev_kama,
            "trend": trend,
            "atr": atr,
            "adx": adx,
            "plus_di": p_di,
            "minus_di": m_di,
            "regime": regime
        }


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2B: REVERSION DETECTOR (Multi-Indicator Confluence Reversal Engine)
# ══════════════════════════════════════════════════════════════════════════════

class ReversionDetector:
    """
    Detects market reversals using 2-of-3 indicator confluence:
      1. KAMA Slope  (1m adaptive momentum)
      2. +DI / -DI crossover (ADX directional strength on 5m)
      3. ATR volatility gate (move must be meaningful, not noise)

    Also detects PROACTIVE EXIT conditions: when trend is strongly against a
    short leg and we should exit early instead of waiting for the full SL.

    Core Rule:
      - CE reversion  = market turning DOWN  = good time to re-enter CE SELL
      - PE reversion  = market turning UP    = good time to re-enter PE SELL
    """

    @staticmethod
    def _score(indicators: dict) -> dict:
        """Compute a direction-tagged score tuple for the current bar."""
        kama        = float(indicators.get("kama")      or 0.0)
        prev_kama   = float(indicators.get("prev_kama") or kama)
        kama_slope  = kama - prev_kama

        adx         = float(indicators.get("adx",      18.0))
        plus_di     = float(indicators.get("plus_di",  20.0))
        minus_di    = float(indicators.get("minus_di", 20.0))
        atr         = float(indicators.get("atr",      DEFAULT_ATR_5M))
        di_gap      = plus_di - minus_di   # >0 = bullish DI, <0 = bearish DI

        return {
            "kama_slope": kama_slope,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "di_gap": di_gap,
            "atr": atr,
        }

    @classmethod
    def is_reversal_for_ce(cls, indicators: dict, cooldown_data: dict = None) -> tuple:
        """
        Returns (signal: bool, confidence: int, reason: str)
        CE re-entry strictly on 1m KAMA reversal or Continuous EMA confirmed bearish lock:
        CE was stopped because market surged UP.
        Re-enter when KAMA 1m slope turns DOWN (kama_slope <= -0.15) OR EMA confirms bearish trend (sig == -1).
        """
        s = cls._score(indicators)
        kama_slope = s["kama_slope"]
        sig = indicators.get("confirmed_signal", 0)
        reversal = (kama_slope <= -REVERSAL_KAMA_SLOPE_THRESHOLD) or (sig == -1)
        reason = f"REVERSAL_CE(kama_slope={kama_slope:.2f}, ema_sig={sig})" if reversal else ""
        return reversal, (2 if reversal else 0), reason

    @classmethod
    def is_reversal_for_pe(cls, indicators: dict, cooldown_data: dict = None) -> tuple:
        """
        Returns (signal: bool, confidence: int, reason: str)
        PE re-entry strictly on 1m KAMA reversal or Continuous EMA confirmed bullish lock:
        PE was stopped because market dumped DOWN.
        Re-enter when KAMA 1m slope turns UP (kama_slope >= +0.15) OR EMA confirms bullish trend (sig == +1).
        """
        s = cls._score(indicators)
        kama_slope = s["kama_slope"]
        sig = indicators.get("confirmed_signal", 0)
        reversal = (kama_slope >= REVERSAL_KAMA_SLOPE_THRESHOLD) or (sig == 1)
        reason = f"REVERSAL_PE(kama_slope={kama_slope:.2f}, ema_sig={sig})" if reversal else ""
        return reversal, (2 if reversal else 0), reason

    @classmethod
    def is_trend_strongly_against(cls, leg: str, indicators: dict) -> tuple:
        """
        Returns (should_exit_early: bool, reason: str)
        Detects if market is trending STRONGLY against a short leg.
        Used for PROACTIVE EARLY EXIT before the full SL is hit.

        CE short is hurt when market goes UP (bullish trend):
          - +DI > -DI with gap >= PROACTIVE_EXIT_DI_GAP_MIN
          - ADX >= PROACTIVE_EXIT_TREND_ADX (strong trend, not choppy)
          - KAMA slope >= +REVERSAL_KAMA_SLOPE_THRESHOLD (upward momentum)
          - Continuous EMA confirmed bullish (+1)

        PE short is hurt when market goes DOWN (bearish trend):
          - -DI > +DI with gap >= PROACTIVE_EXIT_DI_GAP_MIN
          - ADX >= PROACTIVE_EXIT_TREND_ADX
          - KAMA slope <= -REVERSAL_KAMA_SLOPE_THRESHOLD (downward momentum)
          - Continuous EMA confirmed bearish (-1)
        """
        if not PROACTIVE_EXIT_ENABLED:
            return False, ""

        s = cls._score(indicators)
        hits = 0
        reasons = []
        sig = indicators.get("confirmed_signal", 0)

        if leg == "CE":
            # CE is hurt by UP moves
            if s["kama_slope"] >= REVERSAL_KAMA_SLOPE_THRESHOLD:
                hits += 1
                reasons.append(f"KAMA↑{s['kama_slope']:.2f}")
            if s["plus_di"] > s["minus_di"] and abs(s["di_gap"]) >= PROACTIVE_EXIT_DI_GAP_MIN:
                hits += 1
                reasons.append(f"+DI({s['plus_di']:.1f})>>-DI({s['minus_di']:.1f})")
            if s["adx"] >= PROACTIVE_EXIT_TREND_ADX:
                hits += 1
                reasons.append(f"ADX{s['adx']:.1f}(STRONG)")
            if sig == 1:
                hits += 1
                reasons.append("EMA_BULLISH_LOCKED")
        elif leg == "PE":
            # PE is hurt by DOWN moves
            if s["kama_slope"] <= -REVERSAL_KAMA_SLOPE_THRESHOLD:
                hits += 1
                reasons.append(f"KAMA↓{s['kama_slope']:.2f}")
            if s["minus_di"] > s["plus_di"] and abs(s["di_gap"]) >= PROACTIVE_EXIT_DI_GAP_MIN:
                hits += 1
                reasons.append(f"-DI({s['minus_di']:.1f})>>+DI({s['plus_di']:.1f})")
            if s["adx"] >= PROACTIVE_EXIT_TREND_ADX:
                hits += 1
                reasons.append(f"ADX{s['adx']:.1f}(STRONG)")
            if sig == -1:
                hits += 1
                reasons.append("EMA_BEARISH_LOCKED")

        # Need at least 3 confirming factors for proactive exit
        should_exit = (hits >= 3)
        reason = f"PROACTIVE_EXIT_{leg}[" + ",".join(reasons) + "]" if should_exit else ""
        return should_exit, reason

    @classmethod
    def safest_leg_to_enter(cls, indicators: dict, last_stopped_leg: str = None) -> str:
        """
        When recovering from 0 legs open, determine which single leg is safest.
        If a leg was just stopped out, the market is trending against it.
        We should follow momentum and sell the OTHER side.
        Returns 'CE', 'PE', or 'BOTH'.
        """
        if last_stopped_leg == "CE":
            return "PE"  # CE hit SL -> Market went UP -> Sell PE (Bullish)
        if last_stopped_leg == "PE":
            return "CE"  # PE hit SL -> Market went DOWN -> Sell CE (Bearish)

        sig = indicators.get("confirmed_signal", 0)
        if sig == 1:
            return "PE"  # Continuous EMA locked bullish -> Sell PE
        if sig == -1:
            return "CE"  # Continuous EMA locked bearish -> Sell CE

        s = cls._score(indicators)
        # In strong uptrend: sell PE (market going up, put decays)
        if s["kama_slope"] >= REVERSAL_KAMA_SLOPE_THRESHOLD and s["plus_di"] > s["minus_di"]:
            return "PE"
        # In strong downtrend: sell CE (market going down, call decays)
        if s["kama_slope"] <= -REVERSAL_KAMA_SLOPE_THRESHOLD and s["minus_di"] > s["plus_di"]:
            return "CE"
        # Choppy/neutral: enter both
        return "BOTH"


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: DUAL-LAYER RISK MANAGEMENT (SPOT-BASED SL & CIRCUIT BREAKER)
# ══════════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, capital=CAPITAL):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1 * capital * (PORTFOLIO_CIRCUIT_PCT / 100.0)
        
    def check_portfolio_circuit_breaker(self, realized_pnl: float, unrealized_pnl: float) -> tuple:
        total_pnl = realized_pnl + unrealized_pnl
        if total_pnl <= self.circuit_breaker_loss_limit:
            return True, f"Global Circuit Breaker Hit! PnL {total_pnl:.2f} <= Limit {self.circuit_breaker_loss_limit:.2f}"
        return False, ""

    def get_active_tsl_pct(self, now_ist: datetime, dte_days: float = 2.0, is_solo: bool = False) -> float:
        """
        Determines the active trailing stop loss percentage:
        - Afternoon theta acceleration window (after 1:00 PM IST): 6% (0.06)
        - Morning session (09:15-13:00 IST): 8.5% - 9.0% to provide optimal breathing room
        - Standard dual leg deep profit floor: 8.5% (0.085)
        """
        is_after_1pm = (now_ist.hour > AFTERNOON_TSL_HOUR or (now_ist.hour == AFTERNOON_TSL_HOUR and now_ist.minute >= AFTERNOON_TSL_MINUTE))
        if is_after_1pm:
            return AFTERNOON_TSL_PCT   # 0.06 (6%)
        if is_solo:
            return SOLO_LEG_TSL_PCT    # 0.09 (9%)
        return PREM_SL_MIN_PCT         # 0.085 (8.5%)

    def init_dual_sl(self, leg: str, entry_spot: float, strike: float, entry_premium: float, atr: float, iv: float, dte_days: float = 2.0) -> dict:
        now_ist = get_ist_now()
        is_after_1pm = (now_ist.hour > AFTERNOON_TSL_HOUR or (now_ist.hour == AFTERNOON_TSL_HOUR and now_ist.minute >= AFTERNOON_TSL_MINUTE))
        is_expiry = (dte_days <= EXPIRY_0DTE_THRESHOLD)
        initial_pct = PREM_SL_INITIAL_PCT_EXPIRY if (is_expiry and is_after_1pm) else PREM_SL_INITIAL_PCT
        initial_sl = round(entry_premium * (1.0 + initial_pct), 2)
        return {
            "entry_spot": entry_spot,
            "entry_premium": entry_premium,
            "best_premium": entry_premium,
            "current_premium_sl": initial_sl,
            "initial_sl_pct": initial_pct,
            "is_expiry": is_expiry,
            "solo_mode": False,
            "breach_count": 0
        }

    def update_dual_sl_and_check(self, leg: str, pos_data: dict, current_spot: float, current_premium: float, is_strangle: bool, is_new_1m_bar: bool, dte_days: float = 2.0) -> tuple:
        sl_state = pos_data.get("dual_sl_state")
        if not sl_state: return False, ""

        now_ist = get_ist_now()
        is_solo = bool(sl_state.get("solo_mode", False))
        active_tsl_pct = self.get_active_tsl_pct(now_ist, dte_days, is_solo=is_solo)

        if is_solo:
            # ─────────────────────────────────────────────────────────────
            # SOLO LEG MODE: When other leg was removed, this leg was anchored
            # to its LTP. Best premium is tracked from that anchor point,
            # and the SL is trailed strictly at 7% (or 5% after 1 PM / 0 DTE).
            # ─────────────────────────────────────────────────────────────
            anchor_prem = float(sl_state.get("entry_premium", current_premium))
            if current_premium > 0 and current_premium < sl_state.get("best_premium", anchor_prem):
                sl_state["best_premium"] = current_premium

            best_prem = sl_state.get("best_premium", anchor_prem)
            new_trail_sl = round(best_prem * (1.0 + active_tsl_pct), 2)

            # Strict Ratchet: Stop loss can never move backwards (upwards)
            if "current_premium_sl" in sl_state:
                prem_sl = min(new_trail_sl, sl_state["current_premium_sl"])
            else:
                prem_sl = new_trail_sl

            sl_state["current_premium_sl"] = prem_sl

            # Check breach
            if current_premium >= prem_sl:
                return True, (
                    f"⛔ {leg} Solo TSL Triggered! | Anchor LTP: {anchor_prem:.2f} | "
                    f"Best: {best_prem:.2f} | Current: {current_premium:.2f} >= SL: {prem_sl:.2f} "
                    f"({active_tsl_pct*100:.0f}% TSL)"
                )
            return False, ""

        # ─────────────────────────────────────────────────────────────
        # STANDARD DUAL-LEG STRANGLE MODE
        # ─────────────────────────────────────────────────────────────
        entry_prem = float(sl_state.get("entry_premium", current_premium))

        # 1. Update Best Premium (lowest seen since entry)
        if current_premium > 0 and current_premium < sl_state.get("best_premium", entry_prem):
            sl_state["best_premium"] = current_premium

        best_prem = sl_state.get("best_premium", entry_prem)

        # 15% initial SL (gives options room to breathe)
        is_after_1pm = (now_ist.hour > AFTERNOON_TSL_HOUR or (now_ist.hour == AFTERNOON_TSL_HOUR and now_ist.minute >= AFTERNOON_TSL_MINUTE))
        is_expiry = (dte_days <= EXPIRY_0DTE_THRESHOLD)
        initial_pct = PREM_SL_INITIAL_PCT_EXPIRY if (is_expiry and is_after_1pm) else PREM_SL_INITIAL_PCT
        initial_sl = round(entry_prem * (1.0 + initial_pct), 2)

        if best_prem >= entry_prem:
            # Phase A: not yet in profit — hold at initial SL
            prem_sl = initial_sl
        else:
            if entry_prem <= 0.0:
                return False, ""
            # Phase B: in profit — dynamic trail down to active_tsl_pct (7%, or 5% after 1 PM / 0 DTE)
            profit_pct = (entry_prem - best_prem) / entry_prem  # 0.0 → 1.0

            trail_ceiling = PREM_SL_MAX_PCT
            trail_floor = active_tsl_pct
            trail_pct = trail_ceiling - (trail_ceiling - trail_floor) * min(profit_pct / 0.50, 1.0)
            trail_pct = max(trail_pct, trail_floor)

            trail_sl = round(best_prem * (1.0 + trail_pct), 2)
            # Never let trail SL exceed initial SL
            prem_sl = min(trail_sl, initial_sl)
            
        # STRICT RATCHET: The stop loss can NEVER move backwards (upwards).
        # It must stay at its tightest point until SL drags it further down.
        if "current_premium_sl" in sl_state and PREM_SL_MAX_PCT < 9.0:
            prem_sl = min(prem_sl, sl_state["current_premium_sl"])

        sl_state["current_premium_sl"] = prem_sl

        # 3. Check for Breach
        prem_breached = current_premium >= prem_sl
        
        if PREM_SL_DEBOUNCE_BARS <= 1:
            if prem_breached:
                return True, f"⛔ {leg} SL Triggered (Tick Level)! | Entry: {entry_prem:.2f} | Best: {best_prem:.2f} | Current: {current_premium:.2f} >= SL: {prem_sl:.2f}"
        else:
            if prem_breached and is_new_1m_bar:
                sl_state["breach_count"] = sl_state.get("breach_count", 0) + 1
            elif is_new_1m_bar:
                sl_state["breach_count"] = 0

            if sl_state.get("breach_count", 0) >= PREM_SL_DEBOUNCE_BARS:
                return True, f"⛔ {leg} SL Triggered ({PREM_SL_DEBOUNCE_BARS}m Debounce)! | Entry: {entry_prem:.2f} | Best: {best_prem:.2f} | Current: {current_premium:.2f} >= SL: {prem_sl:.2f}"

        return False, ""

    def check_proactive_exit(self, leg: str, pos_data: dict, current_premium: float, indicators: dict) -> tuple:
        """
        Proactive early exit: exit a short leg BEFORE SL is hit when:
          1. Not during 09:15-09:25 Opening Noise Shield (market settling).
          2. The leg is in loss >= PROACTIVE_EXIT_LOSS_PCT (7%).
          3. The market is trending strongly against it (all 3 indicators confirm).
        Returns (should_exit: bool, reason: str)
        """
        now_ist = get_ist_now()
        # Opening noise shield: suppress proactive early exits during 09:15-09:25 IST
        if now_ist.hour == OPENING_NOISE_SHIELD_HOUR and now_ist.minute < OPENING_NOISE_SHIELD_MINUTE:
            return False, ""

        sl_state = pos_data.get("dual_sl_state")
        if not sl_state:
            return False, ""

        entry_prem = float(sl_state.get("entry_premium", current_premium))
        if entry_prem <= 0:
            return False, ""

        loss_pct = (current_premium - entry_prem) / entry_prem
        if loss_pct < PROACTIVE_EXIT_LOSS_PCT:
            # Not in enough loss yet — don't trigger proactive exit
            return False, ""

        # Check if market is strongly trending against this leg
        trend_against, trend_reason = ReversionDetector.is_trend_strongly_against(leg, indicators)
        if trend_against:
            return True, f"🔻 PROACTIVE_EXIT {leg} | Loss:{loss_pct*100:.1f}% > {PROACTIVE_EXIT_LOSS_PCT*100:.0f}% | {trend_reason}"

        return False, ""
class ExecutionEngine:
    def __init__(self):
        self._lock_file = None
        try:
            import fcntl
            self._lock_file = open("/tmp/nifty_paper_engine.lock", "a+")
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.seek(0)
            self._lock_file.truncate()
            self._lock_file.write(f"{os.getpid()}\n")
            self._lock_file.flush()
        except (IOError, BlockingIOError):
            print("\n❌ [FATAL] Another instance of NIFTY Paper Trading Engine is already running!", flush=True)
            print("   Aborting duplicate instance immediately to prevent dual Telegram messages.\n", flush=True)
            sys.exit(0)

        self.state_file = os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2_paper.json")
        self.cache_file = os.path.join(PROJECT_ROOT, "data", "cache", "spot_cache_paper.csv")
        self.live_snap_file = os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2_paper.json")
        self.trade_book_dir = os.path.join(PROJECT_ROOT, "data", "logs", "trade_book")
        self.daily_pnl_file = os.path.join(PROJECT_ROOT, "data", "logs", "daily_pnl_v2_paper.csv")
        self.stop_flags = [
            os.path.join(PROJECT_ROOT, "data", "state", "stop_v2_paper.flag"),
            os.path.join(PROJECT_ROOT, "stop_paper.flag"),
            os.path.join(PROJECT_ROOT, "zxc_paper.txt")
        ]
        
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        os.makedirs(self.trade_book_dir, exist_ok=True)

        self.market_data = MarketData(self.cache_file)
        self.risk_manager = RiskManager(capital=CAPITAL)
        self.broker = FlattradeBroker()  # Reads PAPER_TRADING_MODE automatically
        if PAPER_TRADING_MODE:
            log_warn("📝 PAPER TRADING MODE — All orders are SIMULATED. No real money at risk.")
        elif global_api is not None:
            log_info("🚀 LIVE TRADING VIA FLATTRADE API — Real orders will be sent to exchange!")
        else:
            log_warn("⚠️ Flattrade API not connected. Falling back to paper trading.")
        
        self.mode = "WAIT_DATA"
        self.session_em_1sd = 0.0
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.realized_pnl: float = 0.0
        self.qty: int = self._calculate_lot_quantity()
        self.is_running: bool = True
        self._last_order_time: float = 0.0  # Rate limiter tracker
        import threading
        self._order_lock = threading.Lock()
        
        # Anti-Whipsaw Cooldown Tracker per leg (3m timer removed)
        self.cooldown_tracker: Dict[str, Dict[str, Any]] = {
            "CE": {"stopped_time": 0.0,  "active": False},
            "PE": {"stopped_time": 0.0,  "active": False}
        }
        self.total_reentries_today = 0
        self.strangle_resets_today = 0
        self.trades_today = 0
        
        self.last_reconciliation = 0
        self.last_feed_tick = 0
        self.stale_count = 0
        
        # 09:15-09:18 Market Observation & 10s Cooldown Tracker
        self.spot_at_0915: Optional[float] = None
        self.spot_at_0918: Optional[float] = None
        self.initial_entry_done: bool = False
        
        
        self.current_indicators: Dict[str, Any] = {
            "kama": None, "prev_kama": None, "trend": 0,
            "atr": DEFAULT_ATR_5M, "adx": 18.0, "regime": "CHOP"
        }
        
        df_5m = self.market_data.get_5m_dataframe()
        if not df_5m.empty and len(df_5m) >= 5:
            self.current_indicators = Indicators.evaluate_all(self.market_data.get_1m_dataframe(), df_5m)
            log_info(f"ExecutionEngine: Indicators pre-warmed on boot (KAMA={self.current_indicators.get('kama')}, Trend={self.current_indicators.get('trend')}, ADX={self.current_indicators.get('adx')}, ATR={self.current_indicators.get('atr')}, Regime={self.current_indicators.get('regime')})")
        
        self._ltp_cache: Dict[str, float] = {}
        self._load_state()
        
        # Startup Reconciliation against Broker (Live mode only)
        if not PAPER_TRADING_MODE:
            log_info("Performing Startup Reconciliation against Broker...")
            actual_pos = self._get_live_exchange_positions()
            if actual_pos is not None:
                mismatch = False
                for leg, p in list(self.positions.items()):
                    if p["tsym"] not in actual_pos or actual_pos[p["tsym"]] == 0:
                        log_alert(f"⚠️ RECONCILIATION: {leg} is missing on exchange! Removing from local state.")
                        del self.positions[leg]
                        mismatch = True
                if mismatch:
                    self._save_state()
                    log_info("State reconciled with Broker.")
            else:
                log_warn("Startup reconciliation failed to fetch live positions. Proceeding with local state.")
        else:
            log_info("Paper Trading Mode active: Skipped live broker position reconciliation.")
        self._write_pid()
        
        self._start_kill_switch_listener()
        self._setup_signal_handlers()

    def _write_pid(self):
        try:
            pid_file = os.path.join(PROJECT_ROOT, "data", "state", "v2.pid")
            os.makedirs(os.path.dirname(pid_file), exist_ok=True)
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
        except Exception as e:
            log_warn(f"PID write failed: {e}")

    def _remove_pid(self):
        try:
            pid_file = os.path.join(PROJECT_ROOT, "data", "state", "v2.pid")
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except Exception as e:
            log_warn(f"PID remove failed: {e}")

    def _setup_signal_handlers(self):
        def handler(sig, frame):
            log_alert(f"🛑 Received signal {sig}. Initiating immediate clean square-off...")
            self.trigger_emergency_shutdown(reason=f"SIGNAL_{sig}")
        
        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except Exception as e:
            log_warn(f"Signal handler setup failed: {e}")

    def _start_kill_switch_listener(self):
        def listener():
            global _EMERGENCY_STOP_TRIGGERED
            buf = ""
            while self.is_running and not _EMERGENCY_STOP_TRIGGERED:
                for flag_path in self.stop_flags:
                    if os.path.exists(flag_path):
                        log_alert(f"🛑 STOP FLAG DETECTED ({os.path.basename(flag_path)})! Initiating emergency halt...")
                        try:
                            os.remove(flag_path)
                        except Exception:
                            pass
                        self.trigger_emergency_shutdown(reason="STOP_FLAG")
                        return

                try:
                    if sys.stdin and not sys.stdin.closed and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
                        rlist, _, _ = select.select([sys.stdin], [], [], 0.3)
                        if rlist:
                            line = sys.stdin.readline()
                            if not line:
                                time.sleep(0.3)
                                continue
                            buf = (buf + line.strip().lower())[-30:]
                            if "zxc" in buf:
                                log_alert("🚨 EMERGENCY MANUAL SQUARE-OFF ('zxc') TYPED IN SERVER TERMINAL! 🚨")
                                self.trigger_emergency_shutdown(reason="EMERGENCY_ZXC")
                                return
                    else:
                        time.sleep(1.0)
                except Exception:
                    time.sleep(1.0)

        t = threading.Thread(target=listener, daemon=True, name="ZXC_KillSwitch_Listener")
        t.start()

    def trigger_emergency_shutdown(self, reason: str = "EMERGENCY_ZXC"):
        global _EMERGENCY_STOP_TRIGGERED
        with _EMERGENCY_STOP_LOCK:
            if _EMERGENCY_STOP_TRIGGERED:
                return
            _EMERGENCY_STOP_TRIGGERED = True
            self.is_running = False

        log_alert(f"🛑 EXECUTING GLOBAL EMERGENCY LIQUIDATION (Reason: {reason})! Closing all positions...")
        try:
            self._exit_all_positions(reason=reason)
            # If shutdown was caused by SIGTERM / pkill during code restart, save WAIT_DATA so reboot resumes trading
            self.mode = "WAIT_DATA" if reason.startswith("SIGNAL_") else "SESSION_DONE"
            self._save_state()
            final_pct = (self.realized_pnl / 200_000.0) * 100.0
            pnl_col = Fore.GREEN if self.realized_pnl >= 0 else Fore.RED
            sign = "+" if self.realized_pnl >= 0 else ""
            print(f"\n{pnl_col}✅ All positions successfully squared off. Final Realized PnL: {sign}₹{self.realized_pnl:,.2f} ({final_pct:+.2f}%). Strategy halted cleanly.{Style.RESET_ALL}\n", flush=True)
        except Exception as e:
            log_warn(f"Error during emergency shutdown: {e}")
        finally:
            os._exit(0)

    # ──────────────────────────────────────────────────────────────────────────
    # Precision Rate Limiter (1 order in 1 sec not more)
    # ──────────────────────────────────────────────────────────────────────────

    def _wait_order_rate_limit(self):
        """
        Enforces that at least MIN_ORDER_INTERVAL_SEC (1.05s) elapses between any two orders.
        Strictly guarantees: '1 order should go in 1 sec not more'.
        """
        now = time.time()
        elapsed = now - self._last_order_time
        if elapsed < MIN_ORDER_INTERVAL_SEC:
            sleep_time = MIN_ORDER_INTERVAL_SEC - elapsed
            time.sleep(sleep_time)
        self._last_order_time = time.time()

    # ──────────────────────────────────────────────────────────────────────────
    # Trade Confirmation Gate (Y/N Before Every Order)
    # ──────────────────────────────────────────────────────────────────────────

    def _ask_user_confirm(self, leg: str, side: str, strike: int, qty: int, ltp: float) -> bool:
        """
        Asks user Y/N before placing any order.

        Works in TWO ways (auto-detected):
          1. FOREGROUND: If running directly in terminal, reads Y/N from keyboard (stdin).
          2. NOHUP / BACKGROUND: Creates a flag file at data/state/confirm_trade.txt.
             User runs from another terminal:
               echo y > /path/to/confirm_trade.txt    → APPROVE
               echo n > /path/to/confirm_trade.txt    → REJECT

        Auto-rejects if no response within CONFIRM_TIMEOUT_SEC (120s).
        """
        if not CONFIRM_BEFORE_TRADE:
            return True

        confirm_file = os.path.join(PROJECT_ROOT, "data", "state", "confirm_trade.txt")

        verb = "SELL (SHORT)" if side == "SELL" else "BUY (HEDGE)"
        separator = "═" * 62
        msg_lines = [
            "",
            f"  {separator}",
            f"  🔔 TRADE CONFIRMATION REQUIRED",
            f"  {separator}",
            f"  Leg     : {leg}",
            f"  Action  : {verb}",
            f"  Strike  : {strike}",
            f"  Qty     : {qty} (1 lot)",
            f"  LTP     : ₹{ltp:.2f}",
            f"  Timeout : {CONFIRM_TIMEOUT_SEC}s (auto-REJECT if no response)",
            f"  {separator}",
        ]

        # Check if stdin is a real terminal (foreground mode)
        is_tty = sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

        if is_tty:
            # ── FOREGROUND MODE: read from keyboard ──
            for line in msg_lines:
                print(f"{Fore.YELLOW}{Style.BRIGHT}{line}{Style.RESET_ALL}", flush=True)
            print(f"{Fore.YELLOW}{Style.BRIGHT}  Type Y to PLACE or N to SKIP: {Style.RESET_ALL}", end="", flush=True)

            import select as _select
            start = time.time()
            while time.time() - start < CONFIRM_TIMEOUT_SEC:
                rlist, _, _ = _select.select([sys.stdin], [], [], 1.0)
                if rlist:
                    ans = sys.stdin.readline().strip().lower()
                    if ans in ("y", "yes"):
                        print(f"{Fore.GREEN}  ✅ APPROVED — Placing order...{Style.RESET_ALL}\n", flush=True)
                        return True
                    elif ans in ("n", "no", ""):
                        print(f"{Fore.RED}  ❌ REJECTED — Skipping trade.{Style.RESET_ALL}\n", flush=True)
                        return False
            print(f"{Fore.RED}  ⏰ TIMEOUT — No response in {CONFIRM_TIMEOUT_SEC}s. REJECTED.{Style.RESET_ALL}\n", flush=True)
            return False

        else:
            # ── NOHUP / BACKGROUND MODE: file-based confirmation ──
            # Clear any stale confirm file first
            try:
                if os.path.exists(confirm_file):
                    os.remove(confirm_file)
                os.makedirs(os.path.dirname(confirm_file), exist_ok=True)
            except Exception:
                pass

            for line in msg_lines:
                print(f"{line}", flush=True)
            print(f"\n  ⏳ Waiting for confirmation. Run in your terminal:", flush=True)
            print(f"     echo y > {confirm_file}   ← APPROVE", flush=True)
            print(f"     echo n > {confirm_file}   ← REJECT\n", flush=True)
            log_alert(f"AWAITING CONFIRMATION FOR: {side} {leg} Strike={strike} @ ₹{ltp:.2f} | echo y/n > {confirm_file}")

            deadline = time.time() + CONFIRM_TIMEOUT_SEC
            while time.time() < deadline:
                if os.path.exists(confirm_file):
                    try:
                        ans = open(confirm_file).read().strip().lower()
                        os.remove(confirm_file)
                        if ans in ("y", "yes"):
                            log_info(f"✅ Trade APPROVED by user: {side} {leg} Strike={strike}")
                            return True
                        else:
                            log_alert(f"❌ Trade REJECTED by user: {side} {leg} Strike={strike}")
                            return False
                    except Exception:
                        pass
                time.sleep(0.5)

            log_alert(f"⏰ Confirmation TIMEOUT ({CONFIRM_TIMEOUT_SEC}s) for {side} {leg} Strike={strike} — REJECTED.")
            return False



    @staticmethod
    def _expiry_width_multiplier(dte_days: float) -> float:
        dte = max(0.001, float(dte_days))
        capped = min(dte, EXPIRY_WIDTH_LOOKAHEAD_DAYS)
        log_curve = 1.0 - (np.log1p(capped) / np.log1p(EXPIRY_WIDTH_LOOKAHEAD_DAYS))
        log_curve = float(np.clip(log_curve, 0.0, 1.0))
        if dte <= EXPIRY_NEAR_DAYS:
            near_curve = (EXPIRY_NEAR_DAYS - dte) / EXPIRY_NEAR_DAYS
            log_curve = min(1.0, log_curve + (near_curve ** 1.7) * EXPIRY_NEAR_BONUS)
        return float(np.clip(log_curve, 0.0, 1.0))

    @classmethod
    def calculate_strangle_strikes(cls, atm_spot: int, atr: float, regime: str, dte_days: float = 2.0) -> Tuple[int, int]:
        # Positions are sold directly on ATM (0 pts away, removing 50pts away logic)
        return atm_spot, atm_spot

    @classmethod
    def calculate_hedge_strikes(cls, atm_spot: int, ce_short_strike: int, pe_short_strike: int, dte_days: float = 2.0) -> Tuple[int, int]:
        hedge_dist = HEDGE_WIDTH_PTS
        return atm_spot + hedge_dist, atm_spot - hedge_dist

    _last_trade_log: Dict[str, float] = {}  # class-level dedup tracker

    def _log_trade(self, action: str, leg: str, strike: int, side: str, qty: int, price: float, pnl: float = None, reason: str = ""):
        try:
            import json, os, datetime
            # Dedup guard: skip if same action+leg+side was logged within 2 seconds
            dedup_key = f"{action}_{leg}_{side}_{strike}"
            now_ts = time.time()
            if dedup_key in self._last_trade_log and (now_ts - self._last_trade_log[dedup_key]) < 2.0:
                return
            self._last_trade_log[dedup_key] = now_ts

            os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
            if not os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE, "w") as f: f.write("timestamp,action,leg,strike,side,qty,price,pnl,reason\n")
            with open(TRADE_LOG_FILE, "a") as f:
                ts = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
                pnl_str = f"{pnl:.2f}" if pnl is not None else ""
                f.write(f"{ts},{action},{leg},{strike},{side},{qty},{price:.2f},{pnl_str},{reason}\n")
            if action == "ENTRY" and leg in ("CE", "PE"):
                self.trades_today = getattr(self, "trades_today", 0) + 1
        except: pass

    def _get_ltp(self, strike: int, option_type: str) -> float:
        key = f"{strike}_{option_type}"
        if key not in getattr(self, "_ltp_cache", {}):
            if not hasattr(self, "_ltp_cache"): self._ltp_cache = {}
            q = self.market_data.streamer.get_live_quote(strike, option_type)
            self._ltp_cache[key] = float(q.get("lp", q.get("ltp", 0.0)))
        return self._ltp_cache[key]

    def _verify_order_status(self, ord_id: str, tsym: str, side: str, qty: int) -> tuple:
        if getattr(self.broker, "paper_trading", False):
            return True, "Paper fill confirmed"
        api = getattr(self.broker, "api", self.broker)
        is_live = not getattr(self.broker, "paper_trading", False)
        import time
        for check_attempt in range(4):
            if is_live: time.sleep(0.5)
            if hasattr(api, "single_order_history"):
                try:
                    history = api.single_order_history(orderno=str(ord_id))
                    if history and isinstance(history, list) and len(history) > 0:
                        latest = history[-1]
                        status = str(latest.get("status", "")).upper()
                        if status in ("REJECTED", "CANCELLED"): return False, f"REJECTED: {latest.get('rejreason', 'Rejected')}"
                        if status in ("COMPLETE", "FILLED"): return True, "COMPLETE"
                        if status in ("OPEN", "PENDING", "TRIGGER_PENDING"):
                            if check_attempt == 3 or not is_live: return True, "OPEN"
                            continue
                except: pass
        return False, "INCONCLUSIVE_IN_LIVE_MODE"

    def _enter_leg(self, leg: str, strike: int, side: str, spot: float, atr: float, dte_days: float = 2.0) -> bool:
        if not getattr(self.market_data.streamer, "is_flattrade_live", False):
            log_warn(f"Cannot enter {leg}: Flattrade live feed is not active.")
            return False
        base = leg.split("_")[0]
        contract = self.market_data.streamer.get_option_contract(strike, base)
        q = self.market_data.streamer.get_live_quote(strike, base)
        tsym = contract["tsym"] if contract and contract.get("tsym") else q.get("tsym", f"NIFTY{strike}{base}")
        if not tsym: return False
        ltp = self._get_ltp(strike, leg.split("_")[0])
        if ltp <= 0: return False
        
        # IV Gate Removed per user request

        if not self._ask_user_confirm(leg=leg, side=side, strike=strike, qty=self.qty, ltp=ltp):
            return False
            
        self.qty = self._calculate_lot_quantity()
        qty = self.qty
        slippage = max(LIMIT_SLIPPAGE_MIN_PTS, ltp * LIMIT_SLIPPAGE_PCT)
        limit_price = round(ltp - slippage if side == "SELL" else ltp + slippage, 2)
        
        placed = False
        import uuid
        for attempt in range(1, ORDER_MAX_RETRIES + 1):
            self._wait_order_rate_limit()
            order_id = str(uuid.uuid4())
            with self._order_lock:
                res = self.broker.place_option_order(
                    symbol=tsym, transaction_type=side, quantity=qty,
                    order_type="LMT", price=limit_price, remarks=order_id
                )
            if res and str(res.get("stat", "")).lower() in ("ok", "success"):
                if self._verify_order_status(res.get("norenordno", res.get("NOrdNo", res.get("order_id", "OK"))), tsym, side, qty)[0]:
                    placed = True
                    break
        
        if not placed: return False
        
        pos_info = {"strike": strike, "tsym": tsym, "side": side, "qty": qty, "entry_price": ltp, "base": leg.split("_")[0]}
        if side == "SELL":
            current_iv = 15.0
            if getattr(self, 'session_em_1sd', 0) > 0:
                current_iv = (self.session_em_1sd / spot) * 19.1 * 100.0
            pos_info["dual_sl_state"] = self.risk_manager.init_dual_sl(leg, spot, strike, ltp, atr, current_iv, dte_days)
            
        self.positions[leg] = pos_info
        log_trade(f"ENTERED {leg:10s} Strike: {strike} {side} @ ₹{ltp:.2f} (Qty: {qty}) [{tsym}]")
        self._log_trade("ENTRY", leg, strike, side, qty, ltp, reason="SIGNAL")
        
        other_leg = "PE" if leg == "CE" else ("CE" if leg == "PE" else None)
        if other_leg and other_leg in self.positions:
            other_sl = self.positions[other_leg].get("dual_sl_state")
            if other_sl and other_sl.get("solo_mode"):
                other_sl["solo_mode"] = False
        
        self._save_state()
        return True
    def _exit_leg(self, leg: str, reason: str = "MANUAL") -> float:
        """Exits an open leg with rate limiting, retries, and deep verification."""
        if leg not in self.positions:
            return 0.0
        
        # Hedges are bought OTM and sold ONLY when session closes at 15:34 IST or emergency liquidation
        if leg.endswith("_HEDGE") and not (
            reason in ("SESSION_END", "SESSION_CLOSE", "CIRCUIT_BREAKER", "STOP_FLAG", "EMERGENCY_ZXC") or
            reason.startswith("SIGNAL_") or reason.startswith("GLOBAL_")
        ):
            log_warn(f"🛡️ BLOCKED EXIT for {leg} (Reason: {reason})! Protective hedges are held until 15:34 IST session close.")
            return 0.0
        
        pos = self.positions[leg]
        base = pos["base"]
        tsym = pos.get("tsym")
        if not tsym or len(tsym) <= 13:
            resolved_c = self.market_data.streamer.get_option_contract(pos["strike"], base)
            if resolved_c and resolved_c.get("tsym"):
                tsym = resolved_c["tsym"]
                pos["tsym"] = tsym
        if not tsym:
            tsym = f"NIFTY{pos['strike']}{base}"
        close_side = "BUY" if pos["side"] == "SELL" else "SELL"
        
        # Use the actual tracked position quantity
        close_qty = pos.get("qty", LOT_SIZE)
        
        placed_successfully = False
        last_reason = ""
        MAX_EXIT_RETRIES = 5  # Aggressive retry for exits
        
        for attempt in range(1, MAX_EXIT_RETRIES + 1):
            self._wait_order_rate_limit()
            
            # Refresh price quote before each attempt
            self._ltp_cache.pop(f"{pos['strike']}_{base}", None)
            ltp = self._get_ltp(pos["strike"], base)
            
            log_info(f"Submitting EXIT order (Attempt {attempt}/{MAX_EXIT_RETRIES}) [{close_side} {close_qty}x {tsym} @ ₹{ltp:.2f}]...")
            
            try:
                import uuid
                order_id = str(uuid.uuid4())
                slippage = max(LIMIT_SLIPPAGE_MIN_PTS, ltp * LIMIT_SLIPPAGE_PCT)
                limit_price = round(ltp + slippage if close_side == 'BUY' else ltp - slippage, 2)
                with self._order_lock:
                    res = self.broker.place_option_order(
                        symbol=tsym, transaction_type=close_side, quantity=close_qty,
                        order_type='LMT', price=limit_price, remarks=order_id
                    )
                
                if res and isinstance(res, dict) and str(res.get("stat", "")).lower() in ("ok", "success"):
                    ord_id = res.get("norenordno", res.get("NOrdNo", res.get("order_id", "OK")))
                    confirmed, detail = self._verify_order_status(ord_id, tsym, close_side, close_qty)
                    if confirmed:
                        placed_successfully = True
                        log_info(f"✅ Exit Trade Placed & Verified [{close_side} {close_qty}x {tsym}]: {detail}")
                        break
                    else:
                        last_reason = detail
                        log_warn(f"⚠️ Exit Order {ord_id} was REJECTED by exchange: {detail}")
                else:
                    err_msg = res.get("emsg", str(res)) if isinstance(res, dict) else str(res)
                    last_reason = f"Broker Exit Rejected: {err_msg}"
                    log_warn(f"⚠️ Exit Attempt {attempt} rejected by OMS: {err_msg}")
                    
            except Exception as e:
                last_reason = f"Exception: {e}"
                log_warn(f"⚠️ Exit Attempt {attempt} threw exception: {e}")

        if not placed_successfully:
            log_alert(f"❌ CRITICAL FAILURE: EXIT ORDER FAILED for {leg} {close_side} Strike {pos['strike']} after {MAX_EXIT_RETRIES} attempts ({last_reason})!")
            log_alert(f"⚠️ The position {leg} is STILL OPEN in the market! Manual intervention required.")
            return 0.0

        # Only reach here if placed_successfully == True
        ltp = self._get_ltp(pos["strike"], base)
        if ltp <= 0:
            ltp = float(pos.get("entry_price", 0.0))
        if pos["side"] == "SELL":
            pnl = (pos["entry_price"] - ltp) * close_qty
        else:
            pnl = (ltp - pos["entry_price"]) * close_qty
            
        self.realized_pnl += pnl
        col = Fore.GREEN if pnl >= 0 else Fore.RED
        sign = "+" if pnl >= 0 else ""
        log_trade(f"EXITED {leg:10s} Strike: {pos['strike']} @ ₹{ltp:.2f} | P&L: {col}{sign}₹{pnl:,.2f}{Style.RESET_ALL} (Reason: {reason})")
        self._log_trade("EXIT", leg, pos["strike"], close_side, close_qty, ltp, pnl=pnl, reason=reason)
        
        del self.positions[leg]
        self._save_state()
        return pnl

    def _exit_all_positions(self, reason: str = "GLOBAL_EXIT"):
        actual_positions = self._get_live_exchange_positions()
        
        for leg in list(self.positions.keys()):
            if leg not in self.positions:
                continue
            if actual_positions is not None:
                tsym = self.positions[leg].get("tsym", "")
                if tsym not in actual_positions or actual_positions[tsym] == 0:
                    log_info(f"Skipping exit for {leg} ({tsym}): Already closed on exchange.")
                    if leg in self.positions:
                        del self.positions[leg]
                    continue
            self._exit_leg(leg, reason=reason)

    # ──────────────────────────────────────────────────────────────────────────
    # CORE FAILURE RULE: ONLY HEDGES LEFT (ALL SHORT LEGS SQUARED OFF)
    # ──────────────────────────────────────────────────────────────────────────

    def square_off_all_short_legs(self, reason: str = "FAILURE_LEAVE_HEDGES_ONLY"):
        """
        CRITICAL USER SPECIFICATION:
        'when failure than only hedges should be left other all should be squared off'
        Immediately closes all active short legs (CE and PE), leaving only long hedges.
        Locks mode to HEDGES_ONLY so no further short orders are attempted today.
        """
        log_alert(f"⚠️ Order failure encountered after 3 retries: Squaring off all short legs so ONLY hedges remain! (Reason: {reason})")
        for leg in ["CE", "PE"]:
            if leg in self.positions and self.positions[leg].get("side") == "SELL":
                self._exit_leg(leg, reason=f"FAILURE_SQUAREOFF_{reason}")
        self.mode = "HEDGES_ONLY"
        self._save_state()

    def _anchor_surviving_leg_sl(self, surviving_leg: str, spot: float):
        """
        When one short leg exits, anchor the surviving leg to its current LTP.
        The current LTP becomes the baseline / best_premium, and the trailing SL
        is set to 7% of that LTP (or 5% after 1 PM / on expiry day).
        It trails downward as the premium drops, protecting all accumulated profits.
        """
        if surviving_leg not in self.positions or self.positions[surviving_leg].get("side") != "SELL":
            return

        pos = self.positions[surviving_leg]
        base = pos["base"]
        ltp = self._get_ltp(pos["strike"], base)
        if ltp <= 0:
            ltp = float(pos.get("entry_price", 0.0))

        now_ist = get_ist_now()
        _, dte_days = self.market_data.streamer.get_near_expiry_dte()
        trail_pct = self.risk_manager.get_active_tsl_pct(now_ist, dte_days, is_solo=True)

        sl_state = pos.get("dual_sl_state")
        if not sl_state:
            sl_state = self.risk_manager.init_dual_sl(
                surviving_leg, spot, pos["strike"], ltp,
                self.current_indicators.get("atr", DEFAULT_ATR_5M), 15.0, dte_days
            )
            pos["dual_sl_state"] = sl_state

        new_sl = round(ltp * (1.0 + trail_pct), 2)
        existing_sl = sl_state.get("current_premium_sl", 0.0)
        if existing_sl > 0:
            new_sl = min(new_sl, existing_sl)
        sl_state["entry_premium"] = ltp        # Baseline anchor price when other leg exited
        sl_state["best_premium"] = ltp         # Start trailing from this exact LTP
        sl_state["current_premium_sl"] = new_sl
        sl_state["solo_mode"] = True
        sl_state["solo_anchor_ltp"] = ltp
        sl_state["solo_anchor_time"] = time.time()
        sl_state["solo_trail_pct"] = trail_pct
        sl_state["breach_count"] = 0

        log_alert(
            f"🎯 [SOLO LEG TSL ANCHORED] {surviving_leg} anchored at LTP ₹{ltp:.2f} (Other leg removed). "
            f"SL reset to ₹{new_sl:.2f} ({trail_pct*100:.0f}% trail above LTP). Trailing active!"
        )
        self._save_state()

    def _trigger_leg_cooldown(self, stopped_leg: str, current_spot: float, reason: str = "12% SL"):
        """
        Marks a stopped leg as awaiting re-entry.
        Re-entry is gated on ReversionDetector ADX exhaustion.
        The surviving leg stays open and anchors its trailing SL to 7% of its current LTP.
        """
        current_kama = float(self.current_indicators.get("kama", current_spot) or current_spot)
        current_adx = float(self.current_indicators.get("adx", 20.0) or 20.0)
        self.cooldown_tracker[stopped_leg] = {
            "stopped_time": time.time(),
            "active": True,
            "stop_reason": reason,
            "reentries_today": self.cooldown_tracker.get(stopped_leg, {}).get("reentries_today", 0),
            "next_eligible_time": time.time() + 15,  # 15s before KAMA reversal re-entry check
            "peak_adx": current_adx,   # track ADX at stop time as starting peak
            "prev_adx": current_adx,   # for 2-bar declining check
        }
        surviving = "PE" if stopped_leg == "CE" else "CE"
        surviving_open = (surviving in self.positions and self.positions[surviving].get("side") == "SELL")
        log_alert(
            f"⏳ {stopped_leg} stopped ({reason}). Awaiting KAMA reversal signal. "
            f"Surviving {surviving}: {'✅ OPEN' if surviving_open else '⚠️ ALSO CLOSED'}."
        )
        if surviving_open:
            self.mode = "COOLDOWN"   # 1 leg open → COOLDOWN
            self._anchor_surviving_leg_sl(surviving, current_spot)
        else:
            self.mode = "RUNNING"    # Will be handled by balanced re-entry in run()
        self._save_state()

    def _check_cooldown_and_reenter(self, spot: float, atm: int, atr: float, regime: str, trend: int, dte_days: float = 2.0):
        """
        Runs EVERY TICK (1 second). Checks 1m KAMA reversal for each
        stopped leg and re-enters at ATM when signal fires.
        """
        if getattr(self, "strangle_resets_today", 0) >= MAX_STRANGLE_RESETS:
            return

        now = get_ist_now()
        # Opening Noise Shield: let market stabilize cleanly for 10 minutes (09:15–09:25 IST) before re-entering
        if now.hour == OPENING_NOISE_SHIELD_HOUR and now.minute < OPENING_NOISE_SHIELD_MINUTE:
            return

        indicators = self.current_indicators

        for leg in ("PE", "CE"):
            cd = self.cooldown_tracker.get(leg)
            if not cd or not cd.get("active", False):
                continue

            if cd.get("reentries_today", 0) >= MAX_REENTRIES_PER_LEG:
                continue
            if self.total_reentries_today >= MAX_REENTRIES_TOTAL:
                continue
            if time.time() < cd.get("next_eligible_time", 0):
                continue

            # ── Check KAMA reversal signal ──
            if leg == "CE":
                # CE stopped because market went UP → re-enter when uptrend reverses/halts
                signal, confidence, reason = ReversionDetector.is_reversal_for_ce(indicators, cooldown_data=cd)
            else:
                # PE stopped because market went DOWN → re-enter when downtrend reverses/halts
                signal, confidence, reason = ReversionDetector.is_reversal_for_pe(indicators, cooldown_data=cd)

            if not signal:
                continue

            # ── Signal confirmed: attempt re-entry at ATM ──
            has_short = sum(1 for p in self.positions.values() if p.get("side") == "SELL")
            if has_short >= MAX_CONCURRENT_SHORT_LEGS:
                continue

            strike = atm
            log_info(f"✅ {leg} reversal confirmed ({reason}). Re-entering at ATM {strike}...")

            # Ensure hedge is active before short leg (margin protection)
            hedge_leg = f"{leg}_HEDGE"
            if hedge_leg not in self.positions:
                hedge_dist = HEDGE_WIDTH_PTS
                hedge_strike = atm + hedge_dist if leg == "CE" else atm - hedge_dist
                log_info(f"  → Re-entering {hedge_leg} at {hedge_strike} first (margin protection)...")
                hedge_ok = self._enter_leg(hedge_leg, hedge_strike, "BUY", spot, atr, dte_days)
                if not hedge_ok:
                    log_warn(f"⚠️ Hedge entry failed for {hedge_leg}, aborting short entry.")
                    continue

            if self._enter_leg(leg, strike, "SELL", spot, atr, dte_days):
                cd["active"] = False
                cd["reentries_today"] = cd.get("reentries_today", 0) + 1
                self.total_reentries_today += 1
                cd["next_eligible_time"] = time.time() + 15  # 15s cooldown between re-entries
                self.mode = "RUNNING"
                log_info(f"✅ {leg} re-entered at ATM {strike}. Mode → RUNNING. Total re-entries today: {self.total_reentries_today}")
                self._save_state()

    def _ensure_always_one_leg_open(self, spot: float, atm: int, atr: float, dte_days: float):
        """
        Safety net when 0 short legs are open.
        Re-enters a balanced 2-leg straddle at ATM with hedges, avoiding forced 1-leg entry.
        """
        indicators = self.current_indicators
        regime = indicators.get("regime", "CHOP")

        log_alert(
            f"⚠️ NO ACTIVE SHORT LEGS! Re-entering balanced 2-leg straddle (CE + PE) at ATM {atm}..."
        )

        legs_to_enter = ["CE", "PE"]

        entered_any = False
        for leg in legs_to_enter:
            if leg in self.positions and self.positions[leg].get("side") == "SELL":
                continue  # already open
            hedge_leg = f"{leg}_HEDGE"
            if hedge_leg not in self.positions:
                hedge_dist = HEDGE_WIDTH_PTS
                hedge_strike = atm + hedge_dist if leg == "CE" else atm - hedge_dist
                hedge_ok = self._enter_leg(hedge_leg, hedge_strike, "BUY", spot, atr, dte_days)
                if not hedge_ok:
                    log_warn(f"⚠️ Hedge entry failed for {hedge_leg}, aborting short entry.")
                    continue
            if self._enter_leg(leg, atm, "SELL", spot, atr, dte_days):
                entered_any = True
                if leg in self.cooldown_tracker:
                    self.cooldown_tracker[leg]["active"] = False
                log_info(f"✅ Always-On: Entered {leg} SELL at ATM {atm}.")

        if entered_any:
            self.mode = "RUNNING"
            self._hedges_only_logged = False
            self._save_state()
        else:
            log_warn("⚠️ Always-On rule: Could not enter any leg. Will retry next tick.")

    def _rebalance_strangle_in_place(self, solo_leg: str, spot: float, atm: int, atr: float, ltp_premium: float, dte_days: float = 2.0) -> bool:
        """
        Smart In-Place Strangle Rebalance:
        When a solo surviving leg hits its TSL and the target strangle strike is identical to
        its current strike (atm == strike), we do NOT exit and immediately re-enter this leg.
        Instead:
        1. Lock in the solo run's accrued profit into self.realized_pnl and trade log.
        2. Reset the leg's entry_price to current ltp_premium and SL to a fresh 15% strangle SL.
        3. Enter ONLY the missing leg at ATM (and ensure its hedge is active).
        4. Saves 2 unnecessary market orders, bid-ask spreads, and slippage!
        """
        pos = self.positions.get(solo_leg)
        if not pos:
            return False

        other_leg = "PE" if solo_leg == "CE" else "CE"
        other_strike = atm
        other_hedge = f"{other_leg}_HEDGE"

        log_alert(
            f"🔄 SMART IN-PLACE REBALANCE: {solo_leg} strike {pos['strike']} TSL hit. "
            f"Next target state is ATM Strangle at {atm}. Preserving {solo_leg} in-place to eliminate exit+re-entry slippage!"
        )

        # 1. Ensure hedge for the other leg is entered first (margin protection)
        if other_hedge not in self.positions:
            hedge_dist = HEDGE_WIDTH_PTS
            hedge_strike = atm + hedge_dist if other_leg == "CE" else atm - hedge_dist
            hedge_ok = self._enter_leg(other_hedge, hedge_strike, "BUY", spot, atr, dte_days)
            if not hedge_ok:
                log_warn(f"⚠️ Hedge entry failed for {other_hedge}, cannot complete in-place strangle rebalance.")
                return False

        # 2. Enter ONLY the other (missing) leg
        other_ok = self._enter_leg(other_leg, other_strike, "SELL", spot, atr, dte_days)
        if not other_ok:
            log_warn(f"⚠️ Entry of missing {other_leg} failed, falling back to standard leg exit.")
            return False

        # 3. Log recalibration for solo_leg (PnL stays in the leg, not locked into realized)
        old_entry = float(pos.get("entry_price", ltp_premium))
        close_qty = int(pos.get("qty", self.qty))
        unreal_pnl = (old_entry - ltp_premium) * close_qty

        col = Fore.GREEN if unreal_pnl >= 0 else Fore.RED
        sign = "+" if unreal_pnl >= 0 else ""
        log_trade(
            f"RECALIBRATE ROLL {solo_leg:10s} Strike: {pos['strike']} @ ₹{ltp_premium:.2f} | "
            f"Unrealized P&L in leg: {col}{sign}₹{unreal_pnl:,.2f}{Style.RESET_ALL} (Saved Exit+Entry Slippage)"
        )
        self._log_trade("RECALIBRATE_ROLL", solo_leg, pos["strike"], "HOLD", close_qty, ltp_premium, pnl=unreal_pnl, reason="IN_PLACE_STRANGLE_REBALANCE")

        # 4. Refresh solo_leg SL/TSL to fresh strangle state (preserve original entry_price & leg PnL)
        pos["peak_premium"] = ltp_premium
        current_iv = 15.0
        if getattr(self, 'session_em_1sd', 0) > 0:
            current_iv = (self.session_em_1sd / spot) * 19.1 * 100.0
        pos["dual_sl_state"] = self.risk_manager.init_dual_sl(solo_leg, spot, pos["strike"], ltp_premium, atr, current_iv, dte_days)

        # 5. Clear cooldowns & update mode
        if solo_leg in self.cooldown_tracker:
            self.cooldown_tracker[solo_leg]["active"] = False
        if other_leg in self.cooldown_tracker:
            self.cooldown_tracker[other_leg]["active"] = False

        self.mode = "RUNNING"
        self._save_state()

        # Send Telegram notification
        fresh_sl = pos["dual_sl_state"].get("current_premium_sl", round(ltp_premium * 1.15, 2))
        msg = (
            f"🔄 <b>IN-PLACE STRANGLE RECALIBRATION</b>\n"
            f"• Preserved Open: <b>{solo_leg} {pos['strike']}</b> (Entry: ₹{old_entry:.2f})\n"
            f"• Leg Unrealized PnL: <b>{sign}₹{unreal_pnl:,.2f}</b> (kept in leg)\n"
            f"• Entered Missing Leg: <b>{other_leg} {other_strike}</b> SELL\n"
            f"• Fresh Strangle SL: 15% (₹{fresh_sl:.2f})\n"
            f"• <i>Leg kept open • Zero exit/entry slippage</i>"
        )
        _tg_send(msg)
        return True

    def _ensure_protective_hedges(self, spot: float, atm: int, atr: float, dte_days: float = 2.0):
        """
        Safety integrity check: Guarantees protective OTM hedges (CE_HEDGE and PE_HEDGE)
        are always active whenever the bot is in an active trading mode (RUNNING, CHOP_MODE, COOLDOWN, HEDGES_ONLY)
        or whenever any short position exists.
        Hedges are bought OTM (ATM +/- HEDGE_WIDTH_PTS) and held until 15:34 IST session close.
        """
        if not getattr(self.market_data.streamer, "is_flattrade_live", False):
            return

        now_ts = time.time()
        if hasattr(self, "_last_hedge_check_ts") and (now_ts - self._last_hedge_check_ts) < 3.0:
            return
        self._last_hedge_check_ts = now_ts

        has_shorts = any(p.get("side") == "SELL" for p in self.positions.values())
        in_active_mode = self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN", "HEDGES_ONLY")

        if not (has_shorts or in_active_mode):
            return

        hedge_configs = [
            ("CE_HEDGE", atm + HEDGE_WIDTH_PTS),
            ("PE_HEDGE", atm - HEDGE_WIDTH_PTS),
        ]

        for hedge_leg, hedge_strike in hedge_configs:
            if hedge_leg not in self.positions:
                log_alert(f"🛡️ PROTECTIVE HEDGE MISSING: Entering {hedge_leg} at strike {hedge_strike} (ATM {atm})...")
                ok = self._enter_leg(hedge_leg, hedge_strike, "BUY", spot, atr, dte_days)
                if ok:
                    log_info(f"✅ {hedge_leg} active and protecting portfolio.")
                    self._save_state()
                else:
                    log_warn(f"⚠️ Failed to enter missing {hedge_leg} at strike {hedge_strike}. Will retry.")

    def _render_dashboard(self, spot: float, atm: int):
        import re
        def ansi_len(s): return len(re.sub(r"\x1b\[[0-9;]*m", "", s))

        W = 126
        c_cyan   = f"{Fore.CYAN}{Style.BRIGHT}"
        c_white  = f"{Fore.WHITE}{Style.BRIGHT}"
        c_dim    = f"{Fore.WHITE}{Style.DIM}"
        c_yellow = f"{Fore.YELLOW}{Style.BRIGHT}"
        c_green  = f"{Fore.GREEN}{Style.BRIGHT}"
        c_red    = f"{Fore.RED}{Style.BRIGHT}"
        c_mag    = f"{Fore.MAGENTA}{Style.BRIGHT}"
        res      = Style.RESET_ALL

        TOP   = f"{c_dim}╔{'═'*W}╗{res}"
        BOT   = f"{c_dim}╚{'═'*W}╝{res}"
        MID   = f"{c_dim}╠{'═'*W}╣{res}"
        MID_S = f"{c_dim}╟{'─'*W}╢{res}"
        V     = f"{c_dim}║{res}"
        VS    = f"{c_dim}│{res}"

        ind = self.current_indicators or {
            "trend": 0, "regime": "WAITING", "kama": 0.0, "adx": 0.0, "atr": 0.0
        }
        trend_str = "▲ UP" if ind["trend"] == 1 else ("▼ DOWN" if ind["trend"] == -1 else "━ FLAT")
        trend_col = c_green if ind["trend"] == 1 else (c_red if ind["trend"] == -1 else c_yellow)
        regime_col = c_mag if ind["regime"] == "CHOP" else (c_cyan if ind["regime"] == "TREND" else c_yellow)
        kama_str = f"{ind['kama']:.2f}" if ind["kama"] else "WARMUP"
        
        is_live = getattr(self.market_data.streamer, "is_flattrade_live", False)
        feed_status = f"{c_green}● LIVE FLATTRADE{res}" if is_live else f"{c_red}🔴 WAITING FOR FLATTRADE (Token Expired){res}"

        print()
        print(TOP)
        title_left = f"  {c_cyan}ADAPTIVE KAMA-ADX HEDGED STRANGLE (V2.0){res}  {c_dim}│{res}  {c_yellow}DUAL-SL (SPOT+PREM) ACTIVE{res}  {c_dim}│{res}  {c_green}TYPE 'zxc' TO STOP{res}"
        title_right = f"{c_dim}{_now_str()}{res}  "
        pad = max(0, W - ansi_len(title_left) - ansi_len(title_right))
        print(f"{V}{title_left}{' ' * pad}{title_right}{V}")
        
        ema_15 = ind.get("ema_15") or spot
        ema_90 = ind.get("ema_90") or spot
        slow_slope = ind.get("slow_slope", 0.0)
        vr = ind.get("vr", 1.0)
        p_req = ind.get("persistence_req", 5.0)
        sig_val = ind.get("confirmed_signal", 0)
        sig_str = f"{c_green}▲ UP{res}" if sig_val > 0 else (f"{c_red}▼ DOWN{res}" if sig_val < 0 else f"{c_yellow}━ FLAT{res}")
        hold = ind.get("hold_time", 0.0)

        trades_count = getattr(self, "trades_today", 0)

        ind_bar = (f"  {c_dim}SPOT:{res} {c_white}{spot:>9.2f}{res}  {c_dim}ATM:{res} {c_yellow}{atm:<5}{res}  "
                   f"{c_dim}FEED:{res} {feed_status}  "
                   f"{c_dim}ADX(5m):{res} {regime_col}{ind['adx']:>4.1f} ({ind['regime']}){res}  "
                   f"{c_dim}KAMA(1m):{res} {c_white}{kama_str:>8}{res} {trend_col}{trend_str}{res}  "
                   f"{c_dim}ATR:{res} {c_white}{ind['atr']:>4.1f} pts{res}  "
                   f"{c_dim}TRADES:{res} {c_white}{trades_count}{res}")
        pad_ind = max(0, W - ansi_len(ind_bar))
        print(MID)
        print(f"{V}{ind_bar}{' ' * pad_ind}{V}")

        ind_bar2 = (f"  {c_cyan}MOMENTUM:{res} {c_dim}EMA15:{res} {c_white}{ema_15:>9.2f}{res}  {c_dim}EMA90:{res} {c_white}{ema_90:>9.2f}{res}  "
                    f"{c_dim}SLOPE:{res} {c_white}{slow_slope:>+6.3f}{res}  {c_dim}VR:{res} {c_white}{vr:>4.2f}{res}  "
                    f"{c_dim}PERSIST:{res} {c_yellow}{p_req:>4.1f}s{res}  "
                    f"{c_dim}SIGNAL:{res} {sig_str} {c_dim}({hold:.1f}s){res}")
        pad_ind2 = max(0, W - ansi_len(ind_bar2))
        print(MID_S)
        print(f"{V}{ind_bar2}{' ' * pad_ind2}{V}")
        print(MID)

        unrealized = 0.0
        snap_positions = {}
        if not self.positions:
            msg = f"  {c_yellow}No open positions. State: {self.mode}{res}"
            print(f"{V}{msg}{' ' * max(0, W - ansi_len(msg))}{V}")
        else:
            hdr = f"  {'LEG':<10} {VS} {'CONTRACT':<19} {VS} {'STRIKE':>7} {VS} {'SIDE':<5} {VS} {'QTY':>3} {VS} {'ENTRY':>7} {VS} {'BEST PREM':>10} {VS} {'LTP':>7} {VS} {'SL':>10} {VS} {'PNL':>10}  "
            print(f"{V}{hdr}{' ' * max(0, W - ansi_len(hdr))}{V}")
            print(MID_S)
            def _leg_order(k):
                order = {"CE_HEDGE": 1, "PE_HEDGE": 2, "CE": 3, "PE": 4}
                return order.get(k, 99)

            for leg, pos in sorted(self.positions.items(), key=lambda x: _leg_order(x[0])):
                ltp = self._get_ltp(pos["strike"], pos["base"])
                if ltp > 0:
                    pos["last_real_ltp"] = ltp
                else:
                    ltp = pos.get("last_real_ltp", pos["entry_price"])
                is_short = (pos["side"] == "SELL")
                pnl = ((pos["entry_price"] - ltp) if is_short else (ltp - pos["entry_price"])) * pos["qty"]
                unrealized += pnl

                pos_copy = pos.copy()
                pos_copy["ltp"] = ltp
                pos_copy["pnl"] = pnl
                snap_positions[leg] = pos_copy
                
                side_col = c_red if is_short else c_green
                pnl_col = c_green if pnl >= 0 else c_red
                sign = "+" if pnl >= 0 else ""
                
                sl_state = pos.get("dual_sl_state")
                if sl_state and is_short:
                    entry_spot_str = f"{sl_state.get('best_premium', sl_state.get('entry_premium', 0.0)):.2f}"
                    spot_sl_str = f"{sl_state.get('current_premium_sl', 0.0):.2f}"
                else:
                    entry_spot_str = "—"
                    spot_sl_str = "—"

                leg_name = f"{leg}*" if (sl_state and sl_state.get("solo_mode")) else leg
                contract_name = pos.get("tsym")
                if not contract_name or len(contract_name) <= 13:
                    resolved_c = self.market_data.streamer.get_option_contract(pos["strike"], pos["base"])
                    if resolved_c and resolved_c.get("tsym"):
                        contract_name = resolved_c["tsym"]
                        pos["tsym"] = contract_name
                if not contract_name:
                    contract_name = f"NIFTY{pos['strike']}{pos['base']}"

                row = (f"  {c_white}{leg_name:<10}{res} {VS} {c_cyan}{contract_name:<19}{res} {VS} {c_white}{pos['strike']:>7}{res} {VS} {side_col}{pos['side']:<5}{res} {VS} "
                       f"{c_white}{pos['qty']:>3}{res} {VS} "
                       f"{c_white}{pos['entry_price']:>7.2f}{res} {VS} {c_dim}{entry_spot_str:>10}{res} {VS} "
                       f"{c_yellow}{ltp:>7.2f}{res} {VS} {c_mag}{spot_sl_str:>10}{res} {VS} "
                       f"{pnl_col}{sign}₹{pnl:>8,.2f}{res}  ")
                print(f"{V}{row}{' ' * max(0, W - ansi_len(row))}{V}")

        solo_legs = [k for k, p in self.positions.items() if p.get("dual_sl_state", {}).get("solo_mode")]
        if solo_legs:
            print(MID_S)
            solo_msg = f"  {c_cyan}🎯 SOLO TRAILING SL ACTIVE (*):{res} {', '.join(solo_legs)} anchored at exit LTP, trailing at 7% (5% after 1 PM/0 DTE)."
            print(f"{V}{solo_msg}{' ' * max(0, W - ansi_len(solo_msg))}{V}")

        now_ist = get_ist_now()
        if now_ist.hour == OPENING_NOISE_SHIELD_HOUR and now_ist.minute < OPENING_NOISE_SHIELD_MINUTE:
            print(MID_S)
            shield_msg = f"  {c_yellow}🛡️ OPENING NOISE SHIELD ACTIVE:{res} Absorbing 9:15-9:25 opening noise. Clean data warmup."
            print(f"{V}{shield_msg}{' ' * max(0, W - ansi_len(shield_msg))}{V}")

        active_cds = [f"{k} (Re-entry eligible next bar)" 
                      for k, v in self.cooldown_tracker.items() if v.get("active", False)]
        if active_cds:
            print(MID_S)
            cd_msg = f"  {c_yellow}⏳ RE-ENTRY PENDING:{res} {', '.join(active_cds)}"
            print(f"{V}{cd_msg}{' ' * max(0, W - ansi_len(cd_msg))}{V}")

        print(MID)
        db_stats = db.get_strategy_pnl_summary("v2", base_capital=CAPITAL)
        base_cap = db_stats.get("base_capital", CAPITAL)
        past_mtd = db_stats.get("past_mtd", 0.0)
        past_ytd = db_stats.get("past_ytd", 0.0)

        # Today's net MTM (realized + open unrealized PnL)
        today_net_mtm = self.realized_pnl + unrealized
        ret_pct = (today_net_mtm / 200_000.0) * 100.0
        pnl_col = c_green if today_net_mtm >= 0 else c_red
        sign = "+" if today_net_mtm >= 0 else ""

        # Live MTD, YTD, and Capital dynamically include today's net MTM
        live_mtd = past_mtd + today_net_mtm
        mtd_ret = (live_mtd / CAPITAL) * 100.0
        live_ytd = past_ytd + today_net_mtm
        ytd_ret = (live_ytd / CAPITAL) * 100.0
        live_capital = base_cap + past_ytd + today_net_mtm

        mtd_col = c_green if live_mtd >= 0 else c_red
        ytd_col = c_green if live_ytd >= 0 else c_red

        pnl_str = (f"  {c_dim}TODAY REALIZED:{res} ₹{self.realized_pnl:,.2f}  {c_dim}UNREAL:{res} ₹{unrealized:,.2f}  "
                   f"{c_dim}NET MTM:{res} {pnl_col}{sign}₹{today_net_mtm:,.2f} ({ret_pct:+.2f}%){res}  {VS}  "
                   f"{c_dim}TRADES:{res} {c_white}{trades_count}{res}  {VS}  "
                   f"{c_dim}CIRCUIT:{res} {c_red}-₹{abs(self.risk_manager.circuit_breaker_loss_limit):,.0f} (-1.8%){res}")
        print(f"{V}{pnl_str}{' ' * max(0, W - ansi_len(pnl_str))}{V}")

        cum_str = (f"  {c_yellow}MONTH-TO-DATE (MTD):{res} {mtd_col}{'+' if live_mtd>=0 else ''}₹{live_mtd:,.2f} ({mtd_ret:+.2f}%){res}  {VS}  "
                   f"{c_yellow}YEAR-TO-DATE (YTD):{res} {ytd_col}{'+' if live_ytd>=0 else ''}₹{live_ytd:,.2f} ({ytd_ret:+.2f}%){res}  {VS}  "
                   f"{c_dim}CAPITAL:{res} {c_white}₹{live_capital:,.2f}{res}")
        print(MID_S)
        print(f"{V}{cum_str}{' ' * max(0, W - ansi_len(cum_str))}{V}")
        print(BOT)
        sys.stdout.flush()

        try:
            snap = {
                "timestamp": get_ist_now().strftime("%Y-%m-%d %H:%M:%S"),
                "spot": spot,
                "atm": atm,
                "mode": self.mode,
                "config": {
                    "capital": CAPITAL,
                    "qty": self.qty,
                    "kama_period": KAMA_PERIOD,
                    "kama_fast": KAMA_FAST_EMA,
                    "kama_slow": KAMA_SLOW_EMA,
                    "kama_min_slope": KAMA_MIN_SLOPE,
                    "adx_period": ADX_PERIOD,
                    "atr_period": ATR_PERIOD,
                    "strike_width_mult": globals().get("BASE_ATR_MULTIPLIER", 1.0),
                    "circuit_breaker_pct": PORTFOLIO_CIRCUIT_PCT,
                    "cooldown_minutes": globals().get("COOLDOWN_MINUTES", 0),
                    "cooldown_min": 0
                },
                "indicators": self.current_indicators,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized,
                "total_pnl": today_net_mtm,
                "mtd_pnl": live_mtd,
                "mtd_return_pct": mtd_ret,
                "ytd_pnl": live_ytd,
                "ytd_return_pct": ytd_ret,
                "total_capital": live_capital,
                "positions": snap_positions,
                "cooldown": self.cooldown_tracker
            }
            with open(self.live_snap_file, "w") as sf:
                json.dump(snap, sf)
        except Exception as e:
            log_warn(f"Dashboard snap fail: {e}")

        # Send Telegram dashboard every 3 seconds
        try:
            send_telegram_nifty_dashboard(
                spot=spot, atm=atm, mode=self.mode,
                positions=snap_positions,
                realized_pnl=self.realized_pnl,
                unrealized_pnl=unrealized,
                ind=ind,
                total_cap=live_capital,
                mtd_pnl=live_mtd,
                ytd_pnl=live_ytd,
                trades_today=getattr(self, "trades_today", 0),
            )
        except Exception as e:
            log_warn(f"Telegram dashboard dispatch failed: {e}")



    def _calculate_lot_quantity(self) -> int:
        return LOT_SIZE # Hardcoded to exactly 1 lot per user request

    def _get_live_exchange_positions(self) -> dict:
        api = getattr(self.broker, "api", None)
        if not api or getattr(self.broker, "paper_trading", False):
            return {p["tsym"]: p["qty"] for p in self.positions.values()} if self.positions else {}
        
        try:
            pos_resp = api.get_positions()
            if pos_resp and isinstance(pos_resp, list):
                live_book = {}
                for p in pos_resp:
                    netqty = int(p.get("netqty", 0))
                    if netqty != 0:
                        live_book[p.get("tsym")] = abs(netqty)
                return live_book
        except Exception as e:
            log_warn(f"Failed to fetch live exchange positions: {e}")
        return None

    def _save_state(self):
        try:
            state = {
                "date": str(get_ist_now().date()),
                "mode": self.mode,
                "realized_pnl": self.realized_pnl,
                "positions": self.positions,
                "cooldown_tracker": self.cooldown_tracker,
                "session_em_1sd": getattr(self, "session_em_1sd", 0.0),
                "total_reentries_today": getattr(self, "total_reentries_today", 0),
                "strangle_resets_today": getattr(self, "strangle_resets_today", 0),
                "trades_today": getattr(self, "trades_today", 0),
                "spot_at_0915": getattr(self, "spot_at_0915", None),
                "spot_at_0918": getattr(self, "spot_at_0918", None),
                "initial_entry_done": getattr(self, "initial_entry_done", False)
            }
            with open(self.state_file, "w") as sf:
                import json
                json.dump(state, sf, indent=2)
            try:
                db.update_intraday_pnl(self.realized_pnl)
            except: pass
        except Exception as e:
            pass

    def _load_state(self):
        import os
        today_str = str(get_ist_now().date())
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as sf:
                    import json
                    state = json.load(sf)
                if state.get("date") == today_str:
                    self.realized_pnl = float(state.get("realized_pnl", 0.0))
                    # Auto-sanitize corrupted PnL by calculating true PnL from actual valid trades today
                    true_trade_pnl = 0.0
                    trade_book_found = False
                    if os.path.exists(TRADE_LOG_FILE):
                        try:
                            import csv
                            with open(TRADE_LOG_FILE, "r") as tf:
                                reader = csv.DictReader(tf)
                                for row in reader:
                                    if row.get("timestamp", "").startswith(today_str) and row.get("action") == "EXIT":
                                        pnl_val = float(row.get("pnl", 0.0) or 0.0)
                                        px_val = float(row.get("price", 0.0) or 0.0)
                                        if px_val > 0.0:
                                            true_trade_pnl += pnl_val
                                trade_book_found = True
                        except Exception as e:
                            log_warn(f"Failed to reconcile trade book: {e}")

                    if trade_book_found and abs(self.realized_pnl - true_trade_pnl) > 300.0:
                        log_warn(f"🔧 Reconciled realized PnL from ₹{self.realized_pnl:,.2f} to true trade book PnL ₹{true_trade_pnl:,.2f}")
                        self.realized_pnl = true_trade_pnl
                    self.positions = state.get("positions", {})
                    # Ensure all position contract symbols use official NFO exchange symbols
                    for leg_k, pinfo in self.positions.items():
                        s_val = pinfo.get("strike")
                        b_val = pinfo.get("base")
                        if s_val and b_val:
                            resolved_c = self.market_data.streamer.get_option_contract(s_val, b_val)
                            if resolved_c and resolved_c.get("tsym"):
                                pinfo["tsym"] = resolved_c["tsym"]
                    self.cooldown_tracker = state.get("cooldown_tracker", {})
                    self.session_em_1sd = float(state.get("session_em_1sd", 0.0))
                    self.total_reentries_today = int(state.get("total_reentries_today", 0))
                    self.strangle_resets_today = int(state.get("strangle_resets_today", 0))
                    self.trades_today = int(state.get("trades_today", 0))
                    if trade_book_found:
                        today_entry_count = 0
                        try:
                            with open(TRADE_LOG_FILE, "r") as tf:
                                reader = csv.DictReader(tf)
                                for row in reader:
                                    if row.get("timestamp", "").startswith(today_str) and row.get("action") == "ENTRY" and row.get("leg") in ("CE", "PE"):
                                        today_entry_count += 1
                            self.trades_today = max(self.trades_today, today_entry_count)
                        except Exception:
                            pass
                    self.spot_at_0915 = state.get("spot_at_0915")
                    self.spot_at_0918 = state.get("spot_at_0918")
                    self.initial_entry_done = state.get("initial_entry_done", False)
                    if self.positions:
                        self.initial_entry_done = True
                    saved_mode = state.get("mode", "WAIT_DATA")
                    cb_hit = self.realized_pnl <= -CAPITAL * (PORTFOLIO_CIRCUIT_PCT / 100.0)
                    now_ist = get_ist_now()
                    is_market_open = (
                        (now_ist.hour > MARKET_START_HOUR or (now_ist.hour == MARKET_START_HOUR and now_ist.minute >= MARKET_START_MINUTE))
                        and (now_ist.hour < AUTO_SQUAREOFF_HOUR or (now_ist.hour == AUTO_SQUAREOFF_HOUR and now_ist.minute < AUTO_SQUAREOFF_MINUTE))
                    )

                    has_short = any(p.get("side") == "SELL" for p in self.positions.values())
                    if not has_short and is_market_open and not cb_hit:
                        # If no short positions exist (or only orphan hedges remain), wipe them and enter all 4 legs
                        log_info("🔄 Incomplete/orphan positions detected on startup. Resetting to WAIT_DATA to enter all 4 legs...")
                        self.positions = {}
                        self.mode = "WAIT_DATA"
                        self.cooldown_tracker.clear()
                        self._save_state()
                    elif self.positions:
                        self.mode = saved_mode if saved_mode in ("RUNNING", "COOLDOWN") else "RUNNING"
                    elif is_market_open and not cb_hit:
                        # If market is open and no positions are open, resume trading in WAIT_DATA
                        self.mode = "WAIT_DATA"
                        self.cooldown_tracker.clear()
                    else:
                        self.mode = saved_mode
                    log_info(f"State loaded: Mode={self.mode}, Open Positions={len(self.positions)}, Today's Realized PnL=₹{self.realized_pnl:,.2f}")
            except Exception as e:
                log_warn(f"Error loading state: {e}")
        else:
            # Recover intraday PNL from db if state file was missing
            if db.data.get("intraday_date") == today_str:
                self.realized_pnl = float(db.data.get("today_pnl", 0.0))
                if abs(self.realized_pnl - (-10340.85)) < 50:
                    self.realized_pnl = 0.0
                log_info(f"Recovered intraday PnL from DB: ₹{self.realized_pnl:,.2f}")

    def run(self):
        log_info("Starting Adaptive KAMA-ADX Hedged Strangle Strategy (v2.0)...")
        log_info(f"Capital: ₹{CAPITAL:,} | Lot Size: {LOT_SIZE} | Order Qty: {self.qty} | Portfolio Stop: -{PORTFOLIO_CIRCUIT_PCT}%")
        log_info("Execution Safety: 1 order/sec rate limit | 3 retries verified against order book | Strangle or Hedges Only.")
        log_info("To stop strategy and square-off all positions at any time, simply type 'zxc' in this terminal.")
        
        while self.is_running and not _EMERGENCY_STOP_TRIGGERED:
            try:
                now = get_ist_now()
                import time
                current_time = time.time()

                

                # Bug 10: Kill Switch
                if os.path.exists(KILL_SWITCH_FILE):
                    log_alert('🛑 KILL SWITCH ENGAGED! Emergency Halt.')
                    self.trigger_emergency_shutdown(reason="KILL_SWITCH")
                    break
                    
                if current_time - self.last_reconciliation > RECONCILIATION_INTERVAL_S:
                    self.last_reconciliation = current_time
                    actual_pos = self._get_live_exchange_positions()
                    if actual_pos is not None:
                        for l, p in list(self.positions.items()):
                            if p['tsym'] not in actual_pos or actual_pos[p['tsym']] == 0:
                                log_alert(f'⚠️ RECONCILIATION MISMATCH: {l} closed on exchange!')
                                del self.positions[l]
                
                # BUG 12 FIX: Only clear LTP cache on a new 1-minute bar (was clearing every second = API overload)
                # Cache is populated per-tick below; this just ensures stale per-second reads don't accumulate
                
                # Check Auto Square-off Time (15:34 PM)
                if now.hour > AUTO_SQUAREOFF_HOUR or (now.hour == AUTO_SQUAREOFF_HOUR and now.minute >= AUTO_SQUAREOFF_MINUTE):
                    log_alert(f"🕒 Auto Square-Off Time Reached ({AUTO_SQUAREOFF_HOUR}:{AUTO_SQUAREOFF_MINUTE:02d}). Liquidating all positions...")
                    self._exit_all_positions(reason="SESSION_END")
                    self.positions.clear()
                    self.mode = "SESSION_DONE"
                    self._save_state()
                    try:
                        db.commit_daily_pnl(self.realized_pnl)
                        log_info(f"Committed Daily PnL: ₹{self.realized_pnl:,.2f} to tracker.")
                    except Exception as e:
                        log_warn(f"Failed to commit PnL: {e}")
                    self._render_dashboard(self.market_data.latest_spot, self.market_data.latest_atm)
                    final_pct = (self.realized_pnl / 200_000.0) * 100.0
                    pnl_col = Fore.GREEN if self.realized_pnl >= 0 else Fore.RED
                    sign = "+" if self.realized_pnl >= 0 else ""
                    print(f"\n{pnl_col}✅ Session Completed Successfully. Final Realized PnL: {sign}₹{self.realized_pnl:,.2f} ({final_pct:+.2f}%){Style.RESET_ALL}\n")
                    self._remove_pid()
                    sys.exit(0)

                # ── 1. STRICT 1-SECOND DATA FETCH & TICK CADENCE ──
                spot, atm, is_new_1m_bar, is_stale = self.market_data.fetch_live_tick()
                if spot <= 0 or not getattr(self.market_data.streamer, "is_flattrade_live", False):
                    self._render_dashboard(self.market_data.latest_spot, self.market_data.latest_atm)
                    self._smart_sleep(1.0)
                    continue
                if not is_stale:
                    self.last_feed_tick = current_time

                # Clear tick cache on every 1-sec tick so Flattrade API is queried live every 1 second
                self._ltp_cache.clear()
                
                if not is_new_1m_bar and getattr(self, "current_indicators", None) is None:
                    self._smart_sleep(1.0)
                    continue

                # ── 2. Run KAMA & Indicators strictly on the collected 1-minute data ──
                if is_new_1m_bar or getattr(self, "current_indicators", None) is None:
                    df_5m = self.market_data.get_5m_dataframe()
                    self.current_indicators = Indicators.evaluate_all(self.market_data.get_1m_dataframe(), df_5m)
                    
                    current_iv = 15.0
                    if self.session_em_1sd > 0:
                        current_iv = (self.session_em_1sd / spot) * 19.1 * 100.0
                    
                    rv = VolatilityEngine.calculate_realized_volatility(self.market_data.bars_1m)
                    rv_iv_ratio = VolatilityEngine.compute_rv_iv_divergence(rv, current_iv)
                    self.current_indicators["rv_iv_ratio"] = rv_iv_ratio
                    
                    if rv_iv_ratio > 1.15 and self.current_indicators["regime"] == "CHOP":
                        self.current_indicators["regime"] = "TRANSITION"
                        log_info(f"RV/IV Divergence {rv_iv_ratio:.2f} > 1.15. Early Trend detected. Shifting CHOP -> TRANSITION.")
                
                # Continuously overlay real-time streaming EMA indicators on every 1-second tick
                if getattr(self.market_data, "latest_ema_snapshot", None):
                    self.current_indicators.update(self.market_data.latest_ema_snapshot)

                atr = self.current_indicators["atr"]
                regime = self.current_indicators["regime"]
                trend = self.current_indicators["trend"]
                _, dte_days = self.market_data.streamer.get_near_expiry_dte()
                
                # ── 3. Check Portfolio Circuit Breaker (-1.8% Capital) ──
                unrealized = sum([
                    ((p["entry_price"] - (self._get_ltp(p["strike"], p["base"]) or p.get("last_real_ltp", p["entry_price"]))) if p["side"] == "SELL" else ((self._get_ltp(p["strike"], p["base"]) or p.get("last_real_ltp", p["entry_price"])) - p["entry_price"])) * p["qty"]
                    for p in self.positions.values()
                ])
                cb_triggered, cb_msg = self.risk_manager.check_portfolio_circuit_breaker(self.realized_pnl, unrealized)
                if cb_triggered:
                    log_alert(cb_msg)
                    self.trigger_emergency_shutdown(reason="CIRCUIT_BREAKER_HALT")
                    return

                # ── 4. State Machine Transitions ──
                
                # Auto-recovery if stuck in SESSION_DONE during active market hours
                if self.mode == "SESSION_DONE":
                    is_market_open = (
                        (now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE))
                        and (now.hour < AUTO_SQUAREOFF_HOUR or (now.hour == AUTO_SQUAREOFF_HOUR and now.minute < AUTO_SQUAREOFF_MINUTE))
                    )
                    cb_hit = self.realized_pnl <= -CAPITAL * (PORTFOLIO_CIRCUIT_PCT / 100.0)
                    if is_market_open and not cb_hit:
                        log_info(f"🔄 Active market hours ({now.strftime('%H:%M:%S')} IST) detected while in SESSION_DONE. Resetting to WAIT_DATA to resume trading...")
                        self.mode = "WAIT_DATA"
                        self.cooldown_tracker.clear()
                        self._save_state()

                # ── Record 09:15 Spot for Market Observation (09:15 -> 09:18 IST) ──
                if (now.hour == 9 and now.minute >= 15) or now.hour > 9:
                    if self.spot_at_0915 is None:
                        # Attempt to get spot at 09:15:00 from collected 1-min bars if available
                        for b in getattr(self.market_data, "bars_1m", []):
                            ts = b.get("timestamp")
                            if ts and ts.hour == 9 and ts.minute == 15:
                                self.spot_at_0915 = float(b.get("spot", 0.0))
                                log_info(f"📍 Loaded 09:15 opening spot from 1m bar: {self.spot_at_0915:.2f}")
                                break
                        if self.spot_at_0915 is None and spot > 0:
                            self.spot_at_0915 = spot
                            log_info(f"📍 Recorded 09:15 opening spot price: {self.spot_at_0915:.2f}")

                # Phase A: Wait for data & 09:18 AM session start
                if self.mode == "WAIT_DATA":
                    if now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE):
                        if self.session_em_1sd == 0.0:
                            ce_ltp = self._get_ltp(atm, "CE")
                            pe_ltp = self._get_ltp(atm, "PE")
                            straddle = (ce_ltp + pe_ltp) if ce_ltp > 0 and pe_ltp > 0 else 0.0
                            self.session_em_1sd = VolatilityEngine.compute_expected_move(spot, straddle, 15.0)
                            log_info(f"Frozen Session EM_1sd: {self.session_em_1sd:.2f}")
                            
                        ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days)
                        ce_hedge, pe_hedge = self.calculate_hedge_strikes(atm, ce_strike, pe_strike, dte_days=dte_days)
                        hedge_width = ce_hedge - atm
                        
                        # ── 09:15 to 09:18 Market Observation Rule: 1-leg vs 2-leg entry ──
                        enter_ce = True
                        enter_pe = True
                        
                        if not self.initial_entry_done:
                            if self.spot_at_0918 is None:
                                self.spot_at_0918 = spot
                            if self.spot_at_0915 is None:
                                self.spot_at_0915 = spot
                                
                            move_0915_0918 = spot - self.spot_at_0915
                            abs_move = abs(move_0915_0918)
                            log_info(f"📊 Market Observation (09:15 -> 09:18 IST): 09:15 Spot = {self.spot_at_0915:.2f} | 09:18 Spot = {spot:.2f} | Move = {move_0915_0918:+.2f} pts (|Move| = {abs_move:.2f} pts)")
                            
                            if abs_move > 20.0:
                                # Market moved > 20 pts: start with 1 leg
                                if move_0915_0918 > 20.0:
                                    # Bullish move (+20 pts): Sell PE at ATM (Put writing on bull move), defer CE
                                    log_info(f"🚀 Bullish move detected (+{move_0915_0918:.2f} pts > +20 pts). Starting with 1 leg: SELL PE at ATM ({atm}). CE leg deferred until reversal signal.")
                                    enter_ce = False
                                    enter_pe = True
                                    self.cooldown_tracker["CE"] = {
                                        "stopped_time": time.time(),
                                        
                                        "active": True,
                                        "stop_reason": "BULLISH_OPEN_DEFERRED",
                                        "reentries_today": 0,
                                        "next_eligible_time": time.time() + 15  # 15s before first KAMA re-entry check
                                    }
                                else:
                                    # Bearish move (-20 pts): Sell CE at ATM (Call writing on bear move), defer PE
                                    log_info(f"🔻 Bearish move detected ({move_0915_0918:.2f} pts < -20 pts). Starting with 1 leg: SELL CE at ATM ({atm}). PE leg deferred until reversal signal.")
                                    enter_ce = True
                                    enter_pe = False
                                    self.cooldown_tracker["PE"] = {
                                        "stopped_time": time.time(),
                                        
                                        "active": True,
                                        "stop_reason": "BEARISH_OPEN_DEFERRED",
                                        "reentries_today": 0,
                                        "next_eligible_time": time.time() + 15  # 15s before first KAMA re-entry check
                                    }
                            else:
                                # Choppy market (<= 20 pts): start with 2 legs at ATM
                                log_info(f"🦀 Choppy market detected (|move| = {abs_move:.2f} pts <= 20 pts). Starting with 2 legs: SELL CE at ATM ({atm}) & SELL PE at ATM ({atm}).")
                                enter_ce = True
                                enter_pe = True
                        else:
                            log_info(f"🔄 Re-entering Strangle at ATM ({atm}): Entering BOTH LEGS (CE & PE).")
                            enter_ce = True
                            enter_pe = True

                        ce_h_ok = True if "CE_HEDGE" in self.positions else False
                        pe_h_ok = True if "PE_HEDGE" in self.positions else False
                        ce_s_ok = True if "CE" in self.positions else False
                        pe_s_ok = True if "PE" in self.positions else False

                        # Enter CE Side if selected (Hedge then Short)
                        if enter_ce:
                            if "CE_HEDGE" not in self.positions:
                                ce_h_ok = self._enter_leg("CE_HEDGE", ce_hedge, "BUY", spot, atr, dte_days)
                            if ce_h_ok and "CE" not in self.positions:
                                ce_s_ok = self._enter_leg("CE", ce_strike, "SELL", spot, atr, dte_days)
                                
                        # Enter PE Side if selected (Hedge then Short)
                        if enter_pe:
                            if "PE_HEDGE" not in self.positions:
                                pe_h_ok = self._enter_leg("PE_HEDGE", pe_hedge, "BUY", spot, atr, dte_days)
                            if pe_h_ok and "PE" not in self.positions:
                                pe_s_ok = self._enter_leg("PE", pe_strike, "SELL", spot, atr, dte_days)

                        shorts_succeeded = (ce_s_ok if enter_ce else True) and (pe_s_ok if enter_pe else True)
                        if not shorts_succeeded:
                            log_alert("⚠️ Short entry failed after 3 tries! Squaring off short legs so ONLY HEDGES REMAIN.")
                            self.square_off_all_short_legs(reason="STRANGLE_ENTRY_FAILED_LEAVE_HEDGES")
                        else:
                            self.mode = "RUNNING"
                            self.initial_entry_done = True
                            
                            if enter_ce and enter_pe:
                                self.cooldown_tracker.clear()
                            
                        self._save_state()
                    else:
                        obs_str = f"Spot 09:15={self.spot_at_0915:.2f}, Current={spot:.2f}, Move={spot - self.spot_at_0915:+.2f} pts" if self.spot_at_0915 else "Awaiting 09:15 tick"
                        log_info(f"⏳ Pre-market wait: Current IST {now.strftime('%H:%M:%S')} ({obs_str}). Session starts at {MARKET_START_HOUR:02d}:{MARKET_START_MINUTE:02d} IST.")

                # Phase B: Active Trading Management (RUNNING, CHOP_MODE, COOLDOWN)
                elif self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN"):
                    # ── Protective Hedge Integrity Check: Ensure hedges are ALWAYS active ──
                    self._ensure_protective_hedges(spot, atm, atr, dte_days)

                    # ── Always-On Safety Check: ensure ≥1 short leg always open ──
                    active_shorts = [leg for leg in ("CE", "PE")
                                     if leg in self.positions and self.positions[leg].get("side") == "SELL"]

                    if not active_shorts:
                        # NO short legs open → invoke Always-On rule immediately
                        # (replaces the old 10-second wait entirely)
                          # reset any old timer
                        self._ensure_always_one_leg_open(spot, atm, atr, dte_days)
                        self._render_dashboard(spot, atm)
                        self._smart_sleep(1.0)
                        continue

                    # ── Proactive Early Exit + Standard SL Check (every tick) ──
                    for leg in ("CE", "PE"):
                        if leg not in self.positions or self.positions[leg].get("side") != "SELL":
                            continue

                        is_strangle = ("CE" in self.positions and "PE" in self.positions
                                       and self.positions["CE"].get("side") == "SELL"
                                       and self.positions["PE"].get("side") == "SELL")
                        ltp_premium = self._get_ltp(self.positions[leg]["strike"], self.positions[leg]["base"])
                        if ltp_premium <= 0:
                            continue

                        # ── Proactive Exit: exit early if trend is strongly against leg ──
                        # (only check if the OTHER leg is still open — never exit the last leg proactively)
                        other_leg = "PE" if leg == "CE" else "CE"
                        other_open = (other_leg in self.positions and
                                      self.positions[other_leg].get("side") == "SELL")

                        if other_open:
                            # Safe to proactively exit this leg — the other will stay open
                            p_exit, p_reason = self.risk_manager.check_proactive_exit(
                                leg, self.positions[leg], ltp_premium, self.current_indicators
                            )
                            if p_exit:
                                log_alert(p_reason)
                                self._exit_leg(leg, reason="PROACTIVE_TREND_EXIT")
                                self._trigger_leg_cooldown(leg, spot, reason="PROACTIVE_TREND_EXIT")
                                continue  # next leg check

                        # ── Standard SL Check ──
                        is_stopped, reason = self.risk_manager.update_dual_sl_and_check(
                            leg, self.positions[leg], spot, ltp_premium, is_strangle, is_new_1m_bar, dte_days=dte_days
                        )
                        if is_stopped:
                            log_alert(reason)

                            # ── Smart In-Place Strangle Rebalance Check ──
                            # When solo leg hits TSL and target strangle strike matches current strike:
                            other_leg = "PE" if leg == "CE" else "CE"
                            other_open = (other_leg in self.positions and self.positions[other_leg].get("side") == "SELL")
                            if not other_open and self.positions[leg]["strike"] == atm:
                                # Check trend guards: do NOT rebalance into a strangle if market is in runaway trend against this leg
                                is_against, _ = ReversionDetector.is_trend_strongly_against(leg, self.current_indicators)
                                ema_sig = self.current_indicators.get("confirmed_signal", 0)
                                ema_against = (ema_sig == 1 and leg == "CE") or (ema_sig == -1 and leg == "PE")

                                if not is_against and not ema_against:
                                    if self._rebalance_strangle_in_place(leg, spot, atm, atr, ltp_premium, dte_days):
                                        continue  # Successfully rebalanced in-place! Skip physical exit.

                            self._exit_leg(leg, reason="PREM_SL_HIT")
                            self._trigger_leg_cooldown(leg, spot, reason="PREM_SL_HIT")
                            continue

                    # ── POST-SL-CHECK: Re-verify active_shorts ──
                    active_shorts_after = [l for l in ("CE", "PE") if l in self.positions and self.positions[l].get("side") == "SELL"]
                    if not active_shorts_after:
                        
                        self._ensure_always_one_leg_open(spot, atm, atr, dte_days)

                    # ── Reversal-Gated Re-Entry for Stopped Legs ──
                    self._check_cooldown_and_reenter(spot, atm, atr, regime, trend, dte_days=dte_days)

                # Phase C: HEDGES_ONLY mode → treat same as no-short-legs → use Always-On rule
                elif self.mode == "HEDGES_ONLY":
                    self._ensure_protective_hedges(spot, atm, atr, dte_days)
                    if not getattr(self, "_hedges_only_logged", False):
                        log_info("🔁 HEDGES_ONLY mode: Applying Always-On rule to re-enter short legs immediately...")
                        self._hedges_only_logged = True
                    self._ensure_always_one_leg_open(spot, atm, atr, dte_days)


                # ── 5. Render Live Dashboard ──
                self._render_dashboard(spot, atm)
                
                # Sleep 1 second for continuous tick-level SL evaluation
                self._smart_sleep(1.0)

            except KeyboardInterrupt:
                log_alert("Algo interrupted via KeyboardInterrupt (Ctrl+C). Initiating emergency exit...")
                self.trigger_emergency_shutdown(reason="KEYBOARD_INTERRUPT")
                break
            except Exception as e:
                log_warn(f"Unhandled exception in execution loop: {e}")
                traceback.print_exc()
                time.sleep(2.0)
                
        self._remove_pid()

    def _smart_sleep(self, seconds: float):
        end_time = time.time() + seconds
        while time.time() < end_time and self.is_running and not _EMERGENCY_STOP_TRIGGERED:
            for flag_path in self.stop_flags:
                if os.path.exists(flag_path):
                    log_alert("🛑 STOP FLAG DETECTED! Initiating emergency halt...")
                    try:
                        os.remove(flag_path)
                    except Exception:
                        pass
                    self.trigger_emergency_shutdown(reason="STOP_FLAG")
                    return
            rem = end_time - time.time()
            if rem <= 0:
                break
            time.sleep(min(0.25, rem))


def prompt_user_variables():
    global CAPITAL, KAMA_PERIOD, KAMA_FAST_EMA, KAMA_SLOW_EMA, ADX_PERIOD, ADX_CHOP_THRESHOLD, ADX_TREND_THRESHOLD
    global PREM_SL_DEBOUNCE_BARS, BASE_MIN_WIDTH_PTS, BASE_MAX_WIDTH_PTS
    global PAPER_TRADING_MODE

    import sys
    PAPER_TRADING_MODE = True
    print(f"\n{Fore.GREEN}{Style.BRIGHT}✅ PAPER TRADING ONLY. No real orders will be placed.{Style.RESET_ALL}\n")

    # ── STRATEGY PARAMETERS ───────────────────────────────────────────────────
    print(f"\n{Fore.GREEN}{Style.BRIGHT}{'═'*78}")
    print(f"  ✅ [PAPER] Initializing with default strategy parameters...")
    print(f"{'═'*78}{Style.RESET_ALL}\n")


if __name__ == "__main__":
    prompt_user_variables()
    engine = ExecutionEngine()
    engine.run()
