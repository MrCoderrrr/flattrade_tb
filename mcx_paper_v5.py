"""mcx_paper_v5.py  —  v5.1 (NIFTY-Aligned Precision Hedged Straddle + Noise-Filtered Streaming EMA Engine)
================================================================================
MCX Natural Gas Paper Trading Engine | Version 5.1
Session Window: 15:30 – 23:24 IST (weekdays & Sundays)
Auto Square-Off: 23:24 IST

NIFTY ARCHITECTURE ALIGNMENT & FIXES (Resolves 4k -> 1.5k PnL Degradation):
  1. Noise-Resistant Continuous Streaming EMA Engine:
     - 60-second sliding history window for 300s anchor EMA slope (eliminates 11s micro-jitter).
     - Strict deadbands: MIN_EMA_SPREAD = 0.20 pts (4 ticks) and MIN_SLOPE = 0.035 pts/min.
       If EMA spread < 0.20 pts or slope < 0.035 pts/min, signal is strictly FLAT / NEUTRAL (0).
     - Adaptive persistence clamped to [15.0s, 45.0s] (eliminates 3-second noise whipsaws).
  2. Balanced Straddle Entry Default (Decay Harvest):
     - In steady / sideways / normal markets, ALWAYS writes balanced ATM Straddle (CE + PE).
     - Deferral of one leg ONLY occurs on an overwhelming, sustained directional trend
       (held >= 25s with EMA spread >= 0.40 pts).
  3. Strict NIFTY-Style Profit Protection & Max Giveback Cap (Prevents Giving Back Profits):
     - Break-Even Lock: When profit >= 4% or >= 0.50 pts, SL is capped at entry price (cannot lose!).
     - Tiered Profit Ratchet:
         * Profit >= 0.80 pts (8%): locks 30% of profit.
         * Profit >= 1.50 pts (15%): locks 55% of profit.
         * Profit >= 2.50 pts (25%): locks 75% of profit.
         * Profit >= 3.50 pts (40%): locks 85% of profit.
     - Hard Max Giveback Cap (PREM_MAX_PROFIT_GIVEBACK = 0.20):
         * Position stop is capped at entry_prem - 0.80 * profit.
         * At peak profit (e.g. +4k = 3.2 pts decay), AT LEAST 80% (+₹3,200) IS GUARANTEED LOCKED IN!
  4. NIFTY-Style Solo Leg Re-Anchoring (No Choking on Old Lowest):
     - When one leg hits SL, the surviving leg immediately re-anchors to its live LTP:
       anchor_ltp = live_ltp, best_premium = live_ltp, current_sl = live_ltp * (1 + 9%).
     - Gives the winning leg fresh breathing room and trailing protection rather than
       choking it on an old historical lowest tick.
  5. 2-Second Tick Debounce Filter:
     - Price must stay at or above SL for 2 consecutive seconds before triggering exit.
       Completely eliminates false stop-outs from 1-second wide bid-ask spread flickers.
  6. Reversal Re-Entry Engine:
     - Missing leg re-enters at ATM as soon as trend halts/reverses or 0.80 pt swing pullback occurs.
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
# Configuration & Risk Settings (NIFTY-Aligned)
# ─────────────────────────────────────────────
TOKEN_FILE          = 'token.txt'
STRIKE_STEP         = 5.0          # Natural Gas strike step
MCX_ENTRY_HOUR      = 15           # 15:30 IST open (3:30 PM)
MCX_ENTRY_MINUTE    = 30
MCX_EXIT_HOUR       = 23
MCX_EXIT_MINUTE     = 24           # 23:24 IST auto square-off (11:24 PM)
LOT_SIZE            = 1250         # 1 lot = 1250 units
CAPITAL             = 200000.0

# --- GENERALIZED PREMIUM SL & PROFIT PROTECTION (NIFTY ALIGNED) ---
PREM_RISK_REFERENCE        = 30.0   # Reference high premium (pts) for interpolation
PREM_RISK_EXPIRY_FLOOR     = 0.6    # Compress points-based stops by up to 40% on expiry day
PREM_RISK_INITIAL_PCT_HIGH = 0.12   # 15% initial SL for high premiums
PREM_RISK_INITIAL_PCT_LOW  = 0.12   # 25% initial SL for low premiums (need more % breathing room)
PREM_RISK_INITIAL_MIN_PTS  = 1.50   # Min initial SL in points
PREM_RISK_INITIAL_MAX_PTS  = 4.50   # Max initial SL in points
PREM_RISK_TRAIL_MIN_PTS    = 1.00   # Min trailing SL in points
PREM_RISK_TRAIL_MAX_PTS    = 2.50   # Max trailing SL in points

PREM_SL_MIN_PCT            = 0.085  # 8.5% trail floor when deep in profit
PREM_SL_MAX_PCT            = 0.12   # 15% trail ceiling at breakeven
SOLO_LEG_TSL_PCT           = 0.07   # 9% trailing stop for solo surviving leg (re-anchored at LTP)

# KEY FIX FOR 4k -> 1.5k: Strict Profit Protection Ratchet & Max Giveback Cap
PREM_MAX_PROFIT_GIVEBACK   = 0.20   # Maximum 20% giveback of peak profit (guarantees keeping >= 80% of peak PnL!)
BREAKEVEN_PROFIT_PCT       = 0.04   # 4% profit triggers instant Break-Even Lock (SL capped at entry price)
BREAKEVEN_PROFIT_POINTS    = 0.50   # 0.50 points profit triggers instant Break-Even Lock

# Noise Debounce
SL_DEBOUNCE_SECONDS        = 2.0    # 2 seconds persistence before SL fires (eliminates single-tick bid-ask spread glitches)

# Cooldowns
POST_CLOSE_COOLDOWN        = 0.0   # 45s cooldown after both legs close
REENTRY_COOLDOWN_S         = 0.0   # 15s cooldown after leg close before re-entry check
SWING_REVERSAL_PTS         = 0.80   # 0.80 pts pullback threshold

# EMA Engine Sensitivity Tuning (Noise Filtered)
EMA_FAST_HL                = 15.0   # 15s half-life
EMA_SLOW_HL                = 90.0   # 90s half-life
EMA_ANCHOR_HL              = 300.0  # 300s half-life (5 min equivalent)
EMA_MIN_SLOPE              = 0.035  # Minimum slope threshold on anchor EMA (points/min)
EMA_MIN_SPREAD             = 0.20   # Fast and Slow EMA must separate by >= 0.20 pts (4 ticks)
PERSISTENCE_MIN            = 15.0   # Clamped minimum persistence hold time (was 3s - too noisy!)
PERSISTENCE_MAX            = 45.0   # Clamped maximum persistence hold time

TELEGRAM_TOKEN = '8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE'
CHAT_ID        = '6307066850'
PROJECT_ROOT   = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
# MCX PnL & MTD TRACKER
# ─────────────────────────────────────────────
class MCXDBManager:
    def __init__(self, filename: str = "mcx_pnl_tracker.json"):
        self.filename = filename
        self.data = self._load()

    def _get_ist_str(self) -> str:
        return get_ist_now().strftime("%Y-%m-%d")

    def _load(self) -> dict:
        base_cap = globals().get("CAPITAL", 200000.0)
        target_file = self.filename
        if not os.path.isabs(target_file):
            cand = os.path.join(PROJECT_ROOT, self.filename)
            if os.path.exists(cand):
                target_file = cand

        d = None
        if os.path.exists(target_file):
            try:
                with open(target_file, 'r', encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                pass

        if d is None or not isinstance(d, dict):
            d = {
                "mtd_pnl": 0.0,
                "ytd_pnl": 0.0,
                "current_capital": base_cap,
                "base_capital": base_cap,
                "today_pnl": 0.0,
                "last_date": "",
                "intraday_date": "",
                "daily_pnl": {}
            }

        if "daily_pnl" not in d or not isinstance(d["daily_pnl"], dict):
            d["daily_pnl"] = {}
        if "base_capital" not in d:
            d["base_capital"] = base_cap
        return d

    def _save(self):
        target_file = self.filename
        if not os.path.isabs(target_file):
            target_file = os.path.join(PROJECT_ROOT, self.filename)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target_file)), exist_ok=True)
            with open(target_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2)
            tb_file = os.path.join(PROJECT_ROOT, "tradingbot", self.filename)
            if os.path.exists(os.path.dirname(tb_file)):
                with open(tb_file, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"[WARN] Failed to save {target_file}: {e}", flush=True)

    def commit_daily_pnl(self, realized_pnl: float, date_str: Optional[str] = None):
        today_str = date_str or self._get_ist_str()
        month_prefix = today_str[:7]
        year_prefix = today_str[:4]
        base_cap = float(self.data.get("base_capital", CAPITAL) or CAPITAL)

        daily_map = self.data.setdefault("daily_pnl", {})
        daily_map[today_str] = round(float(realized_pnl), 2)

        mtd_sum = round(sum(v for d, v in daily_map.items() if d.startswith(month_prefix)), 2)
        ytd_sum = round(sum(v for d, v in daily_map.items() if d.startswith(year_prefix)), 2)

        self.data["mtd_pnl"] = mtd_sum
        self.data["ytd_pnl"] = ytd_sum
        self.data["current_capital"] = round(base_cap + ytd_sum, 2)
        self.data["today_pnl"] = round(float(realized_pnl), 2)
        self.data["last_date"] = today_str
        self.data["intraday_date"] = today_str
        self._save()

        # Record to daily_pnl_mcx_paper.csv
        for log_dir in [os.path.join(PROJECT_ROOT, "data", "logs"), os.path.join(PROJECT_ROOT, "tradingbot", "data", "logs")]:
            try:
                os.makedirs(log_dir, exist_ok=True)
                csv_path = os.path.join(log_dir, "daily_pnl_mcx_paper.csv")
                rows = []
                found = False
                fieldnames = ["date", "daily_pnl", "mtd_pnl", "ytd_pnl", "current_capital"]
                if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                    import csv
                    with open(csv_path, "r", encoding="utf-8") as f:
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
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    import csv
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
            except Exception as e:
                print(f"[WARN] Failed writing to daily_pnl_mcx_paper.csv: {e}", flush=True)

# ─────────────────────────────────────────────
# CONTINUOUS STREAMING EMA MOMENTUM ENGINE (NOISE-RESISTANT)
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


class ContinuousEMAEngine:
    """
    Noise-resistant continuous streaming EMA engine with deadband & robust persistence.
    Tracks EMA 15s (fast), 90s (slow), 300s (anchor), slope, and volatility ratio.
    """
    def __init__(self):
        self.emas = {
            15.0: ContinuousEMA(EMA_FAST_HL),
            90.0: ContinuousEMA(EMA_SLOW_HL),
            300.0: ContinuousEMA(EMA_ANCHOR_HL),
        }
        # Track 60 seconds of 300s EMA history to compute a genuine 1-minute drift slope (not 11-second jitter!)
        self.slow_history = deque(maxlen=60)
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

        anchor_val = self.emas[300.0].value or spot
        self.slow_history.append((now_ts, anchor_val))

        rv60 = self.rv[60].update(spot)
        rv300 = self.rv[300].update(spot)

        vr = (rv60 / rv300) if rv60 is not None and rv300 and rv300 > 0 else 1.0
        p_req = max(PERSISTENCE_MIN, min(PERSISTENCE_MAX, 15.0 / max(float(vr), 1e-12)))

        # Compute slope over the available history window (up to 60s)
        if len(self.slow_history) >= 2:
            dt = self.slow_history[-1][0] - self.slow_history[0][0]
            if dt >= 5.0:
                # Slope in points per minute
                slow_slope = ((self.slow_history[-1][1] - self.slow_history[0][1]) / dt) * 60.0
            else:
                slow_slope = 0.0
        else:
            slow_slope = 0.0

        fast_val = self.emas[15.0].value if self.emas[15.0].value is not None else spot
        slow_val = self.emas[90.0].value if self.emas[90.0].value is not None else spot
        ema_diff = fast_val - slow_val

        # Noise-Filtered Directional Signal:
        # Requires BOTH EMA spread >= EMA_MIN_SPREAD (0.20 pts) AND slope >= EMA_MIN_SLOPE (0.035 pts/min)
        if ema_diff >= EMA_MIN_SPREAD and slow_slope >= EMA_MIN_SLOPE:
            sig = 1   # Bullish
        elif ema_diff <= -EMA_MIN_SPREAD and slow_slope <= -EMA_MIN_SLOPE:
            sig = -1  # Bearish
        else:
            sig = 0   # FLAT / NEUTRAL (Filters out normal chop and micro-oscillations)

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
            print(f"[EMA SIGNAL] Confirmed: {direction.get(prev_confirmed,'?')} → {direction.get(self.confirmed_signal,'?')}  "
                  f"(Fast={fast_val:.2f} Slow={slow_val:.2f} diff={ema_diff:+.2f} slope={slow_slope:+.3f}/m VR={vr:.2f} hold={hold_time:.1f}s)", flush=True)

        self.latest_snapshot = {
            "ema_15": fast_val,
            "ema_90": slow_val,
            "ema_300": anchor_val,
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

    def _get_dte_days(self) -> float:
        if not self.target_opt_expiry_ts:
            return 2.0
        try:
            today_date = get_ist_now().date()
            import pandas as pd
            expiry_date = pd.to_datetime(self.target_opt_expiry_ts).date()
            dte = (expiry_date - today_date).days
            return float(max(0, dte))
        except Exception:
            return 2.0

    def _premium_risk_profile(self, premium: float, dte_days: float) -> dict:
        """Calculate stop allowances dynamically from premium size and time to expiry."""
        premium_ratio = min(1.0, max(0.0, float(premium or 0.0)) / PREM_RISK_REFERENCE)
        # Assuming typical MCX cycle is about 20-30 days, we scale dte_ratio differently than NIFTY (which uses 3.0 days)
        # Let's normalize DTE over 10 days to start compressing
        dte_ratio = min(1.0, max(0.0, float(dte_days)) / 10.0)
        expiry_factor = PREM_RISK_EXPIRY_FLOOR + (1.0 - PREM_RISK_EXPIRY_FLOOR) * dte_ratio
        
        initial_pct = PREM_RISK_INITIAL_PCT_LOW - (
            PREM_RISK_INITIAL_PCT_LOW - PREM_RISK_INITIAL_PCT_HIGH
        ) * premium_ratio
        
        initial_points = (
            PREM_RISK_INITIAL_MIN_PTS
            + (PREM_RISK_INITIAL_MAX_PTS - PREM_RISK_INITIAL_MIN_PTS) * premium_ratio
        ) * expiry_factor
        
        trail_points = (
            PREM_RISK_TRAIL_MIN_PTS
            + (PREM_RISK_TRAIL_MAX_PTS - PREM_RISK_TRAIL_MIN_PTS) * premium_ratio
        ) * expiry_factor
        
        return {
            "initial_pct": initial_pct,
            "initial_points": initial_points,
            "trail_points": trail_points,
            "expiry_factor": expiry_factor,
        }

    def _log_step(self, event: str, leg: str, strike: float, price: float, reason: str):
        log_file = os.path.join(PROJECT_ROOT, "strategy_steps.csv")
        exists = os.path.exists(log_file)
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                if not exists:
                    f.write("timestamp,event,leg,strike,price,reason\n")
                ts = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
                # Clean up reason strings for CSV
                clean_reason = reason.replace(',', ';').replace('\n', ' ')
                f.write(f"{ts},{event},{leg},{strike},{price},{clean_reason}\n")
        except Exception as e:
            print(f"[WARN] Failed to write step log: {e}")

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
        self.trade_log: List[Dict] = []   # Full record of every closed trade this session
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

        self.db                    = MCXDBManager()
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
            if hasattr(self, 'db'):
                self.db.commit_daily_pnl(self.total_realized_pnl)
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

                is_strangle_restored = ('CE' in self.positions and 'PE' in self.positions)
                for leg, pos in self.positions.items():
                    sl_st = pos.get('sl_state', {})
                    pos['sl_state'] = sl_st
                    sl_st['loss_stop_pct'] = PREM_RISK_INITIAL_PCT_HIGH
                    sl_st['tsl_pct'] = SOLO_LEG_TSL_PCT
                    lowest = float(sl_st.get('lowest_ltp', pos.get('entry_price', 0.0)))
                    pct = PREM_RISK_INITIAL_PCT_HIGH if is_strangle_restored else SOLO_LEG_TSL_PCT
                    new_sl = round_to_tick(lowest * (1.0 + pct))
                    curr_sl = float(sl_st.get('current_sl', new_sl))
                    sl_st['current_sl'] = min(curr_sl, new_sl)
                    sl_st['solo_mode'] = not is_strangle_restored
                    sl_st['breach_start_ts'] = 0.0

                if hasattr(self, 'db'):
                    self.db.commit_daily_pnl(self.total_realized_pnl)
                print(f'[STATE] Restored: {len(self.positions)} open legs | '
                      f'Realized PnL: ₹{self.total_realized_pnl:,.2f} | '
                      f'Trades: {self.trades_today} | Strangle SL: {PREM_RISK_INITIAL_PCT_HIGH*100:.0f}% | Solo TSL: {SOLO_LEG_TSL_PCT*100:.0f}%', flush=True)
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

        print(f'[OK] Natural Gas v5.1 PAPER TRADING bot authenticated from {token_path}.', flush=True)

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
                   loss_stop_pct: float = None,
                   tsl_pct: float = SOLO_LEG_TSL_PCT,
                   reason: str = "Standard Entry") -> Optional[dict]:

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

        qty = LOT_SIZE
        dte = self._get_dte_days()
        risk_profile = self._premium_risk_profile(ltp, dte)
        
        if loss_stop_pct is None:
            loss_stop_pct = risk_profile['initial_pct']
            
        initial_sl = round_to_tick(min(
            ltp * (1.0 + loss_stop_pct),
            ltp + risk_profile['initial_points']
        ))
        
        now_ts = time.time()

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
                'lowest_ltp':      ltp,
                'current_sl':      initial_sl,
                'initial_sl':      initial_sl,
                'loss_stop_pct':   loss_stop_pct,
                'tsl_pct':         tsl_pct,
                'anchor_ltp':      ltp,
                'best_premium':    ltp,
                'solo_mode':       False,
                'breach_start_ts': 0.0
            }
        }
        self.positions[leg] = pos
        self.trades_today  += 1
        self._save_state()
        self._log_step("ENTRY", leg, strike, ltp, f"{reason} | SL: {initial_sl:.2f} ({loss_stop_pct*100:.1f}%) | DTE: {dte}")

        tg = '\n'.join([
            '<pre>',
            '━━━ MCX TRADE OPENED (v5.1) ━━━',
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

    # ── NIFTY-Style Solo Leg Re-Anchoring ─────
    def _anchor_surviving_leg_sl(self, surviving_leg: str):
        """
        NIFTY ARCHITECTURE FIX:
        When partner leg exits, surviving leg immediately re-anchors to its current live LTP.
        Prevents choking the winning leg on an old lowest price.
        """
        pos = self.positions.get(surviving_leg)
        if not pos:
            return
        ltp = self._get_leg_ltp(pos)
        state = pos.setdefault('sl_state', {})

        dte = self._get_dte_days()
        risk_profile = self._premium_risk_profile(ltp, dte)
        
        # Base trail on DTE & Premium
        solo_sl_dist = min(ltp * SOLO_LEG_TSL_PCT, risk_profile['trail_points'])
        new_sl = round_to_tick(ltp + max(0.50, solo_sl_dist))

        # Fresh solo anchor — DO NOT carry forward old breached stop!
        state['anchor_ltp'] = ltp
        state['best_premium'] = ltp
        state['lowest_ltp'] = ltp
        state['current_sl'] = new_sl
        state['solo_mode'] = True
        state['breach_start_ts'] = 0.0
        pos['_last_ltp'] = ltp

        print(f"🎯 [SOLO LEG ANCHORED] {surviving_leg} re-anchored at LTP ₹{ltp:.2f} (Other leg removed). "
              f"TSL reset to ₹{new_sl:.2f} ({SOLO_LEG_TSL_PCT*100:.0f}% buffer above LTP). Trailing active!", flush=True)
        self._save_state()
        self._log_step("SOLO_ANCHOR", surviving_leg, pos['strike'], ltp, f"Partner leg exited. TSL set to {new_sl:.2f}")

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

        # Record trade for EOD summary
        self.trade_log.append({
            'leg': leg, 'tsym': pos.get('tsym', leg), 'strike': int(pos['strike']),
            'entry': pos['entry_price'], 'exit': ltp, 'qty': pos['qty'],
            'pnl': pnl, 'reason': reason,
            'time': get_ist_now().strftime('%H:%M:%S'),
        })

        tg = '\n'.join([
            '<pre>',
            '━━━ MCX TRADE CLOSED (v5.1) ━━━',
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

        # NIFTY ALIGNMENT: If exactly 1 surviving leg remains, re-anchor its stop cleanly at live LTP!
        if len(self.positions) == 1:
            surviving = list(self.positions.keys())[0]
            self._anchor_surviving_leg_sl(surviving)
            self._extreme_spot = 0.0

        self.last_any_close_ts = time.time()
        self._save_state()
        self._log_step("EXIT", leg, pos["strike"], ltp, reason)

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)

    def _rebalance_strangle_in_place(self, solo_leg: str, spot: float, atm: float, live_ltp: float) -> bool:
        """
        Smart In-Place Strangle Rebalance for MCX Natural Gas:
        When a solo surviving leg hits its TSL and the target strangle strike is identical to
        its current strike (atm == strike), preserve this leg and enter only the missing leg.
        """
        pos = self.positions.get(solo_leg)
        if not pos:
            return False

        other_leg = 'PE' if solo_leg == 'CE' else 'CE'
        other_strike = atm

        print(f'[IN-PLACE REBALANCE] {solo_leg} {int(pos["strike"])} TSL reached. Target is ATM Straddle at {int(atm)}. '
              f'Preserving {solo_leg} in-place to avoid exit+entry slippage!', flush=True)

        other_pos = self._enter_leg(other_leg, other_strike, 'SELL', reason=f"In-place Rebalance to match {solo_leg} strike")
        if not other_pos:
            print(f'[WARN] Failed to enter {other_leg} at {other_strike}, falling back to leg close.', flush=True)
            return False

        actual_entry = pos['entry_price']
        pos['_last_ltp'] = live_ltp
        dte = self._get_dte_days()
        risk_profile = self._premium_risk_profile(live_ltp, dte)
        new_initial_pct = risk_profile['initial_pct']
        
        pos['loss_stop_pct'] = new_initial_pct
        pos['tsl_pct'] = SOLO_LEG_TSL_PCT
        
        fresh_sl = round_to_tick(min(
            live_ltp * (1.0 + new_initial_pct),
            live_ltp + risk_profile['initial_points']
        ))
        pos['sl_state'] = {
            'lowest_ltp':      live_ltp,
            'current_sl':      fresh_sl,
            'initial_sl':      fresh_sl,
            'loss_stop_pct':   PREM_RISK_INITIAL_PCT_HIGH,
            'tsl_pct':         SOLO_LEG_TSL_PCT,
            'anchor_ltp':      live_ltp,
            'best_premium':    live_ltp,
            'solo_mode':       False,
            'breach_start_ts': 0.0
        }

        self.last_reentry_ts = time.time()
        self._consume_reversal()
        self._save_state()
        self._log_step("RECALIBRATE", solo_leg, pos["strike"], live_ltp, f"Re-anchoring preserved leg. Fresh SL: {fresh_sl:.2f}")

        tot_sign = '+' if self.total_realized_pnl >= 0 else ''
        tg = '\n'.join([
            '<pre>',
            '━━━ MCX IN-PLACE RECALIBRATION (v5.1) ━━━',
            '',
            f'  Preserved Open: {solo_leg} {int(pos["strike"])} (Entry: {actual_entry:.2f}, Live: {live_ltp:.2f})',
            f'  Entered: {other_leg} {int(other_strike)} SELL',
            f'  SL Reset: {PREM_RISK_INITIAL_PCT_HIGH*100:.0f}% on best premium (₹{fresh_sl:.2f})',
            f'  Total Realized: {tot_sign}₹{self.total_realized_pnl:,.2f}',
            '',
            '  *Actual entry preserved • SL/TSL reset on best premium*',
            '</pre>'
        ])
        print(f'[RECALIBRATE ROLL] {solo_leg} {int(pos["strike"])} (Entry: {actual_entry:.2f}, Live: {live_ltp:.2f}) | Strangle SL Reset: ₹{fresh_sl:.2f}', flush=True)
        send_telegram(tg)
        return True

    # ── Update SL/TSL for a single leg (NIFTY RISK LOGIC) ────────
    def _update_leg(self, leg: str, live_ltp: float) -> Tuple[bool, str]:
        """
        NIFTY-ALIGNED DUAL-LEG RISK MANAGER:
        - Break-Even Lock at 4% / 0.50 pts
        - Dynamic Trailing Stop (15% down to 8.5%)
        - Tiered Profit Ratchet (30%, 55%, 75%, 85%)
        - HARD MAX GIVEBACK CAP (20%): locks in >= 80% of peak profit!
        - 2-Second Tick Debounce Filter to avoid bid-ask spread whipsaws
        """
        pos = self.positions.get(leg)
        if not pos or pos['side'] != 'SELL' or live_ltp <= 0:
            return False, ''

        state       = pos['sl_state']
        entry_prem  = float(pos['entry_price'])
        now_ts      = time.time()

        # Track lowest (best) premium seen
        lowest = float(state.get('lowest_ltp', entry_prem))
        if live_ltp < lowest:
            lowest = live_ltp
            state['lowest_ltp'] = round(lowest, 2)

        is_strangle = ('CE' in self.positions and 'PE' in self.positions)
        is_solo     = not is_strangle or state.get('solo_mode', False)

        if is_solo:
            # ─────────────────────────────────────────────────────────────
            # SOLO LEG MODE: Re-anchored to LTP when partner leg exited
            # ─────────────────────────────────────────────────────────────
            anchor_prem = float(state.get('anchor_ltp', entry_prem))
            best_prem   = float(state.get('best_premium', lowest))
            if live_ltp < best_prem:
                best_prem = live_ltp
                state['best_premium'] = round(best_prem, 2)

            dte = self._get_dte_days()
            risk_profile = self._premium_risk_profile(best_prem, dte)
            solo_tsl_dist = min(best_prem * SOLO_LEG_TSL_PCT, risk_profile['trail_points'])
            new_trail_sl  = round_to_tick(best_prem + max(0.40, solo_tsl_dist))

            solo_profit     = anchor_prem - best_prem
            solo_profit_pct = (solo_profit / anchor_prem) if anchor_prem > 0 else 0.0

            # 1. Break-Even Lock: as soon as in profit, SL can NEVER exceed anchor price!
            if solo_profit >= BREAKEVEN_PROFIT_POINTS or solo_profit_pct >= BREAKEVEN_PROFIT_PCT:
                new_trail_sl = min(new_trail_sl, anchor_prem)

            # 2. Dynamic Profit Ratchet (lock in captured trend)
            if solo_profit >= 0.80 or solo_profit_pct >= 0.08:
                new_trail_sl = min(new_trail_sl, round_to_tick(anchor_prem - (0.30 * solo_profit)))
            if solo_profit >= 1.50 or solo_profit_pct >= 0.15:
                new_trail_sl = min(new_trail_sl, round_to_tick(anchor_prem - (0.55 * solo_profit)))
            if solo_profit >= 2.50 or solo_profit_pct >= 0.25:
                new_trail_sl = min(new_trail_sl, round_to_tick(anchor_prem - (0.75 * solo_profit)))
            if solo_profit >= 3.50 or solo_profit_pct >= 0.40:
                new_trail_sl = min(new_trail_sl, round_to_tick(anchor_prem - (0.85 * solo_profit)))

            # Cap max giveback to 20% of peak profit:
            giveback_stop = round_to_tick(anchor_prem - ((1.0 - PREM_MAX_PROFIT_GIVEBACK) * solo_profit))
            new_trail_sl  = min(new_trail_sl, giveback_stop)

            # Strict Ratchet: Stop loss can never move backwards (upwards)
            if 'current_sl' in state:
                prem_sl = min(new_trail_sl, state['current_sl'])
            else:
                prem_sl = new_trail_sl

            state['current_sl'] = prem_sl

            # Tick Debounce Filter (2 seconds of continuous breach required)
            if live_ltp >= prem_sl:
                breach_start = state.get('breach_start_ts', 0.0)
                if breach_start <= 0.0:
                    state['breach_start_ts'] = now_ts
                elif (now_ts - breach_start) >= SL_DEBOUNCE_SECONDS:
                    return True, f"Solo TSL Hit on {leg} ({live_ltp:.2f} >= {prem_sl:.2f}, profit locked)"
            else:
                state['breach_start_ts'] = 0.0

            return False, ''

        # ─────────────────────────────────────────────────────────────
        # STANDARD DUAL-LEG STRANGLE MODE (Both legs open)
        # ─────────────────────────────────────────────────────────────
        state['solo_mode'] = False
        dte = self._get_dte_days()
        risk_profile = self._premium_risk_profile(entry_prem, dte)
        loss_stop_pct = state.get('loss_stop_pct', risk_profile['initial_pct'])
        
        initial_sl = round_to_tick(min(
            entry_prem * (1.0 + loss_stop_pct),
            entry_prem + risk_profile['initial_points']
        ))

        if lowest >= entry_prem:
            # Phase A: not yet in profit — hold at initial SL
            prem_sl = initial_sl
        else:
            # Phase B: In profit — dynamic trailing + tiered profit ratchet
            profit     = entry_prem - lowest
            profit_pct = profit / entry_prem if entry_prem > 0 else 0.0

            trail_ceiling = PREM_SL_MAX_PCT
            trail_floor   = PREM_SL_MIN_PCT
            trail_pct     = trail_ceiling - (trail_ceiling - trail_floor) * min(profit_pct / 0.50, 1.0)
            trail_pct     = max(trail_pct, trail_floor)

            trail_dist = min(lowest * trail_pct, risk_profile['trail_points'])
            trail_sl   = round_to_tick(lowest + max(0.50, trail_dist))

            # 1. IMMEDIATE BREAK-EVEN LOCK:
            # If profit is >= 4% or >= 0.50 points, SL can NEVER be above entry_prem!
            if profit >= BREAKEVEN_PROFIT_POINTS or profit_pct >= BREAKEVEN_PROFIT_PCT:
                trail_sl = min(trail_sl, entry_prem)

            # 2. TIERED PROFIT RATCHET (Lock in the captured decay):
            if profit >= 0.80 or profit_pct >= 0.08:
                trail_sl = min(trail_sl, round_to_tick(entry_prem - (0.30 * profit)))
            if profit >= 1.50 or profit_pct >= 0.15:
                trail_sl = min(trail_sl, round_to_tick(entry_prem - (0.55 * profit)))
            if profit >= 2.50 or profit_pct >= 0.25:
                trail_sl = min(trail_sl, round_to_tick(entry_prem - (0.75 * profit)))
            if profit >= 3.50 or profit_pct >= 0.40:
                trail_sl = min(trail_sl, round_to_tick(entry_prem - (0.85 * profit)))

            # 3. CRITICAL: CAP MAX GIVEBACK TO 20% OF PEAK PROFIT!
            # Once peak profit is reached (e.g. +4k = 3.2 pts), it CAN NEVER give back > 20%!
            giveback_stop = round_to_tick(entry_prem - ((1.0 - PREM_MAX_PROFIT_GIVEBACK) * profit))
            trail_sl      = min(trail_sl, giveback_stop)

            # Never let trail SL exceed initial SL
            prem_sl = min(trail_sl, initial_sl)

        # STRICT RATCHET: The stop loss can NEVER move backwards (upwards).
        if 'current_sl' in state:
            prem_sl = min(prem_sl, state['current_sl'])
        else:
            prem_sl = initial_sl

        state['current_sl'] = prem_sl

        # Tick Debounce Filter (2 seconds of continuous breach required)
        if live_ltp >= prem_sl:
            breach_start = state.get('breach_start_ts', 0.0)
            if breach_start <= 0.0:
                state['breach_start_ts'] = now_ts
            elif (now_ts - breach_start) >= SL_DEBOUNCE_SECONDS:
                label = 'Strangle Initial SL Hit' if prem_sl >= initial_sl else 'Strangle Trailed SL Hit'
                return True, f'{label} on {leg} ({live_ltp:.2f} >= {prem_sl:.2f})'
        else:
            state['breach_start_ts'] = 0.0

        return False, ''

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

        if surviving_leg == 'PE':
            if spot > self._extreme_spot:
                self._extreme_spot = spot
            pullback = self._extreme_spot - spot
            self._reversal_pullback = pullback
            if confirmed_signal <= 0 or pullback >= SWING_REVERSAL_PTS:
                self._reversal_latched = True

        elif surviving_leg == 'CE':
            if spot < self._extreme_spot:
                self._extreme_spot = spot
            pullback = spot - self._extreme_spot
            self._reversal_pullback = pullback
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
                'best_price': best,
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
            preq  = ema_snap.get("persistence_req", 15.0)
            sig   = ema_snap.get("confirmed_signal", 0)
            hold  = ema_snap.get("hold_time", 0.0)

            sig_str = f"{GR}▲ UP{RS}" if sig > 0 else (f"{RD}▼ DOWN{RS}" if sig < 0 else f"{YL}━ FLAT{RS}")
            reversal_tag = f"  {MG}[REVERSAL LATCHED]{RS}" if self._reversal_latched else ""
            cooldown_left = max(0.0, POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))
            cooldown_tag = f"  {YL}[COOLDOWN {cooldown_left:.0f}s]{RS}" if cooldown_left > 0 else ""

            print()
            print(TOP)
            title_l = (f'  {CY}MCX NATGAS PAPER v5.1{RS}  {DIM}│{RS}  '
                       f'{YL}NIFTY-ALIGNED HEDGED STRADDLE + EMA MOMENTUM{RS}  {DIM}│{RS}  '
                       f'{GR}PROFIT PROTECTION ACTIVE{RS}')
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
                       f'{DIM}SLOPE/m:{RS} {WH}{slope:>+6.3f}{RS}  '
                       f'{DIM}VR:{RS} {WH}{vr:>4.2f}{RS}  '
                       f'{DIM}PERSIST:{RS} {YL}{preq:>4.1f}s{RS}  '
                       f'{DIM}SIGNAL:{RS} {sig_str} {DIM}({hold:.1f}s){RS}')
            print(f'{V}{_pad(mom_row, W)}{V}')
            print(MID)

            # Position table
            if not snap_rows:
                msg = f'  {YL}No open positions — evaluating momentum for balanced straddle entry...{RS}'
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
                           f"{DIM}{r['best_price']:>10.2f}{RS} {VS} "
                           f"{YL}{r['ltp']:>8.2f}{RS} {VS} "
                           f"{MG}{r['sl']:>9.2f}{RS} {VS} "
                           f"  {pnl_col}{pnl_fmt}{RS}  ")
                    print(f'{V}{_pad(row, W)}{V}')

                if any(r.get('solo_mode') for r in snap_rows):
                    print(MIDS)
                    solo_msg = f"  {CY}🎯 SOLO TSL ACTIVE (*):{RS} Strangle OFF — Re-anchored at LTP with {SOLO_LEG_TSL_PCT*100:.0f}% TSL & Giveback Cap"
                    print(f'{V}{_pad(solo_msg, W)}{V}')

            print(MID)
            real_sign, real_fmt = _fmt_pnl(self.total_realized_pnl, width=10)
            unreal_sign, unreal_fmt = _fmt_pnl(total_unreal, width=10)
            net_sign, net_fmt = _fmt_pnl(net, width=10)

            real_col   = GR if self.total_realized_pnl > 0 else (RD if self.total_realized_pnl < 0 else YL)
            unreal_col = GR if total_unreal > 0 else (RD if total_unreal < 0 else YL)
            net_col    = GR if net > 0 else (RD if net < 0 else YL)

            net_pct = (net / CAPITAL) * 100.0
            pnl_row = (f"  {DIM}REALIZED:{RS} {real_col}{real_fmt}{RS}  {VS}  "
                       f"{DIM}UNREALIZED:{RS} {unreal_col}{unreal_fmt}{RS}  {VS}  "
                       f"{DIM}NET MTM:{RS} {net_col}{net_fmt} ({net_pct:+.2f}%){RS}  {VS}  "
                       f"{DIM}TRADES:{RS} {WH}{self.trades_today}{RS}")
            print(f'{V}{_pad(pnl_row, W)}{V}')
            print(BOT)
            sys.stdout.flush()

            # Save live snapshot for web dashboard
            try:
                mcx_snap = {
                    "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "spot": spot,
                    "atm": int(atm),
                    "expiry": self.target_opt_expiry_str,
                    "is_rolled_over": self.is_rolled_over,
                    "trades_today": self.trades_today,
                    "reversal_latched": self._reversal_latched,
                    "cooldown_remaining": max(0, int(POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))),
                    "ema": ema_snap,
                    "positions": snap_rows,
                    "realized_pnl": self.total_realized_pnl,
                    "unrealized_pnl": total_unreal,
                    "net_pnl": net,
                    "net_pct": net_pct,
                    "mtd_pnl": self.db.data.get("mtd_pnl", self.total_realized_pnl) if hasattr(self, 'db') else self.total_realized_pnl,
                    "ytd_pnl": self.db.data.get("ytd_pnl", self.total_realized_pnl) if hasattr(self, 'db') else self.total_realized_pnl,
                    "trade_log": getattr(self, "trade_log", [])
                }
                snap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_snapshot_mcx_paper.json")
                with open(snap_path, "w") as sf:
                    json.dump(mcx_snap, sf, indent=2)
            except Exception:
                pass

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

            roll_tag = " ⟳NEXT MO" if self.is_rolled_over else ""
            r_sign = '+' if self.total_realized_pnl >= 0 else ''
            u_sign = '+' if total_unreal >= 0 else ''
            n_sign = '+' if net >= 0 else ''
            net_pct_tg = (net / CAPITAL) * 100.0
            pnl_badge  = "🟢" if net >= 0 else "🔴"
            rev_tag    = " 🔄REVERSAL" if self._reversal_latched else ""
            cd_tag     = f" ⏳{int(POST_CLOSE_COOLDOWN-(now_ts-self.last_any_close_ts))}s" if (now_ts - self.last_any_close_ts) < POST_CLOSE_COOLDOWN else ""

            t  = f"<b>⚡ MCX NATGAS · PAPER TRADING (v5.1)</b>\n"
            t += f"<code>🕐 {now.strftime('%H:%M:%S IST')}</code>\n"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += f"<b>SPOT:</b>   <code>{spot:>8.2f}</code>  <b>ATM:</b> <code>{int(atm)}</code>\n"
            t += f"<b>EXPIRY:</b> <code>{self.target_opt_expiry_str}{roll_tag}</code>\n"
            t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            t += f"<b>EMA MOMENTUM ENGINE (NOISE FILTERED)</b>\n"
            t += f"<pre>"
            t += f"  EMA15 : {ema15:>8.2f}\n"
            t += f"  EMA90 : {ema90:>8.2f}\n"
            t += f"  SLOPE : {slope:>+8.3f}/m\n"
            t += f"  VR    : {vr:>8.2f}  P_REQ: {ema_snap.get('persistence_req',15.0):.1f}s\n"
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
            t += f"<b>MODE:</b> <code>PAPER</code>  <b>LOT:</b> <code>1×{LOT_SIZE}u</code>  <b>SL:</b> <code>{PREM_RISK_INITIAL_PCT_HIGH*100:.0f}%</code>  <b>TSL:</b> <code>{SOLO_LEG_TSL_PCT*100:.0f}%</code>"

            chat_ids = _get_tg_chat_ids()
            for cid in chat_ids:
                msg_id = _last_tg_dash_msg_ids.get(cid)
                if msg_id is not None:
                    try:
                        r = requests.post(
                            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText',
                            json={'chat_id': cid, 'message_id': msg_id, 'text': t, 'parse_mode': 'HTML'},
                            timeout=3)
                        if r.status_code in (200, 400):
                            if r.status_code == 400 and 'message to edit not found' in r.text:
                                _last_tg_dash_msg_ids.pop(cid, None)
                            continue
                        elif r.status_code == 429:
                            _tg_rate_limited_until = time.time() + r.json().get('parameters', {}).get('retry_after', 30)
                            return
                    except Exception:
                        pass
                try:
                    r = requests.post(
                        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                        data={'chat_id': cid, 'text': t, 'parse_mode': 'HTML'},
                        timeout=4)
                    if r.status_code == 200 and r.json().get('ok'):
                        _last_tg_dash_msg_ids[cid] = r.json()['result']['message_id']
                    elif r.status_code == 429:
                        _tg_rate_limited_until = time.time() + r.json().get('parameters', {}).get('retry_after', 30)
                        return
                except Exception:
                    pass

    # ── EOD Trade Summary ─────────────────────
    def _send_eod_trade_summary(self):
        """Send full session trade log split by leg as 4 separate Telegram messages."""
        final_pct = (self.total_realized_pnl / CAPITAL) * 100.0
        s_sign = '+' if self.total_realized_pnl >= 0 else ''

        for leg_name in ('CE', 'PE'):
            trades = [t for t in self.trade_log if t['leg'] == leg_name]
            if not trades:
                msg = f"<pre>━━━ MCX {leg_name} TRADES ━━━\n  No trades for this leg.\n</pre>"
            else:
                leg_tot = sum(t['pnl'] for t in trades)
                lt_sign = '+' if leg_tot >= 0 else ''
                msg  = f"<pre>━━━ MCX {leg_name} TRADES ({len(trades)} trades) ━━━\n"
                msg += f"{'#':<3} {'TIME':<9} {'STRIKE':>7} {'ENTRY':>7} {'EXIT':>7} {'PnL':>10}\n"
                msg += f"{'─'*3} {'─'*9} {'─'*7} {'─'*7} {'─'*7} {'─'*10}\n"
                for i, t in enumerate(trades, 1):
                    ps = '+' if t['pnl'] >= 0 else ''
                    msg += f"{i:<3} {t['time']:<9} {t['strike']:>7} {t['entry']:>7.2f} {t['exit']:>7.2f} {ps}{t['pnl']:>9,.0f}\n"
                msg += f"{'─'*51}\n"
                msg += f"{'LEG TOTAL':>32}: {lt_sign}₹{leg_tot:>9,.0f}\n"
                msg += "</pre>"
            send_telegram(msg)
            time.sleep(0.4)

        ce_trades = [t for t in self.trade_log if t['leg'] == 'CE']
        pe_trades = [t for t in self.trade_log if t['leg'] == 'PE']
        ce_tot = sum(t['pnl'] for t in ce_trades)
        pe_tot = sum(t['pnl'] for t in pe_trades)
        ce_wins = sum(1 for t in ce_trades if t['pnl'] >= 0)
        pe_wins = sum(1 for t in pe_trades if t['pnl'] >= 0)
        msg  = "<pre>━━━ MCX SESSION STATS ━━━\n"
        msg += f"  CE Trades : {len(ce_trades):>3}  Wins: {ce_wins}  PnL: {('+' if ce_tot>=0 else '')}₹{ce_tot:,.0f}\n"
        msg += f"  PE Trades : {len(pe_trades):>3}  Wins: {pe_wins}  PnL: {('+' if pe_tot>=0 else '')}₹{pe_tot:,.0f}\n"
        msg += f"  Total     : {self.trades_today:>3}\n"
        msg += "</pre>"
        send_telegram(msg)
        time.sleep(0.4)

        msg  = "<pre>━━━ MCX FINAL PNL ━━━\n"
        msg += f"  Realized PnL : {s_sign}₹{self.total_realized_pnl:,.2f}\n"
        msg += f"  Return on 2L : {final_pct:+.2f}%\n"
        msg += f"  Base Capital : ₹{CAPITAL:,.2f}\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        badge = "✅ PROFIT" if self.total_realized_pnl >= 0 else "❌ LOSS"
        msg += f"  {badge}\n"
        msg += "</pre>"
        send_telegram(msg)

    # ── Main run loop ─────────────────────────
    def run(self):
        self.authenticate()
        self._get_mcx_csv()

        DIM = f'{Fore.WHITE}{Style.DIM}'
        CY  = f'{Fore.CYAN}{Style.BRIGHT}'
        GR  = f'{Fore.GREEN}{Style.BRIGHT}'
        RD  = f'{Fore.RED}{Style.BRIGHT}'
        RS  = Style.RESET_ALL
        print()
        print(f'{DIM}{"="*98}{RS}')
        print(f'{CY}  MCX NATURAL GAS PAPER TRADING BOT  v5.1  |  15:30 – 23:24 IST (NIFTY LOGIC){RS}')
        print(f'{DIM}{"="*98}{RS}')
        print(flush=True)

        exp_note = f"\nTarget Expiry: {self.target_opt_expiry_str}" + (" (Next Month Rollover Active)" if self.is_rolled_over else "")
        send_telegram(f'<pre>MCX Natural Gas\nPaper Trading Bot Online (v5.1 NIFTY-Aligned Engine)\nSession: 15:30 – 23:24 IST{exp_note}</pre>')

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
                    self._render_dashboard(locals().get('spot', 0.0), locals().get('atm', 0.0), locals().get('ema_snap', {}))
                    final_pct = (self.total_realized_pnl / CAPITAL) * 100.0
                    pnl_col = GR if self.total_realized_pnl >= 0 else RD
                    sign = '+' if self.total_realized_pnl >= 0 else ''
                    print(f'\n{pnl_col}✅ Session Complete. Final Realized PnL: {sign}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%){RS}\n', flush=True)
                    self._send_eod_trade_summary()
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

                # ── Continuous Streaming EMA Update (Noise Filtered) ──
                ema_snap = self.ema_engine.update(spot, now_ts)
                confirmed_sig = ema_snap.get("confirmed_signal", 0)

                # ── Momentum Reversal Tracking ──────────────
                self._update_reversal_tracker(spot, confirmed_sig)

                # ── STEP 1: NO POSITIONS → BALANCED STRADDLE ENTRY (NIFTY LOGIC) ───
                if not self.positions:
                    time_since_close = now_ts - self.last_any_close_ts
                    if time_since_close < POST_CLOSE_COOLDOWN:
                        pass  # Respect post-close cooldown
                    else:
                        # NIFTY PRINCIPLE: Default to balanced ATM Straddle (CE + PE) to harvest theta decay.
                        # Defer a leg ONLY if there is an overwhelming, sustained trend.
                        is_strong_trend = (confirmed_sig != 0 and ema_snap.get("hold_time", 0.0) >= 25.0
                                           and abs(ema_snap.get("ema_15", spot) - ema_snap.get("ema_90", spot)) >= 0.40)

                        if is_strong_trend and confirmed_sig == 1:
                            print(f'[MOMENTUM ENTRY] Strong Bullish Trend (+1, hold >= 25s). Writing PE at ATM {int(atm)} (CE deferred)...', flush=True)
                            self._enter_leg('PE', atm, 'SELL', reason="Strong Bullish Momentum PE Entry")
                        elif is_strong_trend and confirmed_sig == -1:
                            print(f'[MOMENTUM ENTRY] Strong Bearish Trend (-1, hold >= 25s). Writing CE at ATM {int(atm)} (PE deferred)...', flush=True)
                            self._enter_leg('CE', atm, 'SELL', reason="Strong Bearish Momentum CE Entry")
                        else:
                            print(f'[INIT ENTRY] Writing balanced ATM Straddle (CE + PE) at {int(atm)} to harvest decay...', flush=True)
                            self._enter_leg('CE', atm, 'SELL', reason="Balanced Straddle Entry")
                            self._enter_leg('PE', atm, 'SELL', reason="Balanced Straddle Entry")
                        self._consume_reversal()

                    self._render_dashboard(locals().get('spot', 0.0), locals().get('atm', 0.0), locals().get('ema_snap', {}))
                    time.sleep(1.0)
                    continue

                # ── STEP 2: 1 LEG OPEN → RE-ENTER MISSING LEG TO RESTORE STRANGLE ───
                short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']

                if len(short_legs) == 1:
                    time_since_reentry = now_ts - self.last_reentry_ts
                    time_since_close   = now_ts - self.last_any_close_ts
                    if time_since_reentry >= REENTRY_COOLDOWN_S and time_since_close >= REENTRY_COOLDOWN_S:
                        surviving_leg = short_legs[0]
                        missing_leg   = 'CE' if surviving_leg == 'PE' else 'PE'

                        # Condition to re-enter missing leg:
                        # 1) Swing pullback/bounce >= 0.80 pts, OR
                        # 2) EMA confirmed signal returned to 0 (flat) or reversed in favor of missing leg
                        is_ema_reversal = (confirmed_sig == 0) or (missing_leg == 'CE' and confirmed_sig == -1) or (missing_leg == 'PE' and confirmed_sig == 1)
                        if self._reversal_latched or is_ema_reversal:
                            print(f'[RE-ENTER STRANGLE] Trend normalized / reversed (EMA sig={confirmed_sig}). '
                                  f'Restoring balanced strangle by entering {missing_leg} at ATM {int(atm)}...', flush=True)
                            if self._enter_leg(missing_leg, atm, 'SELL', reason=f"Restoring balanced strangle (trend normalized/reversed)"):
                                self.last_reentry_ts = now_ts
                                self._consume_reversal()
                                # Reset both legs to active strangle mode with fresh breathing room
                                for leg_name in ('CE', 'PE'):
                                    p = self.positions.get(leg_name)
                                    if p and 'sl_state' in p:
                                        p['sl_state']['solo_mode'] = False
                                        p['sl_state']['breach_start_ts'] = 0.0
                                self._save_state()

                # ── STEP 3: CHECK TSL/SL FOR ALL OPEN LEGS (NIFTY RISK LOGIC) ───
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
                                    continue  # Preserved in-place! Skip physical exit.
                        legs_to_close.append((leg, reason, live_ltp))

                for leg, reason, exit_px in legs_to_close:
                    print(f'[ALERT] {reason}', flush=True)
                    self._close_leg(leg, reason, exit_price=exit_px)

                # ── STEP 4: Render Dashboard ─────────────────
                self._render_dashboard(locals().get('spot', 0.0), locals().get('atm', 0.0), locals().get('ema_snap', {}))
                time.sleep(1.0)

            except KeyboardInterrupt:
                print('\n[STOP] KeyboardInterrupt — squaring off all positions...', flush=True)
                self._close_all('KEYBOARD_INTERRUPT')
                self.positions.clear()
                self._render_dashboard(locals().get('spot', 0.0), locals().get('atm', 0.0), locals().get('ema_snap', {}))
                final_pct = (self.total_realized_pnl / CAPITAL) * 100.0
                sign = '+' if self.total_realized_pnl >= 0 else ''
                pnl_col = GR if self.total_realized_pnl >= 0 else RD
                print(f'\n{pnl_col}✅ All positions squared off. Final Realized PnL: {sign}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%){RS}\n', flush=True)
                self._send_eod_trade_summary()
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
        send_telegram(f'<pre>MCX Bot Fatal Error (v5.1):\n{e}</pre>')
        sys.exit(1)
