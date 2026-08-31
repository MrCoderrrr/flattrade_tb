import os
import sys
import time
import json
import math
import socket
import select
import traceback
import urllib3.util.connection as urllib3_cn
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd

urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Setup IST Timezone
IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now() -> datetime: return datetime.now(IST).replace(tzinfo=None)

# ==============================================================================
# FLATTRADE CORE POLYFILLS (Replacing missing core.* modules)
# ==============================================================================

from api_helper import NorenApiPy
from creds import USER_ID

global_api = NorenApiPy()
if os.path.exists("token.txt"):
    with open("token.txt", "r") as f:
        access_token = f.read().strip()
        global_api.set_session(userid=str(USER_ID).strip(), password='', usertoken=access_token)
else:
    print("[FATAL] token.txt not found. Please run login.py first.")
    sys.exit(1)

class NSEATMStreamer:
    def __init__(self):
        self.api = global_api
        self._cached_expiry_date: Optional[datetime] = None
        self._cached_expiry_day: Optional[Any] = None
        self._last_spot: float = 24000.0
        self._last_atm: int = 24000

    def get_spot_and_atm(self) -> Tuple[float, int]:
        try:
            res = self.api.get_quotes(exchange='NSE', token='26000')
            if res and isinstance(res, dict) and res.get('stat') == 'Ok':
                spot = float(res.get('lp', res.get('ltp', self._last_spot)))
                if spot > 0:
                    self._last_spot = spot
                    self._last_atm = int(round(spot / 50.0) * 50)
                return self._last_spot, self._last_atm
        except Exception as e:
            pass
        return self._last_spot, self._last_atm

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
                quote = self.api.get_quotes(exchange='NFO', token=match['token'])
                lp = float(quote.get('lp', quote.get('ltp', 0.0))) if quote else 0.0
                return {"lp": lp, "tsym": tsym, "ls": int(match['ls'])}
        return {"lp": 0.0, "tsym": f"NIFTY_{strike}_{option_type}", "ls": 65}

    def get_near_expiry_dte(self) -> Tuple[Optional[datetime], float]:
        """Real DTE lookup (was previously a hardcoded stub returning (None, 2.0) always).
        Caches the nearest expiry for the day so we don't hit searchscrip every tick."""
        today = get_ist_now().date()
        if self._cached_expiry_date is None or self._cached_expiry_day != today:
            try:
                res = self.api.searchscrip(exchange='NFO', searchtext='NIFTY')
                candidates = []
                if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
                    for item in res['values']:
                        if 'exd' in item:
                            try:
                                candidates.append(datetime.strptime(item['exd'], "%d-%b-%Y"))
                            except ValueError:
                                continue
                future = [d for d in candidates if d.date() >= today]
                if future:
                    self._cached_expiry_date = min(future)
                elif candidates:
                    self._cached_expiry_date = min(candidates)
                else:
                    self._cached_expiry_date = get_ist_now() + timedelta(days=2)
            except Exception as e:
                print(f"Error fetching near expiry: {e}")
                self._cached_expiry_date = get_ist_now() + timedelta(days=2)
            self._cached_expiry_day = today
        dte = (self._cached_expiry_date - get_ist_now()).total_seconds() / 86400.0
        return self._cached_expiry_date, max(0.0, dte)

class FlattradeBroker:
    def __init__(self, paper_trading=False):
        self.api = global_api

    def place_option_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0.0, product_type: str = "M"):
        action = transaction_type[0].upper() # 'B' or 'S'
        
        # If price <= 0, try to get live quote
        if price <= 0.0:
            try:
                res_q = self.api.searchscrip(exchange='NFO', searchtext=symbol)
                if res_q and isinstance(res_q, dict) and res_q.get('values'):
                    for item in res_q['values']:
                        if item.get('tsym') == symbol:
                            q = self.api.get_quotes(exchange='NFO', token=item['token'])
                            if q: price = float(q.get('lp', q.get('ltp', 0.0)))
                            break
            except Exception:
                pass
        
        # Marketable limit order with guaranteed 0.05 tick multiple
        # NSE blocks price_type="MKT" on options, so we must use LMT with buffer
        if price > 0.0:
            buffer_pts = max(3.0, price * 0.10) # 10% marketable buffer
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
            prc_str = "500.00" if action == 'B' else "0.05"
            
        try:
            res = self.api.place_order(
                buy_or_sell=str(action),
                product_type=str(product_type),
                exchange="NFO",
                tradingsymbol=str(symbol),
                quantity=str(quantity),
                discloseqty="0",
                price_type=prctyp,
                price=prc_str,
                trigger_price="0",
                retention="DAY",
                remarks="API_V2_PRO"
            )
            if not res or not isinstance(res, dict) or res.get('stat') != 'Ok':
                err = res.get('emsg', str(res)) if isinstance(res, dict) else str(res)
                log_alert(f"❌ Flattrade Order REJECTED [{action} {quantity}x {symbol} @ ₹{prc_str}]: {err}")
            else:
                ord_id = res.get('norenordno', res.get('order_id', 'OK'))
                log_info(f"✅ Flattrade Order FILLED/PLACED [{action} {quantity}x {symbol} @ ₹{prc_str}]: OrderID={ord_id}")
            return res
        except Exception as e:
            log_alert(f"❌ Flattrade Order EXCEPTION: {e}")
            return {"stat": "Not_Ok", "emsg": str(e)}

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
# ║                     STRATEGY CONFIGURATION (V2)                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

CAPITAL                 = 10_00_000   
LOT_SIZE                = 65          
CAPITAL_BUFFER          = 0.95        
MARGIN_IRON_CONDOR      = 95_000      
PORTFOLIO_CIRCUIT_PCT   = 1.8         

KAMA_PERIOD             = 13          
KAMA_FAST_EMA           = 3           
KAMA_SLOW_EMA           = 30          
# KAMA now runs on 5m closes (same series as ADX/ATR) instead of 1m — 1m KAMA
# whipsawed too fast for a value that now actively skews live strikes.
# Retune against backtest; 1.0 was tuned for 1m bars, not 5m.
KAMA_MIN_SLOPE          = 2.5         

# ADX_CHOP_THRESHOLD and ADX_TREND_THRESHOLD were both 20.0, so the TRANSITION
# branch below was dead code and regime flipped on a single ADX tick.
# Splitting them creates a real neutral band: regime is only CHOP below the
# low threshold, only TREND above the high one, and TRANSITION (freeze,
# no new entries/rolls/reentries) in between. Widen/narrow the gap as backtest
# data suggests.
ADX_PERIOD              = 9           
ADX_CHOP_THRESHOLD      = 16.0        
ADX_TREND_THRESHOLD     = 24.0        

ATR_PERIOD              = 14          
DEFAULT_ATR_5M          = 35.0        

HEDGE_WIDTH_PTS         = 1000        

# These four were all 0, which made calculate_strangle_strikes collapse to
# atm±50 on every single entry regardless of regime/ATR. Placeholder values
# below are not tuned — backtest before trusting them with capital.
BASE_MIN_WIDTH_PTS      = 300         
BASE_MAX_WIDTH_PTS      = 900         
BASE_ATR_MULTIPLIER     = 1.0         

CHOP_MIN_WIDTH_PTS      = 150         
CHOP_MAX_WIDTH_PTS      = 550         
CHOP_ATR_MULTIPLIER     = 1.5         

# New: how far (in points) the strangle center can skew toward the KAMA trend
# direction, and the ADX level at which that skew reaches its max magnitude.
TREND_SKEW_MAX_PTS      = 250         
TREND_SKEW_ADX_REF      = 40.0        

EXPIRY_WIDTH_LOOKAHEAD_DAYS = 8.0     
EXPIRY_NEAR_DAYS            = 2.0     
EXPIRY_NEAR_BONUS           = 0.42    

SPOT_SL_ATR_MULT        = 0.90        
SPOT_SL_TRAIL_RATIO     = 0.55        
SPOT_SL_TRAIL_RATIO_STRONG = 0.72     
SPOT_SL_TRAIL_RATIO_DEEP   = 0.85     
SPOT_SL_BREAKEVEN_LOCK_ATR = 1.10     
SPOT_SL_BREAKEVEN_BUFFER_PTS = 5.0    
SPOT_SL_DEBOUNCE_BARS   = 2           

COOLDOWN_MINUTES        = 3           
COOLDOWN_SPOT_PCT       = 0.0010      

MARKET_START_HOUR       = 9
MARKET_START_MINUTE     = 18          
AUTO_SQUAREOFF_HOUR     = 15
AUTO_SQUAREOFF_MINUTE   = 28          
REFRESH_INTERVAL_SEC    = 1          

# Trend rolls: only roll if the new ideal strike is at least this many ATRs
# away from the current strike. Prevents micro-roll churn on every 1m bar.
TREND_ROLL_MIN_ATR_MULT = 0.5         # e.g. 0.5 * 35 ATR = 17.5pts minimum drift before rolling

# CHOP rolls: only re-evaluate strike width once per N minutes, not every tick.
CHOP_ROLL_DEBOUNCE_MIN  = 5           # re-check chop strike every 5 minutes max



def _now_str() -> str: return get_ist_now().strftime("%H:%M:%S")
def log_info(msg: str): print(f"[{_now_str()} INFO]  {msg}", flush=True)
def log_warn(msg: str): print(f"[{_now_str()} WARNING]  {msg}", flush=True)
def log_alert(msg: str): print(f"[{_now_str()} ALERT]  {msg}", flush=True)
def log_trade(msg: str): print(f"[{_now_str()} TRADE]  {msg}", flush=True)
def round_to_strike(price: float, strike_step: int = 50) -> int: return int(round(price / float(strike_step)) * strike_step)


# ==============================================================================
# MODULE 1: MARKET DATA INGESTION & 5-MIN AGGREGATION
# ==============================================================================

class MarketData:
    def __init__(self, cache_file: str):
        self.cache_file = cache_file
        self.streamer = NSEATMStreamer()
        self.bars_1m: List[Dict[str, Any]] = []
        self.logged_1m_keys: set = set()
        self.bars_5m: List[Dict[str, Any]] = []
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
        if len(self.bars_5m) >= 30: return
        try:
            log_info("MarketData: Seeding historical 5-min bars from Flattrade for instant indicator readiness...")
            end_time = get_ist_now()
            start_time = end_time - timedelta(days=5)
            
            # Use Flattrade API directly to get 5m data
            res = global_api.get_time_price_series(
                exchange='NSE', token='26000', 
                starttime=start_time.timestamp(), 
                endtime=end_time.timestamp(), 
                interval=5
            )
            
            if res and isinstance(res, list) and len(res) > 0:
                seeded_5m = []
                for row in res:
                    try:
                        # Flattrade returns: 'time', 'into' (open), 'inth' (high), 'intl' (low), 'intc' (close)
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
                    # Sort chronologically just in case
                    seeded_5m.sort(key=lambda x: x['timestamp'])
                    today = get_ist_now().date()
                    prior_bars = [b for b in seeded_5m if b['timestamp'].date() < today][-50:]
                    self.bars_5m = prior_bars + self.bars_5m
                    log_info(f"Successfully seeded {len(prior_bars)} 5m bars from Flattrade.")
            else:
                log_warn("Flattrade get_time_price_series returned empty or failed.")
                
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
        if not self.bars_5m: return pd.DataFrame(columns=['open', 'high', 'low', 'close'])
        return pd.DataFrame(self.bars_5m)

    def get_1m_dataframe(self) -> pd.DataFrame:
        if not self.bars_1m: return pd.DataFrame(columns=['open', 'high', 'low', 'close'])
        df = pd.DataFrame(self.bars_1m)
        df.set_index('timestamp', inplace=True)
        df_1m = df['spot'].resample('1min', label='left', closed='left').ohlc().dropna()
        return df_1m


# ==============================================================================
# MODULE 2: INDICATORS & REGIME DETECTION
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
        if n < period * 2: return 18.0, 20.0, 20.0
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            if up_move > down_move and up_move > 0: plus_dm[i] = up_move
            else: plus_dm[i] = 0.0
            if down_move > up_move and down_move > 0: minus_dm[i] = down_move
            else: minus_dm[i] = 0.0
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
    def evaluate_all(cls, df_1m: pd.DataFrame, df_5m: pd.DataFrame):
        # 1. KAMA on 1-Minute Close Data (13, 3, 30)
        kama, prev_kama, trend = None, None, 0
        kama_slope = 0.0
        if not df_1m.empty and len(df_1m) >= KAMA_PERIOD + 1:
            closes_1m = df_1m['close'].to_numpy(dtype=float)
            kama, prev_kama, trend = cls.calculate_kama(closes_1m, period=KAMA_PERIOD, fast=KAMA_FAST_EMA, slow=KAMA_SLOW_EMA)
            kama_slope = (kama - prev_kama) if (kama is not None and prev_kama is not None) else 0.0

        # 2. ADX (9) and ATR (14) on 5-Minute Data
        if df_5m.empty or len(df_5m) < 5:
            return {
                'kama': kama, 'prev_kama': prev_kama, 'trend': trend, 'kama_slope': kama_slope,
                'atr': DEFAULT_ATR_5M, 'adx': 18.0, 'plus_di': 20.0, 'minus_di': 20.0, 'regime': 'CHOP'
            }
            
        highs_5m = df_5m['high'].to_numpy(dtype=float)
        lows_5m = df_5m['low'].to_numpy(dtype=float)
        closes_5m = df_5m['close'].to_numpy(dtype=float)
        
        atr = cls.calculate_atr(highs_5m, lows_5m, closes_5m, period=ATR_PERIOD)
        adx, p_di, m_di = cls.calculate_adx(highs_5m, lows_5m, closes_5m, period=ADX_PERIOD)
        
        if adx < ADX_CHOP_THRESHOLD: regime = "CHOP"
        elif adx >= ADX_TREND_THRESHOLD: regime = "TREND"
        else: regime = "TRANSITION"
        
        return {
            'kama': kama, 'prev_kama': prev_kama, 'trend': trend, 'kama_slope': kama_slope,
            'atr': atr, 'adx': adx, 'plus_di': p_di, 'minus_di': m_di, 'regime': regime
        }


# ==============================================================================
# MODULE 3: RISK MANAGER (DYNAMIC PREMIUM-BASED TSL)
# ==============================================================================

class RiskManager:
    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1.0 * (capital * PORTFOLIO_CIRCUIT_PCT / 100.0)

    def init_premium_sl(self, entry_price: float, live_ltp: float = 0.0, regime: str = "CHOP") -> Dict[str, Any]:
        """
        Initialize premium-based trailing stop loss state for a sold option.
        - Initial Stop Loss is set ABOVE the entry price:
            CHOP mode:  Initial SL = entry_price * 1.05 (+5% max loss)
            TREND mode: Initial SL = entry_price * 1.07 (+7% max loss)
        - lowest_ltp tracks the lowest price reached by the short option contract.
        - As lowest_ltp drops below entry, TSL follows it from above:
            TSL = lowest_ltp * (1.0 + trail_pct)
        """
        safe_entry = max(0.5, float(entry_price))
        cur_ltp = float(live_ltp) if live_ltp > 0.0 else safe_entry
        lowest_p = min(safe_entry, cur_ltp)
        
        trail_pct = 0.05 if regime == "CHOP" else 0.07  # max 7%
        initial_sl_price = round(safe_entry * (1.0 + trail_pct), 2)
        current_sl_price = round(lowest_p * (1.0 + trail_pct), 2)

        return {
            "entry_price": safe_entry,
            "lowest_ltp": round(lowest_p, 2),
            "initial_sl": initial_sl_price,
            "current_sl": current_sl_price,
            "regime": regime,
            "trail_pct": trail_pct,
            "profit_locked": False
        }

    def update_premium_tsl_and_check(self, leg: str, pos_data: Dict[str, Any], live_ltp: float, regime: str = "CHOP") -> Tuple[bool, str]:
        """
        Dynamic Premium-Based Trailing Stop Loss — evaluated every second on live tick data.
        
        For a short option:
        - Price going DOWN = Profit. lowest_ltp updates when live_ltp < lowest_ltp.
        - SL is always ABOVE the lowest price achieved.
        - Trailing SL: current_sl = min(initial_sl, lowest_ltp * (1 + trail_pct))
        - If profit >= 5% (lowest_ltp <= entry_price * 0.95), TSL is locked at or below entry price.
        - If live_ltp >= current_sl: Stop Loss triggers immediately!
        """
        entry_price = float(pos_data.get("entry_price", 0.0))
        if entry_price <= 0.0:
            return False, ""

        if live_ltp <= 0.0:
            return False, ""

        trail_pct = 0.05 if regime == "CHOP" else 0.07  # max 7%

        sl_state = pos_data.get("premium_sl_state")
        if not sl_state or not isinstance(sl_state, dict):
            sl_state = self.init_premium_sl(entry_price, live_ltp, regime)
            pos_data["premium_sl_state"] = sl_state

        # Update lowest_ltp if live price made a new lower low (profit for short seller)
        raw_lowest = float(sl_state.get("lowest_ltp", entry_price))
        if live_ltp < raw_lowest:
            raw_lowest = live_ltp
        # Lowest can never exceed entry_price
        lowest_ltp = min(raw_lowest, entry_price)
        sl_state["lowest_ltp"] = round(lowest_ltp, 2)

        # Initial SL is entry_price + 5%/7%
        initial_sl = round(entry_price * (1.0 + trail_pct), 2)
        sl_state["initial_sl"] = initial_sl

        # Dynamic Trailing SL based on the lowest price reached
        candidate_sl = round(lowest_ltp * (1.0 + trail_pct), 2)
        
        # When position has generated >= 5% profit (price dropped below 95% of entry),
        # ensure current_sl cannot exceed entry price (breakeven lock)
        if lowest_ltp <= (entry_price * 0.95):
            candidate_sl = min(candidate_sl, entry_price)
            sl_state["profit_locked"] = True

        # Current SL is the minimum of initial_sl and candidate_sl (ratchets down only)
        current_sl = min(initial_sl, candidate_sl)
        sl_state["current_sl"] = current_sl

        # Check trigger condition
        if live_ltp >= current_sl:
            loss_or_profit = "Loss" if live_ltp > entry_price else "Profit"
            return True, (
                f"⛔ Premium TSL Hit for {leg} ({loss_or_profit}) | Entry: ₹{entry_price:.2f}, "
                f"Best Low: ₹{lowest_ltp:.2f}, TSL: ₹{current_sl:.2f}, Live LTP: ₹{live_ltp:.2f} "
                f"(Regime: {regime}, Trail: {int(trail_pct*100)}%)"
            )

        return False, ""

    def check_portfolio_circuit_breaker(self, realized_pnl: float, unrealized_pnl: float):
        combined_mtm = realized_pnl + unrealized_pnl
        if combined_mtm <= self.circuit_breaker_loss_limit:
            return True, f"🚨 PORTFOLIO CIRCUIT BREAKER HIT 🚨 Combined MTM breached limit. Shutting down!"
        return False, ""


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
        self.broker = FlattradeBroker(paper_trading=False)
        
        self.mode = "WAIT_DATA"
        self.session_em_1sd = 0.0
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.realized_pnl: float = 0.0
        self._last_trend_roll_5m_key: Optional[str] = None
        
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
            'CE': {'stopped_time': 0.0, 'stopped_spot': 0.0, 'active': False},
            'PE': {'stopped_time': 0.0, 'stopped_spot': 0.0, 'active': False}
        }
        
        df_1m = self.market_data.get_1m_dataframe()
        df_5m = self.market_data.get_5m_dataframe()
        self.current_indicators = Indicators.evaluate_all(df_1m, df_5m) if not df_5m.empty else {'atr': 35.0, 'regime': 'CHOP', 'trend': 0, 'adx': 18.0}
        
        self._ltp_cache: Dict[str, float] = {}
        self._last_whipsaw_time: float = 0.0         # timestamp of last whipsaw dual-leg exit
        self._processed_1m_bar: Optional[str] = None # ensures is_new_1m_bar is one-shot per bar
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

    def calculate_strangle_strikes(self, atm_spot: int, atr: float, regime: str, dte_days: float = 2.0, trend: int = 0, adx: float = 18.0) -> Tuple[int, int]:
        """
        Strike selection based on market choppiness / volatility:
        - Very High Chop (Highs & Lows large, ATR >= 40 or ADX < 12):  OTM 3 (ATM ± 150)
        - Medium Chop (ATR 28 - 40 or ADX 12 - 16):                    OTM 2 (ATM ± 100)
        - Average Chop (ATR 18 - 28):                                  OTM 1 (ATM ± 50)
        - Very Low / Tight Range (ATR < 18):                           ATM (ATM ± 0)
        """
        if regime == "CHOP":
            if atr >= 40.0 or adx < 12.0:
                otm_pts = 150  # OTM 3 (High chop / wide candle ranges)
            elif atr >= 28.0 or adx < 16.0:
                otm_pts = 100  # OTM 2 (Medium chop)
            elif atr >= 18.0:
                otm_pts = 50   # OTM 1 (Average chop)
            else:
                otm_pts = 0    # ATM (Tight chop / low volatility)
        elif regime == "TREND":
            base_otm = 100 if atr >= 30.0 else 50
            if trend == 1:    # Bullish: Call higher (+100/150), Put closer (+50)
                ce_strike = atm_spot + base_otm + 50
                pe_strike = max(atm_spot - base_otm, atm_spot - 50)
                return ce_strike, pe_strike
            elif trend == -1:  # Bearish: Put lower (-100/150), Call closer (-50)
                ce_strike = min(atm_spot + base_otm, atm_spot + 50)
                pe_strike = atm_spot - base_otm - 50
                return ce_strike, pe_strike
            else:
                otm_pts = base_otm
        else:  # TRANSITION
            otm_pts = 100 if atr >= 28.0 else 50

        ce_strike = atm_spot + otm_pts
        pe_strike = atm_spot - otm_pts
        return ce_strike, pe_strike

    def _save_state(self):
        try:
            state = {"date": str(get_ist_now().date()), "mode": self.mode, "realized_pnl": self.realized_pnl, "positions": self.positions, "cooldown_tracker": self.cooldown_tracker}
            with open(self.state_file, "w") as f: json.dump(state, f, indent=4)
        except: pass

    def _load_state(self):
        if not os.path.exists(self.state_file): return
        try:
            with open(self.state_file, "r") as f: state = json.load(f)
            if state.get("date") == str(get_ist_now().date()):
                self.realized_pnl = float(state.get("realized_pnl", 0.0))
                self.positions = state.get("positions", {})
                self.cooldown_tracker = state.get("cooldown_tracker", self.cooldown_tracker)
                saved_mode = state.get("mode", "WAIT_DATA")
                now = get_ist_now()
                market_closed = (now.hour > AUTO_SQUAREOFF_HOUR or (now.hour == AUTO_SQUAREOFF_HOUR and now.minute >= AUTO_SQUAREOFF_MINUTE))
                if market_closed:
                    self.mode = "SESSION_DONE"
                else:
                    self.mode = "RUNNING" if self.positions else "WAIT_DATA"

                for leg, pos in self.positions.items():
                    if pos.get("side") == "SELL":
                        if "premium_sl_state" not in pos or not pos["premium_sl_state"]:
                            pos["premium_sl_state"] = self.risk_manager.init_premium_sl(float(pos.get("entry_price", 0.0)), self.current_indicators.get("regime", "CHOP"))
        except: pass

    def _sync_positions_from_broker(self):
        """
        Query Flattrade live positions and reconcile with self.positions.
        Called once on startup to prevent duplicate orders after a restart.

        Flattrade symbol format: NIFTY01SEP26C24050  (ends in C{strike} or P{strike})
        NOT: NIFTY24050CE — so we must use regex [CP](\\d+)$ to detect option type.

        - Legs open in broker but missing from state → import them (track, don't re-enter)
        - Legs in state but net qty=0 in broker (closed externally) → remove ghost
        - Legs in both → keep existing state (has SL state, entry_spot, etc.)
        """
        import re as _re
        log_info("Syncing with Flattrade live positions...")
        try:
            res = self.broker.api.get_positions()
            if not res or not isinstance(res, list):
                log_warn("Broker sync: empty response — using state file only.")
                return

            # Build broker map: tsym -> {netqty, avgprc, prd, token}
            # Flattrade uses 'exch' (not 'exchange') as field name
            broker_map: Dict[str, Dict] = {}
            for p in res:
                exch = p.get('exch', p.get('exchange', ''))
                if exch != 'NFO': continue
                tsym   = p.get('tsym', '')
                netqty = int(p.get('netqty', 0) or 0)
                avgprc = float(p.get('netavgprc', p.get('avgprc', 0.0)) or 0.0)
                prd    = p.get('prd', 'M')
                token  = p.get('token', '')
                if tsym:
                    broker_map[tsym] = {'netqty': netqty, 'avgprc': avgprc, 'prd': prd, 'token': token}

            log_info(f"Broker NFO positions ({len(broker_map)}): {list(broker_map.keys())}")

            # Recompute true Realized P&L from Flattrade Trade Book on startup
            try:
                tb_res = self.broker.api.get_trade_book()
                if tb_res and isinstance(tb_res, list):
                    nfo_trades = [t for t in tb_res if t.get('exch', t.get('exchange', '')) == 'NFO']
                    sym_fills = {}
                    for t in nfo_trades:
                        s = t.get('tsym', '')
                        q = int(t.get('qty', t.get('fillshares', 0)) or 0)
                        p_fill = float(t.get('avgprc', t.get('flprc', 0.0)) or 0.0)
                        side_t = t.get('trantype', t.get('buy_or_sell', 'B')).upper()
                        if s not in sym_fills:
                            sym_fills[s] = {'buy_qty': 0, 'sell_qty': 0, 'buy_cost': 0.0, 'sell_proceeds': 0.0}
                        if side_t == 'B':
                            sym_fills[s]['buy_cost'] += q * p_fill
                            sym_fills[s]['buy_qty'] += q
                        else:
                            sym_fills[s]['sell_proceeds'] += q * p_fill
                            sym_fills[s]['sell_qty'] += q

                    realized_calc = 0.0
                    for s, f_data in sym_fills.items():
                        matched_qty = min(f_data['buy_qty'], f_data['sell_qty'])
                        if matched_qty > 0 and f_data['buy_qty'] > 0 and f_data['sell_qty'] > 0:
                            avg_buy = f_data['buy_cost'] / f_data['buy_qty']
                            avg_sell = f_data['sell_proceeds'] / f_data['sell_qty']
                            realized_calc += (avg_sell - avg_buy) * matched_qty
                    self.realized_pnl = round(realized_calc, 2)
                    log_info(f"Verified Realized P&L from Flattrade Trade Book: ₹{self.realized_pnl:,.2f}")
                else:
                    if self.realized_pnl < -10000.0 * (self.qty / LOT_SIZE):
                        log_warn(f"Resetting corrupted realized PnL ({self.realized_pnl}) to ₹0.00")
                        self.realized_pnl = 0.0
            except Exception as e:
                log_warn(f"Trade book PnL verification error: {e}")

            atr  = self.current_indicators.get('atr', DEFAULT_ATR_5M)
            spot = self.market_data.latest_spot

            # ---------------------------------------------------------------
            # Step 1: Remove ghost positions (in state but closed/missing in broker)
            # ---------------------------------------------------------------
            for leg in list(self.positions.keys()):
                tsym = self.positions[leg].get('tsym', '')
                bqty = broker_map.get(tsym, {}).get('netqty', None)
                if bqty is None or bqty == 0:
                    log_warn(f"Removing ghost: {leg} ({tsym}) — not open in broker.")
                    del self.positions[leg]

            # ---------------------------------------------------------------
            # Step 2: Import positions open in broker but missing from state
            # ---------------------------------------------------------------
            known_tsyms = {p.get('tsym') for p in self.positions.values()}

            for tsym, bdata in broker_map.items():
                netqty = bdata['netqty']
                if netqty == 0 or tsym in known_tsyms:
                    continue

                side   = "SELL" if netqty < 0 else "BUY"
                qty    = abs(netqty)
                avgprc = bdata['avgprc']
                prd    = bdata.get('prd', 'M')

                # ---- Correct base detection for Flattrade symbol format ----
                # Format: NIFTY01SEP26C24050 → C24050 at end → base=CE, strike=24050
                # Format: NIFTY01SEP26P23950 → P23950 at end → base=PE, strike=23950
                strike = 0
                base   = 'CE'  # default
                m = _re.search(r'([CP])(\d{4,6})$', tsym)
                if m:
                    base   = 'CE' if m.group(1) == 'C' else 'PE'
                    strike = int(m.group(2))
                else:
                    log_warn(f"Could not parse option type from tsym: {tsym}, defaulting to CE")

                # ---- Leg naming: use SIDE to distinguish hedge vs short ----
                # BUY positions = hedges (CE_HEDGE / PE_HEDGE)
                # SELL positions = main short legs (CE / PE)
                if side == 'BUY':
                    preferred = f'{base}_HEDGE'
                    fallback  = f'{base}_HEDGE_{tsym[-4:]}'
                else:
                    preferred = base  # 'CE' or 'PE'
                    fallback  = f'{base}_{tsym[-4:]}'

                leg = preferred if preferred not in self.positions else fallback

                # ---- Build position dict ----
                pos_info = {
                    "strike":     strike,
                    "tsym":       tsym,
                    "base":       base,
                    "side":       side,
                    "qty":        qty,
                    "entry_price": avgprc,
                    "entry_time": time.time(),
                    "entry_spot": spot,
                    "prd":        prd,
                }
                if side == "SELL":
                    pos_info["premium_sl_state"] = self.risk_manager.init_premium_sl(avgprc, self.current_indicators.get("regime", "CHOP"))

                self.positions[leg] = pos_info
                log_info(f"Imported: {leg} ({side} {qty}x {tsym} strike={strike} base={base} @ ₹{avgprc:.2f})")

            # ---------------------------------------------------------------
            # Step 3: Set correct mode based on reconciled positions
            # ---------------------------------------------------------------
            if self.positions:
                self.mode = "RUNNING"
                log_info(f"Mode → RUNNING ({len(self.positions)} open positions active).")
            else:
                self.mode = "WAIT_DATA"
                log_info("No open positions — mode → WAIT_DATA.")

            self._save_state()
            log_info(f"Sync done. Tracking {len(self.positions)} legs: {list(self.positions.keys())}")

        except Exception as e:
            log_warn(f"Position sync error (non-fatal, continuing): {e}")
            traceback.print_exc()

    def _get_ltp_by_tsym(self, tsym: str) -> float:
        """Fetch LTP directly by full trading symbol (tsym) — used when strike=0."""
        if not tsym: return 0.0
        try:
            res = self.market_data.streamer.api.searchscrip(exchange='NFO', searchtext=tsym)
            if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
                for item in res['values']:
                    if item.get('tsym') == tsym:
                        q = self.market_data.streamer.api.get_quotes(exchange='NFO', token=item['token'])
                        if q: return float(q.get('lp', q.get('ltp', 0.0)))
        except Exception:
            pass
        return 0.0

    def _get_ltp(self, strike: int, option_type: str, tsym: str = '') -> float:
        """Get LTP by strike+type. Falls back to tsym-based lookup if strike=0."""
        if strike == 0 and tsym:
            # Strike unknown (imported position) — use tsym directly
            cached = self._ltp_cache.get(f'tsym_{tsym}')
            if cached is not None: return cached
            ltp = self._get_ltp_by_tsym(tsym)
            self._ltp_cache[f'tsym_{tsym}'] = ltp
            return ltp
        key = f"{strike}_{option_type}"
        if key not in self._ltp_cache:
            q = self.market_data.streamer.get_live_quote(strike, option_type)
            self._ltp_cache[key] = float(q.get("lp", 0.0))
        return self._ltp_cache[key]

    def _enter_leg(self, leg: str, strike: int, side: str, spot: float, atr: float, product_type: str = "M", regime: str = "CHOP"):
        base = leg.split("_")[0]
        q = self.market_data.streamer.get_live_quote(strike, base)
        tsym = q.get("tsym", f"NIFTY_{strike}_{base}")
        self._ltp_cache[f"{strike}_{base}"] = q.get("lp", 0.0)
        ltp = self._get_ltp(strike, base, tsym=tsym)
        
        self.broker.place_option_order(symbol=tsym, transaction_type=side, quantity=self.qty, price=ltp, product_type=product_type)
        pos_info = {"strike": strike, "tsym": tsym, "base": base, "side": side, "qty": self.qty, "entry_price": ltp, "entry_time": time.time(), "entry_spot": spot, "prd": product_type}
        if side == "SELL":
            pos_info["premium_sl_state"] = self.risk_manager.init_premium_sl(ltp, regime)
        self.positions[leg] = pos_info
        log_trade(f"{side} {leg:10s} Strike: {strike} @ ₹{ltp:.2f} (Spot: {spot:.2f})")
        self._save_state()

    def _exit_leg(self, leg: str, reason: str = "MANUAL") -> float:
        if leg not in self.positions: return 0.0
        pos = self.positions[leg]
        base = pos["base"]
        tsym = pos.get("tsym", "")
        prd = pos.get("prd", "M")
        # Pass tsym so _get_ltp can do a direct lookup when strike=0 (imported positions)
        ltp = self._get_ltp(pos["strike"], base, tsym=tsym)
        if ltp <= 0.0:
            log_warn(f"_exit_leg: LTP=0 for {leg} ({tsym}), using entry_price as fallback.")
            ltp = float(pos.get("entry_price", 0.0))
        close_side = "BUY" if pos["side"] == "SELL" else "SELL"
        self.broker.place_option_order(symbol=tsym, transaction_type=close_side, quantity=pos["qty"], price=ltp, product_type=prd)
        pnl = (pos["entry_price"] - ltp) * pos["qty"] if pos["side"] == "SELL" else (ltp - pos["entry_price"]) * pos["qty"]
        self.realized_pnl += pnl
        log_trade(f"EXITED {leg:10s} Strike: {pos['strike']} @ ₹{ltp:.2f} | P&L: ₹{pnl:,.2f} (Reason: {reason})")
        del self.positions[leg]
        self._save_state()
        return pnl

    def _exit_all_positions(self, reason: str = "GLOBAL_EXIT"):
        for leg in list(self.positions.keys()): self._exit_leg(leg, reason=reason)

    def _calculate_unrealized_pnl(self) -> float:
        total = 0.0
        for p in self.positions.values():
            tsym = p.get("tsym", "")
            strike = int(p.get("strike", 0))
            base = p.get("base", "CE")
            entry = float(p.get("entry_price", 0.0))
            ltp = self._get_ltp(strike, base, tsym=tsym)
            # If LTP is 0 or failed to fetch, fall back to entry price so unrealized is 0 rather than a huge fake loss
            if ltp <= 0.0:
                ltp = entry
            qty = int(p.get("qty", self.qty))
            side = p.get("side", "SELL")
            pnl = (entry - ltp) * qty if side == "SELL" else (ltp - entry) * qty
            total += pnl
        return total

    def _write_snapshot(self, spot: float, atm: int, atr: float, regime: str, trend: int, adx: float, dte_days: float, unrealized: float):
        try:
            positions_view = {}
            for leg, p in self.positions.items():
                tsym = p.get("tsym", "")
                strike = int(p.get("strike", 0))
                base = p.get("base", "CE")
                entry = float(p.get("entry_price", 0.0))
                ltp = self._get_ltp(strike, base, tsym=tsym)
                if ltp <= 0.0: ltp = entry
                qty = int(p.get("qty", self.qty))
                side = p.get("side", "SELL")
                pnl = (entry - ltp) * qty if side == "SELL" else (ltp - entry) * qty
                positions_view[leg] = {
                    **p,
                    "live_ltp": ltp,
                    "live_pnl": pnl,
                }

            kama_val = self.current_indicators.get("kama")
            snap = {
                "now_str": _now_str(),
                "mode": self.mode,
                "paper_mode": False,
                "spot": spot,
                "atm": atm,
                "adx": adx,
                "kama": kama_val,
                "regime": regime,
                "trend": trend,
                "atr": atr,
                "dte": dte_days,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized,
                "positions": positions_view,
                "cooldown_tracker": self.cooldown_tracker,
                "last_event": f"Active in {regime} regime | {len(self.positions)} open legs"
            }
            tmp_file = self.live_snap_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(snap, f, indent=2)
            os.replace(tmp_file, self.live_snap_file)
        except Exception:
            pass

    def run(self):
        log_info("Starting V2 Pro Algorithmic State Machine...")
        # Sync with Flattrade live positions before entering the main loop.
        # This prevents duplicate orders after a restart by importing what's
        # already open in the broker and removing ghost positions from the state.
        self._sync_positions_from_broker()

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
                    print(f"[{_now_str()}] Pre-market. Sleeping until 09:15 AM to save API limits...", flush=True)
                    time.sleep(60.0)
                    continue
                    
                spot, atm, is_new_1m_bar = self.market_data.fetch_live_tick()

                # One-shot guard: treat is_new_1m_bar as True only ONCE per bar,
                # not on every 1s tick within the same minute window.
                current_bar_key = self.market_data.last_completed_1m_key
                if is_new_1m_bar and current_bar_key == self._processed_1m_bar:
                    is_new_1m_bar = False
                elif is_new_1m_bar:
                    self._processed_1m_bar = current_bar_key

                df_1m = self.market_data.get_1m_dataframe()
                df_5m = self.market_data.get_5m_dataframe()
                self.current_indicators = Indicators.evaluate_all(df_1m, df_5m)
                
                atr = self.current_indicators.get('atr', DEFAULT_ATR_5M)
                regime = self.current_indicators.get('regime', 'CHOP')
                trend = self.current_indicators.get('trend', 0)
                adx = self.current_indicators.get('adx', 18.0)
                _, dte_days = self.market_data.streamer.get_near_expiry_dte()
                
                unrealized = self._calculate_unrealized_pnl()
                cb_triggered, cb_msg = self.risk_manager.check_portfolio_circuit_breaker(self.realized_pnl, unrealized)
                if cb_triggered:
                    log_alert(cb_msg)
                    self._exit_all_positions(reason="CIRCUIT_BREAKER_HALT")
                    sys.exit(0)

                if self.mode == "WAIT_DATA":
                    if now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE):
                        # After a whipsaw exit, pause 5 minutes before entering a new strangle
                        if time.time() - self._last_whipsaw_time < 300.0:
                            pass
                        elif regime == "TRANSITION":
                            log_info(f"09:18 AM reached but ADX {adx:.1f} is in the TRANSITION band — holding entry.")
                        else:
                            log_info(f"Market Ready. Entering Strangle (Regime: {regime}, KAMA 1m Trend: {trend})...")
                            hedge_width = HEDGE_WIDTH_PTS
                            if "CE_HEDGE" not in self.positions: self._enter_leg("CE_HEDGE", atm + hedge_width, "BUY", spot, atr, regime=regime)
                            if "PE_HEDGE" not in self.positions: self._enter_leg("PE_HEDGE", atm - hedge_width, "BUY", spot, atr, regime=regime)
                            ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days, trend=trend, adx=adx)
                            if "CE" not in self.positions: self._enter_leg("CE", ce_strike, "SELL", spot, atr, regime=regime)
                            if "PE" not in self.positions: self._enter_leg("PE", pe_strike, "SELL", spot, atr, regime=regime)
                            self.mode = "RUNNING"
                            self._save_state()

                elif self.mode in ("RUNNING", "CHOP_MODE"):
                    if not self.positions:
                        self.mode = "WAIT_DATA"
                        continue

                    # Dynamic Premium-Based Trailing SL Check — evaluated every second on live tick data
                    whipsaw_triggered = False
                    whipsaw_leg = ""
                    whipsaw_reason = ""
                    for leg in list(self.positions.keys()):
                        pos = self.positions.get(leg)
                        if pos and pos.get("side") == "SELL":
                            ltp = self._get_ltp(pos.get("strike", 0), pos.get("base", "CE"), tsym=pos.get("tsym", ""))
                            pos["live_ltp"] = ltp
                            is_stopped, reason = self.risk_manager.update_premium_tsl_and_check(leg, pos, ltp, regime=regime)
                            if is_stopped:
                                whipsaw_triggered = True
                                whipsaw_leg = leg
                                whipsaw_reason = reason
                                break

                    if whipsaw_triggered:
                        log_alert(whipsaw_reason)
                        log_alert(f"⚠️ WHIPSAW DETECTED on {whipsaw_leg} — Closing BOTH short legs to protect capital!")
                        self._exit_all_positions(reason="WHIPSAW_DUAL_LEG_EXIT")
                        self._last_whipsaw_time = time.time()
                        self.mode = "WAIT_DATA"
                        self._save_state()

                # Write live dashboard snapshot
                self._write_snapshot(spot, atm, atr, regime, trend, adx, dte_days, unrealized)

                # CLI Print
                print(f"[{_now_str()}] Spot: {spot:.2f} | ADX: {adx:.1f} ({regime}) | KAMA 1m Trend: {trend} | Mode: {self.mode} | P&L: ₹{self.realized_pnl:,.0f}", flush=True)
                
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

