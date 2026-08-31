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

    def place_option_order(self, symbol, transaction_type, quantity, price):
        action = transaction_type[0] # 'B' or 'S'
        res = self.api.place_order(
            buy_or_sell=str(action),
            product_type="M",
            exchange="NFO",
            tradingsymbol=str(symbol),
            quantity=str(quantity),
            discloseqty="0",
            price_type="MKT",
            price="0",
            trigger_price=None,
            retention="DAY"
        )
        return res

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
        if df_5m.empty or len(df_5m) < 5:
            return {'kama': None, 'prev_kama': None, 'trend': 0, 'atr': DEFAULT_ATR_5M, 'adx': 18.0, 'plus_di': 20.0, 'minus_di': 20.0, 'regime': 'CHOP'}
            
        highs = df_5m['high'].to_numpy(dtype=float)
        lows = df_5m['low'].to_numpy(dtype=float)
        closes = df_5m['close'].to_numpy(dtype=float)
        
        atr = cls.calculate_atr(highs, lows, closes)
        adx, p_di, m_di = cls.calculate_adx(highs, lows, closes)
        
        # KAMA now computed on the SAME 5m closes as ADX/ATR (was 1m before).
        # It now actively skews live strike selection in TREND regime, so it
        # needs to move on the same clock as the regime call, not a noisier one.
        if len(closes) < KAMA_PERIOD + 1:
            kama, prev_kama, trend = None, None, 0
        else:
            kama, prev_kama, trend = cls.calculate_kama(closes)
        
        # ADX_CHOP_THRESHOLD < ADX_TREND_THRESHOLD creates a real neutral band
        # (previously both were 20.0, so this middle branch never fired).
        if adx < ADX_CHOP_THRESHOLD: regime = "CHOP"
        elif adx >= ADX_TREND_THRESHOLD: regime = "TREND"
        else: regime = "TRANSITION"
        return {'kama': kama, 'prev_kama': prev_kama, 'trend': trend, 'atr': atr, 'adx': adx, 'plus_di': p_di, 'minus_di': m_di, 'regime': regime}


# ==============================================================================
# MODULE 3: RISK MANAGER
# ==============================================================================

class RiskManager:
    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1.0 * (capital * PORTFOLIO_CIRCUIT_PCT / 100.0)

    def init_spot_sl(self, leg: str, entry_spot: float, atr: float):
        initial_distance = max(12.0, SPOT_SL_ATR_MULT * atr)
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
        deep_lock_at = favorable_lock_at * 1.6
        sl_buffer = max(1.5, safe_atr * 0.08)
        
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
            is_breaching = (current_spot >= (sl_state["current_sl"] - sl_buffer))
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
            is_breaching = (current_spot <= (sl_state["current_sl"] + sl_buffer))

        if is_new_1m_bar:
            if is_breaching:
                breach_count += 1
                sl_state["breach_count"] = breach_count
            else:
                sl_state["breach_count"] = 0
        else:
            if is_breaching and breach_count == 0:
                breach_count = 1
                sl_state["breach_count"] = 1
            elif not is_breaching and breach_count == 1:
                sl_state["breach_count"] = 0

        if sl_state["breach_count"] >= SPOT_SL_DEBOUNCE_BARS:
            return True, f"⛔ Spot SL Triggered for {leg} | Spot: {current_spot:.2f} breached SL: {sl_state['current_sl']:.2f}"
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
        self._last_chop_roll_time: float = 0.0       # timestamp of last CHOP roll check
        self._last_trend_roll_bar: Optional[str] = None  # 1m key of last TREND roll
        self._processed_1m_bar: Optional[str] = None    # ensures is_new_1m_bar is one-shot per bar
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
        skew = 0.0
        if regime == "CHOP":
            min_w, max_w, mult = CHOP_MIN_WIDTH_PTS, CHOP_MAX_WIDTH_PTS, CHOP_ATR_MULTIPLIER
            # Continuous choppiness score: further below the CHOP threshold ADX
            # sits, the choppier the tape, the wider we sell (was previously a
            # flat multiplier that got zeroed out by the dead width config).
            chop_score = float(np.clip((ADX_CHOP_THRESHOLD - adx) / max(1e-6, ADX_CHOP_THRESHOLD), 0.0, 1.0))
            score_width = min_w + (max_w - min_w) * chop_score
            calculated_width = max(min_w, min(max_w, max(score_width, mult * atr)))
        else:
            min_w, max_w, mult = BASE_MIN_WIDTH_PTS, BASE_MAX_WIDTH_PTS, BASE_ATR_MULTIPLIER
            calculated_width = max(min_w, min(max_w, mult * atr))
            if regime == "TREND" and trend != 0:
                # KAMA-driven trend follow: shift the whole strangle center in
                # the trend direction. The side price is pushing toward gets
                # more room (protection); the side it's moving away from gets
                # pulled in (more premium, since breach risk there is falling).
                skew_score = float(np.clip(adx / TREND_SKEW_ADX_REF, 0.0, 1.0))
                skew = trend * TREND_SKEW_MAX_PTS * skew_score

        if float(dte_days) <= 1.0: return atm_spot, atm_spot
        expiry_curve = self._expiry_width_multiplier(dte_days)
        expiry_floor = 50 if dte_days <= EXPIRY_NEAR_DAYS else min_w
        compressed_width = max(expiry_floor, round(max(min_w, calculated_width) * (1.0 - 0.65 * expiry_curve) + min_w * (0.65 * expiry_curve)))
        stride_50 = int(round(compressed_width / 50.0) * 50)
        final_width = max(expiry_floor, min(max_w, stride_50))
        skew_stride = int(round(skew / 50.0) * 50)

        ce_strike = atm_spot + final_width + skew_stride
        pe_strike = atm_spot - final_width + skew_stride
        if ce_strike <= atm_spot or pe_strike >= atm_spot or ce_strike == pe_strike:
            ce_strike, pe_strike = atm_spot + 50, atm_spot - 50
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
                if saved_mode in ("SESSION_DONE", "SESSION_DONE_FLAT") and not market_closed:
                    self.mode = "RUNNING" if self.positions else "WAIT_DATA"
                else: self.mode = saved_mode
                for leg, pos in self.positions.items():
                    if pos.get("side") == "SELL" and leg in ("CE", "PE"):
                        if "spot_sl_state" not in pos or not pos["spot_sl_state"]:
                            pos["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, float(pos.get("entry_spot", self.market_data.latest_spot)), float(self.current_indicators.get("atr", DEFAULT_ATR_5M)))
        except: pass

    def _sync_positions_from_broker(self):
        """
        Query Flattrade live positions and reconcile with self.positions.
        Called once on startup to prevent duplicate orders after a restart.

        - Legs in broker but NOT in self.positions  → import them (bot will track, not re-enter)
        - Legs in self.positions but net qty=0 in broker → remove (ghost, already closed externally)
        - Legs in both → keep self.positions data (has SL state, entry_spot, etc.)
        """
        import re as _re
        log_info("Syncing with Flattrade live positions...")
        try:
            res = self.broker.api.get_positions()
            if not res or not isinstance(res, list):
                log_warn("Broker sync: empty response — using state file only.")
                return

            # Build broker map: tsym -> {netqty, avgprc}
            broker_map: Dict[str, Dict] = {}
            for p in res:
                if p.get('exchange') != 'NFO': continue
                tsym   = p.get('tsym', '')
                netqty = int(p.get('netqty', 0) or 0)
                avgprc = float(p.get('netavgprc', p.get('avgprc', 0.0)) or 0.0)
                if tsym:
                    broker_map[tsym] = {'netqty': netqty, 'avgprc': avgprc}

            log_info(f"Broker NFO positions: {list(broker_map.keys())}")

            atr  = self.current_indicators.get('atr', DEFAULT_ATR_5M)
            spot = self.market_data.latest_spot

            # Step 1: Remove ghost positions (closed in broker but still in state)
            for leg in list(self.positions.keys()):
                tsym   = self.positions[leg].get('tsym', '')
                bqty   = broker_map.get(tsym, {}).get('netqty', None)
                if bqty is None or bqty == 0:
                    log_warn(f"Removing ghost: {leg} ({tsym}) — not open in broker.")
                    del self.positions[leg]

            # Step 2: Import positions open in broker but missing from state
            known_tsyms = {p.get('tsym') for p in self.positions.values()}
            for tsym, bdata in broker_map.items():
                netqty = bdata['netqty']
                if netqty == 0 or tsym in known_tsyms: continue

                side   = "SELL" if netqty < 0 else "BUY"
                qty    = abs(netqty)
                avgprc = bdata['avgprc']
                base   = 'CE' if 'CE' in tsym else ('PE' if 'PE' in tsym else 'XX')

                # Determine leg label
                if base == 'CE':
                    leg = 'CE' if 'CE' not in self.positions else ('CE_HEDGE' if 'CE_HEDGE' not in self.positions else f'CE_{tsym[-4:]}')
                elif base == 'PE':
                    leg = 'PE' if 'PE' not in self.positions else ('PE_HEDGE' if 'PE_HEDGE' not in self.positions else f'PE_{tsym[-4:]}')
                else:
                    leg = tsym[:8]

                # Try to parse strike
                strike = 0
                m = _re.search(r'(\d{4,6})(CE|PE)$', tsym)
                if m: strike = int(m.group(1))

                pos_info = {
                    "strike": strike, "tsym": tsym, "base": base,
                    "side": side, "qty": qty, "entry_price": avgprc,
                    "entry_time": time.time(), "entry_spot": spot
                }
                if side == "SELL":
                    pos_info["spot_sl_state"] = self.risk_manager.init_spot_sl(base, spot, atr)

                self.positions[leg] = pos_info
                log_info(f"Imported from broker: {leg} — {side} {qty}x {tsym} @ ₹{avgprc:.2f}")

            # Step 3: Fix mode based on reconciled positions
            if self.positions:
                if self.mode in ("WAIT_DATA", "SESSION_DONE", "SESSION_DONE_FLAT"):
                    self.mode = "RUNNING"
                    log_info(f"Mode set to RUNNING ({len(self.positions)} legs found).")
            else:
                if self.mode not in ("COOLDOWN",):
                    self.mode = "WAIT_DATA"
                    log_info("No open positions — mode set to WAIT_DATA.")

            self._save_state()
            log_info(f"Sync done. Tracking {len(self.positions)} legs: {list(self.positions.keys())}")

        except Exception as e:
            log_warn(f"Position sync error (non-fatal, continuing): {e}")
            traceback.print_exc()

    def _get_ltp(self, strike: int, option_type: str) -> float:
        key = f"{strike}_{option_type}"
        if key not in self._ltp_cache:
            q = self.market_data.streamer.get_live_quote(strike, option_type)
            self._ltp_cache[key] = float(q.get("lp", 0.0))
        return self._ltp_cache[key]

    def _enter_leg(self, leg: str, strike: int, side: str, spot: float, atr: float):
        base = leg.split("_")[0]
        q = self.market_data.streamer.get_live_quote(strike, base)
        tsym = q.get("tsym", f"NIFTY_{strike}_{base}")
        self._ltp_cache[f"{strike}_{base}"] = q.get("lp", 0.0)
        ltp = self._get_ltp(strike, base)
        
        self.broker.place_option_order(symbol=tsym, transaction_type=side, quantity=self.qty, price=ltp)
        pos_info = {"strike": strike, "tsym": tsym, "base": base, "side": side, "qty": self.qty, "entry_price": ltp, "entry_time": time.time(), "entry_spot": spot}
        if side == "SELL": pos_info["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, spot, atr)
        self.positions[leg] = pos_info
        log_trade(f"{side} {leg:10s} Strike: {strike} @ ₹{ltp:.2f} (Spot: {spot:.2f})")
        self._save_state()

    def _exit_leg(self, leg: str, reason: str = "MANUAL") -> float:
        if leg not in self.positions: return 0.0
        pos = self.positions[leg]
        base = pos["base"]
        ltp = self._get_ltp(pos["strike"], base)
        close_side = "BUY" if pos["side"] == "SELL" else "SELL"
        self.broker.place_option_order(symbol=pos["tsym"], transaction_type=close_side, quantity=pos["qty"], price=ltp)
        pnl = (pos["entry_price"] - ltp) * pos["qty"] if pos["side"] == "SELL" else (ltp - pos["entry_price"]) * pos["qty"]
        self.realized_pnl += pnl
        log_trade(f"EXITED {leg:10s} Strike: {pos['strike']} @ ₹{ltp:.2f} | P&L: ₹{pnl:,.2f} (Reason: {reason})")
        del self.positions[leg]
        self._save_state()
        return pnl

    def _exit_all_positions(self, reason: str = "GLOBAL_EXIT"):
        for leg in list(self.positions.keys()): self._exit_leg(leg, reason=reason)

    def _trigger_leg_cooldown(self, stopped_leg: str, current_spot: float):
        self.cooldown_tracker[stopped_leg] = {'stopped_time': time.time(), 'stopped_spot': current_spot, 'active': True}
        log_alert(f"⏳ {stopped_leg} entered Adaptive COOLDOWN.")
        self.mode = "COOLDOWN"
        self._save_state()

    def _check_cooldown_and_reenter(self, spot: float, atm: int, atr: float, regime: str, trend: int, dte_days: float = 2.0):
        for leg in ("CE", "PE"):
            cd = self.cooldown_tracker.get(leg)
            if not cd or not cd.get("active", False): continue
            elapsed_sec = time.time() - cd["stopped_time"]
            spot_displacement = abs(spot - cd["stopped_spot"]) / max(0.1, cd["stopped_spot"])
            
            indicator_cleared = False
            if leg == "CE" and trend == -1: indicator_cleared = True
            elif leg == "PE" and trend == 1: indicator_cleared = True
            elif regime == "CHOP" and elapsed_sec >= 60.0: indicator_cleared = True
            elif spot_displacement >= COOLDOWN_SPOT_PCT: indicator_cleared = True
            elif elapsed_sec >= COOLDOWN_MINUTES * 60: indicator_cleared = True
            
            if indicator_cleared:
                trend_safe = True
                if leg == "CE" and regime == "TREND" and trend == 1: trend_safe = False
                elif leg == "PE" and regime == "TREND" and trend == -1: trend_safe = False
                if trend_safe:
                    log_info(f"✅ Cooldown Bypassed for {leg} -> Re-shorting...")
                    ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days, trend=trend, adx=self.current_indicators.get('adx', 18.0))
                    if leg not in self.positions: self._enter_leg(leg, ce_strike if leg == "CE" else pe_strike, "SELL", spot, atr)
                    cd["active"] = False
                    self._save_state()
                    if not any(v.get("active", False) for v in self.cooldown_tracker.values()):
                        self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"

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
                
                unrealized = sum([((p["entry_price"] - self._get_ltp(p["strike"], p["base"])) if p["side"] == "SELL" else (self._get_ltp(p["strike"], p["base"]) - p["entry_price"])) * p["qty"] for p in self.positions.values()])
                cb_triggered, cb_msg = self.risk_manager.check_portfolio_circuit_breaker(self.realized_pnl, unrealized)
                if cb_triggered:
                    log_alert(cb_msg)
                    self._exit_all_positions(reason="CIRCUIT_BREAKER_HALT")
                    sys.exit(0)

                if self.mode == "WAIT_DATA":
                    if now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE):
                        if regime == "TRANSITION":
                            log_info(f"09:18 AM reached but ADX {adx:.1f} is in the TRANSITION band — holding entry.")
                        else:
                            log_info(f"09:18 AM Reached. Entering Market (Regime: {regime})...")
                            hedge_width = HEDGE_WIDTH_PTS
                            if "CE_HEDGE" not in self.positions: self._enter_leg("CE_HEDGE", atm + hedge_width, "BUY", spot, atr)
                            if "PE_HEDGE" not in self.positions: self._enter_leg("PE_HEDGE", atm - hedge_width, "BUY", spot, atr)
                            ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days, trend=trend, adx=adx)
                            if "CE" not in self.positions: self._enter_leg("CE", ce_strike, "SELL", spot, atr)
                            if "PE" not in self.positions: self._enter_leg("PE", pe_strike, "SELL", spot, atr)
                            self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"
                            self._save_state()

                elif self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN"):
                    if not self.positions:
                        self.mode = "WAIT_DATA"
                        continue

                    # Spot-Based SL Check — always runs, even in TRANSITION.
                    for leg in ("CE", "PE"):
                        if leg in self.positions and self.positions[leg]["side"] == "SELL":
                            is_stopped, reason = self.risk_manager.update_spot_sl_and_check(leg, self.positions[leg], spot, is_new_1m_bar)
                            if is_stopped:
                                log_alert(reason)
                                self._exit_leg(leg, reason="SPOT_TSL_HIT")
                                self._trigger_leg_cooldown(leg, spot)

                    if regime == "TRANSITION":
                        # Dead zone: SL check stays active but no rolls or re-entries.
                        pass
                    else:
                        self._check_cooldown_and_reenter(spot, atm, atr, regime, trend, dte_days=dte_days)

                        # Fix: If cooldown cleared and both legs present, restore mode.
                        if self.mode == "COOLDOWN":
                            if not any(v.get("active", False) for v in self.cooldown_tracker.values()):
                                self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"
                                log_info(f"✅ All cooldowns cleared. Resuming mode: {self.mode}")

                        if regime == "CHOP":
                            # CHOP rolls: debounced to once per CHOP_ROLL_DEBOUNCE_MIN minutes
                            now_ts = time.time()
                            if now_ts - self._last_chop_roll_time >= CHOP_ROLL_DEBOUNCE_MIN * 60:
                                chop_ce, chop_pe = self.calculate_strangle_strikes(atm, atr, "CHOP", dte_days=dte_days, adx=adx)
                                rolled = False
                                if "CE" in self.positions and self.positions["CE"]["strike"] < chop_ce:
                                    log_info(f"Choppiness rising. Rolling CE from {self.positions['CE']['strike']} to {chop_ce}...")
                                    self._exit_leg("CE", reason="CHOP_ROLL_OUT")
                                    self._enter_leg("CE", chop_ce, "SELL", spot, atr)
                                    rolled = True
                                if "PE" in self.positions and self.positions["PE"]["strike"] > chop_pe:
                                    log_info(f"Choppiness rising. Rolling PE from {self.positions['PE']['strike']} to {chop_pe}...")
                                    self._exit_leg("PE", reason="CHOP_ROLL_OUT")
                                    self._enter_leg("PE", chop_pe, "SELL", spot, atr)
                                    rolled = True
                                if rolled:
                                    self._last_chop_roll_time = now_ts

                        elif regime == "TREND" and is_new_1m_bar:
                            # TREND rolls: only fire once per new 1m bar AND only if
                            # the strike has drifted more than TREND_ROLL_MIN_ATR_MULT * ATR.
                            min_drift = max(50, int(TREND_ROLL_MIN_ATR_MULT * atr))
                            trend_ce, trend_pe = self.calculate_strangle_strikes(atm, atr, "TREND", dte_days=dte_days, trend=trend, adx=adx)
                            if "CE" in self.positions and abs(self.positions["CE"]["strike"] - trend_ce) >= min_drift:
                                log_info(f"KAMA trend-follow. Rolling CE from {self.positions['CE']['strike']} to {trend_ce} (drift >= {min_drift}pts)...")
                                self._exit_leg("CE", reason="TREND_FOLLOW_ROLL")
                                self._enter_leg("CE", trend_ce, "SELL", spot, atr)
                            if "PE" in self.positions and abs(self.positions["PE"]["strike"] - trend_pe) >= min_drift:
                                log_info(f"KAMA trend-follow. Rolling PE from {self.positions['PE']['strike']} to {trend_pe} (drift >= {min_drift}pts)...")
                                self._exit_leg("PE", reason="TREND_FOLLOW_ROLL")
                                self._enter_leg("PE", trend_pe, "SELL", spot, atr)

                # CLI Print
                print(f"[{_now_str()}] Spot: {spot:.2f} | ADX: {adx:.1f} ({regime}) | KAMA Trend: {trend} | Mode: {self.mode} | P&L: ₹{self.realized_pnl:,.0f}", flush=True)
                
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

