"""mcx_paper_v5.py  —  v5.0 (Continuous Streaming EMA Momentum + Hedged Straddle)
================================================================================
MCX Natural Gas Paper Trading Engine | Version 5.0
Session Window: 15:30 – 23:24 IST (weekdays & Sundays)
Auto Square-Off: 23:24 IST

Key Architecture & Rules:
  1. Continuous Streaming EMA Momentum Engine (from NIFTY 50 architecture):
     - Fast EMA: 15s half-life
     - Slow EMA: 90s half-life
     - Anchor EMA: 300s half-life
     - Rolling Volatility: RV60 / RV300 -> Volatility Ratio (VR)
     - Adaptive Persistence: P_req = clamp(10.0 / VR, 3.0s, 30.0s)
     - Directional Lock: Confirmed Signal (+1 Bullish, -1 Bearish, 0 Flat)
  2. Momentum-Guided Entry:
     - Confirmed Bullish (+1): Write PE solo at ATM, defer CE.
     - Confirmed Bearish (-1): Write CE solo at ATM, defer PE.
     - Neutral (0): Write balanced ATM Straddle (CE + PE).
  3. Proactive Early Trend Exit:
     - When both legs are open, if momentum locks strongly against an open leg,
       proactively exits before full SL is reached.
  4. Momentum Reversal Re-Entry:
     - Surviving leg re-enters missing side when momentum flips/halts.
  5. Dual-State SL & Solo TSL:
     - Strangle active: 15% initial SL, ratcheting into 8% TSL when in profit.
     - Solo active: Initial SL disabled, trailing strictly at 8% TSL.
  6. Box-Drawing ANSI Dashboard + 3-second live Telegram broadcast.
================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import math
import glob
import re
import json
import urllib.request
import zipfile
import io
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

from colorama import init as colorama_init, Fore, Style
colorama_init(autoreset=True)

try:
    from creds import USER_ID
except Exception:
    USER_ID = os.getenv('USER_ID', '')

_noren_import_error = ""
try:
    from api_helper import NorenApiPy
except Exception as e:
    NorenApiPy = None
    _noren_import_error = str(e)

# ─────────────────────────────────────────────
# IST Timezone helpers
# ─────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(IST)

def is_expiry_week(today_date: Any, expiry_date: Any) -> bool:
    """
    Determines whether today falls within the expiry week of the given expiry date.
    Returns True if:
      1. today is in the same calendar week (Monday to Sunday) as expiry_date, OR
      2. Days to expiration (DTE) <= 4 calendar days (safeguards weekend rollovers before Mon/Tue expiries).
    """
    if hasattr(expiry_date, 'date'):
        expiry_date = expiry_date.date()
    if hasattr(today_date, 'date'):
        today_date = today_date.date()

    days_to_expiry = (expiry_date - today_date).days
    if days_to_expiry < 0:
        return False

    monday_of_expiry_week = expiry_date - timedelta(days=expiry_date.weekday())
    sunday_of_expiry_week = monday_of_expiry_week + timedelta(days=6)

    in_calendar_week = (monday_of_expiry_week <= today_date <= sunday_of_expiry_week)
    within_dte_window = (days_to_expiry <= 4)
    return in_calendar_week or within_dte_window

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
TOKEN_FILE          = 'token.txt'
STRIKE_STEP         = 5.0          # Natural Gas strike step
MCX_ENTRY_HOUR      = 15           # 15:30 IST open (3:30 PM)
MCX_ENTRY_MINUTE    = 30
MCX_EXIT_HOUR       = 23
MCX_EXIT_MINUTE     = 24           # 23:24 IST auto square-off (11:24 PM)
LOT_SIZE            = 1250         # 1 lot = 1250 units
DEFAULT_SL_PCT      = 0.15         # 15% initial stop-loss (fresh straddles)
REENTRY_SL_PCT      = 0.15         # 15% initial stop-loss for reversal re-entry
DEFAULT_TSL_PCT     = 0.08         # 8% trailing stop-loss
POST_CLOSE_COOLDOWN = 60.0         # Seconds to wait after any close before re-entry (60s to let market breathe)
REENTRY_COOLDOWN_S  = 30.0         # Min seconds between single-leg re-entries
SWING_REVERSAL_PTS  = 0.80         # Fallback swing reversal pullback threshold

TELEGRAM_TOKEN = '8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE'
CHAT_ID        = '6307066850'

# ─────────────────────────────────────────────
# CONTINUOUS STREAMING EMA MOMENTUM ENGINE
# ─────────────────────────────────────────────
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
        prev_confirmed = self.confirmed_signal
        if self.raw_signal != 0 and hold_time >= p_req:
            self.confirmed_signal = self.raw_signal
        else:
            self.confirmed_signal = 0

        if self.confirmed_signal != prev_confirmed:
            direction = {1: "BULLISH▲", -1: "BEARISH▼", 0: "FLAT━"}
            print(f"[EMA SIGNAL] Confirmed signal changed: {direction.get(prev_confirmed,'?')} → {direction.get(self.confirmed_signal,'?')}  "
                  f"(EMA15={fast_val:.2f} EMA90={slow_val:.2f} slope={slow_slope:+.3f} VR={vr:.2f} hold={hold_time:.1f}s)", flush=True)

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


# ─────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────
_last_tg_dash_msg_ids: Dict[str, int] = {}
_last_tg_dash_new_msg_ts: float = 0.0
_last_tg_dash_edit_ts: float    = 0.0
_tg_rate_limited_until: float   = 0.0


def _get_tg_chat_ids() -> List[str]:
    if isinstance(CHAT_ID, list):
        return [str(c).strip() for c in CHAT_ID if str(c).strip()]
    if isinstance(CHAT_ID, (str, int)):
        return [c.strip() for c in str(CHAT_ID).split(',') if c.strip()]
    return []


def send_telegram(msg: str):
    """Fire-and-forget alert message (clears live dashboard anchor)."""
    global _last_tg_dash_msg_ids
    chat_ids = _get_tg_chat_ids()
    if not (TELEGRAM_TOKEN and chat_ids):
        return
    for cid in chat_ids:
        try:
            url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
            requests.post(url, data={'chat_id': cid, 'text': msg, 'parse_mode': 'HTML'}, timeout=5)
        except Exception:
            pass
    _last_tg_dash_msg_ids.clear()


# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────
def round_to_price(value: float, step: float = STRIKE_STEP) -> float:
    return round_to_tick(math.floor(value / step + 0.5) * step)


def round_to_tick(value: float) -> float:
    return round(value * 20.0) / 20.0


def _ansi_len(s: str) -> int:
    return len(re.sub(r'\x1b\[[0-9;]*m', '', s))


def _pad(row: str, width: int) -> str:
    return row + ' ' * max(0, width - _ansi_len(row))


def _fmt_pnl(val: float, width: int = 12) -> Tuple[str, str]:
    if abs(val) < 1e-4:
        val = 0.0
    sign = '+' if val > 0 else ('-' if val < 0 else ' ')
    pnl_str = f"{sign}₹{abs(val):,.2f}"
    return sign, f"{pnl_str:>{width}}"


# ─────────────────────────────────────────────
# Main Bot Engine
# ─────────────────────────────────────────────
class NaturalGasPaperBot:

    def __init__(self):
        self._lock_file = None
        try:
            import fcntl
            self._lock_file = open("/tmp/mcx_paper_engine.lock", "a+")
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.seek(0)
            self._lock_file.truncate()
            self._lock_file.write(f"{os.getpid()}\n")
            self._lock_file.flush()
        except (IOError, BlockingIOError):
            print("\n❌ [FATAL] Another instance of MCX Paper Trading Bot is already running!", flush=True)
            print("   Aborting duplicate instance immediately to prevent conflicting Telegram states.\n", flush=True)
            sys.exit(0)

        self.api                   = NorenApiPy() if NorenApiPy else None
        self.positions: Dict[str, Dict] = {}
        self.ema_engine            = ContinuousEMAEngine()
        self._reversal_latched     = False       # Latched reversal trigger
        self.last_reentry_ts       = 0.0         # Timestamp of last single-leg re-entry
        self.last_any_close_ts     = 0.0         # Timestamp of last leg close
        self.total_realized_pnl    = 0.0
        self.trades_today          = 0
        self.state_file            = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   'mcx_state_paper_v5.json')
        self._mcx_master           = None
        self._spot_cache           = {'ts': 0.0, 'val': 0.0}
        self._last_tg_dash_ts      = 0.0
        self._last_console_dash_ts = 0.0
        self.front_month_futs_token: Optional[str] = None
        self.front_month_futs_symbol: Optional[str] = None
        self.target_opt_expiry_ts: Optional[Any] = None
        self.target_opt_expiry_str: str = ""
        self.is_rolled_over: bool = False

        # Reversal tracking state
        self._spot_history: deque  = deque(maxlen=60)
        self._extreme_spot         = 0.0
        self._reversal_pullback    = 0.0

        self._load_state()

    # ── State persistence ─────────────────────
    def _save_state(self):
        try:
            state = {
                'date':               get_ist_now().strftime('%Y-%m-%d'),
                'positions':          self.positions,
                'total_realized_pnl': self.total_realized_pnl,
                'trades_today':       self.trades_today,
                'last_reentry_ts':    self.last_reentry_ts,
                'last_any_close_ts':  self.last_any_close_ts,
            }
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f'[WARN] Failed saving MCX state: {e}', flush=True)

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            today_str = get_ist_now().strftime('%Y-%m-%d')
            with open(self.state_file, 'r') as f:
                state = json.load(f)
            if state.get('date') == today_str:
                self.positions          = state.get('positions', {})
                self.total_realized_pnl = float(state.get('total_realized_pnl', 0.0))
                self.trades_today       = int(state.get('trades_today', 0))
                self.last_reentry_ts    = float(state.get('last_reentry_ts', 0.0))
                self.last_any_close_ts  = float(state.get('last_any_close_ts', 0.0))
                print(f'[STATE] Restored: {len(self.positions)} open legs | '
                      f'Realized PnL: ₹{self.total_realized_pnl:,.2f} | '
                      f'Trades: {self.trades_today}', flush=True)
        except Exception as e:
            print(f'[WARN] Error loading MCX state: {e}', flush=True)

    # ── Authentication ────────────────────────
    def authenticate(self):
        if not self.api:
            detail = f' ({_noren_import_error})' if _noren_import_error else ''
            raise RuntimeError(
                f'NorenApiPy not available{detail}. '
                'Activate venv: source flat_venv/bin/activate')

        candidates = [
            TOKEN_FILE,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), TOKEN_FILE),
            '/home/ubuntu/flattrade_tb/flattrade_tb/token.txt',
            '/home/ubuntu/flattrade_tb/token.txt',
        ]
        token_path = next((c for c in candidates
                           if os.path.exists(c) and os.path.getsize(c) > 0), None)
        if not token_path:
            raise FileNotFoundError(f'{TOKEN_FILE} missing or empty. Run login.py first.')

        with open(token_path, 'r') as f:
            access_token = f.read().strip()

        self.api.set_session(userid=str(USER_ID).strip(), password='', usertoken=access_token)

        try:
            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get('stat') != 'Ok':
                print('[WARN] Token validation notice: proceeding in paper mode.', flush=True)
        except Exception as e:
            print(f'[WARN] Flattrade session warning: {e}. Proceeding in paper mode.', flush=True)

        print(f'[OK] Natural Gas v5.0 PAPER TRADING bot authenticated from {token_path}.', flush=True)

    # ── MCX Symbol master ─────────────────────
    def _get_mcx_csv(self):
        if self._mcx_master is not None:
            return self._mcx_master

        import pandas as pd
        today_ist = get_ist_now().strftime('%Y-%m-%d')
        csv_file  = f'MCX_symbols_{today_ist}.csv'

        if not os.path.exists(csv_file):
            print(f'[INFO] {csv_file} not found. Downloading from Shoonya...', flush=True)
            try:
                url = 'https://api.shoonya.com/MCX_symbols.txt.zip'
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    with zipfile.ZipFile(io.BytesIO(resp.read())) as z:
                        with z.open('MCX_symbols.txt') as f:
                            df = pd.read_csv(f)
                            df.to_csv(csv_file, index=False)
                            print(f'[OK] Downloaded and cached {csv_file}', flush=True)
            except Exception as e:
                print(f'[WARN] Could not auto-download: {e}', flush=True)

        if not os.path.exists(csv_file):
            existing = sorted(glob.glob('MCX_symbols_*.csv'), reverse=True)
            if existing:
                csv_file = existing[0]
                print(f'[INFO] Using latest available file: {csv_file}', flush=True)
            else:
                print('[ERROR] No MCX_symbols_*.csv found!', flush=True)
                return None

        try:
            df = pd.read_csv(csv_file)
            df['ExpiryDate'] = pd.to_datetime(df['Expiry'], format='%d-%b-%Y', errors='coerce')
            self._mcx_master = df

            today_date = get_ist_now().date()
            today_ts   = pd.Timestamp(today_date)

            # ── 1. Determine Target Option Expiry (Rollover if in Expiry Week) ──
            opt_df = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'OPTFUT')]
            avail_opts = opt_df[opt_df['ExpiryDate'] >= today_ts]
            sorted_opt_expiries = sorted(avail_opts['ExpiryDate'].dropna().unique())
            if not sorted_opt_expiries:
                sorted_opt_expiries = sorted(opt_df['ExpiryDate'].dropna().unique())

            if sorted_opt_expiries:
                curr_opt_expiry_ts   = sorted_opt_expiries[0]
                curr_opt_expiry_date = pd.to_datetime(curr_opt_expiry_ts).date()

                if is_expiry_week(today_date, curr_opt_expiry_date) and len(sorted_opt_expiries) > 1:
                    target_opt_expiry_ts = sorted_opt_expiries[1]
                    self.is_rolled_over  = True
                    dte = (curr_opt_expiry_date - today_date).days
                    print(f'[EXPIRY ROLLOVER] Today ({today_date}) is in Expiry Week of current expiry '
                          f'{curr_opt_expiry_date.strftime("%d-%b-%Y")} (DTE: {dte}d). '
                          f'--> Rolled over to NEXT MONTH expiry: {pd.to_datetime(target_opt_expiry_ts).strftime("%d-%b-%Y")}', flush=True)
                else:
                    target_opt_expiry_ts = sorted_opt_expiries[0]
                    self.is_rolled_over  = False
                    dte = (curr_opt_expiry_date - today_date).days
                    print(f'[EXPIRY] Using Front-Month Option Expiry: {curr_opt_expiry_date.strftime("%d-%b-%Y")} '
                          f'(DTE: {dte}d)', flush=True)

                self.target_opt_expiry_ts  = target_opt_expiry_ts
                self.target_opt_expiry_str = pd.to_datetime(target_opt_expiry_ts).strftime('%d-%b-%Y')
            else:
                self.target_opt_expiry_ts  = today_ts
                self.target_opt_expiry_str = today_date.strftime('%d-%b-%Y')
                self.is_rolled_over        = False

            # ── 2. Determine Underlying Tracking Future ──
            futs = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'FUTCOM')]
            future_f = futs[futs['ExpiryDate'] >= self.target_opt_expiry_ts]
            if future_f.empty:
                future_f = futs[futs['ExpiryDate'] >= today_ts]
            if future_f.empty:
                future_f = futs
            if not future_f.empty:
                row = future_f.sort_values('ExpiryDate').iloc[0]
                self.front_month_futs_token  = str(row['Token'])
                self.front_month_futs_symbol = str(row['TradingSymbol'])
                fut_exp_str = pd.to_datetime(row['ExpiryDate']).strftime('%d-%b-%Y')
                print(f'[INFO] Tracking Underlying Future: {self.front_month_futs_symbol} '
                      f'(Token: {self.front_month_futs_token}, Expiry: {fut_exp_str})', flush=True)
            return self._mcx_master
        except Exception as e:
            print(f'[ERROR] Failed loading {csv_file}: {e}', flush=True)
            return None

    # ── Live spot price ───────────────────────
    def get_spot(self) -> float:
        now_ts = time.time()
        if now_ts - self._spot_cache['ts'] < 0.95 and self._spot_cache['val'] > 0:
            return self._spot_cache['val']

        if not self.front_month_futs_token:
            self._get_mcx_csv()

        if self.front_month_futs_token and self.api:
            try:
                q   = self.api.get_quotes(exchange='MCX', token=self.front_month_futs_token)
                val = float(q.get('lp', q.get('ltp', 0.0)) or 0.0) if q and isinstance(q, dict) else 0.0
                if val > 50.0:
                    self._spot_cache = {'ts': now_ts, 'val': val}
                    return val
            except Exception:
                pass

        if self.api:
            try:
                res = self.api.searchscrip(exchange='MCX', searchtext='NATURALGAS')
                if res and isinstance(res, dict) and res.get('values'):
                    for item in res['values']:
                        tsym = str(item.get('tsym', '')).upper()
                        if ('NATURALGAS' in tsym and 'MINI' not in tsym
                                and not tsym.endswith('CE') and not tsym.endswith('PE')):
                            q   = self.api.get_quotes(exchange='MCX', token=item.get('token'))
                            val = float(q.get('lp', q.get('ltp', 0.0)) or 0.0) if q and isinstance(q, dict) else 0.0
                            if val > 50.0:
                                self._spot_cache = {'ts': now_ts, 'val': val}
                                return val
            except Exception:
                pass

        return self._spot_cache['val']

    # ── Option symbol lookup ──────────────────
    def find_option_symbol(self, strike: float, option_type: str) -> Optional[Dict]:
        import pandas as pd
        df = self._get_mcx_csv()
        if df is None:
            return None
        try:
            opt_df   = df[
                (df['Symbol']      == 'NATURALGAS') &
                (df['Instrument']  == 'OPTFUT') &
                (df['OptionType']  == option_type) &
                (df['StrikePrice'] == float(strike))
            ]
            if opt_df.empty:
                return None

            # Filter for target option expiry
            if getattr(self, 'target_opt_expiry_ts', None) is not None:
                target_opts = opt_df[opt_df['ExpiryDate'] == self.target_opt_expiry_ts]
            else:
                today_ts = pd.Timestamp(get_ist_now().date())
                target_opts = opt_df[opt_df['ExpiryDate'] >= today_ts]

            if target_opts.empty:
                today_ts = pd.Timestamp(get_ist_now().date())
                target_opts = opt_df[opt_df['ExpiryDate'] >= today_ts]
                if target_opts.empty:
                    target_opts = opt_df

            row   = target_opts.sort_values('ExpiryDate').iloc[0]
            token = str(row['Token'])
            tsym  = str(row['TradingSymbol'])
            lp    = 0.0
            if self.api:
                try:
                    q  = self.api.get_quotes(exchange='MCX', token=token)
                    lp = float(q.get('lp', q.get('ltp', 0.0)) or 0.0) if q and isinstance(q, dict) else 0.0
                except Exception:
                    pass
            return {'tsym': tsym, 'lp': lp, 'ls': LOT_SIZE, 'token': token}
        except Exception as e:
            print(f'[ERROR] Failed resolving option {strike} {option_type}: {e}', flush=True)
            return None

    # ── Live LTP for an open leg ──────────────
    def _get_leg_ltp(self, pos: dict, max_age: float = 0.8) -> float:
        now_ts = time.time()
        if now_ts - pos.get('_last_ltp_ts', 0.0) < max_age and pos.get('_last_ltp', 0.0) > 0:
            return pos['_last_ltp']

        token = pos.get('token')
        if token and self.api:
            try:
                q = self.api.get_quotes(exchange='MCX', token=token)
                if q and isinstance(q, dict):
                    for field in ('lp', 'ltp', 'sp1', 'bp1'):
                        raw = q.get(field)
                        if raw is not None:
                            try:
                                val = float(raw)
                                if val > 0:
                                    pos['_last_ltp'] = val
                                    pos['_last_ltp_ts'] = now_ts
                                    return val
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass
        return pos.get('_last_ltp', pos['entry_price'])

    # ── Enter a single leg ────────────────────
    def _enter_leg(self, leg: str, strike: float, side: str = 'SELL',
                   loss_stop_pct: float = DEFAULT_SL_PCT,
                   tsl_pct: float = DEFAULT_TSL_PCT) -> Optional[dict]:

        option_type = 'CE' if leg == 'CE' else 'PE'
        match = self.find_option_symbol(strike, option_type)
        if not match:
            print(f'[WARN] Could not resolve contract for {leg} Strike {strike}.', flush=True)
            return None

        tsym = match['tsym']
        ltp  = float(match.get('lp', 0.0))
        if ltp <= 0:
            print(f'[WARN] LTP is 0 for {tsym}. Skipping entry.', flush=True)
            return None

        qty        = LOT_SIZE
        initial_sl = round_to_tick(ltp * (1.0 + loss_stop_pct))
        now_ts     = time.time()

        pos = {
            'leg':           leg,
            'tsym':          tsym,
            'token':         match.get('token', ''),
            'strike':        strike,
            'side':          side,
            'qty':           qty,
            'entry_price':   ltp,
            '_last_ltp':     ltp,
            '_last_ltp_ts':  now_ts,
            'loss_stop_pct': loss_stop_pct,
            'tsl_pct':       tsl_pct,
            'sl_state': {
                'lowest_ltp':    ltp,
                'current_sl':    initial_sl,
                'initial_sl':    initial_sl,
                'loss_stop_pct': loss_stop_pct,
                'tsl_pct':       tsl_pct,
            }
        }
        self.positions[leg] = pos
        self.trades_today  += 1
        self._save_state()

        tg = '\n'.join([
            '<pre>',
            '━━━ MCX TRADE OPENED (v5.0) ━━━',
            '',
            f'  {leg:<4} {int(strike):<5} {side} @ {ltp:.2f}',
            f'  SL  {loss_stop_pct*100:.0f}%  →  {initial_sl:.2f}',
            f'  Qty {qty}',
            '',
            f'  {tsym}',
            '</pre>',
        ])
        print(f'[PAPER ENTRY] {side} {qty}x {leg} Strike {int(strike)} ({tsym}) @ ₹{ltp:.2f}', flush=True)
        send_telegram(tg)
        return pos

    # ── Close a single leg ────────────────────
    def _close_leg(self, leg: str, reason: str, exit_price: Optional[float] = None):
        pos = self.positions.get(leg)
        if not pos:
            return
        if exit_price is not None and exit_price > 0:
            ltp = exit_price
        else:
            ltp = self._get_leg_ltp(pos)
        trade_side = 'BUY' if pos['side'] == 'SELL' else 'SELL'
        pnl        = (pos['entry_price'] - ltp) * pos['qty'] if pos['side'] == 'SELL' \
                     else (ltp - pos['entry_price']) * pos['qty']

        self.total_realized_pnl += pnl
        sign     = '+' if pnl >= 0 else ''
        tot_sign = '+' if self.total_realized_pnl >= 0 else ''

        tg = '\n'.join([
            '<pre>',
            '━━━ MCX TRADE CLOSED (v5.0) ━━━',
            '',
            f'  {leg:<4} {int(pos["strike"]):<5} {reason}',
            f'  Entry  {pos["entry_price"]:.2f}',
            f'  Exit   {ltp:.2f}',
            f'  PnL    {sign}₹{pnl:,.2f}',
            '',
            f'  Total Realized: {tot_sign}₹{self.total_realized_pnl:,.2f}',
            '</pre>',
        ])
        tsym = pos.get('tsym', leg)
        print(f'[PAPER EXIT] {trade_side} {pos["qty"]}x {tsym} @ ₹{ltp:.2f} '
              f'| PnL: {sign}₹{pnl:,.2f} | {reason}', flush=True)
        send_telegram(tg)
        del self.positions[leg]
        if len(self.positions) == 1:
            self._extreme_spot = 0.0
        self.last_any_close_ts = time.time()
        self._save_state()

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)

    def _rebalance_strangle_in_place(self, solo_leg: str, spot: float, atm: float, live_ltp: float) -> bool:
        """
        Smart In-Place Strangle Rebalance for MCX Natural Gas:
        When a solo surviving leg hits its TSL and the target strangle strike is identical to
        its current strike (atm == strike), we do NOT exit and immediately re-enter this leg.
        Instead:
        1. Enter ONLY the missing leg at ATM.
        2. Lock in the solo run's accrued profit into self.total_realized_pnl.
        3. Reset the leg's entry_price to current live_ltp and SL to a fresh 15% strangle SL.
        4. Saves 2 unnecessary market orders, bid-ask spreads, and slippage!
        """
        pos = self.positions.get(solo_leg)
        if not pos:
            return False

        other_leg = 'PE' if solo_leg == 'CE' else 'CE'
        other_strike = atm

        print(f'[IN-PLACE REBALANCE] {solo_leg} {int(pos["strike"])} TSL reached. Target is ATM Straddle at {int(atm)}. '
              f'Preserving {solo_leg} in-place to avoid exit+entry slippage!', flush=True)

        # 1. Enter ONLY the missing leg at ATM
        other_pos = self._enter_leg(other_leg, other_strike, 'SELL', loss_stop_pct=DEFAULT_SL_PCT)
        if not other_pos:
            print(f'[WARN] Failed to enter {other_leg} at {other_strike}, falling back to leg close.', flush=True)
            return False

        # 2. Lock in accrued profit for solo_leg & update entry price
        old_entry = pos['entry_price']
        run_pnl = (old_entry - live_ltp) * pos['qty']
        self.total_realized_pnl += run_pnl
        sign = '+' if run_pnl >= 0 else ''
        tot_sign = '+' if self.total_realized_pnl >= 0 else ''

        # 3. Update entry_price to current live_ltp and refresh SL/TSL in-place
        pos['entry_price'] = live_ltp
        pos['_last_ltp'] = live_ltp
        pos['loss_stop_pct'] = DEFAULT_SL_PCT
        pos['tsl_pct'] = DEFAULT_TSL_PCT
        fresh_sl = round_to_tick(live_ltp * (1.0 + DEFAULT_SL_PCT))
        pos['sl_state'] = {
            'lowest_ltp': live_ltp,
            'current_sl': fresh_sl,
            'initial_sl': fresh_sl,
            'loss_stop_pct': DEFAULT_SL_PCT,
            'tsl_pct': DEFAULT_TSL_PCT,
            'solo_mode': False
        }

        self.last_reentry_ts = time.time()
        self._consume_reversal()
        self._save_state()

        # 4. Telegram alert
        tg = '\n'.join([
            '<pre>',
            '━━━ MCX IN-PLACE RECALIBRATION (v5.0) ━━━',
            '',
            f'  Preserved Open: {solo_leg} {int(pos["strike"])} (Entry: {old_entry:.2f} -> {live_ltp:.2f})',
            f'  Locked Profit: {sign}₹{run_pnl:,.2f}',
            f'  Entered: {other_leg} {int(other_strike)} SELL',
            f'  SL Reset: 15% (₹{fresh_sl:.2f})',
            f'  Total Realized: {tot_sign}₹{self.total_realized_pnl:,.2f}',
            '',
            '  *Entry price updated • Zero exit/entry slippage*',
            '</pre>'
        ])
        print(f'[RECALIBRATE ROLL] {solo_leg} {int(pos["strike"])} (Entry: {old_entry:.2f} -> {live_ltp:.2f}) | Locked: {sign}₹{run_pnl:,.2f} | Reset SL: ₹{fresh_sl:.2f}', flush=True)
        send_telegram(tg)
        return True

    # ── Update SL/TSL for a single leg ────────
    def _update_leg(self, leg: str, live_ltp: float) -> Tuple[bool, str]:
        pos = self.positions.get(leg)
        if not pos or pos['side'] != 'SELL' or live_ltp <= 0:
            return False, ''

        state       = pos['sl_state']
        entry_prem  = pos['entry_price']
        lowest      = float(state.get('lowest_ltp', entry_prem))
        if live_ltp < lowest:
            lowest = live_ltp
            state['lowest_ltp'] = round(lowest, 2)

        is_strangle = ('CE' in self.positions and 'PE' in self.positions)
        initial_sl  = round_to_tick(entry_prem * (1.0 + pos['loss_stop_pct']))

        if is_strangle:
            # ── STRANGLE IS ON (both legs open) ──
            state['solo_mode'] = False
            if lowest >= entry_prem:
                target_sl = initial_sl
            else:
                trail_sl  = round_to_tick(lowest * (1.0 + pos['tsl_pct']))
                target_sl = min(trail_sl, initial_sl)

            current_sl = min(target_sl, state.get('current_sl', initial_sl))
            state['current_sl'] = current_sl

            if live_ltp >= current_sl:
                label = 'TSL Hit' if current_sl < initial_sl else 'SL Hit'
                return True, f'{label} on {leg} ({live_ltp:.2f} >= {current_sl:.2f})'
        else:
            # ── STRANGLE IS OFF (Solo surviving leg) ──
            state['solo_mode'] = True
            solo_tsl = round_to_tick(lowest * (1.0 + pos['tsl_pct']))

            if 'current_sl' in state:
                current_sl = min(solo_tsl, state['current_sl'])
            else:
                current_sl = solo_tsl
            state['current_sl'] = current_sl

            if live_ltp >= current_sl:
                return True, f'Solo TSL Hit on {leg} ({live_ltp:.2f} >= {current_sl:.2f})'

        return False, ''

    # ── Proactive Early Exit (NIFTY 50 logic ported) ───
    def _check_proactive_exit(self, confirmed_signal: int) -> Optional[Tuple[str, str]]:
        """
        When both legs are open, if continuous streaming EMA locks strongly against an open leg,
        proactively exit it early before full SL is hit.
        Returns (leg, reason) if triggered.
        """
        if not ('CE' in self.positions and 'PE' in self.positions):
            return None

        # CE short is hurt when market goes strongly UP
        if confirmed_signal == 1 and 'CE' in self.positions:
            ce_ltp = self._get_leg_ltp(self.positions['CE'])
            if ce_ltp > self.positions['CE']['entry_price'] * 1.05:
                return ('CE', 'PROACTIVE_EXIT_CE[EMA_BULLISH_LOCKED]')

        # PE short is hurt when market goes strongly DOWN
        if confirmed_signal == -1 and 'PE' in self.positions:
            pe_ltp = self._get_leg_ltp(self.positions['PE'])
            if pe_ltp > self.positions['PE']['entry_price'] * 1.05:
                return ('PE', 'PROACTIVE_EXIT_PE[EMA_BEARISH_LOCKED]')

        return None

    # ── Momentum reversal & extreme tracking ──
    def _update_reversal_tracker(self, spot: float, confirmed_signal: int):
        self._spot_history.append(spot)
        short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']
        if len(short_legs) != 1:
            self._extreme_spot = 0.0
            self._reversal_pullback = 0.0
            return

        surviving_leg = short_legs[0]
        if self._extreme_spot <= 0:
            self._extreme_spot = spot

        # Surviving leg is PE (Call was hit on bull rally). We look for rally reversal to re-enter Call.
        if surviving_leg == 'PE':
            if spot > self._extreme_spot:
                self._extreme_spot = spot
            pullback = self._extreme_spot - spot
            self._reversal_pullback = pullback

            # Reversal: EMA confirmed flat/bearish (sig <= 0) OR significant swing pullback >= 0.80 pts
            if confirmed_signal <= 0 or pullback >= SWING_REVERSAL_PTS:
                self._reversal_latched = True

        # Surviving leg is CE (Put was hit on bear dump). We look for dump reversal to re-enter Put.
        elif surviving_leg == 'CE':
            if spot < self._extreme_spot:
                self._extreme_spot = spot
            pullback = spot - self._extreme_spot
            self._reversal_pullback = pullback

            # Reversal: EMA confirmed flat/bullish (sig >= 0) OR significant swing bounce >= 0.80 pts
            if confirmed_signal >= 0 or pullback >= SWING_REVERSAL_PTS:
                self._reversal_latched = True

    def _consume_reversal(self):
        self._reversal_latched = False

    # ── Unified Dashboard ─────────────────────
    def _render_dashboard(self, spot: float, atm: float, ema_snap: Dict[str, Any]):
        global _last_tg_dash_msg_ids, _last_tg_dash_new_msg_ts, _last_tg_dash_edit_ts, _tg_rate_limited_until

        now    = get_ist_now()
        now_ts = time.time()

        snap_rows = []
        total_unreal = 0.0
        for leg, pos in list(self.positions.items()):
            ltp = self._get_leg_ltp(pos)
            is_short = (pos['side'] == 'SELL')
            pnl = ((pos['entry_price'] - ltp) if is_short else (ltp - pos['entry_price'])) * pos['qty']
            total_unreal += pnl
            sl = pos.get('sl_state', {}).get('current_sl', 0.0)
            best = pos.get('sl_state', {}).get('lowest_ltp', pos['entry_price'])
            snap_rows.append({
                'leg': leg,
                'strike': pos['strike'],
                'side': pos['side'],
                'entry': pos['entry_price'],
                'best': best,
                'ltp': ltp,
                'sl': sl,
                'pnl': pnl,
                'qty': pos['qty'],
                'tsym': pos.get('tsym', ''),
                'solo_mode': pos.get('sl_state', {}).get('solo_mode', False)
            })

        net = self.total_realized_pnl + total_unreal

        # ── Console Dashboard (every 1 second) ─
        if now_ts - self._last_console_dash_ts >= 1.0:
            self._last_console_dash_ts = now_ts

            W   = 118
            DIM = f'{Fore.WHITE}{Style.DIM}'
            CY  = f'{Fore.CYAN}{Style.BRIGHT}'
            WH  = f'{Fore.WHITE}{Style.BRIGHT}'
            YL  = f'{Fore.YELLOW}{Style.BRIGHT}'
            GR  = f'{Fore.GREEN}{Style.BRIGHT}'
            RD  = f'{Fore.RED}{Style.BRIGHT}'
            MG  = f'{Fore.MAGENTA}{Style.BRIGHT}'
            RS  = Style.RESET_ALL

            TOP   = f'{DIM}╔{"═"*W}╗{RS}'
            BOT   = f'{DIM}╚{"═"*W}╝{RS}'
            MID   = f'{DIM}╠{"═"*W}╣{RS}'
            MIDS  = f'{DIM}╟{"─"*W}╢{RS}'
            V     = f'{DIM}║{RS}'
            VS    = f'{DIM}│{RS}'

            # Momentum metrics
            ema15 = ema_snap.get("ema_15", spot)
            ema90 = ema_snap.get("ema_90", spot)
            slope = ema_snap.get("slow_slope", 0.0)
            vr    = ema_snap.get("vr", 1.0)
            preq  = ema_snap.get("persistence_req", 10.0)
            sig   = ema_snap.get("confirmed_signal", 0)
            hold  = ema_snap.get("hold_time", 0.0)

            sig_str = f"{GR}▲ UP{RS}" if sig > 0 else (f"{RD}▼ DOWN{RS}" if sig < 0 else f"{YL}━ FLAT{RS}")
            reversal_tag = f"  {MG}[REVERSAL LATCHED]{RS}" if self._reversal_latched else ""
            cooldown_left = max(0.0, POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))
            cooldown_tag = f"  {YL}[COOLDOWN {cooldown_left:.0f}s]{RS}" if cooldown_left > 0 else ""

            print()
            print(TOP)
            title_l = (f'  {CY}MCX NATGAS PAPER v5.0{RS}  {DIM}│{RS}  '
                       f'{YL}STREAMING EMA MOMENTUM + STRADDLE{RS}  {DIM}│{RS}  '
                       f'{GR}tail -f mcx.log{RS}')
            title_r = f'{DIM}{now.strftime("%H:%M:%S IST")}{RS}  '
            pad_top = max(1, W - _ansi_len(title_l) - _ansi_len(title_r))
            print(f'{V}{title_l}{" " * pad_top}{title_r}{V}')

            print(MID)
            exp_badge = f"{YL}{self.target_opt_expiry_str}{RS} ({CY}ROLLOVER{RS})" if self.is_rolled_over else f"{WH}{self.target_opt_expiry_str}{RS}"
            ind_row = (f'  {DIM}SPOT:{RS} {WH}{spot:>8.2f}{RS}  '
                       f'{DIM}ATM:{RS} {YL}{int(atm):<5}{RS}  '
                       f'{DIM}EXPIRY:{RS} {exp_badge}  '
                       f'{DIM}TRADES:{RS} {WH}{self.trades_today}{RS}{reversal_tag}{cooldown_tag}')
            print(f'{V}{_pad(ind_row, W)}{V}')

            print(MIDS)
            mom_row = (f'  {CY}MOMENTUM:{RS} {DIM}EMA15:{RS} {WH}{ema15:>8.2f}{RS}  '
                       f'{DIM}EMA90:{RS} {WH}{ema90:>8.2f}{RS}  '
                       f'{DIM}SLOPE:{RS} {WH}{slope:>+6.3f}{RS}  '
                       f'{DIM}VR:{RS} {WH}{vr:>4.2f}{RS}  '
                       f'{DIM}PERSIST:{RS} {YL}{preq:>4.1f}s{RS}  '
                       f'{DIM}SIGNAL:{RS} {sig_str} {DIM}({hold:.1f}s){RS}')
            print(f'{V}{_pad(mom_row, W)}{V}')
            print(MID)

            # Position table
            if not snap_rows:
                msg = f'  {YL}No open positions — evaluating momentum for entry...{RS}'
                print(f'{V}{_pad(msg, W)}{V}')
            else:
                hdr = (f'  {"LEG":<6} {VS} {"CONTRACT":<22} {VS} {"STRIKE":>7} {VS} {"SIDE":<5} {VS} '
                       f'{"ENTRY":>8} {VS} {"BEST PREM":>10} {VS} {"LTP":>8} {VS} '
                       f'{"CURR SL":>9} {VS} {"PNL":>14}  ')
                print(f'{V}{_pad(hdr, W)}{V}')
                print(MIDS)

                for r in snap_rows:
                    pnl_sign, pnl_fmt = _fmt_pnl(r['pnl'], width=12)
                    pnl_col  = GR if r['pnl'] > 0 else (RD if r['pnl'] < 0 else YL)
                    side_col = RD if r['side'] == 'SELL' else GR
                    leg_label = f"{r['leg']}*" if r.get('solo_mode') else r['leg']

                    row = (f"  {WH}{leg_label:<6}{RS} {VS} {CY}{r['tsym']:<22}{RS} {VS} {WH}{int(r['strike']):>7}{RS} {VS} "
                           f"{side_col}{r['side']:<5}{RS} {VS} "
                           f"{WH}{r['entry']:>8.2f}{RS} {VS} "
                           f"{DIM}{r['best']:>10.2f}{RS} {VS} "
                           f"{YL}{r['ltp']:>8.2f}{RS} {VS} "
                           f"{MG}{r['sl']:>9.2f}{RS} {VS} "
                           f"  {pnl_col}{pnl_fmt}{RS}  ")
                    print(f'{V}{_pad(row, W)}{V}')

                if any(r.get('solo_mode') for r in snap_rows):
                    print(MIDS)
                    solo_msg = f"  {CY}🎯 SOLO TSL ACTIVE (*):{RS} Strangle OFF — trailing strictly at {DEFAULT_TSL_PCT*100:.0f}% TSL"
                    print(f'{V}{_pad(solo_msg, W)}{V}')

            print(MID)
            real_sign, real_fmt = _fmt_pnl(self.total_realized_pnl, width=10)
            unreal_sign, unreal_fmt = _fmt_pnl(total_unreal, width=10)
            net_sign, net_fmt = _fmt_pnl(net, width=10)

            real_col   = GR if self.total_realized_pnl > 0 else (RD if self.total_realized_pnl < 0 else YL)
            unreal_col = GR if total_unreal > 0 else (RD if total_unreal < 0 else YL)
            net_col    = GR if net > 0 else (RD if net < 0 else YL)

            net_pct = (net / 200_000.0) * 100.0
            pnl_row = (f"  {DIM}REALIZED:{RS} {real_col}{real_fmt}{RS}  {VS}  "
                       f"{DIM}UNREALIZED:{RS} {unreal_col}{unreal_fmt}{RS}  {VS}  "
                       f"{DIM}NET MTM:{RS} {net_col}{net_fmt} ({net_pct:+.2f}%){RS}  {VS}  "
                       f"{DIM}TRADES:{RS} {WH}{self.trades_today}{RS}")
            print(f'{V}{_pad(pnl_row, W)}{V}')
            print(BOT)
            sys.stdout.flush()

        # ── Telegram live dashboard (every 3s) ─
        if now_ts - _last_tg_dash_edit_ts >= 3.0:
            if now_ts < _tg_rate_limited_until:
                return
            _last_tg_dash_edit_ts = now_ts

            ema15 = ema_snap.get("ema_15", spot)
            ema90 = ema_snap.get("ema_90", spot)
            slope = ema_snap.get("slow_slope", 0.0)
            vr    = ema_snap.get("vr", 1.0)
            sig   = ema_snap.get("confirmed_signal", 0)
            sig_txt = "▲ UP" if sig > 0 else ("▼ DOWN" if sig < 0 else "━ FLAT")

            has_ce = 'CE' in self.positions
            has_pe = 'PE' in self.positions
            if has_ce and has_pe:
                status_str = "🛡️ STRANGLE ACTIVE"
            elif any(r.get('solo_mode') for r in snap_rows):
                status_str = "🎯 SOLO TRAILING"
            elif has_ce or has_pe:
                status_str = "🎯 1-LEG ACTIVE"
            elif (now_ts - self.last_any_close_ts) < POST_CLOSE_COOLDOWN:
                rem_cd = int(POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))
                status_str = f"⏳ COOLDOWN ({rem_cd}s)"
            else:
                status_str = "⚙️ SCANNING"

            # ── Redesigned clean Telegram dashboard ──────────────────────────────
            roll_tag = " ⟳NEXT MO" if self.is_rolled_over else ""
            r_sign = '+' if self.total_realized_pnl >= 0 else ''
            u_sign = '+' if total_unreal >= 0 else ''
            n_sign = '+' if net >= 0 else ''
            net_pct_tg = (net / 200_000.0) * 100.0
            pnl_badge  = "🟢" if net >= 0 else "🔴"
            rev_tag    = " 🔄REVERSAL" if self._reversal_latched else ""
            cd_tag     = f" ⏳{int(POST_CLOSE_COOLDOWN-(now_ts-self.last_any_close_ts))}s" if (now_ts - self.last_any_close_ts) < POST_CLOSE_COOLDOWN else ""

            t  = f"<b>⚡ MCX NATGAS · PAPER TRADING</b>\n"
            t += f"<code>🕐 {now.strftime('%H:%M:%S IST')}</code>\n"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += f"<b>SPOT:</b>   <code>{spot:>8.2f}</code>  <b>ATM:</b> <code>{int(atm)}</code>\n"
            t += f"<b>EXPIRY:</b> <code>{self.target_opt_expiry_str}{roll_tag}</code>\n"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += f"<b>EMA MOMENTUM ENGINE</b>\n"
            t += f"<pre>"
            t += f"  EMA15 : {ema15:>8.2f}\n"
            t += f"  EMA90 : {ema90:>8.2f}\n"
            t += f"  SLOPE : {slope:>+8.3f}\n"
            t += f"  VR    : {vr:>8.2f}  P_REQ: {ema_snap.get('persistence_req',10.0):.1f}s\n"
            t += f"  SIGNAL: {sig_txt:>8}  HOLD:  {ema_snap.get('hold_time',0.0):.1f}s\n"
            t += f"</pre>"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += f"<b>STATUS:</b> {status_str}{rev_tag}{cd_tag}  <b>TRADES:</b> <code>{self.trades_today}</code>\n"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += "<pre>"
            t += f"{'LEG':<6} {'STRIKE':>7} {'ENTRY':>7} {'LTP':>7} {'SL':>8} {'PnL':>10}\n"
            t += f"{'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*8} {'─'*10}\n"
            if snap_rows:
                for r in snap_rows:
                    pnl_sign = '+' if r['pnl'] >= 0 else ''
                    sl_val = r.get('sl', 0.0)
                    sl_str = f"{sl_val:>8.2f}" if sl_val > 0 else "       —"
                    leg_tag = f"{r['leg']}*" if r.get('solo_mode') else r['leg']
                    t += f"{leg_tag:<6} {int(r['strike']):>7} {r['entry']:>7.2f} {r['ltp']:>7.2f} {sl_str} {pnl_sign}{r['pnl']:>9,.0f}\n"
            else:
                t += "  — No Open Positions —\n"
            t += f"{'─'*48}\n"
            t += f"{'Realized':>16}: {r_sign}₹{self.total_realized_pnl:>10,.2f}\n"
            t += f"{'Unrealized':>16}: {u_sign}₹{total_unreal:>10,.2f}\n"
            t += f"{'─'*48}\n"
            t += f"{'NET MTM':>16}: {pnl_badge}{n_sign}₹{net:>9,.2f} ({net_pct_tg:+.2f}%)\n"
            t += "</pre>"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += f"<b>MODE:</b> <code>PAPER</code>  <b>LOT:</b> <code>1×{LOT_SIZE}u</code>  <b>SL:</b> <code>{DEFAULT_SL_PCT*100:.0f}%</code>  <b>TSL:</b> <code>{DEFAULT_TSL_PCT*100:.0f}%</code>"

            chat_ids   = _get_tg_chat_ids()
            # Refresh every 15 seconds (new message) or edit in-place
            is_refresh = (now_ts - _last_tg_dash_new_msg_ts) >= 15.0

            for cid in chat_ids:
                msg_id = _last_tg_dash_msg_ids.get(cid)
                edited = False
                if msg_id is not None and not is_refresh:
                    try:
                        r = requests.post(
                            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText',
                            json={'chat_id': cid, 'message_id': msg_id, 'text': t, 'parse_mode': 'HTML'},
                            timeout=3)
                        if r.status_code == 200 and r.json().get('ok'):
                            edited = True
                        elif r.status_code == 400 and 'message is not modified' in r.text:
                            edited = True
                        elif r.status_code == 429:
                            _tg_rate_limited_until = time.time() + r.json().get('parameters', {}).get('retry_after', 30)
                            return
                    except Exception:
                        pass

                if not edited:
                    try:
                        r = requests.post(
                            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                            data={'chat_id': cid, 'text': t, 'parse_mode': 'HTML'},
                            timeout=4)
                        if r.status_code == 200 and r.json().get('ok'):
                            _last_tg_dash_msg_ids[cid] = r.json().get('result', {}).get('message_id')
                            _last_tg_dash_new_msg_ts   = now_ts
                        elif r.status_code == 429:
                            _tg_rate_limited_until = time.time() + r.json().get('parameters', {}).get('retry_after', 30)
                            return
                    except Exception:
                        pass

            if is_refresh:
                _last_tg_dash_new_msg_ts = now_ts


    # ── Main run loop ─────────────────────────
    def run(self):
        self.authenticate()
        self._get_mcx_csv()

        DIM = f'{Fore.WHITE}{Style.DIM}'
        CY  = f'{Fore.CYAN}{Style.BRIGHT}'
        RS  = Style.RESET_ALL
        print()
        print(f'{DIM}{"="*98}{RS}')
        print(f'{CY}  MCX NATURAL GAS PAPER TRADING BOT  v5.0  |  15:30 – 23:24 IST (EMA ENGINE){RS}')
        print(f'{DIM}{"="*98}{RS}')
        print(flush=True)

        exp_note = f"\nTarget Expiry: {self.target_opt_expiry_str}" + (" (Next Month Rollover Active)" if self.is_rolled_over else "")
        send_telegram(f'<pre>MCX Natural Gas\nPaper Trading Bot Online (v5.0 EMA Engine)\nSession: 15:30 – 23:24 IST{exp_note}</pre>')

        last_wait_msg_ts = 0.0

        while True:
            try:
                now    = get_ist_now()
                now_ts = time.time()

                # ── Session guards ──────────────────────────
                if now.weekday() == 6:                              # Sunday
                    print(f'[{now.strftime("%H:%M:%S")}] Sunday — markets closed.', flush=True)
                    time.sleep(60)
                    continue

                if now.weekday() == 5 and now.hour >= 17:          # Saturday after 17:00
                    print(f'[{now.strftime("%H:%M:%S")}] Saturday 17:00+ — MCX closed.', flush=True)
                    self._close_all('SATURDAY_CLOSE')
                    time.sleep(60)
                    continue

                if now.hour > MCX_EXIT_HOUR or (now.hour == MCX_EXIT_HOUR and now.minute >= MCX_EXIT_MINUTE):
                    print(f'[AUTO] {MCX_EXIT_HOUR}:{MCX_EXIT_MINUTE:02d} IST — squaring off all positions...', flush=True)
                    self._close_all('SESSION_END')
                    self.positions.clear()
                    self._render_dashboard(spot, atm, ema_snap)
                    final_pct = (self.total_realized_pnl / 200_000.0) * 100.0
                    pnl_col = GR if self.total_realized_pnl >= 0 else RD
                    sign = '+' if self.total_realized_pnl >= 0 else ''
                    print(f'\n{pnl_col}✅ Session Completed Successfully. Final Realized PnL: {sign}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%){RS}\n', flush=True)
                    send_telegram(
                        f'<pre>MCX Session Complete (v5.0)\n'
                        f'Final Realized PnL: {sign}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%)\n'
                        f'Trades Today:       {self.trades_today}</pre>')
                    break

                if now.hour < MCX_ENTRY_HOUR or (now.hour == MCX_ENTRY_HOUR and now.minute < MCX_ENTRY_MINUTE):
                    if now_ts - last_wait_msg_ts > 60.0:
                        last_wait_msg_ts = now_ts
                        print(f'[WAIT] Market opens at {MCX_ENTRY_HOUR}:{MCX_ENTRY_MINUTE:02d} IST. '
                              f'Current: {now.strftime("%H:%M:%S")}', flush=True)
                    time.sleep(10)
                    continue

                # ── Spot & ATM Ingestion ────────────────────
                spot = self.get_spot()
                if spot <= 50.0:
                    time.sleep(3)
                    continue
                atm = round_to_price(spot, STRIKE_STEP)

                # ── Continuous Streaming EMA Update ─────────
                ema_snap = self.ema_engine.update(spot, now_ts)
                confirmed_sig = ema_snap.get("confirmed_signal", 0)

                # ── Momentum Reversal Tracking ──────────────
                self._update_reversal_tracker(spot, confirmed_sig)

                # ── STEP 1: NO POSITIONS → MOMENTUM ENTRY ───
                if not self.positions:
                    time_since_close = now_ts - self.last_any_close_ts
                    if time_since_close < POST_CLOSE_COOLDOWN:
                        pass  # Brief cooldown (5s) after close
                    else:
                        if confirmed_sig == 1:
                            print(f'[MOMENTUM ENTRY] Bullish trend confirmed (+1). Writing PE at ATM {int(atm)} (CE deferred)...', flush=True)
                            self._enter_leg('PE', atm, 'SELL')
                        elif confirmed_sig == -1:
                            print(f'[MOMENTUM ENTRY] Bearish trend confirmed (-1). Writing CE at ATM {int(atm)} (PE deferred)...', flush=True)
                            self._enter_leg('CE', atm, 'SELL')
                        else:
                            print(f'[INIT ENTRY] Neutral / Flat market (signal 0). Writing balanced ATM Straddle at {int(atm)}...', flush=True)
                            self._enter_leg('CE', atm, 'SELL')
                            self._enter_leg('PE', atm, 'SELL')
                        self._consume_reversal()

                    self._render_dashboard(spot, atm, ema_snap)
                    time.sleep(1.0)
                    continue

                # ── STEP 2: PROACTIVE EARLY EXIT (RUNAWAY TREND) ──
                # If market locks in runaway trend against an open leg, exit early before full SL
                proactive = self._check_proactive_exit(confirmed_sig)
                if proactive:
                    p_leg, p_reason = proactive
                    print(f'[PROACTIVE ALERT] {p_reason}. Exiting {p_leg} early to cut loss.', flush=True)
                    self._close_leg(p_leg, p_reason)

                # ── STEP 3: 1 LEG OPEN → MOMENTUM RE-ENTRY ───
                short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']

                if len(short_legs) == 1 and self._reversal_latched:
                    time_since_reentry = now_ts - self.last_reentry_ts
                    if time_since_reentry >= REENTRY_COOLDOWN_S:
                        surviving_leg    = short_legs[0]
                        surviving_strike = self.positions[surviving_leg]['strike']
                        missing_leg      = 'CE' if surviving_leg == 'PE' else 'PE'

                        dist = min(abs(surviving_strike - atm), 25.0)
                        if dist < STRIKE_STEP:
                            reentry_strike = atm
                        else:
                            reentry_strike = round_to_price(
                                atm + dist if missing_leg == 'CE' else atm - dist, STRIKE_STEP)

                        print(f'[REENTRY] Momentum reversal confirmed! '
                              f'Re-entering {missing_leg} at {int(reentry_strike)} '
                              f'(Surviving: {surviving_leg} {int(surviving_strike)}) '
                              f'with {REENTRY_SL_PCT*100:.0f}% instant SL', flush=True)

                        if self._enter_leg(missing_leg, reentry_strike, 'SELL', loss_stop_pct=REENTRY_SL_PCT):
                            self.last_reentry_ts = now_ts
                            self._consume_reversal()

                            # Reshape surviving leg & update entry price to current LTP
                            surv_pos = self.positions.get(surviving_leg)
                            if surv_pos:
                                surv_ltp = self._get_leg_ltp(surv_pos)
                                surv_old_entry = surv_pos.get('entry_price', surv_ltp)
                                surv_run_pnl = (surv_old_entry - surv_ltp) * surv_pos['qty']
                                self.total_realized_pnl += surv_run_pnl
                                surv_pos['entry_price'] = surv_ltp
                                surv_pos['_last_ltp'] = surv_ltp
                                surv_pos['loss_stop_pct'] = DEFAULT_SL_PCT
                                surv_pos['tsl_pct'] = DEFAULT_TSL_PCT
                                fresh_surv_sl = round_to_tick(surv_ltp * (1.0 + DEFAULT_SL_PCT))
                                surv_pos['sl_state'] = {
                                    'lowest_ltp': surv_ltp,
                                    'current_sl': fresh_surv_sl,
                                    'initial_sl': fresh_surv_sl,
                                    'loss_stop_pct': DEFAULT_SL_PCT,
                                    'tsl_pct': DEFAULT_TSL_PCT,
                                    'solo_mode': False
                                }
                                print(f'[RESHAPE SURVIVOR] {surviving_leg} {int(surviving_strike)} entry reset {surv_old_entry:.2f} -> {surv_ltp:.2f} | '
                                      f'Locked PnL: ₹{surv_run_pnl:,.2f} | Fresh SL: ₹{fresh_surv_sl:.2f}', flush=True)
                                self._save_state()

                # ── STEP 4: CHECK TSL/SL FOR ALL OPEN LEGS ───
                legs_to_close: List[Tuple[str, str, float]] = []
                for leg in list(self.positions.keys()):
                    pos      = self.positions.get(leg)
                    if not pos or pos['side'] != 'SELL':
                        continue
                    live_ltp = self._get_leg_ltp(pos)
                    hit, reason = self._update_leg(leg, live_ltp)
                    if hit:
                        # ── Smart In-Place Strangle Rebalance Check ──
                        other_leg = 'PE' if leg == 'CE' else 'CE'
                        other_open = (other_leg in self.positions and self.positions[other_leg].get('side') == 'SELL')
                        if not other_open and pos['strike'] == atm:
                            ema_against = (confirmed_sig == 1 and leg == 'CE') or (confirmed_sig == -1 and leg == 'PE')
                            if not ema_against:
                                if self._rebalance_strangle_in_place(leg, spot, atm, live_ltp):
                                    continue  # Successfully rebalanced in-place! Skip physical exit.
                        legs_to_close.append((leg, reason, live_ltp))

                for leg, reason, exit_px in legs_to_close:
                    print(f'[ALERT] {reason}', flush=True)
                    self._close_leg(leg, reason, exit_price=exit_px)

                # ── STEP 5: Render Dashboard ─────────────────
                self._render_dashboard(spot, atm, ema_snap)
                time.sleep(1.0)

            except KeyboardInterrupt:
                print('\n[STOP] KeyboardInterrupt — squaring off all positions...', flush=True)
                self._close_all('KEYBOARD_INTERRUPT')
                self.positions.clear()
                self._render_dashboard(spot, atm, ema_snap)
                final_pct = (self.total_realized_pnl / 200_000.0) * 100.0
                sign = '+' if self.total_realized_pnl >= 0 else ''
                pnl_col = GR if self.total_realized_pnl >= 0 else RD
                print(f'\n{pnl_col}✅ All positions squared off. Final Realized PnL: {sign}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%){RS}\n', flush=True)
                break
            except Exception as e:
                print(f'[ERROR] Loop exception: {e}', flush=True)
                time.sleep(3.0)


# ─────────────────────────────────────────────
if __name__ == '__main__':
    try:
        bot = NaturalGasPaperBot()
        bot.run()
    except Exception as e:
        print(f'[FATAL] {e}', flush=True)
        send_telegram(f'<pre>MCX Bot Fatal Error (v5.0):\n{e}</pre>')
        sys.exit(1)
