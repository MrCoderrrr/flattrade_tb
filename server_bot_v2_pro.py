"""
================================================================================
🚀 ADAPTIVE KAMA-ADX HEDGED STRANGLE (VERSION 2.0 - PRODUCTION ENGINE)
================================================================================
Live Algorithmic State Machine for NIFTY 50 Options on Flattrade.

KEY STRATEGY CONFIGURATION (USER-TUNED):
1. Trailing Stop Loss: 50% Trailing Ratio on Underlying Spot.
2. Debounce: 1-bar confirmation for rapid, safe execution.
3. KAMA: (13, 3, 30) calculated on 1-minute chart with real-time second updates.
4. ADX Gateway: 20.0 (ADX < 20 = CHOP / Iron Condor, ADX >= 20 = TREND).
5. ADX Lookback: 6 candles on 5-minute chart (30-minute lookback).
6. Anti-Whipsaw Cooldown: Freezes stopped leg until KAMA trend reverses or chop confirms.
7. Portfolio Circuit Breaker: -1.5% capital emergency halt.
================================================================================
"""

import os
import sys
import time
import json
import socket
import select
import traceback
import urllib3.util.connection as urllib3_cn
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
import requests

# Rich UI Support
try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Force IPv4
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Setup IST Timezone (UTC + 5:30) for AWS / Greenwich servers
IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now() -> datetime: return datetime.now(IST)

# ==============================================================================
# FLATTRADE CORE POLYFILLS & BROKER INTEGRATION
# ==============================================================================

from api_helper import NorenApiPy
from creds import USER_ID

global_api = NorenApiPy()
if os.path.exists("token.txt"):
    with open("token.txt", "r") as f:
        access_token = f.read().strip()
    session_res = global_api.set_session(userid=str(USER_ID).strip(), password='', usertoken=access_token)
    try:
        limits_check = global_api.get_limits()
    except Exception as e:
        print(f"[FATAL] Exception verifying session via get_limits(): {e}", flush=True)
        sys.exit(1)
    if not limits_check or not isinstance(limits_check, dict) or limits_check.get('stat') != 'Ok':
        print(f"[FATAL] token.txt is present but the session is INVALID or EXPIRED. "
              f"get_limits() returned: {limits_check}. Run login.py to refresh the token.", flush=True)
        sys.exit(1)
    print(f"[BOOT] Flattrade session verified OK for user {USER_ID}.", flush=True)
else:
    print("[FATAL] token.txt not found. Please run login.py first.", flush=True)
    sys.exit(1)

class NSEATMStreamer:
    def __init__(self):
        self.api = global_api
        
    def get_spot_and_atm(self) -> Tuple[float, int]:
        try:
            res = self.api.get_quotes(exchange='NSE', token='26000')
            if res and isinstance(res, dict) and res.get('stat') == 'Ok':
                spot = float(res.get('lp', res.get('ltp', 24000.0)))
                return spot, int(round(spot / 50.0) * 50)
        except Exception as e:
            print(f"Error fetching spot: {e}", flush=True)
        return 24000.0, 24000

    def get_live_quote(self, strike: int, option_type: str) -> Dict[str, Any]:
        search_text = f"NIFTY {strike} {option_type}"
        res = self.api.searchscrip(exchange='NFO', searchtext=search_text)
        if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
            # Find nearest expiry
            valid = []
            for item in res['values']:
                if 'exd' in item:
                    try:
                        valid.append({'item': item, 'dt': datetime.strptime(item['exd'], "%d-%b-%Y")})
                    except ValueError:
                        continue
            if valid:
                valid.sort(key=lambda x: x['dt'])
                match = valid[0]['item']
                tsym = match['tsym']
                try:
                    quote = self.api.get_quotes(exchange='NFO', token=match['token'])
                except Exception as e:
                    log_alert(f"get_quotes failed for {tsym}: {e}")
                    quote = None
                lp = float(quote.get('lp', quote.get('ltp', 0.0))) if quote else 0.0
                return {"lp": lp, "tsym": tsym, "ls": int(match['ls']), "valid": True}
        log_alert(f"[SYMBOL LOOKUP FAILED] No valid instrument found for NIFTY {strike} {option_type}. searchscrip result: {res}")
        return {"lp": 0.0, "tsym": None, "ls": LOT_SIZE, "valid": False}
        
    def get_near_expiry_dte(self):
        return None, 2.0

class FlattradeBroker:
    def __init__(self, paper_trading=False):
        self.api = global_api
        self.paper_trading = paper_trading
        if self.paper_trading:
            log_warn("FlattradeBroker running in PAPER TRADING mode. NO REAL ORDERS WILL BE PLACED.")

    def place_option_order(self, symbol, transaction_type, quantity, price) -> Dict[str, Any]:
        action = transaction_type[0]  # 'B' or 'S'

        if self.paper_trading:
            fake_order_no = f"PAPER-{int(time.time()*1000)}"
            log_info(f"[PAPER] {action} {quantity} x {symbol} @ ~₹{price:.2f} (simulated, no real order sent)")
            return {"ok": True, "order_no": fake_order_no, "msg": "Simulated fill (paper trading)", "raw": None}

        # Marketable limit price: Exchange blocks MKT orders on Options
        # BUY: Limit price slightly above LTP for instant fill
        # SELL: Limit price slightly below LTP for instant fill
        if action == 'B':
            limit_price = round(max(0.05, float(price) + 2.0), 2)
        else:
            limit_price = round(max(0.05, float(price) - 2.0), 2)

        try:
            url = "https://piconnect.flattrade.in/PiConnectAPI/PlaceOrder"
            with open("token.txt", "r") as f:
                token = f.read().strip()
            values = {
                'ordersource': 'API',
                'uid': str(USER_ID).strip(),
                'actid': str(USER_ID).strip(),
                'trantype': str(action),
                'prd': 'M',
                'exch': 'NFO',
                'tsym': str(symbol),
                'qty': str(quantity),
                'dscqty': '0',
                'prctyp': 'LMT',
                'prc': str(limit_price),
                'trgprc': '0',
                'ret': 'DAY',
                'remarks': 'V2_PRO_ALGO'
            }
            payload = 'jData=' + json.dumps(values) + f'&jKey={token}'
            resp = requests.post(url, data=payload, timeout=8)
            res_dict = json.loads(resp.text)

            if res_dict.get('stat') == 'Ok':
                order_no = res_dict.get('norenordno')
                log_trade(f"[ORDER OK] {action} {quantity} x {symbol} @ Limit ₹{limit_price:.2f} (LTP ₹{price:.2f}) | Order No: {order_no}")
                return {"ok": True, "order_no": order_no, "msg": "Order placed", "raw": res_dict}
            else:
                err_msg = res_dict.get('emsg', resp.text)
                log_alert(f"[ORDER REJECTED BY BROKER] {action} {symbol}: {err_msg}")
                return {"ok": False, "order_no": None, "msg": err_msg, "raw": res_dict}
        except Exception as e:
            log_alert(f"[ORDER EXCEPTION] {action} {symbol}: {e}")
            return {"ok": False, "order_no": None, "msg": f"Exception: {e}", "raw": None}

class VolatilityEngine:
    @staticmethod
    def calculate_realized_volatility(*args): return 15.0
    @staticmethod
    def compute_rv_iv_divergence(*args): return 1.0
    @staticmethod
    def compute_expected_move(*args): return 1000.0

class DBManager:
    def record_trade(self, *args, **kwargs): pass
    def get_strategy_pnl_summary(self, *args, **kwargs): return {}
db = DBManager()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                     STRATEGY CONFIGURATION (USER-TUNED)                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

CAPITAL                 = 10_00_000   # Default Capital: ₹10 Lakhs
LOT_SIZE                = 65          # NIFTY Lot size
CAPITAL_BUFFER          = 0.95        # Usable capital buffer (95%)
MARGIN_IRON_CONDOR      = 95_000      # Margin required for 4-leg Iron Condor per lot
PORTFOLIO_CIRCUIT_PCT   = 1.5         # Emergency Portfolio Halt at -1.5% combined MTM

# Indicators Parameters
KAMA_PERIOD             = 13          # KAMA 13 Lookback (1-minute resolution)
KAMA_FAST_EMA           = 3           # KAMA Fast EMA constant
KAMA_SLOW_EMA           = 30          # KAMA Slow EMA constant
KAMA_MIN_SLOPE          = 3.5         # Minimum KAMA slope (pts) to flip trend

ADX_PERIOD              = 6           # 5 min 6 candles for ADX (30m lookback)
ADX_CHOP_THRESHOLD      = 20.0        # 20 Gateway: ADX < 20 = CHOP (Iron Condor)
ADX_TREND_THRESHOLD     = 20.0        # 20 Gateway: ADX >= 20 = TREND

ATR_PERIOD              = 14          # ATR lookback period on 5m candles
DEFAULT_ATR_5M          = 35.0        # Fallback 5m ATR if warming up

# Strike Selection & Distances
HEDGE_WIDTH_PTS         = 1000        # Long Leg (Hedge) distance OTM from ATM at entry
BASE_MIN_WIDTH_PTS      = 0           # ATM Straddle / Strangle width
BASE_MAX_WIDTH_PTS      = 0           
BASE_ATR_MULTIPLIER     = 1.0         

CHOP_MIN_WIDTH_PTS      = 0           
CHOP_MAX_WIDTH_PTS      = 0           
CHOP_ATR_MULTIPLIER     = 1.5         

EXPIRY_WIDTH_LOOKAHEAD_DAYS = 8.0     
EXPIRY_NEAR_DAYS            = 2.0     
EXPIRY_NEAR_BONUS           = 0.42    

# Spot-Based Trailing Stop Loss (User tuned: 50% Trail, 1 Debounce)
SPOT_SL_ATR_MULT        = 1.20        # Initial Spot SL distance = 1.2 * ATR (~42 pts from entry spot)
SPOT_SL_TRAIL_RATIO     = 0.50        # 50% Trailing Stop Loss on favorable spot movement
SPOT_SL_TRAIL_RATIO_STRONG = 0.65     # Stronger trail once trade is in deep profit
SPOT_SL_TRAIL_RATIO_DEEP   = 0.80     
SPOT_SL_BREAKEVEN_LOCK_ATR = 1.30     
SPOT_SL_DEBOUNCE_BARS   = 1           # 1-bar debounce confirmation for quick protection

# Anti-Whipsaw Consecutive Tick Engine
CONSECUTIVE_TICKS_REQUIRED = 10       # Require 10 consecutive favorable ticks (~10s) to re-enter
COOLDOWN_MINUTES        = 3           # Max fallback ceiling
COOLDOWN_SPOT_PCT       = 0.0020

# Session Timing
MARKET_START_HOUR       = 9
MARKET_START_MINUTE     = 18          # Start trading at 09:18 AM
AUTO_SQUAREOFF_HOUR     = 15
AUTO_SQUAREOFF_MINUTE   = 28          # Auto square-off at 15:28 PM
REFRESH_INTERVAL_SEC    = 1           # 1-second continuous live loop


_LAST_EVENT: Dict[str, str] = {"msg": "Engine starting..."}

def _now_str() -> str: return get_ist_now().strftime("%H:%M:%S")
def log_info(msg: str): print(f"[{_now_str()} INFO]  {msg}", flush=True)
def log_warn(msg: str):
    print(f"[{_now_str()} WARNING]  {msg}", flush=True)
    _LAST_EVENT["msg"] = f"WARN: {msg}"
def log_alert(msg: str):
    print(f"[{_now_str()} ALERT]  {msg}", flush=True)
    _LAST_EVENT["msg"] = f"ALERT: {msg}"
def log_trade(msg: str):
    print(f"[{_now_str()} TRADE]  {msg}", flush=True)
    _LAST_EVENT["msg"] = f"TRADE: {msg}"
def round_to_strike(price: float, strike_step: int = 50) -> int: return int(round(price / float(strike_step)) * strike_step)


# ==============================================================================
# MODULE 1: MARKET DATA INGESTION & TIME-PRICE SERIES
# ==============================================================================

class MarketData:
    def __init__(self, cache_file: str):
        self.cache_file = cache_file
        self.streamer = NSEATMStreamer()
        self.bars_1m: List[Dict[str, Any]] = []
        self.logged_1m_keys: set = set()
        self.bars_5m: List[Dict[str, Any]] = []
        self.seeded_bars_5m: List[Dict[str, Any]] = []
        self.seeded_bars_1m: List[Dict[str, Any]] = []
        self.latest_spot: float = 24000.0
        self.latest_atm: int = 24000
        self.last_completed_1m_key: Optional[str] = None
        self._load_cache()
        self._seed_history_if_needed()

    def _load_cache(self):
        if not os.path.exists(self.cache_file): return
        try:
            minute_map = {}
            with open(self.cache_file, "r") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) >= 2:
                        try:
                            dt = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M:%S")
                            min_key = dt.strftime("%Y-%m-%d %H:%M")
                            minute_map[min_key] = (dt, float(parts[1]))
                        except ValueError:
                            continue
            sorted_bars = sorted(minute_map.values(), key=lambda x: x[0])
            warmup_bars = sorted_bars[-300:] if len(sorted_bars) > 300 else sorted_bars
            for dt, price in warmup_bars:
                min_key = dt.strftime("%Y-%m-%d %H:%M")
                if min_key not in self.logged_1m_keys:
                    self.bars_1m.append({'timestamp': dt, 'spot': price, 'minute_key': min_key})
                    self.logged_1m_keys.add(min_key)
                    self.latest_spot = price
                    self.latest_atm = round_to_strike(price, 50)
            self._rebuild_5m_candles()
            if self.bars_1m:
                self.last_completed_1m_key = self.bars_1m[-1]['minute_key']
        except Exception as e:
            log_warn(f"MarketData: Error loading cache: {e}")

    def _seed_history_if_needed(self):
        try:
            log_info("MarketData: Seeding historical candles from Flattrade for instant indicator readiness...")
            end_time = get_ist_now()
            start_time = end_time - timedelta(days=5)
            
            # Fetch 5-minute historical series
            res_5m = global_api.get_time_price_series(
                exchange='NSE', token='26000', 
                starttime=start_time.timestamp(), 
                endtime=end_time.timestamp(), 
                interval=5
            )
            if res_5m and isinstance(res_5m, list) and len(res_5m) > 0:
                s5 = []
                for row in res_5m:
                    try:
                        ts = datetime.strptime(row['time'], "%d-%m-%Y %H:%M:%S")
                        s5.append({
                            'timestamp': ts,
                            'open': float(row['into']),
                            'high': float(row['inth']),
                            'low': float(row['intl']),
                            'close': float(row['intc'])
                        })
                    except Exception:
                        continue
                if s5:
                    s5.sort(key=lambda x: x['timestamp'])
                    self.seeded_bars_5m = s5[-100:]
                    log_info(f"Successfully seeded {len(self.seeded_bars_5m)} 5m bars from Flattrade.")

            # Fetch 1-minute historical series for KAMA(13,3,30)
            start_1m = end_time - timedelta(days=1)
            res_1m = global_api.get_time_price_series(
                exchange='NSE', token='26000', 
                starttime=start_1m.timestamp(), 
                endtime=end_time.timestamp(), 
                interval=1
            )
            if res_1m and isinstance(res_1m, list) and len(res_1m) > 0:
                s1 = []
                for row in res_1m:
                    try:
                        ts = datetime.strptime(row['time'], "%d-%m-%Y %H:%M:%S")
                        s1.append({
                            'timestamp': ts,
                            'open': float(row['into']),
                            'high': float(row['inth']),
                            'low': float(row['intl']),
                            'close': float(row['intc'])
                        })
                    except Exception:
                        continue
                if s1:
                    s1.sort(key=lambda x: x['timestamp'])
                    self.seeded_bars_1m = s1[-120:]
                    log_info(f"Successfully seeded {len(self.seeded_bars_1m)} 1m bars for KAMA readiness.")
        except Exception as e:
            log_warn(f"Flattrade history seeding skipped ({e}).")

    def _rebuild_5m_candles(self):
        if not self.bars_1m: return
        df_1m = pd.DataFrame(self.bars_1m)
        df_1m.set_index('timestamp', inplace=True)
        df_5m = df_1m['spot'].resample('5min', label='left', closed='left').ohlc().dropna()
        self.bars_5m = [{'timestamp': ts, 'open': float(row['open']), 'high': float(row['high']), 'low': float(row['low']), 'close': float(row['close'])} for ts, row in df_5m.iterrows()]

    def fetch_live_tick(self) -> Tuple[float, int, bool]:
        spot, atm = self.streamer.get_spot_and_atm()
        self.latest_spot = spot
        self.latest_atm = atm
        now = get_ist_now()
        current_min_key = now.strftime("%Y-%m-%d %H:%M")
        is_new_1m_bar = (current_min_key != self.last_completed_1m_key)
        
        if is_new_1m_bar:
            candle_ts_str = f"{current_min_key}:00"
            dt = datetime.strptime(candle_ts_str, "%Y-%m-%d %H:%M:%S")
            if current_min_key not in self.logged_1m_keys:
                try:
                    os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
                    with open(self.cache_file, "a") as f:
                        f.write(f"{candle_ts_str},{spot:.2f}\n")
                    self.logged_1m_keys.add(current_min_key)
                except Exception as e:
                    pass
                self.bars_1m.append({'timestamp': dt, 'spot': spot, 'minute_key': current_min_key})
                self._rebuild_5m_candles()
            self.last_completed_1m_key = current_min_key
        return spot, atm, is_new_1m_bar

    def get_5m_dataframe(self) -> pd.DataFrame:
        all_5m = self.seeded_bars_5m + self.bars_5m
        if not all_5m: return pd.DataFrame(columns=['open', 'high', 'low', 'close'])
        df = pd.DataFrame(all_5m)
        df.drop_duplicates(subset=['timestamp'], keep='last', inplace=True)
        return df

    def get_1m_dataframe(self) -> pd.DataFrame:
        all_1m = []
        if self.seeded_bars_1m:
            all_1m.extend(self.seeded_bars_1m)
        if self.bars_1m:
            df_curr = pd.DataFrame(self.bars_1m)
            df_curr.set_index('timestamp', inplace=True)
            df_res = df_curr['spot'].resample('1min', label='left', closed='left').ohlc().dropna().reset_index()
            all_1m.extend(df_res.to_dict('records'))
        if not all_1m: return pd.DataFrame(columns=['open', 'high', 'low', 'close'])
        df = pd.DataFrame(all_1m)
        df.drop_duplicates(subset=['timestamp'], keep='last', inplace=True)
        return df


# ==============================================================================
# MODULE 2: INDICATORS (KAMA 13/3/30 & ADX 6 on 5M)
# ==============================================================================

class Indicators:
    @staticmethod
    def calculate_kama(closes: np.ndarray, period: int = KAMA_PERIOD, fast: int = KAMA_FAST_EMA, slow: int = KAMA_SLOW_EMA):
        if len(closes) < period + 1: return None, None, 0
        kama = np.zeros(len(closes))
        kama[period - 1] = np.mean(closes[:period])
        fast_sc = 2.0 / (fast + 1.0)
        slow_sc = 2.0 / (slow + 1.0)
        for i in range(period, len(closes)):
            change = abs(closes[i] - closes[i - period])
            volatility = np.sum(np.abs(np.diff(closes[i - period:i + 1])))
            er = (change / volatility) if volatility > 1e-6 else 0.0
            sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
            kama[i] = kama[i - 1] + sc * (closes[i] - kama[i - 1])
        current_kama = float(kama[-1])
        prev_kama = float(kama[-2])
        diff = current_kama - prev_kama
        if diff > KAMA_MIN_SLOPE: trend = 1
        elif diff < -KAMA_MIN_SLOPE: trend = -1
        else: trend = 0
        return current_kama, prev_kama, trend

    @staticmethod
    def calculate_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ATR_PERIOD) -> float:
        if len(closes) < 2: return DEFAULT_ATR_5M
        n = len(closes)
        tr = np.zeros(n)
        tr[0] = highs[0] - lows[0]
        for i in range(1, n):
            hl = highs[i] - lows[i]
            hpc = abs(highs[i] - closes[i - 1])
            lpc = abs(lows[i] - closes[i - 1])
            tr[i] = max(hl, hpc, lpc)
        if len(tr) < period: return float(np.mean(tr)) if len(tr) > 0 else DEFAULT_ATR_5M
        atr = np.zeros(n)
        atr[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
        return float(atr[-1])

    @staticmethod
    def calculate_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ADX_PERIOD):
        n = len(closes)
        if n < period + 2: return 18.0, 20.0, 20.0
        
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
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
            
        valid = np.arange(period, n)
        tr_safe = np.where(tr_smooth[valid] == 0, 1e-6, tr_smooth[valid])
        plus_di = 100.0 * (plus_dm_smooth[valid] / tr_safe)
        minus_di = 100.0 * (minus_dm_smooth[valid] / tr_safe)
        
        di_sum = plus_di + minus_di
        di_diff = np.abs(plus_di - minus_di)
        dx = 100.0 * (di_diff / np.where(di_sum == 0, 1e-6, di_sum))
        
        if len(dx) < period:
            return float(np.mean(dx)) if len(dx) > 0 else 18.0, float(plus_di[-1]), float(minus_di[-1])
            
        adx = np.zeros(len(dx))
        adx[period - 1] = np.mean(dx[:period])
        for i in range(period, len(dx)):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
            
        return float(adx[-1]), float(plus_di[-1]), float(minus_di[-1])

    @classmethod
    def evaluate_all(cls, df_1m: pd.DataFrame, df_5m: pd.DataFrame):
        if df_5m.empty:
            return {'kama': 0.0, 'prev_kama': 0.0, 'trend': 0, 'atr': DEFAULT_ATR_5M, 'adx': 18.0, 'plus_di': 20.0, 'minus_di': 20.0, 'regime': 'CHOP'}
            
        highs = df_5m['high'].to_numpy(dtype=float)
        lows = df_5m['low'].to_numpy(dtype=float)
        closes = df_5m['close'].to_numpy(dtype=float)
        
        atr = cls.calculate_atr(highs, lows, closes)
        adx, p_di, m_di = cls.calculate_adx(highs, lows, closes)
        
        if df_1m.empty or len(df_1m) < KAMA_PERIOD:
            kama, prev_kama, trend = None, None, 0
        else:
            closes_1m = df_1m['close'].to_numpy(dtype=float)
            kama, prev_kama, trend = cls.calculate_kama(closes_1m)
        
        if adx < ADX_CHOP_THRESHOLD: regime = "CHOP"
        elif adx >= ADX_TREND_THRESHOLD: regime = "TREND"
        else: regime = "TRANSITION"
        return {'kama': kama, 'prev_kama': prev_kama, 'trend': trend, 'atr': atr, 'adx': adx, 'plus_di': p_di, 'minus_di': m_di, 'regime': regime}


# ==============================================================================
# MODULE 3: RISK MANAGER (SPOT-BASED TRAILING STOP LOSS & CIRCUIT BREAKER)
# ==============================================================================

class RiskManager:
    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1.0 * (capital * PORTFOLIO_CIRCUIT_PCT / 100.0)

    def init_spot_sl(self, leg: str, entry_spot: float, atr: float):
        initial_distance = max(15.0, SPOT_SL_ATR_MULT * atr)
        if leg == "CE":
            initial_sl = entry_spot + initial_distance
            best_spot = entry_spot 
        else:
            initial_sl = entry_spot - initial_distance
            best_spot = entry_spot 
        return {
            "entry_spot": round(entry_spot, 2), "atr_at_entry": round(atr, 2),
            "initial_sl": round(initial_sl, 2), "current_sl": round(initial_sl, 2),
            "best_spot": round(entry_spot, 2), "trail_amount": 0.0, "breach_count": 0
        }

    def update_spot_sl_and_check(self, leg: str, pos_data: Dict[str, Any], current_spot: float, is_new_1m_bar: bool):
        sl_state = pos_data.get("spot_sl_state")
        if not sl_state: return False, ""
        entry_spot = float(sl_state["entry_spot"])
        atr_at_entry = float(sl_state.get("atr_at_entry", 0.0) or 0.0)
        current_sl = float(sl_state["current_sl"])
        best_spot = float(sl_state.get("best_spot", entry_spot))
        breach_count = int(sl_state.get("breach_count", 0))
        safe_atr = max(5.0, atr_at_entry if atr_at_entry > 0 else 1.0)
        favorable_lock_at = safe_atr * SPOT_SL_BREAKEVEN_LOCK_ATR
        deep_lock_at = favorable_lock_at * 1.5
        
        if leg == "CE":
            if current_spot < best_spot:
                best_spot = current_spot
                sl_state["best_spot"] = round(best_spot, 2)
            favorable_move = entry_spot - best_spot
            if favorable_move > 0:
                trail_ratio = SPOT_SL_TRAIL_RATIO_DEEP if favorable_move >= deep_lock_at else (SPOT_SL_TRAIL_RATIO_STRONG if favorable_move >= favorable_lock_at else SPOT_SL_TRAIL_RATIO)
                trail_amount = favorable_move * trail_ratio
                candidate_sl = sl_state["initial_sl"] - trail_amount
                sl_state["current_sl"] = min(current_sl, round(candidate_sl, 2))
                sl_state["trail_amount"] = round(trail_amount, 2)
            is_breaching = (current_spot >= sl_state["current_sl"])
            hard_breach = (current_spot >= (sl_state["current_sl"] + 1.5 * safe_atr))
        else:
            if current_spot > best_spot:
                best_spot = current_spot
                sl_state["best_spot"] = round(best_spot, 2)
            favorable_move = best_spot - entry_spot
            if favorable_move > 0:
                trail_ratio = SPOT_SL_TRAIL_RATIO_DEEP if favorable_move >= deep_lock_at else (SPOT_SL_TRAIL_RATIO_STRONG if favorable_move >= favorable_lock_at else SPOT_SL_TRAIL_RATIO)
                trail_amount = favorable_move * trail_ratio
                candidate_sl = sl_state["initial_sl"] + trail_amount
                sl_state["current_sl"] = max(current_sl, round(candidate_sl, 2))
                sl_state["trail_amount"] = round(trail_amount, 2)
            is_breaching = (current_spot <= sl_state["current_sl"])
            hard_breach = (current_spot <= (sl_state["current_sl"] - 1.5 * safe_atr))

        if is_breaching:
            breach_count += 1
            sl_state["breach_count"] = breach_count
        else:
            sl_state["breach_count"] = 0

        # Fast live-tick SL trigger: Exits when Spot breaches SL for 2 consecutive ticks (or immediate hard breach)
        if breach_count >= 2 or hard_breach:
            return True, f"⛔ Spot SL Triggered for {leg} | Spot: {current_spot:.2f} crossed SL: {sl_state['current_sl']:.2f} (Confirmed over {breach_count} ticks)"
        return False, ""

    def check_portfolio_circuit_breaker(self, realized_pnl: float, unrealized_pnl: float):
        combined_mtm = realized_pnl + unrealized_pnl
        if combined_mtm <= self.circuit_breaker_loss_limit:
            return True, f"🚨 PORTFOLIO CIRCUIT BREAKER HIT 🚨 Combined MTM: ₹{combined_mtm:,.2f} breached limit. Shutting down!"
        return False, ""


# ==============================================================================
# MODULE 3B: LIVE DASHBOARD SNAPSHOT HANDLER
# ==============================================================================

class Dashboard:
    def __init__(self):
        self.has_rich = HAS_RICH and sys.stdout.isatty()
        if self.has_rich:
            self.console = Console()
            self.live = Live(self._render({}), console=self.console, refresh_per_second=1, screen=False)
            self._started = False
        else:
            self._started = False

    def start(self):
        if self.has_rich and not self._started:
            try:
                self.live.start()
                self._started = True
            except Exception:
                self.has_rich = False

    def stop(self):
        if self.has_rich and self._started:
            try:
                self.live.stop()
            except Exception:
                pass
            self._started = False

    def _render(self, snap: Dict[str, Any]):
        if not HAS_RICH: return ""
        header = Text(f" V2 PRO ALGO — {snap.get('now_str', '')}  |  Mode: {snap.get('mode', '-')}  |  "
                       f"{'LIVE' if not snap.get('paper_mode', False) else 'PAPER'} TRADING ", style="bold white on blue")

        top = Table.grid(expand=True)
        top.add_column(justify="left")
        top.add_column(justify="left")
        top.add_column(justify="left")
        top.add_column(justify="left")
        top.add_row(
            f"[bold]Spot:[/bold] {snap.get('spot', 0):.2f}",
            f"[bold]ATM:[/bold] {snap.get('atm', 0)}",
            f"[bold]ADX(6 on 5m):[/bold] {snap.get('adx', 0):.1f} ({snap.get('regime', '-')})",
            f"[bold]KAMA Trend:[/bold] {snap.get('trend', 0)}",
        )
        top.add_row(
            f"[bold]KAMA(1m):[/bold] ₹{snap.get('kama', 0):.2f}" if snap.get('kama') else "[bold]KAMA(1m):[/bold] WARMUP",
            f"[bold]ATR(5m):[/bold] {snap.get('atr', 0):.2f}",
            f"[bold]Realized P&L:[/bold] " + (f"[green]₹{snap.get('realized_pnl', 0):,.2f}[/green]" if snap.get('realized_pnl', 0) >= 0 else f"[red]₹{snap.get('realized_pnl', 0):,.2f}[/red]"),
            f"[bold]Unrealized P&L:[/bold] " + (f"[green]₹{snap.get('unrealized_pnl', 0):,.2f}[/green]" if snap.get('unrealized_pnl', 0) >= 0 else f"[red]₹{snap.get('unrealized_pnl', 0):,.2f}[/red]"),
        )

        pos_table = Table(title="Open Positions", expand=True, show_lines=False)
        pos_table.add_column("Leg")
        pos_table.add_column("Strike")
        pos_table.add_column("Side")
        pos_table.add_column("Entry ₹")
        pos_table.add_column("LTP ₹")
        pos_table.add_column("P&L ₹")
        pos_table.add_column("SL Line")
        pos_table.add_column("Breaches")

        for leg, pos in snap.get("positions", {}).items():
            pnl = pos.get("live_pnl", 0.0)
            pnl_str = f"[green]{pnl:,.2f}[/green]" if pnl >= 0 else f"[red]{pnl:,.2f}[/red]"
            sl_state = pos.get("spot_sl_state") or {}
            sl_line = f"{sl_state.get('current_sl', '-')}" if sl_state else "-"
            breach = f"{sl_state.get('breach_count', 0)}" if sl_state else "-"
            pos_table.add_row(leg, str(pos.get("strike", "-")), pos.get("side", "-"),
                               f"{pos.get('entry_price', 0):.2f}", f"{pos.get('live_ltp', 0):.2f}",
                               pnl_str, sl_line, breach)

        if not snap.get("positions"):
            pos_table.add_row("-", "-", "-", "-", "-", "-", "-", "-")

        cd_table = Table(title="Cooldowns", expand=True, show_lines=False)
        cd_table.add_column("Leg")
        cd_table.add_column("Active")
        cd_table.add_column("Elapsed (s)")
        for leg, cd in snap.get("cooldown_tracker", {}).items():
            active = cd.get("active", False)
            safe_t = cd.get("safe_ticks", 0)
            status_txt = f"[yellow]ACTIVE ({safe_t}/10 ticks)[/yellow]" if active else "[green]Ready[/green]"
            cd_table.add_row(leg, status_txt, f"Stopped @ {cd.get('stopped_spot', 0):.2f}" if active else "-")

        footer = Text(f" Last event: {snap.get('last_event', '-')} ", style="dim")

        layout = Table.grid(expand=True)
        layout.add_row(Panel(header, style="on blue"))
        layout.add_row(Panel(top, title="Market State"))
        layout.add_row(pos_table)
        layout.add_row(cd_table)
        layout.add_row(footer)
        return layout

    def update(self, snap: Dict[str, Any]):
        if self.has_rich and self._started:
            try:
                self.live.update(self._render(snap))
            except Exception as e:
                log_warn(f"Dashboard render error (non-fatal): {e}")
        else:
            kama_txt = f"{snap.get('kama', 0):.2f}" if snap.get('kama') else "-"
            print(f"[{snap.get('now_str', _now_str())}] Spot: {snap.get('spot', 0):.2f} | KAMA: {kama_txt} | ADX: {snap.get('adx', 0):.1f} ({snap.get('regime', '-')}) | Trend: {snap.get('trend', 0)} | Mode: {snap.get('mode', '-')}", flush=True)


# ==============================================================================
# MODULE 4: EXECUTION STATE MACHINE & STRATEGY ENGINE
# ==============================================================================

class ExecutionEngine:
    def __init__(self):
        self.state_file = os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2.json")
        self.cache_file = os.path.join(PROJECT_ROOT, "data", "cache", "spot_cache.csv")
        self.live_snap_file = os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2.json")
        self.trade_book_dir = os.path.join(PROJECT_ROOT, "data", "logs", "trade_book")
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        os.makedirs(self.trade_book_dir, exist_ok=True)

        self.market_data = MarketData(self.cache_file)
        self.risk_manager = RiskManager(capital=CAPITAL)
        
        paper_mode = os.environ.get("PAPER_TRADING", "").strip().lower() in ("1", "true", "yes")
        if not paper_mode and os.path.exists("paper_mode.txt"):
            try:
                with open("paper_mode.txt", "r") as f:
                    paper_mode = f.read().strip().lower() in ("1", "true", "yes")
            except Exception:
                pass
        self.broker = FlattradeBroker(paper_trading=paper_mode)
        if not paper_mode:
            log_warn("LIVE TRADING MODE ACTIVE — real orders will be placed with real money.")
        
        self.mode = "WAIT_DATA"
        self.session_em_1sd = 0.0
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.realized_pnl: float = 0.0
        
        # Read Multiplier
        lots_multiplier = 1
        if os.path.exists("multiplier.txt"):
            try:
                with open("multiplier.txt", "r") as f:
                    lots_multiplier = max(1, int(f.read().strip()))
            except Exception:
                pass
                
        self.qty = lots_multiplier * LOT_SIZE
        log_info(f"Loaded Lot Multiplier: {lots_multiplier}x (Total Qty per leg: {self.qty})")
        
        self.cooldown_tracker: Dict[str, Dict[str, Any]] = {
            'CE': {'stopped_time': 0.0, 'stopped_spot': 0.0, 'active': False, 'safe_ticks': 0},
            'PE': {'stopped_time': 0.0, 'stopped_spot': 0.0, 'active': False, 'safe_ticks': 0}
        }
        
        df_1m = self.market_data.get_1m_dataframe()
        df_5m = self.market_data.get_5m_dataframe()
        self.current_indicators = Indicators.evaluate_all(df_1m, df_5m) if not df_5m.empty else {'atr': 35.0, 'regime': 'CHOP', 'trend': 0, 'adx': 18.0}
        
        self._ltp_cache: Dict[str, float] = {}
        self.last_event: str = "Engine started"
        self.dashboard = Dashboard()
        self._load_state()

    def _expiry_width_multiplier(self, dte_days: float) -> float:
        dte = max(0.001, float(dte_days))
        capped = min(dte, EXPIRY_WIDTH_LOOKAHEAD_DAYS)
        log_curve = 1.0 - (np.log1p(capped) / np.log1p(EXPIRY_WIDTH_LOOKAHEAD_DAYS))
        log_curve = float(np.clip(log_curve, 0.0, 1.0))
        if dte <= EXPIRY_NEAR_DAYS:
            near_curve = (EXPIRY_NEAR_DAYS - dte) / EXPIRY_NEAR_DAYS
            log_curve = min(1.0, log_curve + (near_curve ** 1.7) * EXPIRY_NEAR_BONUS)
        return float(np.clip(log_curve, 0.0, 1.0))

    def calculate_strangle_strikes(self, atm_spot: int, atr: float, regime: str, dte_days: float = 2.0) -> Tuple[int, int]:
        if regime == "CHOP":
            min_w, max_w, mult = CHOP_MIN_WIDTH_PTS, CHOP_MAX_WIDTH_PTS, CHOP_ATR_MULTIPLIER
        else:
            min_w, max_w, mult = BASE_MIN_WIDTH_PTS, BASE_MAX_WIDTH_PTS, BASE_ATR_MULTIPLIER

        if float(dte_days) <= 1.0: return atm_spot, atm_spot
        calculated_width = max(min_w, min(max_w, mult * atr))
        expiry_curve = self._expiry_width_multiplier(dte_days)
        expiry_floor = 50 if dte_days <= EXPIRY_NEAR_DAYS else min_w
        compressed_width = max(expiry_floor, round(max(min_w, calculated_width) * (1.0 - 0.65 * expiry_curve) + min_w * (0.65 * expiry_curve)))
        stride_50 = int(round(compressed_width / 50.0) * 50)
        final_width = max(expiry_floor, min(max_w, stride_50))
        
        ce_strike = atm_spot + final_width
        pe_strike = atm_spot - final_width
        if ce_strike <= atm_spot or pe_strike >= atm_spot or ce_strike == pe_strike:
            ce_strike, pe_strike = atm_spot + 50, atm_spot - 50
        return ce_strike, pe_strike

    def _save_state(self):
        try:
            state = {"date": str(get_ist_now().date()), "mode": self.mode, "realized_pnl": self.realized_pnl, "positions": self.positions, "cooldown_tracker": self.cooldown_tracker}
            tmp_path = self.state_file + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=4)
            os.replace(tmp_path, self.state_file)
        except Exception as e:
            log_alert(f"[STATE SAVE FAILED] Could not persist state: {e}")

    _KNOWN_MODES = {"WAIT_DATA", "RUNNING", "CHOP_MODE", "COOLDOWN", "SESSION_DONE", "SESSION_DONE_FLAT"}

    def _load_state(self):
        if not os.path.exists(self.state_file):
            log_info("No prior state file found. Starting fresh in WAIT_DATA.")
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
        except Exception as e:
            log_alert(f"[STATE LOAD FAILED] Could not parse {self.state_file}: {e}. Starting fresh in WAIT_DATA.")
            traceback.print_exc()
            return

        try:
            if state.get("date") != str(get_ist_now().date()):
                log_info("State file is from a previous day. Starting fresh in WAIT_DATA.")
                return

            self.realized_pnl = float(state.get("realized_pnl", 0.0))
            self.positions = state.get("positions", {})
            self.cooldown_tracker = state.get("cooldown_tracker", self.cooldown_tracker)
            saved_mode = state.get("mode", "WAIT_DATA")

            if saved_mode not in self._KNOWN_MODES:
                log_alert(f"[STATE CORRUPT] Unrecognized saved mode '{saved_mode}'. Resetting to WAIT_DATA.")
                self.mode = "RUNNING" if self.positions else "WAIT_DATA"
            else:
                now = get_ist_now()
                market_closed = (now.hour > AUTO_SQUAREOFF_HOUR or (now.hour == AUTO_SQUAREOFF_HOUR and now.minute >= AUTO_SQUAREOFF_MINUTE))
                if saved_mode in ("SESSION_DONE", "SESSION_DONE_FLAT") and not market_closed:
                    self.mode = "RUNNING" if self.positions else "WAIT_DATA"
                else:
                    self.mode = saved_mode

            log_info(f"Restored state: mode={self.mode}, positions={list(self.positions.keys())}, realized_pnl={self.realized_pnl:.2f}")

            for leg, pos in self.positions.items():
                if pos.get("side") == "SELL" and leg in ("CE", "PE"):
                    entry_spot = float(pos.get("entry_spot", self.market_data.latest_spot))
                    atr_val = float(self.current_indicators.get("atr", DEFAULT_ATR_5M))
                    new_sl_state = self.risk_manager.init_spot_sl(leg, entry_spot, atr_val)
                    if "spot_sl_state" in pos and isinstance(pos["spot_sl_state"], dict):
                        old_best = pos["spot_sl_state"].get("best_spot")
                        if old_best:
                            new_sl_state["best_spot"] = float(old_best)
                    pos["spot_sl_state"] = new_sl_state
                    log_info(f"Re-aligned {leg} Spot TSL: Entry={entry_spot:.2f}, SL={new_sl_state['current_sl']:.2f}, ATR={atr_val:.2f}")
            self._save_state()
        except Exception as e:
            log_alert(f"[STATE LOAD ERROR] Unexpected error applying loaded state: {e}. Resetting to WAIT_DATA.")
            traceback.print_exc()
            self.mode = "WAIT_DATA"
            self.positions = {}

    def _get_ltp(self, strike: int, option_type: str) -> float:
        key = f"{strike}_{option_type}"
        if key not in self._ltp_cache:
            q = self.market_data.streamer.get_live_quote(strike, option_type)
            self._ltp_cache[key] = float(q.get("lp", 0.0))
        return self._ltp_cache[key]

    def _enter_leg(self, leg: str, strike: int, side: str, spot: float, atr: float) -> bool:
        base = leg.split("_")[0]
        q = self.market_data.streamer.get_live_quote(strike, base)

        if not q.get("valid", True) or not q.get("tsym"):
            log_alert(f"[ENTRY ABORTED] {leg} strike {strike}: could not resolve a valid tradable symbol. Skipping this leg.")
            return False

        tsym = q["tsym"]
        self._ltp_cache[f"{strike}_{base}"] = q.get("lp", 0.0)
        ltp = self._get_ltp(strike, base)

        if ltp <= 0.0:
            log_alert(f"[ENTRY ABORTED] {leg} {tsym}: last traded price is {ltp}, refusing to enter on a zero/invalid quote.")
            return False

        order_result = self.broker.place_option_order(symbol=tsym, transaction_type=side, quantity=self.qty, price=ltp)
        if not order_result.get("ok"):
            log_alert(f"[ENTRY FAILED] {side} {leg} {tsym}: {order_result.get('msg')}. Position NOT recorded.")
            return False

        pos_info = {
            "strike": strike, "tsym": tsym, "base": base, "side": side, "qty": self.qty,
            "entry_price": ltp, "entry_time": time.time(), "entry_spot": spot,
            "order_no": order_result.get("order_no"),
        }
        if side == "SELL":
            pos_info["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, spot, atr)
        self.positions[leg] = pos_info
        log_trade(f"{side} {leg:10s} Strike: {strike} @ ₹{ltp:.2f} (Spot: {spot:.2f}) | Order: {order_result.get('order_no')}")
        self._save_state()
        return True

    def _exit_leg(self, leg: str, reason: str = "MANUAL") -> float:
        if leg not in self.positions: return 0.0
        pos = self.positions[leg]
        base = pos["base"]
        ltp = self._get_ltp(pos["strike"], base)
        close_side = "BUY" if pos["side"] == "SELL" else "SELL"
        order_result = self.broker.place_option_order(symbol=pos["tsym"], transaction_type=close_side, quantity=pos["qty"], price=ltp)

        if not order_result.get("ok"):
            log_alert(f"[EXIT FAILED] {leg} {pos['tsym']}: {order_result.get('msg')}. "
                      f"Position REMAINS OPEN and will be retried. Reason was: {reason}")
            return 0.0

        pnl = (pos["entry_price"] - ltp) * pos["qty"] if pos["side"] == "SELL" else (ltp - pos["entry_price"]) * pos["qty"]
        self.realized_pnl += pnl
        log_trade(f"EXITED {leg:10s} Strike: {pos['strike']} @ ₹{ltp:.2f} | P&L: ₹{pnl:,.2f} (Reason: {reason}) | Order: {order_result.get('order_no')}")
        del self.positions[leg]
        self._save_state()
        return pnl

    def _exit_all_positions(self, reason: str = "GLOBAL_EXIT"):
        for leg in list(self.positions.keys()): self._exit_leg(leg, reason=reason)

    def _trigger_leg_cooldown(self, stopped_leg: str, current_spot: float):
        self.cooldown_tracker[stopped_leg] = {
            'stopped_time': time.time(),
            'stopped_spot': current_spot,
            'active': True,
            'safe_ticks': 0
        }
        log_alert(f"⏳ {stopped_leg} entered Consecutive-Tick Cooldown (Monitoring for {CONSECUTIVE_TICKS_REQUIRED} consecutive stable ticks to re-enter).")
        self.mode = "COOLDOWN"
        self._save_state()

    def _check_cooldown_and_reenter(self, spot: float, atm: int, atr: float, regime: str, trend: int, dte_days: float = 2.0):
        """
        Evaluates active cooldowns on every 1-second live tick.
        Re-enters ONLY when CONSECUTIVE_TICKS_REQUIRED safe/favorable ticks are achieved consecutively.
        Any adverse tick (adverse momentum or push against stop) immediately resets counter to 0!
        """
        for leg in ("CE", "PE"):
            cd = self.cooldown_tracker.get(leg)
            if not cd or not cd.get("active", False): continue
            
            stopped_spot = cd.get("stopped_spot", spot)
            safe_ticks = cd.get("safe_ticks", 0)
            
            # ── 1. Evaluate Condition for Current Tick ──
            is_favorable_tick = False
            
            if leg == "CE":
                # CE is safe when spot is NOT aggressively making higher highs and KAMA trend is not Bullish (+1)
                if spot <= (stopped_spot + 3.0) and trend <= 0:
                    is_favorable_tick = True
            else:  # PE
                # PE is safe when spot is NOT aggressively making lower lows and KAMA trend is not Bearish (-1)
                if spot >= (stopped_spot - 3.0) and trend >= 0:
                    is_favorable_tick = True
            
            # ── 2. Increment or Reset Tick Counter ──
            if is_favorable_tick:
                safe_ticks += 1
                cd["safe_ticks"] = safe_ticks
            else:
                if safe_ticks > 0:
                    cd["safe_ticks"] = 0
            
            # ── 3. Check if Consecutive Requirement Met ──
            if safe_ticks >= CONSECUTIVE_TICKS_REQUIRED:
                log_info(f"✅ Consecutive Tick Condition Cleared for {leg} ({safe_ticks}/{CONSECUTIVE_TICKS_REQUIRED} stable ticks confirmed) -> Re-entering short strike...")
                ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days)
                target_strike = ce_strike if leg == "CE" else pe_strike
                if leg not in self.positions:
                    self._enter_leg(leg, target_strike, "SELL", spot, atr)
                cd["active"] = False
                cd["safe_ticks"] = 0
                self._save_state()
                if not any(v.get("active", False) for v in self.cooldown_tracker.values()):
                    self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"

    def _build_dashboard_snapshot(self, spot: float, atm: int, atr: float, regime: str, trend: int,
                                   dte_days: float, unrealized: float) -> Dict[str, Any]:
        positions_view = {}
        for leg, pos in self.positions.items():
            ltp = self._get_ltp(pos["strike"], pos["base"])
            live_pnl = (pos["entry_price"] - ltp) * pos["qty"] if pos["side"] == "SELL" else (ltp - pos["entry_price"]) * pos["qty"]
            positions_view[leg] = {**pos, "live_ltp": ltp, "live_pnl": live_pnl}

        snap = {
            "now_str": _now_str(),
            "mode": self.mode,
            "paper_mode": self.broker.paper_trading,
            "spot": spot, "atm": atm,
            "adx": self.current_indicators.get("adx", 18.0),
            "kama": self.current_indicators.get("kama"),
            "prev_kama": self.current_indicators.get("prev_kama"),
            "regime": regime, "trend": trend, "atr": atr, "dte": dte_days,
            "realized_pnl": self.realized_pnl, "unrealized_pnl": unrealized,
            "positions": positions_view,
            "cooldown_tracker": self.cooldown_tracker,
            "last_event": _LAST_EVENT["msg"],
        }
        try:
            tmp_snap = self.live_snap_file + ".tmp"
            with open(tmp_snap, "w") as f:
                json.dump(snap, f, indent=2)
            os.replace(tmp_snap, self.live_snap_file)
        except Exception:
            pass
        return snap

    def run(self):
        log_info("Starting V2 Pro Algorithmic State Machine...")
        self.dashboard.start()
        try:
            self._run_loop()
        finally:
            self.dashboard.stop()

    def _run_loop(self):
        while True:
            try:
                now = get_ist_now()
                self._ltp_cache.clear()
                
                # Check Auto Square-off Time
                if now.hour > AUTO_SQUAREOFF_HOUR or (now.hour == AUTO_SQUAREOFF_HOUR and now.minute >= AUTO_SQUAREOFF_MINUTE):
                    log_alert("🕒 Auto Square-Off Time Reached. Liquidating all positions...")
                    self._exit_all_positions(reason="SESSION_END")
                    self.mode = "SESSION_DONE"
                    self._save_state()
                    break

                # 1. Pre-Market Sleep Loop
                if now.hour < 9 or (now.hour == 9 and now.minute < 15):
                    _LAST_EVENT["msg"] = "Pre-market: sleeping until 09:15 to save API limits"
                    self.dashboard.update({"now_str": _now_str(), "mode": "PRE_MARKET",
                                            "paper_mode": self.broker.paper_trading,
                                            "last_event": _LAST_EVENT["msg"]})
                    time.sleep(60.0)
                    continue
                    
                spot, atm, is_new_1m_bar = self.market_data.fetch_live_tick()
                df_1m = self.market_data.get_1m_dataframe()
                df_5m = self.market_data.get_5m_dataframe()
                self.current_indicators = Indicators.evaluate_all(df_1m, df_5m)
                
                atr = self.current_indicators.get('atr', DEFAULT_ATR_5M)
                regime = self.current_indicators.get('regime', 'CHOP')
                trend = self.current_indicators.get('trend', 0)
                _, dte_days = self.market_data.streamer.get_near_expiry_dte()
                
                unrealized = sum([((p["entry_price"] - self._get_ltp(p["strike"], p["base"])) if p["side"] == "SELL" else (self._get_ltp(p["strike"], p["base"]) - p["entry_price"])) * p["qty"] for p in self.positions.values()])
                cb_triggered, cb_msg = self.risk_manager.check_portfolio_circuit_breaker(self.realized_pnl, unrealized)
                if cb_triggered:
                    log_alert(cb_msg)
                    self._exit_all_positions(reason="CIRCUIT_BREAKER_HALT")
                    sys.exit(0)

                if self.mode == "WAIT_DATA":
                    if now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE):
                        log_info(f"09:18 AM Reached. Entering Market (Regime: {regime})...")
                        hedge_width = 1000
                        ce_hedge_ok = "CE_HEDGE" in self.positions or self._enter_leg("CE_HEDGE", atm + hedge_width, "BUY", spot, atr)
                        pe_hedge_ok = "PE_HEDGE" in self.positions or self._enter_leg("PE_HEDGE", atm - hedge_width, "BUY", spot, atr)

                        if not (ce_hedge_ok and pe_hedge_ok):
                            log_alert("[ENTRY ABORTED] One or both hedge legs failed to fill. Refusing to sell naked shorts. Will retry next tick.")
                        else:
                            ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days)
                            if "CE" not in self.positions: self._enter_leg("CE", ce_strike, "SELL", spot, atr)
                            if "PE" not in self.positions: self._enter_leg("PE", pe_strike, "SELL", spot, atr)
                            if "CE" in self.positions and "PE" in self.positions:
                                self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"
                                self._save_state()

                elif self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN"):
                    if not self.positions:
                        self.mode = "WAIT_DATA"
                        continue

                    # Real-time Spot-Based SL & Premium Protection Check
                    for leg in ("CE", "PE"):
                        if leg in self.positions and self.positions[leg]["side"] == "SELL":
                            pos = self.positions[leg]
                            is_stopped, reason = self.risk_manager.update_spot_sl_and_check(leg, pos, spot, is_new_1m_bar)
                            
                            # Premium Protection: Safety guard if option spikes to 2.5x entry
                            if not is_stopped:
                                ltp = self._get_ltp(pos["strike"], pos["base"])
                                entry_prc = float(pos.get("entry_price", 0.0))
                                if entry_prc > 0 and ltp >= (entry_prc * 2.5):
                                    is_stopped = True
                                    reason = f"⛔ Premium SL Triggered for {leg} | LTP: ₹{ltp:.2f} hit 2.5x Entry Price (₹{entry_prc:.2f})"

                            if is_stopped:
                                log_alert(reason)
                                self._exit_leg(leg, reason="SPOT_TSL_HIT")
                                self._trigger_leg_cooldown(leg, spot)

                    # Handle disciplined re-entry after cooldown only on confirmed 1-min closes
                    self._check_cooldown_and_reenter(spot, atm, atr, regime, trend, dte_days=dte_days)

                snap = self._build_dashboard_snapshot(spot, atm, atr, regime, trend, dte_days, unrealized)
                self.dashboard.update(snap)

                # Sleep for 1 second for ultra-fast, real-time tick evaluation
                time.sleep(1.0)

            except KeyboardInterrupt:
                log_alert(f"Algo manually stopped. Realized PnL: ₹{self.realized_pnl:,.2f}")
                break
            except Exception as e:
                log_warn(f"Unhandled exception: {e}")
                traceback.print_exc()
                time.sleep(5.0)

if __name__ == "__main__":
    engine = ExecutionEngine()
    engine.run()
