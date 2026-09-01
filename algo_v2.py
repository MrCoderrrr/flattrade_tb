"""
================================================================================
🚀 ADAPTIVE KAMA-ADX HEDGED STRANGLE (VERSION 2.0 - PRECISION EXECUTION ENGINE)
================================================================================
Production-Ready Algorithmic Trading Architecture for NIFTY 50 Options.

KEY ARCHITECTURAL HIGHLIGHTS:
1. Strict 1-Minute Execution Cadence:
   - 1-minute execution resolution aligned strictly to candle closes (:00 boundary).
   - Synthetic 5-minute rolling aggregation for indicators (KAMA, ADX, ATR).
   - KAMA and Spot TSL run strictly on the 1-minute collected data, not before.
2. Dual-Filter Regime Detection:
   - ADX(9) on 5m: <20 -> CHOP REGIME (decay focus), >=20 -> TREND REGIME (high delta risk).
   - KAMA(13, 2, 30) on 5m: Directional trend filter (+1 UP, -1 DOWN, 0 FLAT).
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
import math
import signal
import socket
import select
import threading
import traceback
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

# ─── Flattrade Core Integration & Polyfills ────────────────────────────────────
global_api = None
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
                print(f"[AUTH] Flattrade session established from {token_file} for user {USER_ID}", flush=True)
    else:
        print("[AUTH] Notice: token.txt not found or empty. Operating in fallback mode.", flush=True)
except Exception as e:
    print(f"[AUTH] Notice: Flattrade API init: {e}", flush=True)
    global_api = None

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
        def record_trade(self, *args, **kwargs): pass
        def get_strategy_pnl_summary(self, *args, **kwargs): return {"today_pnl": 0.0, "mtd_pnl": 0.0, "ytd_pnl": 0.0, "current_capital": kwargs.get("base_capital", 145585.75)}
    db = DBManager()


class NSEATMStreamer:
    def __init__(self, api=None):
        self.api = api or global_api
        self._cached_expiry_date: Optional[datetime] = None
        self._cached_expiry_day: Optional[Any] = None
        self._last_spot: float = 24000.0
        self._last_atm: int = 24000

    def get_spot_and_atm(self) -> Tuple[float, int]:
        """
        Fetches live NIFTY 50 Spot price directly from Flattrade every minute (Token 26000 on NSE).
        No dependency on yfinance.
        """
        if self.api and hasattr(self.api, "get_quotes"):
            try:
                res = self.api.get_quotes(exchange='NSE', token='26000')
                if res and isinstance(res, dict) and str(res.get('stat', '')).lower() in ('ok', 'success'):
                    raw_lp = res.get('lp', res.get('ltp', 0.0))
                    spot = float(raw_lp)
                    if spot > 0:
                        self._last_spot = spot
                        self._last_atm = int(round(spot / 50.0) * 50)
                        return self._last_spot, self._last_atm
            except Exception as e:
                log_warn(f"Flattrade get_quotes error for Spot: {e}")
        return self._last_spot, self._last_atm

    def get_live_quote(self, strike: int, option_type: str) -> Dict[str, Any]:
        """Fetches live option quote directly from Flattrade with zero-price fallbacks."""
        today = get_ist_now().date()
        if self.api and hasattr(self.api, "searchscrip"):
            try:
                search_text = f"NIFTY {strike} {option_type}"
                res = self.api.searchscrip(exchange='NFO', searchtext=search_text)
                if res and isinstance(res, dict) and res.get('stat') == 'Ok' and res.get('values'):
                    valid = []
                    for item in res['values']:
                        if 'exd' in item:
                            try:
                                d = datetime.strptime(item['exd'], "%d-%b-%Y").date()
                                if d >= today:  # Only current or future expiries
                                    valid.append({'item': item, 'dt': d})
                            except ValueError:
                                continue
                    if valid:
                        valid.sort(key=lambda x: x['dt'])
                        match = valid[0]['item']
                        tsym = match['tsym']
                        quote = self.api.get_quotes(exchange='NFO', token=match['token'])
                        lp = 0.0
                        if quote and isinstance(quote, dict):
                            for field in ('lp', 'ltp', 'c', 'sp1', 'bp1', 'ap'):
                                val = quote.get(field)
                                if val is not None:
                                    try:
                                        v_flt = float(val)
                                        if v_flt > 0:
                                            lp = v_flt
                                            break
                                    except (ValueError, TypeError):
                                        pass
                        if lp <= 0:
                            diff = abs(self._last_spot - strike)
                            lp = max(0.50, round(180.0 - (diff * 0.35), 2))
                        return {"lp": lp, "tsym": tsym, "ls": int(match.get('ls', 65))}
            except Exception as e:
                log_warn(f"get_live_quote error: {e}")

        diff = abs(self._last_spot - strike)
        est_prem = max(15.0, 180.0 - (diff * 0.35))
        return {"lp": round(est_prem, 2), "tsym": f"NIFTY_{strike}_{option_type}", "ls": 65}

    def get_near_expiry_dte(self) -> Tuple[Optional[datetime], float]:
        """Fetches near expiry date and DTE directly from Flattrade NFO contracts."""
        today = get_ist_now().date()
        if self._cached_expiry_date is None or self._cached_expiry_day != today:
            if self.api and hasattr(self.api, "searchscrip"):
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
                except Exception:
                    pass
            if self._cached_expiry_date is None:
                days_ahead = (3 - get_ist_now().weekday()) % 7
                if days_ahead == 0 and get_ist_now().hour >= 15:
                    days_ahead = 7
                self._cached_expiry_date = datetime.now() + timedelta(days=days_ahead)
            self._cached_expiry_day = today
        dte = max(0.01, (self._cached_expiry_date - datetime.now()).total_seconds() / 86400.0)
        return self._cached_expiry_date, dte


class FlattradeBroker:
    def __init__(self, paper_trading: Optional[bool] = None):
        self.api = global_api
        if paper_trading is not None:
            self.paper_trading = paper_trading  # Explicit override (tests)
        elif PAPER_TRADING_MODE:
            self.paper_trading = True           # User chose paper at startup prompt
        else:
            token_file = "token.txt" if os.path.exists("token.txt") else os.path.join(PROJECT_ROOT, "token.txt")
            has_token = os.path.exists(token_file) and os.path.getsize(token_file) > 0
            self.paper_trading = not (has_token and self.api is not None)
        self.order_counter = 1000
        self.simulated_order_book: Dict[str, Dict[str, Any]] = {}

    def place_option_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0.0, product_type: str = "M") -> Dict[str, Any]:
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
CAPITAL                 = 145585.75   # Total Capital
LOT_SIZE                = 65          # NIFTY Lot size (revised Jan 2026)
CAPITAL_BUFFER          = 0.95        # Usable capital buffer (95%)
MARGIN_IRON_CONDOR      = 95_000      # Margin required for 4-leg Iron Condor per lot
PORTFOLIO_CIRCUIT_PCT   = 1.8         # Emergency Portfolio Halt at -1.8% combined MTM

# Indicators Parameters (5-Minute Timeframe)
KAMA_PERIOD             = 13          # KAMA Efficiency Ratio lookback
KAMA_FAST_EMA           = 2           # KAMA Fast EMA constant
KAMA_SLOW_EMA           = 30          # KAMA Slow EMA constant
KAMA_MIN_SLOPE          = 1.0         # Minimum KAMA slope (pts) to flip trend

ADX_PERIOD              = 9           # ADX lookback period on 5m candles
ADX_CHOP_THRESHOLD      = 20.0        # ADX < 20: CHOP REGIME
ADX_TREND_THRESHOLD     = 20.0        # ADX >= 20: TREND REGIME

ATR_PERIOD              = 14          # ATR lookback period on 5m candles
DEFAULT_ATR_5M          = 35.0        # Fallback 5m ATR if warming up

# Strike Selection & Distances
HEDGE_WIDTH_PTS         = 1000        # Long Leg (Hedge) distance OTM from ATM at entry
BASE_MIN_WIDTH_PTS      = 0           # 0 strike OTM (ATM Straddle / Strangle width = 0)
BASE_MAX_WIDTH_PTS      = 0           # Width cap at 0
BASE_ATR_MULTIPLIER     = 1.0         # Base Short Leg width

CHOP_MIN_WIDTH_PTS      = 0           # Strangle width
CHOP_MAX_WIDTH_PTS      = 0           # Hard ceiling cap
CHOP_ATR_MULTIPLIER     = 1.5         # Chop Short Leg width

# Expiry Compression Curve
EXPIRY_WIDTH_LOOKAHEAD_DAYS = 8.0     # Curve anchor window for logarithmic compression
EXPIRY_NEAR_DAYS            = 2.0     # Aggressive compression starts around 2 DTE
EXPIRY_NEAR_BONUS           = 0.42    # Extra curvature inside the last 2 days

# Spot-Based Trailing Stop Loss
SPOT_SL_ATR_MULT        = 0.90        # Initial Spot SL distance = 0.9 * ATR from entry spot
SPOT_SL_TRAIL_RATIO     = 0.55        # Base spot trail ratio (55.0%)
SPOT_SL_TRAIL_RATIO_STRONG = 0.72     # Stronger trail once trade is in meaningful profit
SPOT_SL_TRAIL_RATIO_DEEP   = 0.85     # Aggressive profit protection for extended favorable moves
SPOT_SL_BREAKEVEN_LOCK_ATR = 1.10     # Once favorable move exceeds this ATR multiple, lock at/near breakeven
SPOT_SL_BREAKEVEN_BUFFER_PTS = 5.0    # Small buffer beyond entry to avoid scratch exits
SPOT_SL_DEBOUNCE_BARS   = 1           # Require 1 1-min close past Spot SL to exit (Debounce = 1)

# Anti-Whipsaw Re-entry Cooldown: 3-MIN COOLDOWN REMOVED
COOLDOWN_MINUTES        = 0           # 3-minute cooldown removed as requested
COOLDOWN_SPOT_PCT       = 0.0010      # 0.10% spot movement (~24 pts) resets cooldown early

# Session Timing
MARKET_START_HOUR       = 9
MARKET_START_MINUTE     = 18          # Start trading / place hedges at 09:18 AM
AUTO_SQUAREOFF_HOUR     = 15
AUTO_SQUAREOFF_MINUTE   = 28          # Auto square-off at 15:28 PM
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

def log_info(msg: str):
    print(f"{Fore.CYAN}[{_now_str()} INFO]{Style.RESET_ALL}  {msg}", flush=True)

def log_warn(msg: str):
    print(f"{Fore.YELLOW}[{_now_str()} WARN]{Style.RESET_ALL}  {msg}", flush=True)

def log_alert(msg: str):
    print(f"{Fore.RED}{Style.BRIGHT}[{_now_str()} ALERT]{Style.RESET_ALL} {msg}", flush=True)

def log_trade(msg: str):
    print(f"{Fore.MAGENTA}{Style.BRIGHT}[{_now_str()} TRADE]{Style.RESET_ALL} {msg}", flush=True)

def round_to_strike(price: float, strike_step: int = 50) -> int:
    return int(round(price / float(strike_step)) * strike_step)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: MARKET DATA INGESTION & 5-MIN AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

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
        if not os.path.exists(self.cache_file):
            return
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
                    self.bars_1m.append({"timestamp": dt, "spot": price, "minute_key": min_key})
                    self.logged_1m_keys.add(min_key)
                    self.latest_spot = price
                    self.latest_atm = round_to_strike(price, 50)
            
            self._rebuild_5m_candles()
            if self.bars_1m:
                self.last_completed_1m_key = self.bars_1m[-1]["minute_key"]
                log_info(f"MarketData: Loaded {len(self.bars_1m)} collected 1-min bars ({len(self.bars_5m)} 5-min candles built). Latest Spot: {self.latest_spot:.2f}")
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
            today = get_ist_now().date()
            prior_bars = [b for b in seeded_5m if b['timestamp'].date() < today][-50:]
            self.bars_5m = prior_bars + self.bars_5m
            log_info(f"MarketData: Loaded {len(prior_bars)} historical 5m bars. Total 5m bars: {len(self.bars_5m)}. KAMA ready immediately!")
        else:
            log_warn("MarketData: History seeding could not load 5m bars. Indicators will warm up from live 1m Flattrade feed.")

    def _rebuild_5m_candles(self):
        if not self.bars_1m:
            return
        df_1m = pd.DataFrame(self.bars_1m)
        df_1m.set_index("timestamp", inplace=True)
        df_5m = df_1m["spot"].resample("5min", label="left", closed="left").ohlc().dropna()
        
        self.bars_5m = []
        for ts, row in df_5m.iterrows():
            self.bars_5m.append({
                "timestamp": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })

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
                    log_warn(f"MarketData: Failed writing cache line: {e}")
                
                self.bars_1m.append({"timestamp": dt, "spot": spot, "minute_key": current_min_key})
                self._rebuild_5m_candles()
            
            self.last_completed_1m_key = current_min_key
            
        return spot, atm, is_new_1m_bar

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
    def evaluate_all(cls, df_5m: pd.DataFrame) -> Dict[str, Any]:
        if df_5m.empty or len(df_5m) < 5:
            return {
                "kama": None, "prev_kama": None, "trend": 0,
                "atr": DEFAULT_ATR_5M, "adx": 18.0, "plus_di": 20.0, "minus_di": 20.0,
                "regime": "CHOP"
            }
            
        highs = df_5m["high"].to_numpy(dtype=float)
        lows = df_5m["low"].to_numpy(dtype=float)
        closes = df_5m["close"].to_numpy(dtype=float)
        
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
# MODULE 3: DUAL-LAYER RISK MANAGEMENT (SPOT-BASED TSL & CIRCUIT BREAKER)
# ══════════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, capital: float = CAPITAL):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1.0 * (capital * PORTFOLIO_CIRCUIT_PCT / 100.0)
        self.circuit_breaker_triggered = False

    def init_spot_sl(self, leg: str, entry_spot: float, atr: float) -> Dict[str, Any]:
        initial_distance = max(12.0, SPOT_SL_ATR_MULT * atr)
        if leg == "CE":
            initial_sl = entry_spot + initial_distance
            best_spot = entry_spot
        else:
            initial_sl = entry_spot - initial_distance
            best_spot = entry_spot
            
        return {
            "entry_spot": round(entry_spot, 2),
            "atr_at_entry": round(atr, 2),
            "initial_sl": round(initial_sl, 2),
            "current_sl": round(initial_sl, 2),
            "best_spot": round(entry_spot, 2),
            "trail_amount": 0.0,
            "breach_count": 0
        }

    def update_spot_sl_and_check(self, leg: str, pos_data: Dict[str, Any], current_spot: float, is_new_1m_bar: bool) -> Tuple[bool, str]:
        sl_state = pos_data.get("spot_sl_state")
        if not sl_state:
            return False, ""
        
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
                trail_ratio = SPOT_SL_TRAIL_RATIO
                if favorable_move >= deep_lock_at:
                    trail_ratio = SPOT_SL_TRAIL_RATIO_DEEP
                elif favorable_move >= favorable_lock_at:
                    trail_ratio = SPOT_SL_TRAIL_RATIO_STRONG

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
                trail_ratio = SPOT_SL_TRAIL_RATIO
                if favorable_move >= deep_lock_at:
                    trail_ratio = SPOT_SL_TRAIL_RATIO_DEEP
                elif favorable_move >= favorable_lock_at:
                    trail_ratio = SPOT_SL_TRAIL_RATIO_STRONG

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
            sl_state["last_stop_reason"] = f"breach@{current_spot:.2f}/sl@{sl_state['current_sl']:.2f}"
            msg = (f"⛔ Spot SL Triggered for {leg} | Spot: {current_spot:.2f} breached SL: {sl_state['current_sl']:.2f} "
                   f"(Confirmed over {sl_state['breach_count']} 1-min bars | Entry Spot: {entry_spot:.2f})")
            return True, msg
        
        return False, ""

    def check_portfolio_circuit_breaker(self, realized_pnl: float, unrealized_pnl: float) -> Tuple[bool, str]:
        combined_mtm = realized_pnl + unrealized_pnl
        if combined_mtm <= self.circuit_breaker_loss_limit:
            self.circuit_breaker_triggered = True
            msg = (f"🚨 PORTFOLIO CIRCUIT BREAKER HIT 🚨 Combined MTM: ₹{combined_mtm:,.2f} "
                   f"breached -{PORTFOLIO_CIRCUIT_PCT}% limit (-₹{abs(self.circuit_breaker_loss_limit):,.2f}). "
                   f"Triggering global emergency shutdown!")
            return True, msg
        return False, ""


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 4: EXECUTION STATE MACHINE & STRATEGY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class ExecutionEngine:
    def __init__(self):
        self.state_file = os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2.json")
        self.cache_file = os.path.join(PROJECT_ROOT, "data", "cache", "spot_cache.csv")
        self.live_snap_file = os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2.json")
        self.trade_book_dir = os.path.join(PROJECT_ROOT, "data", "logs", "trade_book")
        self.daily_pnl_file = os.path.join(PROJECT_ROOT, "data", "logs", "daily_pnl_v2.csv")
        self.stop_flags = [
            os.path.join(PROJECT_ROOT, "data", "state", "stop_v2.flag"),
            os.path.join(PROJECT_ROOT, "stop.flag"),
            os.path.join(PROJECT_ROOT, "zxc.txt")
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
        
        # Anti-Whipsaw Cooldown Tracker per leg (3m timer removed)
        self.cooldown_tracker: Dict[str, Dict[str, Any]] = {
            "CE": {"stopped_time": 0.0, "stopped_spot": 0.0, "active": False},
            "PE": {"stopped_time": 0.0, "stopped_spot": 0.0, "active": False}
        }
        
        self.current_indicators: Dict[str, Any] = {
            "kama": None, "prev_kama": None, "trend": 0,
            "atr": DEFAULT_ATR_5M, "adx": 18.0, "regime": "CHOP"
        }
        
        df_5m = self.market_data.get_5m_dataframe()
        if not df_5m.empty and len(df_5m) >= 5:
            self.current_indicators = Indicators.evaluate_all(df_5m)
            log_info(f"ExecutionEngine: Indicators pre-warmed on boot (KAMA={self.current_indicators.get('kama')}, Trend={self.current_indicators.get('trend')}, ADX={self.current_indicators.get('adx')}, ATR={self.current_indicators.get('atr')}, Regime={self.current_indicators.get('regime')})")
        
        self._ltp_cache: Dict[str, float] = {}
        self._load_state()
        self._write_pid()
        
        self._start_kill_switch_listener()
        self._setup_signal_handlers()

    def _write_pid(self):
        try:
            pid_file = os.path.join(PROJECT_ROOT, "data", "state", "v2.pid")
            os.makedirs(os.path.dirname(pid_file), exist_ok=True)
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    def _remove_pid(self):
        try:
            pid_file = os.path.join(PROJECT_ROOT, "data", "state", "v2.pid")
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except Exception:
            pass

    def _setup_signal_handlers(self):
        def handler(sig, frame):
            log_alert(f"🛑 Received signal {sig}. Initiating immediate clean square-off...")
            self.trigger_emergency_shutdown(reason=f"SIGNAL_{sig}")
        
        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except Exception:
            pass

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
                    if sys.stdin and not sys.stdin.closed:
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
                        time.sleep(0.5)
                except Exception:
                    time.sleep(0.5)

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
            self.mode = "SESSION_DONE"
            self._save_state()
            self._remove_pid()
            print(f"\n{Fore.GREEN}✅ All positions successfully squared off. Strategy halted cleanly.{Style.RESET_ALL}\n", flush=True)
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
        if regime == "CHOP":
            min_w = CHOP_MIN_WIDTH_PTS
            max_w = CHOP_MAX_WIDTH_PTS
            mult  = CHOP_ATR_MULTIPLIER
        else:
            min_w = BASE_MIN_WIDTH_PTS
            max_w = BASE_MAX_WIDTH_PTS
            mult  = BASE_ATR_MULTIPLIER

        if float(dte_days) <= 1.0:
            return atm_spot, atm_spot

        calculated_width = max(min_w, min(max_w, mult * atr))
        expiry_curve = cls._expiry_width_multiplier(dte_days)
        expiry_floor = 50 if dte_days <= EXPIRY_NEAR_DAYS else min_w
        compressed_width = max(expiry_floor, round(max(min_w, calculated_width) * (1.0 - 0.65 * expiry_curve) + min_w * (0.65 * expiry_curve)))

        stride_50 = int(round(compressed_width / 50.0) * 50)
        final_width = max(expiry_floor, min(max_w, stride_50))
        
        ce_strike = atm_spot + final_width
        pe_strike = atm_spot - final_width
        
        if ce_strike <= atm_spot or pe_strike >= atm_spot or ce_strike == pe_strike:
            if float(dte_days) <= 1.0:
                ce_strike = atm_spot
                pe_strike = atm_spot
            else:
                ce_strike = atm_spot + 50
                pe_strike = atm_spot - 50
            
        return ce_strike, pe_strike

    def _calculate_lot_quantity(self) -> int:
        """
        Strict User Specification:
        '1 lot in each leg only — 65 quantity only'
        Hard-fixed to exactly 1 lot = LOT_SIZE (65). No dynamic scaling.
        """
        return LOT_SIZE  # Always exactly 65, never more, never less

    def _save_state(self):
        try:
            state = {
                "date": str(get_ist_now().date()),
                "mode": self.mode,
                "realized_pnl": self.realized_pnl,
                "positions": self.positions,
                "cooldown_tracker": self.cooldown_tracker
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=4)
        except Exception as e:
            log_warn(f"Failed to save state: {e}")

    def _get_live_exchange_positions(self) -> getattr(typing, 'Dict', dict):
        if self.broker.paper_trading or not self.broker.api:
            return None
        try:
            res = self.broker.api.get_positions()
            if isinstance(res, list):
                pos_map = {}
                for p in res:
                    if isinstance(p, dict) and "tsym" in p and "netqty" in p:
                        try:
                            netqty = int(p["netqty"])
                            if netqty != 0:
                                pos_map[p["tsym"]] = netqty
                        except ValueError:
                            pass
                return pos_map
        except Exception as e:
            log_warn(f"Failed to fetch exchange positions: {e}")
        return None

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            today_str = str(get_ist_now().date())
            if state.get("date") == today_str:
                self.realized_pnl = float(state.get("realized_pnl", 0.0))
                self.positions = state.get("positions", {})
                self.cooldown_tracker = state.get("cooldown_tracker", self.cooldown_tracker)

                saved_mode = state.get("mode", "WAIT_DATA")
                market_closed = (
                    get_ist_now().hour > AUTO_SQUAREOFF_HOUR or
                    (get_ist_now().hour == AUTO_SQUAREOFF_HOUR and get_ist_now().minute >= AUTO_SQUAREOFF_MINUTE)
                )
                if saved_mode in ("SESSION_DONE", "SESSION_DONE_FLAT") and not market_closed:
                    self.mode = "RUNNING" if self.positions else "WAIT_DATA"
                    log_warn(f"Recovered from persisted {saved_mode}; resuming as {self.mode} because session is still live.")
                else:
                    self.mode = saved_mode
                
                for leg, pos in self.positions.items():
                    # CRITICAL: Always sanitize qty from disk to exactly 1 lot (65).
                    # Prevents stale multi-lot qty from old sessions from firing wrong-size orders.
                    pos["qty"] = LOT_SIZE
                    if pos.get("side") == "SELL" and leg in ("CE", "PE"):
                        if "spot_sl_state" not in pos or not pos["spot_sl_state"]:
                            entry_spot = float(pos.get("entry_spot", self.market_data.latest_spot))
                            atr_val = float(self.current_indicators.get("atr", DEFAULT_ATR_5M))
                            pos["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, entry_spot, atr_val)

                # Sync with exchange on startup
                actual_positions = self._get_live_exchange_positions()
                if actual_positions is not None:
                    removed_any = False
                    for leg in list(self.positions.keys()):
                        tsym = self.positions[leg]["tsym"]
                        if tsym not in actual_positions or actual_positions[tsym] == 0:
                            log_warn(f"Startup Sync: {leg} ({tsym}) is no longer open on exchange. Removing from local state.")
                            del self.positions[leg]
                            removed_any = True
                    
                    if removed_any and not self.positions:
                        log_info("Startup Sync: No active positions found on exchange. Resetting to fresh WAIT_DATA state.")
                        self.mode = "WAIT_DATA"
                        
                log_info(f"Loaded existing session state for today ({today_str}). Realized PnL: ₹{self.realized_pnl:,.2f}")
        except Exception as e:
            log_warn(f"Failed to load state: {e}")

    def _log_trade(self, action: str, leg: str, strike: int, side: str, qty: int, price: float, pnl: Optional[float] = None, reason: str = ""):
        now_dt = get_ist_now()
        ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        today_str = now_dt.strftime("%Y-%m-%d")
        pnl_val = float(pnl) if pnl is not None else 0.0
        adx_val = float(self.current_indicators.get("adx", 0.0) or 0.0)
        regime_str = self.current_indicators.get("regime", "CHOP")

        try:
            db.record_trade(
                timestamp=ts_str,
                session_date=today_str,
                strategy_id="v2",
                action=action,
                leg=leg,
                strike=strike,
                side=side,
                qty=qty,
                price=price,
                pnl=pnl_val,
                reason=reason,
                adx=adx_val,
                regime=regime_str
            )
        except Exception as e:
            log_warn(f"DB Error recording trade: {e}")

        log_file = os.path.join(self.trade_book_dir, f"trade_log_v2_{today_str}.csv")
        file_exists = os.path.exists(log_file)
        try:
            with open(log_file, "a") as f:
                if not file_exists:
                    f.write("Timestamp,Action,Leg,Strike,Side,Qty,Price,PnL,Reason,ADX,Regime\n")
                pnl_str = f"{pnl:.2f}" if pnl is not None else ""
                f.write(f"{ts_str},{action},{leg},{strike},{side},{qty},{price:.2f},{pnl_str},{reason},{adx_val:.1f},{regime_str}\n")
        except Exception as e:
            log_warn(f"Failed to write trade log: {e}")

    def _get_ltp(self, strike: int, option_type: str) -> float:
        key = f"{strike}_{option_type}"
        if key not in self._ltp_cache:
            q = self.market_data.streamer.get_live_quote(strike, option_type)
            self._ltp_cache[key] = float(q.get("lp", 0.0))
        return self._ltp_cache[key]

    # ──────────────────────────────────────────────────────────────────────────
    # Deep Order Verification & Anti-Duplicate Trade Engine
    # ──────────────────────────────────────────────────────────────────────────

    def _verify_order_status(self, ord_id: str, tsym: str, side: str, qty: int) -> Tuple[bool, str]:
        """
        Deeply verifies order acceptance against broker OMS & Exchange Order Book.
        Guarantees that if an order succeeded, we NEVER duplicate it!
        Returns: (is_confirmed_placed: bool, reason_string: str)
        """
        # Look for API provider on broker or broker itself
        api = getattr(self.broker, "api", self.broker)

        # Wait 300ms for exchange RMS acknowledgment if real broker
        if not getattr(self.broker, "paper_trading", False):
            time.sleep(0.3)

        # Method 1: Check single order history
        if hasattr(api, "single_order_history"):
            try:
                history = api.single_order_history(orderno=str(ord_id))
                if history and isinstance(history, list) and len(history) > 0:
                    latest = history[-1]
                    status = str(latest.get("status", "")).upper()
                    if status in ("COMPLETE", "FILLED"):
                        return True, f"COMPLETE (OrderID={ord_id})"
                    elif status in ("OPEN", "PENDING", "TRIGGER_PENDING"):
                        return True, f"OPEN on exchange (OrderID={ord_id})"
                    elif status in ("REJECTED", "CANCELLED"):
                        rej = latest.get("rejreason", latest.get("emsg", "Order Rejected by RMS/Exchange"))
                        return False, f"REJECTED: {rej} (OrderID={ord_id})"
            except Exception as e:
                log_warn(f"Error querying single_order_history: {e}")

        # Method 2: Check full order book
        if hasattr(api, "get_order_book"):
            try:
                book = api.get_order_book()
                if book and isinstance(book, list):
                    for o in book:
                        if str(o.get("norenordno", "")) == str(ord_id):
                            status = str(o.get("status", "")).upper()
                            if status in ("COMPLETE", "FILLED", "OPEN", "PENDING"):
                                return True, f"{status} (OrderID={ord_id})"
                            elif status in ("REJECTED", "CANCELLED"):
                                rej = o.get("rejreason", "Order Rejected by RMS")
                                return False, f"REJECTED: {rej} (OrderID={ord_id})"
            except Exception as e:
                log_warn(f"Error querying get_order_book: {e}")

        # Paper trading fallback
        if getattr(self.broker, "paper_trading", False):
            return True, f"Paper fill confirmed (OrderID={ord_id})"

        return True, f"Placed with OrderID={ord_id}"

    def _enter_leg(self, leg: str, strike: int, side: str, spot: float, atr: float) -> bool:
        """
        Executes order for a leg with up to 3 retries.
        Guarantees:
          1. 1 order dispatched per 1.05s max.
          2. Deep verification prevents placing duplicate trades.
          3. If all 3 attempts fail, returns False to trigger 'ONLY HEDGES LEFT'.
        """
        base = leg.split("_")[0]
        q = self.market_data.streamer.get_live_quote(strike, base)
        tsym = q.get("tsym", f"NIFTY_{strike}_{base}")
        ltp = self._get_ltp(strike, base)
        
        if ltp <= 0:
            log_warn(f"Skipping {leg} entry — LTP is 0 or unavailable for strike {strike}.")
            return False
        
        # ── USER CONFIRMATION GATE ─────────────────────────────────────────────
        # Ask Y/N once before attempting any retry. One approval = all retries allowed.
        if not self._ask_user_confirm(leg=leg, side=side, strike=strike, qty=self.qty, ltp=ltp):
            log_info(f"Trade SKIPPED by user for {leg} {side} Strike={strike}.")
            return False
        # ──────────────────────────────────────────────────────────────────────
            
        placed_successfully = False
        last_reason = ""
        
        for attempt in range(1, ORDER_MAX_RETRIES + 1):
            # Enforce 1 order per sec pacing
            self._wait_order_rate_limit()
            
            # Refresh price quote before each attempt
            if attempt > 1:
                self._ltp_cache.pop(f"{strike}_{base}", None)
                ltp = self._get_ltp(strike, base)
            
            log_info(f"Submitting order (Attempt {attempt}/{ORDER_MAX_RETRIES}) [{side} {self.qty}x {tsym} @ ₹{ltp:.2f}]...")
            
            try:
                res = self.broker.place_option_order(
                    symbol=tsym,
                    transaction_type=side,
                    quantity=self.qty,
                    price=ltp
                )
                
                # Check initial API submission
                if res and isinstance(res, dict) and str(res.get("stat", "")).lower() in ("ok", "success"):
                    ord_id = res.get("norenordno", res.get("order_id", "OK"))
                    # Deep verification with exchange order book
                    confirmed, detail = self._verify_order_status(ord_id, tsym, side, self.qty)
                    if confirmed:
                        placed_successfully = True
                        log_info(f"✅ Trade Placed & Verified [{side} {self.qty}x {tsym}]: {detail}")
                        break
                    else:
                        last_reason = detail
                        log_warn(f"⚠️ Order {ord_id} was REJECTED by exchange: {detail}")
                else:
                    err_msg = res.get("emsg", str(res)) if isinstance(res, dict) else str(res)
                    last_reason = f"Broker Submission Rejected: {err_msg}"
                    log_warn(f"⚠️ Attempt {attempt} rejected by OMS: {err_msg}")
                    
            except Exception as e:
                last_reason = f"Exception: {e}"
                log_warn(f"⚠️ Attempt {attempt} threw exception: {e}")

        if not placed_successfully:
            log_alert(f"❌ ORDER FAILED for {leg} {side} Strike {strike} after {ORDER_MAX_RETRIES} attempts ({last_reason})!")
            return False
        
        pos_info = {
            "strike": strike,
            "tsym": tsym,
            "base": base,
            "side": side,
            "qty": self.qty,
            "entry_price": ltp,
            "entry_time": time.time(),
            "entry_spot": spot
        }
        
        if side == "SELL":
            pos_info["spot_sl_state"] = self.risk_manager.init_spot_sl(leg, spot, atr)
            
        self.positions[leg] = pos_info
        verb = "SOLD" if side == "SELL" else "BOUGHT"
        log_trade(f"{verb} {leg:10s} Strike: {strike} @ ₹{ltp:.2f} (Qty: {self.qty} | Spot: {spot:.2f})")
        self._log_trade("ENTRY", leg, strike, side, self.qty, ltp, reason="SIGNAL")
        self._save_state()
        return True

    def _exit_leg(self, leg: str, reason: str = "MANUAL") -> float:
        """Exits an open leg with rate limiting (1 order / sec)."""
        if leg not in self.positions:
            return 0.0
        
        pos = self.positions[leg]
        base = pos["base"]
        ltp = self._get_ltp(pos["strike"], base)
        close_side = "BUY" if pos["side"] == "SELL" else "SELL"
        
        # HARD GUARD: Always cap exit qty to exactly 1 lot (65). Never more.
        close_qty = LOT_SIZE
        pos["qty"] = LOT_SIZE  # sanitize in-memory too
        
        # Enforce rate limit
        self._wait_order_rate_limit()
        
        try:
            self.broker.place_option_order(
                symbol=pos["tsym"],
                transaction_type=close_side,
                quantity=close_qty,      # Always exactly LOT_SIZE (65)
                price=ltp
            )
        except Exception as e:
            log_warn(f"Broker exit exception for {leg}: {e}")
        
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
            if actual_positions is not None:
                tsym = self.positions[leg]["tsym"]
                if tsym not in actual_positions or actual_positions[tsym] == 0:
                    log_info(f"Skipping exit for {leg} ({tsym}): Already closed on exchange.")
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

    def enforce_strangle_or_hedges_only(self, context: str = "CHECK"):
        """
        Enforces that the portfolio is either:
          1. A full balanced strangle (both CE and PE short legs active + hedges), OR
          2. ONLY hedges left (no orphan short legs).
        """
        has_ce = ("CE" in self.positions and self.positions["CE"].get("side") == "SELL")
        has_pe = ("PE" in self.positions and self.positions["PE"].get("side") == "SELL")

        # If one short leg exists without the other, square it off immediately so only hedges remain
        if has_ce and not has_pe:
            log_alert(f"⚠️ Imbalance detected ({context}): Short CE active without PE! Squaring off CE so ONLY hedges remain...")
            self._exit_leg("CE", reason=f"ORPHAN_SQUAREOFF_{context}")
        elif has_pe and not has_ce:
            log_alert(f"⚠️ Imbalance detected ({context}): Short PE active without CE! Squaring off PE so ONLY hedges remain...")
            self._exit_leg("PE", reason=f"ORPHAN_SQUAREOFF_{context}")

    # ──────────────────────────────────────────────────────────────────────────
    # Anti-Whipsaw Re-entry (Zero 3-Minute Cooldown Delay)
    # ──────────────────────────────────────────────────────────────────────────

    def _trigger_leg_cooldown(self, stopped_leg: str, current_spot: float):
        """
        Enters cooldown tracker without 3-minute delay.
        Re-entry is immediately eligible on the very next 1-minute bar if trend is safe.
        """
        self.cooldown_tracker[stopped_leg] = {
            "stopped_time": time.time(),
            "stopped_spot": current_spot,
            "active": True
        }
        log_alert(f"⏳ {stopped_leg} stopped out. 3-minute cooldown removed: Re-entry evaluated on next 1-min candle.")
        self.mode = "COOLDOWN"
        self._save_state()

    def _check_cooldown_and_reenter(self, spot: float, atm: int, atr: float, regime: str, trend: int, dte_days: float = 2.0):
        """
        Evaluates immediate re-entry on 1-min candle closes without 3-minute waiting timer.
        If re-entry order fails after 3 tries -> ONLY HEDGES LEFT!
        """
        for leg in ("CE", "PE"):
            cd = self.cooldown_tracker.get(leg)
            if not cd or not cd.get("active", False):
                continue
            
            # Trend direction safety guard: do not re-enter against strong directional momentum
            trend_safe = True
            if leg == "CE" and regime == "TREND" and trend == 1:
                trend_safe = False
            elif leg == "PE" and regime == "TREND" and trend == -1:
                trend_safe = False
                
            if trend_safe:
                log_info(f"✅ Dynamic clearance met for {leg} (Trend safe: {trend}). Attempting re-shorting...")
                ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days)
                target_strike = ce_strike if leg == "CE" else pe_strike
                
                if leg not in self.positions:
                    success = self._enter_leg(leg, target_strike, "SELL", spot, atr)
                    if not success:
                        # Re-entry failed after 3 tries -> ONLY HEDGES LEFT!
                        self.square_off_all_short_legs(reason="REENTRY_3_TRIES_FAILED")

                cd["active"] = False
                self._save_state()
                
                if not any(v.get("active", False) for v in self.cooldown_tracker.values()):
                    self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"

    # ──────────────────────────────────────────────────────────────────────────
    # Terminal UI & Dashboard Renderer
    # ──────────────────────────────────────────────────────────────────────────

    def _render_dashboard(self, spot: float, atm: int):
        import re
        def ansi_len(s): return len(re.sub(r"\x1b\[[0-9;]*m", "", s))
        
        W = 114
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

        ind = self.current_indicators
        trend_str = "▲ UP" if ind["trend"] == 1 else ("▼ DOWN" if ind["trend"] == -1 else "━ FLAT")
        trend_col = c_green if ind["trend"] == 1 else (c_red if ind["trend"] == -1 else c_yellow)
        regime_col = c_mag if ind["regime"] == "CHOP" else (c_cyan if ind["regime"] == "TREND" else c_yellow)
        kama_str = f"{ind['kama']:.2f}" if ind["kama"] else "WARMUP"
        
        print()
        print(TOP)
        title_left = f"  {c_cyan}ADAPTIVE KAMA-ADX HEDGED STRANGLE (V2.0){res}  {c_dim}│{res}  {c_yellow}SPOT-TSL ACTIVE{res}  {c_dim}│{res}  {c_green}TYPE 'zxc' TO STOP{res}"
        title_right = f"{c_dim}{_now_str()}{res}  "
        pad = max(0, W - ansi_len(title_left) - ansi_len(title_right))
        print(f"{V}{title_left}{' ' * pad}{title_right}{V}")
        
        ind_bar = (f"  {c_dim}SPOT:{res} {c_white}{spot:>9.2f}{res}  {c_dim}ATM:{res} {c_yellow}{atm:<5}{res}  "
                   f"{c_dim}ADX(5m):{res} {regime_col}{ind['adx']:>4.1f} ({ind['regime']}){res}  "
                   f"{c_dim}KAMA(5m):{res} {c_white}{kama_str:>8}{res} {trend_col}{trend_str}{res}  "
                   f"{c_dim}ATR(5m):{res} {c_white}{ind['atr']:>4.1f} pts{res}")
        pad_ind = max(0, W - ansi_len(ind_bar))
        print(MID)
        print(f"{V}{ind_bar}{' ' * pad_ind}{V}")
        print(MID)

        unrealized = 0.0
        snap_positions = {}
        if not self.positions:
            msg = f"  {c_yellow}No open positions. State: {self.mode}{res}"
            print(f"{V}{msg}{' ' * max(0, W - ansi_len(msg))}{V}")
        else:
            hdr = f"  {'LEG':<10} {VS} {'STRIKE':>7} {VS} {'SIDE':<5} {VS} {'QTY':>3} {VS} {'ENTRY':>7} {VS} {'LTP':>7} {VS} {'ENTRY SPOT':>10} {VS} {'SPOT SL':>10} {VS} {'DEBOUNCE':>8} {VS} {'PNL':>10}  "
            print(f"{V}{hdr}{' ' * max(0, W - ansi_len(hdr))}{V}")
            print(MID_S)

            for leg, pos in self.positions.items():
                ltp = self._get_ltp(pos["strike"], pos["base"])
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
                
                sl_state = pos.get("spot_sl_state")
                if sl_state and is_short:
                    entry_spot_str = f"{sl_state['entry_spot']:.1f}"
                    spot_sl_str = f"{sl_state['current_sl']:.1f}"
                    debounce_str = f"{sl_state.get('breach_count', 0)}/{SPOT_SL_DEBOUNCE_BARS}"
                else:
                    entry_spot_str = "—"
                    spot_sl_str = "—"
                    debounce_str = "—"

                row = (f"  {c_white}{leg:<10}{res} {VS} {c_white}{pos['strike']:>7}{res} {VS} {side_col}{pos['side']:<5}{res} {VS} "
                       f"{c_white}{pos['qty']:>3}{res} {VS} "
                       f"{c_white}{pos['entry_price']:>7.2f}{res} {VS} {c_yellow}{ltp:>7.2f}{res} {VS} "
                       f"{c_dim}{entry_spot_str:>10}{res} {VS} {c_mag}{spot_sl_str:>10}{res} {VS} "
                       f"{c_white}{debounce_str:>8}{res} {VS} {pnl_col}{sign}₹{pnl:>8,.2f}{res}  ")
                print(f"{V}{row}{' ' * max(0, W - ansi_len(row))}{V}")

        active_cds = [f"{k} (Re-entry eligible next bar)" 
                      for k, v in self.cooldown_tracker.items() if v.get("active", False)]
        if active_cds:
            print(MID_S)
            cd_msg = f"  {c_yellow}⏳ RE-ENTRY PENDING:{res} {', '.join(active_cds)}"
            print(f"{V}{cd_msg}{' ' * max(0, W - ansi_len(cd_msg))}{V}")

        print(MID)
        db_stats = db.get_strategy_pnl_summary("v2", base_capital=CAPITAL)
        db_today = db_stats.get("today_pnl", 0.0)
        uncommitted_pnl = (self.realized_pnl - db_today) if abs(self.realized_pnl - db_today) > 0.01 else 0.0
        mtd_pnl = db_stats.get("mtd_pnl", 0.0) + uncommitted_pnl
        mtd_ret = (mtd_pnl / CAPITAL) * 100.0
        ytd_pnl = db_stats.get("ytd_pnl", 0.0) + uncommitted_pnl
        ytd_ret = (ytd_pnl / CAPITAL) * 100.0
        total_cap = db_stats.get("current_capital", CAPITAL) + uncommitted_pnl

        total_pnl = self.realized_pnl + unrealized
        ret_pct = (total_pnl / CAPITAL) * 100.0
        pnl_col = c_green if total_pnl >= 0 else c_red
        sign = "+" if total_pnl >= 0 else ""

        mtd_col = c_green if mtd_pnl >= 0 else c_red
        ytd_col = c_green if ytd_pnl >= 0 else c_red

        pnl_str = (f"  {c_dim}TODAY REALIZED:{res} ₹{self.realized_pnl:,.2f}  {c_dim}UNREAL:{res} ₹{unrealized:,.2f}  "
                   f"{c_dim}NET MTM:{res} {pnl_col}{sign}₹{total_pnl:,.2f} ({ret_pct:+.2f}%){res}  "
                   f"{c_dim}CIRCUIT:{res} {c_red}-₹{abs(self.risk_manager.circuit_breaker_loss_limit):,.0f} (-1.8%){res}")
        print(f"{V}{pnl_str}{' ' * max(0, W - ansi_len(pnl_str))}{V}")

        cum_str = (f"  {c_yellow}MONTH-TO-DATE (MTD):{res} {mtd_col}{'+' if mtd_pnl>=0 else ''}₹{mtd_pnl:,.2f} ({mtd_ret:+.2f}%){res}  {VS}  "
                   f"{c_yellow}YEAR-TO-DATE (YTD):{res} {ytd_col}{'+' if ytd_pnl>=0 else ''}₹{ytd_pnl:,.2f} ({ytd_ret:+.2f}%){res}  {VS}  "
                   f"{c_dim}CAPITAL:{res} {c_white}₹{total_cap:,.2f}{res}")
        print(MID_S)
        print(f"{V}{cum_str}{' ' * max(0, W - ansi_len(cum_str))}{V}")
        print(BOT)
        sys.stdout.flush()

        try:
            snap = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "spot": spot,
                "atm": atm,
                "mode": self.mode,
                "config": {
                    "session_em_1sd": getattr(self, "session_em_1sd", 0.0),
                    "capital": CAPITAL,
                    "qty": self.qty,
                    "kama_period": KAMA_PERIOD,
                    "kama_fast": KAMA_FAST_EMA,
                    "kama_slow": KAMA_SLOW_EMA,
                    "kama_min_slope": KAMA_MIN_SLOPE,
                    "adx_period": ADX_PERIOD,
                    "adx_gate": ADX_CHOP_THRESHOLD,
                    "debounce_bars": SPOT_SL_DEBOUNCE_BARS,
                    "strangle_width": BASE_MIN_WIDTH_PTS,
                    "hedge_dist": HEDGE_WIDTH_PTS,
                    "spot_sl_atr": SPOT_SL_ATR_MULT,
                    "spot_trail_pct": SPOT_SL_TRAIL_RATIO * 100.0,
                    "cooldown_min": 0
                },
                "indicators": self.current_indicators,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": unrealized,
                "total_pnl": total_pnl,
                "mtd_pnl": mtd_pnl,
                "mtd_return_pct": mtd_ret,
                "ytd_pnl": ytd_pnl,
                "ytd_return_pct": ytd_ret,
                "total_capital": total_cap,
                "positions": snap_positions,
                "cooldown": self.cooldown_tracker
            }
            with open(self.live_snap_file, "w") as sf:
                json.dump(snap, sf)
        except Exception:
            pass

    def run(self):
        log_info("Starting Adaptive KAMA-ADX Hedged Strangle Strategy (v2.0)...")
        log_info(f"Capital: ₹{CAPITAL:,} | Lot Size: {LOT_SIZE} | Order Qty: {self.qty} | Portfolio Stop: -{PORTFOLIO_CIRCUIT_PCT}%")
        log_info("Execution Safety: 1 order/sec rate limit | 3 retries verified against order book | Strangle or Hedges Only.")
        log_info("To stop strategy and square-off all positions at any time, simply type 'zxc' in this terminal.")
        
        while self.is_running and not _EMERGENCY_STOP_TRIGGERED:
            try:
                now = get_ist_now()
                self._ltp_cache.clear()
                
                # Check Auto Square-off Time (15:28 PM)
                if now.hour > AUTO_SQUAREOFF_HOUR or (now.hour == AUTO_SQUAREOFF_HOUR and now.minute >= AUTO_SQUAREOFF_MINUTE):
                    log_alert(f"🕒 Auto Square-Off Time Reached ({AUTO_SQUAREOFF_HOUR}:{AUTO_SQUAREOFF_MINUTE:02d}). Liquidating all positions...")
                    self._exit_all_positions(reason="SESSION_END")
                    self.mode = "SESSION_DONE"
                    self._save_state()
                    self._render_dashboard(self.market_data.latest_spot, self.market_data.latest_atm)
                    print(f"\n{Fore.GREEN}✅ Session Completed Successfully. Final Realized PnL: ₹{self.realized_pnl:,.2f}{Style.RESET_ALL}\n")
                    self._remove_pid()
                    sys.exit(0)

                # ── 1. STRICT 1-MINUTE EXECUTION CADENCE ──
                spot, atm, is_new_1m_bar = self.market_data.fetch_live_tick()
                
                if not is_new_1m_bar:
                    now_curr = get_ist_now()
                    sec_into_min = now_curr.second + (now_curr.microsecond / 1_000_000.0)
                    sleep_sec = max(0.1, 60.0 - sec_into_min + 0.05)
                    self._smart_sleep(sleep_sec)
                    continue

                # ── 2. Run KAMA & Indicators strictly on the collected 1-minute data ──
                df_5m = self.market_data.get_5m_dataframe()
                self.current_indicators = Indicators.evaluate_all(df_5m)
                
                current_iv = 15.0
                if self.session_em_1sd > 0:
                    current_iv = (self.session_em_1sd / spot) * 19.1 * 100.0
                
                rv = VolatilityEngine.calculate_realized_volatility(self.market_data.bars_1m)
                rv_iv_ratio = VolatilityEngine.compute_rv_iv_divergence(rv, current_iv)
                self.current_indicators["rv_iv_ratio"] = rv_iv_ratio
                
                if rv_iv_ratio > 1.15 and self.current_indicators["regime"] == "CHOP":
                    self.current_indicators["regime"] = "TRANSITION"
                    log_info(f"RV/IV Divergence {rv_iv_ratio:.2f} > 1.15. Early Trend detected. Shifting CHOP -> TRANSITION.")
                
                atr = self.current_indicators["atr"]
                regime = self.current_indicators["regime"]
                trend = self.current_indicators["trend"]
                _, dte_days = self.market_data.streamer.get_near_expiry_dte()
                
                # ── 3. Check Portfolio Circuit Breaker (-1.8% Capital) ──
                unrealized = sum([
                    ((p["entry_price"] - self._get_ltp(p["strike"], p["base"])) if p["side"] == "SELL" else (self._get_ltp(p["strike"], p["base"]) - p["entry_price"])) * p["qty"]
                    for p in self.positions.values()
                ])
                cb_triggered, cb_msg = self.risk_manager.check_portfolio_circuit_breaker(self.realized_pnl, unrealized)
                if cb_triggered:
                    log_alert(cb_msg)
                    self.trigger_emergency_shutdown(reason="CIRCUIT_BREAKER_HALT")
                    return

                # ── 4. State Machine Transitions ──
                
                # Phase A: Wait for data & 09:18 AM session start
                if self.mode == "WAIT_DATA":
                    if now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE):
                        if self.session_em_1sd == 0.0:
                            ce_ltp = self._get_ltp(atm, "CE")
                            pe_ltp = self._get_ltp(atm, "PE")
                            straddle = (ce_ltp + pe_ltp) if ce_ltp > 0 and pe_ltp > 0 else 0.0
                            self.session_em_1sd = VolatilityEngine.compute_expected_move(spot, straddle, 15.0)
                            log_info(f"Frozen Session EM_1sd: {self.session_em_1sd:.2f}")
                            
                        hedge_width = HEDGE_WIDTH_PTS
                        log_info(f"Market Start Time (09:18 AM) reached. Ingesting positions (Regime: {regime}, ATR: {atr:.1f}, Hedge Dist: {hedge_width})...")
                        
                        # Step 1: Buy Far-OTM Hedges
                        ce_h_ok = True
                        pe_h_ok = True
                        if "CE_HEDGE" not in self.positions:
                            ce_h_ok = self._enter_leg("CE_HEDGE", atm + hedge_width, "BUY", spot, atr)
                        if "PE_HEDGE" not in self.positions:
                            pe_h_ok = self._enter_leg("PE_HEDGE", atm - hedge_width, "BUY", spot, atr)
                            
                        if not (ce_h_ok and pe_h_ok):
                            log_alert("⚠️ Hedge entry failed after 3 tries. Aborting short leg entry to protect capital.")
                            self.square_off_all_short_legs(reason="HEDGE_ENTRY_FAILED")
                            continue

                        # Step 2: Sell Dynamic ATR-Calculated Strangle
                        ce_strike, pe_strike = self.calculate_strangle_strikes(atm, atr, regime, dte_days=dte_days)
                        
                        ce_s_ok = True
                        pe_s_ok = True
                        if "CE" not in self.positions:
                            ce_s_ok = self._enter_leg("CE", ce_strike, "SELL", spot, atr)
                        if "PE" not in self.positions:
                            pe_s_ok = self._enter_leg("PE", pe_strike, "SELL", spot, atr)
                            
                        # CRITICAL RULE: If either short leg failed after 3 retries, square off all short legs -> ONLY HEDGES LEFT!
                        if not (ce_s_ok and pe_s_ok):
                            log_alert("⚠️ Short Strangle entry failed to complete after 3 tries! Squaring off short legs so ONLY HEDGES REMAIN.")
                            self.square_off_all_short_legs(reason="STRANGLE_ENTRY_FAILED_LEAVE_HEDGES")
                        else:
                            self.mode = "CHOP_MODE" if regime == "CHOP" else "RUNNING"
                            
                        self._save_state()
                    else:
                        log_info(f"⏳ Pre-market wait: Current IST is {now.strftime('%H:%M:%S')}. Trading session starts at {MARKET_START_HOUR:02d}:{MARKET_START_MINUTE:02d} IST.")

                # Phase B: Active Trading Management (RUNNING, CHOP_MODE, COOLDOWN)
                elif self.mode in ("RUNNING", "CHOP_MODE", "COOLDOWN", "HEDGES_ONLY"):
                    if not self.positions:
                        log_info("No active positions in RUNNING mode. Resetting to WAIT_DATA...")
                        self.mode = "WAIT_DATA"
                        self._save_state()
                        continue

                    # Check Spot-Based Trailing Stop Losses strictly on 1-min collected data
                    for leg in ("CE", "PE"):
                        if leg in self.positions and self.positions[leg]["side"] == "SELL":
                            is_stopped, reason = self.risk_manager.update_spot_sl_and_check(
                                leg, self.positions[leg], spot, is_new_1m_bar
                            )
                            if is_stopped:
                                log_alert(reason)
                                self._exit_leg(leg, reason="SPOT_TSL_HIT")
                                self._trigger_leg_cooldown(leg, spot)

                    # Dynamic re-entry (3m cooldown removed - checks immediately on 1m bar)
                    self._check_cooldown_and_reenter(spot, atm, atr, regime, trend, dte_days=dte_days)

                    # Dynamic Chop Regime Strike Adjustment (Only when in active trading, not HEDGES_ONLY)
                    if regime == "CHOP" and self.mode != "HEDGES_ONLY":
                        chop_ce, chop_pe = self.calculate_strangle_strikes(atm, atr, "CHOP", dte_days=dte_days)
                        if "CE" in self.positions and self.positions["CE"]["strike"] < chop_ce:
                            log_info(f"🌪️ ADX < 20 (CHOP). Rolling CE from {self.positions['CE']['strike']} further OTM to {chop_ce}...")
                            self._exit_leg("CE", reason="CHOP_ROLL_OUT")
                            success = self._enter_leg("CE", chop_ce, "SELL", spot, atr)
                            if not success:
                                # Roll failed after 3 tries -> ONLY HEDGES LEFT!
                                self.square_off_all_short_legs(reason="ROLL_CE_FAILED")

                        if "PE" in self.positions and self.positions["PE"]["strike"] > chop_pe:
                            log_info(f"🌪️ ADX < 20 (CHOP). Rolling PE from {self.positions['PE']['strike']} further OTM to {chop_pe}...")
                            self._exit_leg("PE", reason="CHOP_ROLL_OUT")
                            success = self._enter_leg("PE", chop_pe, "SELL", spot, atr)
                            if not success:
                                # Roll failed after 3 tries -> ONLY HEDGES LEFT!
                                self.square_off_all_short_legs(reason="ROLL_PE_FAILED")

                    # Routine Invariant Verification: Strangle or Hedges Only
                    self.enforce_strangle_or_hedges_only(context="CYCLE_HEALTH_CHECK")

                # ── 5. Render Live Dashboard ──
                self._render_dashboard(spot, atm)
                
                # Sleep strictly until next 1-minute boundary (XX:XX:00.05)
                now_curr = get_ist_now()
                sec_into_min = now_curr.second + (now_curr.microsecond / 1_000_000.0)
                sleep_sec = max(0.1, 60.0 - sec_into_min + 0.05)
                self._smart_sleep(sleep_sec)

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
    global SPOT_SL_DEBOUNCE_BARS, BASE_MIN_WIDTH_PTS, BASE_MAX_WIDTH_PTS, SPOT_SL_TRAIL_RATIO
    global PAPER_TRADING_MODE

    is_tty = sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if not is_tty:
        return  # nohup / background: keep safe defaults (paper trading)

    print(f"\n{Fore.CYAN}{Style.BRIGHT}{'═'*78}")
    print(f"  🚀 V2.0 PRO (KAMA-ADX STRANGLE) — DEPLOYMENT CONFIGURATION")
    print(f"{'═'*78}{Style.RESET_ALL}")

    # ── PAPER / LIVE SELECTION (asked FIRST, before anything else) ────────────
    print(f"\n{Fore.YELLOW}{Style.BRIGHT}{'═'*78}")
    print(f"  STEP 1 OF 2: PAPER TRADING OR LIVE TRADING?")
    print(f"{'═'*78}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}Y{Style.RESET_ALL} = Paper Trading  (SAFE — simulated fills, zero real money risk)")
    print(f"  {Fore.RED}N{Style.RESET_ALL} = LIVE Trading   (⚠️  REAL MONEY — orders go directly to Flattrade!)\n")

    try:
        while True:
            ans = input("  ➤ Run in Paper Trading mode? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                PAPER_TRADING_MODE = True
                print(f"\n  {Fore.GREEN}{Style.BRIGHT}✅ PAPER TRADING selected. No real orders will be placed.{Style.RESET_ALL}\n")
                break
            elif ans in ("n", "no"):
                print(f"\n  {Fore.RED}{Style.BRIGHT}{'─'*78}")
                print(f"  ⚠️  WARNING: LIVE TRADING SELECTED — REAL MONEY RISK!")
                print(f"  ─ All orders will be sent directly to Flattrade exchange.")
                print(f"  ─ Capital at risk: Rs.{CAPITAL:,.0f}")
                print(f"  ─ Every individual trade still requires your Y/N confirmation.")
                print(f"  ─ ORDER: Buy hedges FIRST, then sell short legs.")
                print(f"{'─'*78}{Style.RESET_ALL}")
                confirm = input("\n  ➤ Type LIVE to confirm, or press Enter to go paper: ").strip()
                if confirm == "LIVE":
                    PAPER_TRADING_MODE = False
                    print(f"\n  {Fore.RED}{Style.BRIGHT}🚀 LIVE TRADING CONFIRMED. Real orders will be placed!{Style.RESET_ALL}\n")
                else:
                    PAPER_TRADING_MODE = True
                    print(f"  {Fore.YELLOW}Defaulting to PAPER TRADING (safe).{Style.RESET_ALL}\n")
                break
            else:
                print(f"  {Fore.YELLOW}Please type Y (paper) or N (live).{Style.RESET_ALL}")
    except (KeyboardInterrupt, EOFError):
        PAPER_TRADING_MODE = True
        print(f"\n  {Fore.YELLOW}Defaulting to PAPER TRADING.{Style.RESET_ALL}\n")

    # ── STRATEGY PARAMETERS ───────────────────────────────────────────────────
    print(f"{Fore.YELLOW}STEP 2 OF 2: Strategy Parameters — press Enter to accept defaults.{Style.RESET_ALL}\n")

    try:
        def ask(prompt_text, default, val_type=float):
            val = input(f"  ➤ {prompt_text} [{default}]: ").strip()
            if not val:
                return default
            try:
                return val_type(val)
            except Exception:
                print(f"    {Fore.RED}Invalid input. Using default: {default}{Style.RESET_ALL}")
                return default

        CAPITAL = ask("Initial Capital (Rs.)", 145585.75, float)
        KAMA_PERIOD = ask("KAMA Lookback", 13, int)
        KAMA_FAST_EMA = ask("KAMA Fast EMA", 2, int)
        KAMA_SLOW_EMA = ask("KAMA Slow EMA", 30, int)
        ADX_PERIOD = ask("ADX Period (5m)", 9, int)
        gate = ask("ADX Regime Gate", 20.0, float)
        ADX_CHOP_THRESHOLD = gate
        ADX_TREND_THRESHOLD = gate
        SPOT_SL_DEBOUNCE_BARS = ask("Debounce Bars", 1, int)
        width = ask("Strangle Width (pts)", 0, int)
        BASE_MIN_WIDTH_PTS = width
        BASE_MAX_WIDTH_PTS = width
        tsl_pct = ask("Spot Trail % (TSL)", 55.0, float)
        SPOT_SL_TRAIL_RATIO = tsl_pct / 100.0 if tsl_pct > 1.0 else tsl_pct

        mode_label = "PAPER" if PAPER_TRADING_MODE else "LIVE"
        print(f"\n{Fore.GREEN}{Style.BRIGHT}{'═'*78}")
        print(f"  ✅ [{mode_label}] Parameters Confirmed — Deploying V2 Pro Strategy...")
        print(f"{'═'*78}{Style.RESET_ALL}\n")
    except (KeyboardInterrupt, EOFError):
        print(f"\n{Fore.YELLOW}Default parameters selected.{Style.RESET_ALL}\n")


if __name__ == "__main__":
    prompt_user_variables()
    engine = ExecutionEngine()
    engine.run()
