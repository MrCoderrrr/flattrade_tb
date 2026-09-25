"""mcx_paper_v5.py  —  v6.0 (UPV2-Aligned | MCX Natural Gas Precision Engine)
================================================================================
MCX Natural Gas Paper Trading Engine | Version 6.0
Session Window : 16:00 – 23:24 IST (weekdays)
Auto Square-Off: 23:24 IST

ARCHITECTURE (Ported from UPV2 + MCX-tuned):
  1. ContinuousEMAEngine  – Fast:15s  Slow:90s  Anchor:300s
     - AdaptivePersistence clamped to [3s, 20s]  (fast enough for NatGas bursts)
     - Signal: Fast > Slow AND anchor slope > 0  → +1 (Bullish)
              Fast < Slow AND anchor slope < 0  → -1 (Bearish)
              else 0 (FLAT)
  2. Pure % stop-loss — NO point clamps, NO ratchets, NO giveback floors:
     - Strangle mode : 12% initial SL above entry premium
       → Once in profit, trail strictly at 7% above best (lowest) premium seen
     - Solo leg mode : On partner exit, re-anchor to live LTP
       → Trail at 7% above best premium from the re-anchor point (ratchet only down)
  3. Direction-aware Reversal Detector:
     - CE re-enters ONLY when EMA signal is ≤ 0  (bullish impulse ended)
     - PE re-enters ONLY when EMA signal is ≥ 0  (bearish impulse ended)
  4. Always-One-Leg-Open safety net:
     - If both legs stop out → immediately re-enter balanced ATM Straddle
  5. In-Place Strangle Rebalance:
     - Solo TSL hit AND strike == ATM → preserve leg, enter missing partner, reset SL
  6. 2-second tick debounce on every SL trigger
  7. Trade log includes ISO timestamp for web dashboard compatibility
  8. sl_risk injected into snapshot for web dashboard Max SL Risk tile
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
import csv
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
# IST helpers
# ─────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(IST)

def is_expiry_week(today_date: Any, expiry_date: Any) -> bool:
    if hasattr(expiry_date, 'date'):
        expiry_date = expiry_date.date()
    if hasattr(today_date, 'date'):
        today_date = today_date.date()
    days_to_expiry = (expiry_date - today_date).days
    if days_to_expiry < 0:
        return False
    monday_of_expiry_week = expiry_date - timedelta(days=expiry_date.weekday())
    sunday_of_expiry_week = monday_of_expiry_week + timedelta(days=6)
    return (monday_of_expiry_week <= today_date <= sunday_of_expiry_week) or (days_to_expiry <= 4)


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION  (all tuned for MCX Natural Gas)
# ═══════════════════════════════════════════════════════════════════

TOKEN_FILE       = 'token.txt'
STRIKE_STEP      = 5.0          # Natural Gas strike grid
MCX_ENTRY_HOUR   = 16
MCX_ENTRY_MINUTE = 0
MCX_EXIT_HOUR    = 23
MCX_EXIT_MINUTE  = 24
LOT_SIZE         = 1250         # 1 lot = 1250 units
CAPITAL          = 200000.0

# ── Stop-Loss percentages (pure % — no point clamps) ──────────────
STRANGLE_SL_PCT  = 0.12    # 12 % initial SL in strangle mode
SOLO_TSL_PCT     = 0.07    # 7 % trailing SL for solo surviving leg

# ── Tick Debounce ─────────────────────────────────────────────────
SL_DEBOUNCE_SECS = 2.0     # price must stay >= SL for 2s before firing

# ── Cooldowns ─────────────────────────────────────────────────────
POST_CLOSE_COOLDOWN = 0.0  # seconds between both-legs-closed and next entry
REENTRY_COOLDOWN_S  = 0.0  # seconds before re-entering missing leg

# ── Momentum Bias Threshold for Deferring a Leg at Open ───────────
STRONG_TREND_HOLD_S   = 20.0   # EMA must be locked for this long
STRONG_TREND_SPREAD   = 0.30   # EMA15-EMA90 spread to call "strong"

# ── EMA Engine (MCX-tuned) ────────────────────────────────────────
# Natural Gas makes fast 3–10s micro-bursts — persistence must react quickly
EMA_FAST_HL      = 15.0    # 15s half-life  (fast momentum)
EMA_SLOW_HL      = 90.0    # 90s half-life  (trend anchor)
EMA_ANCHOR_HL    = 300.0   # 300s half-life (anchor slope)
PERSISTENCE_MIN  = 3.0     # minimum signal hold before confirming (seconds)
PERSISTENCE_MAX  = 20.0    # maximum persistence cap (NatGas is faster than Nifty)

# ── Reversal: swing pullback threshold ────────────────────────────
SWING_REVERSAL_PTS = 0.60   # NatGas tick = 0.05, so 0.60 = 12 ticks pullback

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_TOKEN = '8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE'
CHAT_ID        = '6307066850'
PROJECT_ROOT   = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════════════════
# MCX PnL / MTD TRACKER
# ═══════════════════════════════════════════════════════════════════
class MCXDBManager:
    def __init__(self, filename: str = "mcx_pnl_tracker.json"):
        self.filename = filename
        self.data = self._load()

    def _get_ist_str(self) -> str:
        return get_ist_now().strftime("%Y-%m-%d")

    def _load(self) -> dict:
        base_cap = CAPITAL
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
            d = {"mtd_pnl": 0.0, "ytd_pnl": 0.0, "current_capital": base_cap,
                 "base_capital": base_cap, "today_pnl": 0.0,
                 "last_date": "", "intraday_date": "", "daily_pnl": {}}

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
        today_str    = date_str or self._get_ist_str()
        month_prefix = today_str[:7]
        year_prefix  = today_str[:4]
        base_cap     = float(self.data.get("base_capital", CAPITAL) or CAPITAL)

        daily_map = self.data.setdefault("daily_pnl", {})
        daily_map[today_str] = round(float(realized_pnl), 2)

        mtd_sum = round(sum(v for d, v in daily_map.items() if d.startswith(month_prefix)), 2)
        ytd_sum = round(sum(v for d, v in daily_map.items() if d.startswith(year_prefix)), 2)

        self.data["mtd_pnl"]         = mtd_sum
        self.data["ytd_pnl"]         = ytd_sum
        self.data["current_capital"] = round(base_cap + ytd_sum, 2)
        self.data["today_pnl"]       = round(float(realized_pnl), 2)
        self.data["last_date"]       = today_str
        self.data["intraday_date"]   = today_str
        self._save()

        # Write to daily CSV
        for log_dir in [
            os.path.join(PROJECT_ROOT, "data", "logs"),
            os.path.join(PROJECT_ROOT, "tradingbot", "data", "logs"),
        ]:
            try:
                os.makedirs(log_dir, exist_ok=True)
                csv_path   = os.path.join(log_dir, "daily_pnl_mcx_paper.csv")
                fieldnames = ["date", "daily_pnl", "mtd_pnl", "ytd_pnl", "current_capital"]
                rows       = []
                found      = False
                if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                    with open(csv_path, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        fieldnames = reader.fieldnames or fieldnames
                        for r in reader:
                            if r.get("date") == today_str:
                                r["daily_pnl"]       = f"{realized_pnl:.2f}"
                                r["mtd_pnl"]         = f"{mtd_sum:.2f}"
                                r["ytd_pnl"]         = f"{ytd_sum:.2f}"
                                r["current_capital"] = f"{base_cap + ytd_sum:.2f}"
                                found = True
                            rows.append(r)
                if not found:
                    rows.append({"date": today_str, "daily_pnl": f"{realized_pnl:.2f}",
                                 "mtd_pnl": f"{mtd_sum:.2f}", "ytd_pnl": f"{ytd_sum:.2f}",
                                 "current_capital": f"{base_cap + ytd_sum:.2f}"})
                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
            except Exception as e:
                print(f"[WARN] Failed writing to daily_pnl_mcx_paper.csv: {e}", flush=True)


# ═══════════════════════════════════════════════════════════════════
# CONTINUOUS STREAMING EMA ENGINE  (UPV2 port, MCX-tuned)
# ═══════════════════════════════════════════════════════════════════
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
            dt    = max(0.0, timestamp - self.timestamp)
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
        mean     = sum(self.values) / len(self.values)
        variance = sum((x - mean) ** 2 for x in self.values) / (len(self.values) - 1)
        return math.sqrt(max(0.0, variance))


class ContinuousEMAEngine:
    """
    Continuous streaming EMA engine (UPV2 logic, MCX-tuned persistence).

    Signal logic (matches UPV2 exactly):
      +1 Bullish : Fast > Slow  AND  anchor slope > 0
      -1 Bearish : Fast < Slow  AND  anchor slope < 0
       0 Flat    : everything else

    Persistence clamped to [PERSISTENCE_MIN, PERSISTENCE_MAX].
    AdaptivePersistence: P_raw = 10 / VR  → lower VR = calmer market = longer hold.
    For NatGas (high VR bursts) this resolves quickly — typical hold = 3–8s.
    """
    def __init__(self):
        self.emas = {
            EMA_FAST_HL:   ContinuousEMA(EMA_FAST_HL),
            EMA_SLOW_HL:   ContinuousEMA(EMA_SLOW_HL),
            EMA_ANCHOR_HL: ContinuousEMA(EMA_ANCHOR_HL),
        }
        # Sliding 60-entry history for anchor slope (each entry = 1 tick ≈ 1 second)
        self.slow_history: deque = deque(maxlen=60)
        self.rv = {60: RollingVolatility(60), 300: RollingVolatility(300)}
        self.raw_signal:       int            = 0
        self.signal_start_ts:  Optional[float] = None
        self.confirmed_signal: int            = 0
        self.latest_snapshot:  Dict[str, Any] = {}

    def update(self, spot: float, now_ts: float) -> Dict[str, Any]:
        if spot <= 0:
            return self.latest_snapshot

        for ema in self.emas.values():
            ema.update(spot, now_ts)

        anchor_val = self.emas[EMA_ANCHOR_HL].value or spot
        self.slow_history.append((now_ts, anchor_val))

        rv60  = self.rv[60].update(spot)
        rv300 = self.rv[300].update(spot)
        vr    = (rv60 / rv300) if rv60 is not None and rv300 and rv300 > 0 else 1.0

        # AdaptivePersistence: P = 10/VR clamped to [MIN, MAX]
        p_req = max(PERSISTENCE_MIN, min(PERSISTENCE_MAX, 10.0 / max(float(vr), 1e-12)))

        # Anchor slope over available history (points per minute)
        slow_slope = 0.0
        if len(self.slow_history) >= 2:
            dt = self.slow_history[-1][0] - self.slow_history[0][0]
            if dt >= 5.0:
                slow_slope = ((self.slow_history[-1][1] - self.slow_history[0][1]) / dt) * 60.0

        fast_val = self.emas[EMA_FAST_HL].value if self.emas[EMA_FAST_HL].value is not None else spot
        slow_val = self.emas[EMA_SLOW_HL].value if self.emas[EMA_SLOW_HL].value is not None else spot

        # Direction signal (UPV2-identical logic)
        if fast_val > slow_val and slow_slope > 0:
            sig = 1
        elif fast_val < slow_val and slow_slope < 0:
            sig = -1
        else:
            sig = 0

        if sig != 0:
            if sig != self.raw_signal:
                self.raw_signal      = sig
                self.signal_start_ts = now_ts
        else:
            self.raw_signal      = 0
            self.signal_start_ts = None
            self.confirmed_signal = 0

        hold_time     = (now_ts - self.signal_start_ts) if self.signal_start_ts else 0.0
        prev_confirmed = self.confirmed_signal
        if self.raw_signal != 0 and hold_time >= p_req:
            self.confirmed_signal = self.raw_signal
        else:
            self.confirmed_signal = 0

        if self.confirmed_signal != prev_confirmed:
            d = {1: "BULLISH▲", -1: "BEARISH▼", 0: "FLAT━"}
            print(f"[EMA] {d.get(prev_confirmed,'?')} → {d.get(self.confirmed_signal,'?')}  "
                  f"(F={fast_val:.2f} S={slow_val:.2f} slope={slow_slope:+.3f}/m "
                  f"VR={vr:.2f} hold={hold_time:.1f}s p_req={p_req:.1f}s)", flush=True)

        self.latest_snapshot = {
            "ema_15":         fast_val,
            "ema_90":         slow_val,
            "ema_300":        anchor_val,
            "slow_slope":     slow_slope,
            "rv60":           rv60 or 0.0,
            "rv300":          rv300 or 0.0,
            "vr":             vr,
            "persistence_req": p_req,
            "raw_signal":     self.raw_signal,
            "hold_time":      hold_time,
            "confirmed_signal": self.confirmed_signal,
        }
        return self.latest_snapshot


# ═══════════════════════════════════════════════════════════════════
# DIRECTION-AWARE REVERSAL DETECTOR  (UPV2 port)
# ═══════════════════════════════════════════════════════════════════
class ReversionDetector:
    """
    Determines whether the missing leg can safely re-enter.

    CE was stopped because market surged UP.
      → Re-enter CE when the bullish impulse fades: confirmed_signal <= 0
    PE was stopped because market crashed DOWN.
      → Re-enter PE when the bearish impulse fades: confirmed_signal >= 0

    Also triggers if price has pulled back >= SWING_REVERSAL_PTS from extreme.
    """

    @staticmethod
    def can_reenter_ce(confirmed_signal: int, reversal_latched: bool) -> Tuple[bool, str]:
        if reversal_latched:
            return True, "SWING_PULLBACK"
        if confirmed_signal <= 0:
            return True, f"EMA_SIG={confirmed_signal}(bullish_faded)"
        return False, ""

    @staticmethod
    def can_reenter_pe(confirmed_signal: int, reversal_latched: bool) -> Tuple[bool, str]:
        if reversal_latched:
            return True, "SWING_PULLBACK"
        if confirmed_signal >= 0:
            return True, f"EMA_SIG={confirmed_signal}(bearish_faded)"
        return False, ""


# ═══════════════════════════════════════════════════════════════════
# TELEGRAM HELPERS
# ═══════════════════════════════════════════════════════════════════
_last_tg_dash_msg_ids: Dict[str, int] = {}
_last_tg_dash_edit_ts:  float         = 0.0
_tg_rate_limited_until: float         = 0.0


def _get_tg_chat_ids() -> List[str]:
    if isinstance(CHAT_ID, list):
        return [str(c).strip() for c in CHAT_ID if str(c).strip()]
    return [c.strip() for c in str(CHAT_ID).split(',') if c.strip()]


def send_telegram(msg: str):
    global _last_tg_dash_msg_ids
    chat_ids = _get_tg_chat_ids()
    if not (TELEGRAM_TOKEN and chat_ids):
        return
    for cid in chat_ids:
        try:
            requests.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                data={'chat_id': cid, 'text': msg, 'parse_mode': 'HTML'},
                timeout=5)
        except Exception:
            pass
    _last_tg_dash_msg_ids.clear()


# ═══════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════
def round_to_price(value: float, step: float = STRIKE_STEP) -> float:
    return round_to_tick(math.floor(value / step + 0.5) * step)


def round_to_tick(value: float) -> float:
    """MCX Natural Gas min tick = 0.05"""
    return round(value * 20.0) / 20.0


def _ansi_len(s: str) -> int:
    return len(re.sub(r'\x1b\[[0-9;]*m', '', s))


def _pad(row: str, width: int) -> str:
    return row + ' ' * max(0, width - _ansi_len(row))


def _fmt_pnl(val: float, width: int = 12) -> Tuple[str, str]:
    if abs(val) < 1e-4:
        val = 0.0
    sign    = '+' if val > 0 else ('-' if val < 0 else ' ')
    pnl_str = f"{sign}₹{abs(val):,.2f}"
    return sign, f"{pnl_str:>{width}}"


# ═══════════════════════════════════════════════════════════════════
# MAIN BOT ENGINE
# ═══════════════════════════════════════════════════════════════════
class NaturalGasPaperBot:

    # ──────────────────────────────────────────
    # Init
    # ──────────────────────────────────────────
    def __init__(self):
        # Single-instance lock
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
            print("\n❌ [FATAL] Another MCX instance is already running! Aborting.", flush=True)
            sys.exit(0)

        self.api                    = NorenApiPy() if NorenApiPy else None
        self.positions: Dict[str, Dict] = {}
        self.ema_engine             = ContinuousEMAEngine()
        self.total_realized_pnl     = 0.0
        self.trades_today           = 0
        self.trade_log: List[Dict]  = []

        # State file
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'mcx_state_paper_v5.json')

        # Market data cache
        self._mcx_master              = None
        self._spot_cache              = {'ts': 0.0, 'val': 0.0}
        self.front_month_futs_token:  Optional[str] = None
        self.front_month_futs_symbol: Optional[str] = None
        self.target_opt_expiry_ts:    Optional[Any] = None
        self.target_opt_expiry_str:   str           = ""
        self.is_rolled_over:          bool          = False

        # Re-entry tracking
        self.last_reentry_ts   = 0.0
        self.last_any_close_ts = 0.0

        # Reversal / swing tracking
        self._reversal_latched  = False
        self._spot_history: deque = deque(maxlen=60)
        self._extreme_spot      = 0.0

        # Dashboard timestamps
        self._last_console_dash_ts = 0.0

        # PnL tracker
        self.db = MCXDBManager()

        # Load today's state
        self._load_state()

    # ──────────────────────────────────────────
    # State persistence
    # ──────────────────────────────────────────
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
            if state.get('date') != today_str:
                return  # Different day — start fresh

            self.positions          = state.get('positions', {})
            self.total_realized_pnl = float(state.get('total_realized_pnl', 0.0))
            self.trades_today       = int(state.get('trades_today', 0))
            self.last_reentry_ts    = float(state.get('last_reentry_ts', 0.0))
            self.last_any_close_ts  = float(state.get('last_any_close_ts', 0.0))

            is_strangle = ('CE' in self.positions and 'PE' in self.positions)
            for leg, pos in self.positions.items():
                sl_st = pos.setdefault('sl_state', {})
                # Rebuild SL state cleanly
                entry   = float(pos.get('entry_price', 0.0))
                best    = float(sl_st.get('best_premium', entry))
                solo    = not is_strangle

                if solo:
                    anchor = float(sl_st.get('anchor_ltp', entry))
                    new_sl = round_to_tick(best * (1.0 + SOLO_TSL_PCT))
                else:
                    anchor = entry
                    new_sl = round_to_tick(entry * (1.0 + STRANGLE_SL_PCT))

                curr_sl = float(sl_st.get('current_sl', new_sl))
                sl_st['current_sl']    = min(curr_sl, new_sl)  # ratchet to tightest
                sl_st['best_premium']  = best
                sl_st['anchor_ltp']    = anchor
                sl_st['solo_mode']     = solo
                sl_st['breach_start_ts'] = 0.0

            self.db.commit_daily_pnl(self.total_realized_pnl)
            mode = "strangle" if is_strangle else ("solo" if self.positions else "flat")
            print(f'[STATE] Restored: {len(self.positions)} leg(s) | '
                  f'mode={mode} | Realized=₹{self.total_realized_pnl:,.2f} | '
                  f'Trades={self.trades_today}', flush=True)
        except Exception as e:
            print(f'[WARN] Error loading MCX state: {e}', flush=True)

    # ──────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────
    def authenticate(self):
        if not self.api:
            detail = f' ({_noren_import_error})' if _noren_import_error else ''
            raise RuntimeError(f'NorenApiPy not available{detail}.')

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
            if not limits or limits.get('stat') != 'Ok':
                print('[WARN] Token validation notice: proceeding in paper mode.', flush=True)
        except Exception as e:
            print(f'[WARN] Session warning: {e}', flush=True)
        print(f'[OK] MCX NatGas v6.0 PAPER bot authenticated.', flush=True)

    # ──────────────────────────────────────────
    # MCX symbol master
    # ──────────────────────────────────────────
    def _get_mcx_csv(self):
        if self._mcx_master is not None:
            return self._mcx_master

        import pandas as pd
        today_ist = get_ist_now().strftime('%Y-%m-%d')
        csv_file  = f'MCX_symbols_{today_ist}.csv'

        if not os.path.exists(csv_file):
            try:
                url = 'https://api.shoonya.com/MCX_symbols.txt.zip'
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    with zipfile.ZipFile(io.BytesIO(resp.read())) as z:
                        with z.open('MCX_symbols.txt') as f:
                            pd.read_csv(f).to_csv(csv_file, index=False)
                print(f'[OK] Downloaded {csv_file}', flush=True)
            except Exception as e:
                print(f'[WARN] Auto-download failed: {e}', flush=True)

        if not os.path.exists(csv_file):
            existing = sorted(glob.glob('MCX_symbols_*.csv'), reverse=True)
            if existing:
                csv_file = existing[0]
            else:
                print('[ERROR] No MCX_symbols_*.csv found!', flush=True)
                return None

        try:
            df = pd.read_csv(csv_file)
            df['ExpiryDate'] = pd.to_datetime(df['Expiry'], format='%d-%b-%Y', errors='coerce')
            self._mcx_master = df

            today_date = get_ist_now().date()
            today_ts   = pd.Timestamp(today_date)

            opt_df = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'OPTFUT')]
            avail  = opt_df[opt_df['ExpiryDate'] >= today_ts]
            sorted_expiries = sorted(avail['ExpiryDate'].dropna().unique())
            if not sorted_expiries:
                sorted_expiries = sorted(opt_df['ExpiryDate'].dropna().unique())

            if sorted_expiries:
                curr_exp  = sorted_expiries[0]
                curr_date = pd.to_datetime(curr_exp).date()
                if is_expiry_week(today_date, curr_date) and len(sorted_expiries) > 1:
                    self.target_opt_expiry_ts  = sorted_expiries[1]
                    self.is_rolled_over        = True
                    print(f'[ROLLOVER] Rolled to next expiry: '
                          f'{pd.to_datetime(sorted_expiries[1]).strftime("%d-%b-%Y")}', flush=True)
                else:
                    self.target_opt_expiry_ts  = sorted_expiries[0]
                    self.is_rolled_over        = False
                    dte = (curr_date - today_date).days
                    print(f'[EXPIRY] Front-month: {curr_date.strftime("%d-%b-%Y")} (DTE={dte})', flush=True)
                self.target_opt_expiry_str = pd.to_datetime(self.target_opt_expiry_ts).strftime('%d-%b-%Y')
            else:
                self.target_opt_expiry_ts  = today_ts
                self.target_opt_expiry_str = today_date.strftime('%d-%b-%Y')
                self.is_rolled_over        = False

            futs = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'FUTCOM')]
            ff   = futs[futs['ExpiryDate'] >= (self.target_opt_expiry_ts if self.target_opt_expiry_ts else today_ts)]
            if ff.empty:
                ff = futs[futs['ExpiryDate'] >= today_ts]
            if ff.empty:
                ff = futs
            if not ff.empty:
                row = ff.sort_values('ExpiryDate').iloc[0]
                self.front_month_futs_token  = str(row['Token'])
                self.front_month_futs_symbol = str(row['TradingSymbol'])
                print(f'[INFO] Underlying future: {self.front_month_futs_symbol} '
                      f'(Token={self.front_month_futs_token})', flush=True)
            return self._mcx_master
        except Exception as e:
            print(f'[ERROR] Failed loading symbol CSV: {e}', flush=True)
            return None

    # ──────────────────────────────────────────
    # Live spot price
    # ──────────────────────────────────────────
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
                        if 'NATURALGAS' in tsym and 'MINI' not in tsym \
                                and not tsym.endswith('CE') and not tsym.endswith('PE'):
                            q   = self.api.get_quotes(exchange='MCX', token=item.get('token'))
                            val = float(q.get('lp', q.get('ltp', 0.0)) or 0.0) if q and isinstance(q, dict) else 0.0
                            if val > 50.0:
                                self._spot_cache = {'ts': now_ts, 'val': val}
                                return val
            except Exception:
                pass

        return self._spot_cache['val']

    # ──────────────────────────────────────────
    # Option symbol lookup
    # ──────────────────────────────────────────
    def find_option_symbol(self, strike: float, option_type: str) -> Optional[Dict]:
        import pandas as pd
        df = self._get_mcx_csv()
        if df is None:
            return None
        try:
            opt_df = df[
                (df['Symbol']      == 'NATURALGAS') &
                (df['Instrument']  == 'OPTFUT') &
                (df['OptionType']  == option_type) &
                (df['StrikePrice'] == float(strike))
            ]
            if opt_df.empty:
                return None

            if getattr(self, 'target_opt_expiry_ts', None) is not None:
                target = opt_df[opt_df['ExpiryDate'] == self.target_opt_expiry_ts]
            else:
                today_ts = pd.Timestamp(get_ist_now().date())
                target   = opt_df[opt_df['ExpiryDate'] >= today_ts]

            if target.empty:
                today_ts = pd.Timestamp(get_ist_now().date())
                target   = opt_df[opt_df['ExpiryDate'] >= today_ts]
            if target.empty:
                target = opt_df

            row   = target.sort_values('ExpiryDate').iloc[0]
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
            print(f'[ERROR] Symbol lookup {strike} {option_type}: {e}', flush=True)
            return None

    # ──────────────────────────────────────────
    # Live LTP for open leg
    # ──────────────────────────────────────────
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
                                    pos['_last_ltp']    = val
                                    pos['_last_ltp_ts'] = now_ts
                                    return val
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass
        return pos.get('_last_ltp', pos['entry_price'])

    # ──────────────────────────────────────────
    # DTE helper
    # ──────────────────────────────────────────
    def _get_dte_days(self) -> float:
        if not self.target_opt_expiry_ts:
            return 20.0
        try:
            import pandas as pd
            today_date  = get_ist_now().date()
            expiry_date = pd.to_datetime(self.target_opt_expiry_ts).date()
            dte = (expiry_date - today_date).days
            return float(max(0, dte))
        except Exception:
            return 20.0

    # ──────────────────────────────────────────
    # Enter a single leg
    # ──────────────────────────────────────────
    def _enter_leg(self, leg: str, strike: float, side: str = 'SELL',
                   reason: str = "Standard Entry") -> Optional[dict]:
        option_type = 'CE' if leg == 'CE' else 'PE'
        match = self.find_option_symbol(strike, option_type)
        if not match:
            print(f'[WARN] Cannot resolve contract for {leg} Strike {strike}.', flush=True)
            return None

        tsym = match['tsym']
        ltp  = float(match.get('lp', 0.0))
        if ltp <= 0:
            print(f'[WARN] LTP=0 for {tsym}. Skipping.', flush=True)
            return None

        # ── Clean UPV2-style SL state (pure % only) ───────────────
        initial_sl = round_to_tick(ltp * (1.0 + STRANGLE_SL_PCT))
        now_ts     = time.time()

        pos = {
            'leg':          leg,
            'tsym':         tsym,
            'token':        match.get('token', ''),
            'strike':       strike,
            'side':         side,
            'qty':          LOT_SIZE,
            'entry_price':  ltp,
            '_last_ltp':    ltp,
            '_last_ltp_ts': now_ts,
            'sl_state': {
                'best_premium':   ltp,        # lowest seen (best for seller)
                'anchor_ltp':     ltp,        # reference point for solo anchoring
                'current_sl':     initial_sl, # 12% above entry (strangle mode)
                'initial_sl':     initial_sl,
                'solo_mode':      False,
                'breach_start_ts': 0.0,
            }
        }
        self.positions[leg] = pos
        self.trades_today  += 1
        self._save_state()

        tg = (f'<pre>\n━━━ MCX TRADE OPENED (v6.0) ━━━\n\n'
              f'  {leg:<4} Strike {int(strike)}  {side} @ {ltp:.2f}\n'
              f'  SL   {STRANGLE_SL_PCT*100:.0f}%  →  {initial_sl:.2f}\n'
              f'  Qty  {LOT_SIZE}\n  {tsym}\n</pre>')
        print(f'[ENTRY] {side} {LOT_SIZE}x {leg} Strike={int(strike)} ({tsym}) @ ₹{ltp:.2f}', flush=True)
        send_telegram(tg)
        return pos

    # ──────────────────────────────────────────
    # Re-anchor surviving leg to live LTP (solo mode)
    # ──────────────────────────────────────────
    def _anchor_surviving_leg(self, surviving_leg: str):
        """
        When partner leg exits, immediately re-anchor the surviving leg.
        New SL = live_ltp * (1 + 7%)  — ratchets only downward from here.
        """
        pos = self.positions.get(surviving_leg)
        if not pos:
            return
        ltp   = self._get_leg_ltp(pos)
        new_sl = round_to_tick(ltp * (1.0 + SOLO_TSL_PCT))
        state  = pos.setdefault('sl_state', {})

        state['anchor_ltp']      = ltp
        state['best_premium']    = ltp
        state['current_sl']      = new_sl
        state['solo_mode']       = True
        state['breach_start_ts'] = 0.0
        pos['_last_ltp']         = ltp

        print(f'🎯 [SOLO ANCHOR] {surviving_leg} anchored @ LTP=₹{ltp:.2f} | '
              f'TSL=₹{new_sl:.2f} ({SOLO_TSL_PCT*100:.0f}%)', flush=True)
        self._save_state()

    # ──────────────────────────────────────────
    # Close a single leg
    # ──────────────────────────────────────────
    def _close_leg(self, leg: str, reason: str, exit_price: Optional[float] = None):
        pos = self.positions.get(leg)
        if not pos:
            return
        ltp = exit_price if (exit_price and exit_price > 0) else self._get_leg_ltp(pos)
        pnl = (pos['entry_price'] - ltp) * pos['qty'] if pos['side'] == 'SELL' \
              else (ltp - pos['entry_price']) * pos['qty']

        self.total_realized_pnl += pnl
        sign     = '+' if pnl >= 0 else ''
        tot_sign = '+' if self.total_realized_pnl >= 0 else ''

        # Trade log (ISO timestamp for web dashboard)
        self.trade_log.append({
            'leg':       leg,
            'tsym':      pos.get('tsym', leg),
            'strike':    int(pos['strike']),
            'entry':     pos['entry_price'],
            'exit':      ltp,
            'qty':       pos['qty'],
            'pnl':       pnl,
            'reason':    reason,
            'time':      get_ist_now().strftime('%H:%M:%S'),
            'timestamp': get_ist_now().strftime('%Y-%m-%d %H:%M:%S'),
            'action':    'EXIT',
        })

        tg = (f'<pre>\n━━━ MCX TRADE CLOSED (v6.0) ━━━\n\n'
              f'  {leg:<4} {int(pos["strike"]):<5} {reason}\n'
              f'  Entry  {pos["entry_price"]:.2f}\n'
              f'  Exit   {ltp:.2f}\n'
              f'  PnL    {sign}₹{pnl:,.2f}\n\n'
              f'  Realized: {tot_sign}₹{self.total_realized_pnl:,.2f}\n</pre>')
        print(f'[EXIT] {pos["qty"]}x {pos.get("tsym", leg)} @ ₹{ltp:.2f} | '
              f'PnL: {sign}₹{pnl:,.2f} | {reason}', flush=True)
        send_telegram(tg)
        del self.positions[leg]

        # Re-anchor surviving leg immediately
        if len(self.positions) == 1:
            surviving = list(self.positions.keys())[0]
            self._anchor_surviving_leg(surviving)

        self.last_any_close_ts = time.time()
        self._save_state()

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)

    # ──────────────────────────────────────────
    # In-Place Strangle Rebalance
    # ──────────────────────────────────────────
    def _rebalance_in_place(self, solo_leg: str, atm: float, live_ltp: float) -> bool:
        """
        Solo leg's TSL was hit AND its strike == ATM.
        Instead of exiting: preserve the leg, enter the missing partner, reset SL state.
        """
        pos = self.positions.get(solo_leg)
        if not pos:
            return False

        other_leg    = 'PE' if solo_leg == 'CE' else 'CE'
        other_strike = atm

        print(f'[IN-PLACE REBALANCE] {solo_leg} TSL hit at ₹{live_ltp:.2f} (strike={int(pos["strike"])} == ATM={int(atm)}). '
              f'Preserving & entering {other_leg} at {int(atm)}...', flush=True)

        other_pos = self._enter_leg(other_leg, other_strike, 'SELL',
                                    reason=f"In-place rebalance (partner of {solo_leg})")
        if not other_pos:
            print(f'[WARN] Failed to enter {other_leg} for rebalance — falling back to close.', flush=True)
            return False

        # Reset the preserved leg's SL state to fresh strangle mode
        actual_entry = pos['entry_price']
        pos['_last_ltp'] = live_ltp
        fresh_sl = round_to_tick(live_ltp * (1.0 + STRANGLE_SL_PCT))
        pos['sl_state'] = {
            'best_premium':    live_ltp,
            'anchor_ltp':      live_ltp,
            'current_sl':      fresh_sl,
            'initial_sl':      fresh_sl,
            'solo_mode':       False,
            'breach_start_ts': 0.0,
        }

        self.last_reentry_ts = time.time()
        self._reversal_latched = False
        self._save_state()

        tg = (f'<pre>\n━━━ MCX IN-PLACE REBALANCE (v6.0) ━━━\n\n'
              f'  Preserved: {solo_leg} {int(pos["strike"])} (Entry={actual_entry:.2f} Live={live_ltp:.2f})\n'
              f'  Entered:   {other_leg} {int(other_strike)}\n'
              f'  SL reset:  {STRANGLE_SL_PCT*100:.0f}% → ₹{fresh_sl:.2f}\n</pre>')
        send_telegram(tg)
        return True

    # ──────────────────────────────────────────
    # SL / TSL engine  (UPV2 logic — pure %)
    # ──────────────────────────────────────────
    def _update_sl_and_check(self, leg: str, live_ltp: float) -> Tuple[bool, str]:
        """
        Returns (sl_triggered: bool, reason: str).

        Strangle mode (both legs open):
          - Phase A (not in profit): hold at entry * 1.12
          - Phase B (in profit):     trail at best_premium * 1.07  (ratchet down only)

        Solo mode (partner exited, re-anchored):
          - Always trail at best_premium * 1.07  (ratchet down only)

        2-second debounce on every breach.
        """
        pos = self.positions.get(leg)
        if not pos or pos['side'] != 'SELL' or live_ltp <= 0:
            return False, ''

        state      = pos['sl_state']
        entry_prem = float(pos['entry_price'])
        now_ts     = time.time()
        is_solo    = bool(state.get('solo_mode', False))

        # Update best (lowest) premium ever seen
        best = float(state.get('best_premium', entry_prem))
        if live_ltp < best:
            best = live_ltp
            state['best_premium'] = round(best, 2)

        if is_solo:
            # ── SOLO LEG MODE ──────────────────────────────────────
            # Pure 7% TSL above best premium, ratchet only downward
            new_sl = round_to_tick(best * (1.0 + SOLO_TSL_PCT))
            # Ratchet: SL can never go higher than it was
            if 'current_sl' in state:
                prem_sl = min(new_sl, state['current_sl'])
            else:
                prem_sl = new_sl
            state['current_sl'] = prem_sl

        else:
            # ── STRANGLE MODE (both legs open) ─────────────────────
            state['solo_mode'] = False
            initial_sl = round_to_tick(entry_prem * (1.0 + STRANGLE_SL_PCT))

            if best >= entry_prem:
                # Phase A: not in profit yet — hold at initial SL
                prem_sl = initial_sl
            else:
                # Phase B: in profit — trail at 7% above best
                trail_sl = round_to_tick(best * (1.0 + SOLO_TSL_PCT))
                prem_sl  = min(trail_sl, initial_sl)

            # Ratchet: SL can never go higher
            if 'current_sl' in state:
                prem_sl = min(prem_sl, state['current_sl'])
            state['current_sl'] = prem_sl

        # 2-second debounce
        if live_ltp >= prem_sl:
            breach_start = state.get('breach_start_ts', 0.0)
            if breach_start <= 0.0:
                state['breach_start_ts'] = now_ts
            elif (now_ts - breach_start) >= SL_DEBOUNCE_SECS:
                mode_label = 'Solo TSL' if is_solo else 'Strangle SL'
                return True, (f'{mode_label} hit on {leg} | '
                              f'LTP={live_ltp:.2f} >= SL={prem_sl:.2f} | '
                              f'Entry={entry_prem:.2f} Best={best:.2f}')
        else:
            state['breach_start_ts'] = 0.0

        return False, ''

    # ──────────────────────────────────────────
    # Reversal / swing tracker
    # ──────────────────────────────────────────
    def _update_reversal_tracker(self, spot: float, confirmed_signal: int):
        """
        Tracks extreme spot price while a solo leg is open.
        Sets _reversal_latched when price pulls back >= SWING_REVERSAL_PTS from extreme.
        """
        self._spot_history.append(spot)
        short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']
        if len(short_legs) != 1:
            self._extreme_spot = 0.0
            return

        surviving = short_legs[0]
        if self._extreme_spot <= 0:
            self._extreme_spot = spot

        if surviving == 'PE':
            # PE open → market went DOWN → track low and watch for bounce UP
            if spot < self._extreme_spot:
                self._extreme_spot = spot
            pullback = spot - self._extreme_spot
            if pullback >= SWING_REVERSAL_PTS:
                self._reversal_latched = True

        elif surviving == 'CE':
            # CE open → market went UP → track high and watch for pullback DOWN
            if spot > self._extreme_spot:
                self._extreme_spot = spot
            pullback = self._extreme_spot - spot
            if pullback >= SWING_REVERSAL_PTS:
                self._reversal_latched = True

    # ──────────────────────────────────────────
    # Always-one-leg-open safety net
    # ──────────────────────────────────────────
    def _ensure_always_one_leg_open(self, atm: float, confirmed_sig: int):
        """
        If zero short legs remain, immediately re-enter a balanced ATM straddle.
        This is the last-resort safety net — should only fire when both legs hit SL
        in quick succession.
        """
        print(f'⚠️ [ALWAYS-ON] 0 short legs open. Re-entering balanced ATM Straddle at {int(atm)}...', flush=True)
        entered_any = False
        for leg in ('CE', 'PE'):
            if leg in self.positions and self.positions[leg].get('side') == 'SELL':
                continue
            if self._enter_leg(leg, atm, 'SELL', reason="Always-On: restore straddle"):
                entered_any = True
                print(f'✅ [ALWAYS-ON] Entered {leg} SELL at ATM {int(atm)}.', flush=True)
        if entered_any:
            self._reversal_latched = False
            self._save_state()
        else:
            print('[WARN] Always-On: could not enter any leg. Retrying next tick.', flush=True)

    # ──────────────────────────────────────────
    # Console + Telegram dashboard
    # ──────────────────────────────────────────
    def _render_dashboard(self, spot: float, atm: float, ema_snap: Dict[str, Any]):
        global _last_tg_dash_msg_ids, _last_tg_dash_edit_ts, _tg_rate_limited_until

        now    = get_ist_now()
        now_ts = time.time()

        # ── Build snap_rows & compute PnL ─────────────────────────
        snap_rows    = []
        total_unreal = 0.0
        sl_risk_total = 0.0

        for leg, pos in list(self.positions.items()):
            ltp      = self._get_leg_ltp(pos)
            is_short = (pos['side'] == 'SELL')
            pnl      = ((pos['entry_price'] - ltp) if is_short else (ltp - pos['entry_price'])) * pos['qty']
            total_unreal += pnl

            sl   = pos.get('sl_state', {}).get('current_sl', 0.0)
            best = pos.get('sl_state', {}).get('best_premium', pos['entry_price'])

            if is_short and sl > 0.0:
                # Risk if SL hits right now = (Entry - SL) * Qty  (negative = loss)
                sl_risk_total += (pos['entry_price'] - sl) * pos['qty']

            snap_rows.append({
                'leg':        leg,
                'strike':     pos['strike'],
                'side':       pos['side'],
                'entry':      pos['entry_price'],
                'best_price': best,
                'ltp':        ltp,
                'sl':         sl,
                'pnl':        pnl,
                'qty':        pos['qty'],
                'tsym':       pos.get('tsym', ''),
                'solo_mode':  pos.get('sl_state', {}).get('solo_mode', False),
            })

        net     = self.total_realized_pnl + total_unreal
        net_pct = (net / CAPITAL) * 100.0

        # ── Console (every 1 s) ────────────────────────────────────
        if now_ts - self._last_console_dash_ts >= 1.0:
            self._last_console_dash_ts = now_ts

            W   = 120
            DIM = f'{Fore.WHITE}{Style.DIM}'
            CY  = f'{Fore.CYAN}{Style.BRIGHT}'
            WH  = f'{Fore.WHITE}{Style.BRIGHT}'
            YL  = f'{Fore.YELLOW}{Style.BRIGHT}'
            GR  = f'{Fore.GREEN}{Style.BRIGHT}'
            RD  = f'{Fore.RED}{Style.BRIGHT}'
            MG  = f'{Fore.MAGENTA}{Style.BRIGHT}'
            RS  = Style.RESET_ALL

            TOP  = f'{DIM}╔{"═"*W}╗{RS}'
            BOT  = f'{DIM}╚{"═"*W}╝{RS}'
            MID  = f'{DIM}╠{"═"*W}╣{RS}'
            MIDS = f'{DIM}╟{"─"*W}╢{RS}'
            V    = f'{DIM}║{RS}'
            VS   = f'{DIM}│{RS}'

            ema15 = ema_snap.get("ema_15", spot)
            ema90 = ema_snap.get("ema_90", spot)
            slope = ema_snap.get("slow_slope", 0.0)
            vr    = ema_snap.get("vr", 1.0)
            preq  = ema_snap.get("persistence_req", 5.0)
            sig   = ema_snap.get("confirmed_signal", 0)
            hold  = ema_snap.get("hold_time", 0.0)

            sig_str = f"{GR}▲ UP{RS}" if sig > 0 else (f"{RD}▼ DOWN{RS}" if sig < 0 else f"{YL}━ FLAT{RS}")

            cooldown_left = max(0.0, POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))
            cooldown_tag  = f"  {YL}[COOLDOWN {cooldown_left:.0f}s]{RS}" if cooldown_left > 0 else ""
            reversal_tag  = f"  {MG}[REVERSAL LATCHED]{RS}" if self._reversal_latched else ""

            print()
            print(TOP)

            title_l = (f'  {CY}MCX NATGAS PAPER v6.0{RS}  {DIM}│{RS}  '
                       f'{YL}UPV2-ALIGNED EMA ENGINE{RS}  {DIM}│{RS}  '
                       f'{GR}12% SL / 7% TSL{RS}')
            title_r = f'{DIM}{now.strftime("%H:%M:%S IST")}{RS}  '
            pad_top = max(1, W - _ansi_len(title_l) - _ansi_len(title_r))
            print(f'{V}{title_l}{" " * pad_top}{title_r}{V}')
            print(MID)

            exp_badge = (f"{YL}{self.target_opt_expiry_str}{RS} ({CY}ROLLOVER{RS})"
                         if self.is_rolled_over else f"{WH}{self.target_opt_expiry_str}{RS}")
            ind_row = (f'  {DIM}SPOT:{RS} {WH}{spot:>8.2f}{RS}  '
                       f'{DIM}ATM:{RS} {YL}{int(atm):<5}{RS}  '
                       f'{DIM}EXPIRY:{RS} {exp_badge}  '
                       f'{DIM}TRADES:{RS} {WH}{self.trades_today}{RS}'
                       f'{reversal_tag}{cooldown_tag}')
            print(f'{V}{_pad(ind_row, W)}{V}')
            print(MIDS)

            mom_row = (f'  {CY}EMA ENGINE:{RS} {DIM}FAST:{RS} {WH}{ema15:>8.2f}{RS}  '
                       f'{DIM}SLOW:{RS} {WH}{ema90:>8.2f}{RS}  '
                       f'{DIM}SLOPE/m:{RS} {WH}{slope:>+6.3f}{RS}  '
                       f'{DIM}VR:{RS} {WH}{vr:>4.2f}{RS}  '
                       f'{DIM}PERSIST:{RS} {YL}{preq:>4.1f}s{RS}  '
                       f'{DIM}SIGNAL:{RS} {sig_str} {DIM}({hold:.1f}s){RS}')
            print(f'{V}{_pad(mom_row, W)}{V}')
            print(MID)

            if not snap_rows:
                msg = f'  {YL}No open positions — scanning for ATM entry...{RS}'
                print(f'{V}{_pad(msg, W)}{V}')
            else:
                hdr = (f'  {"LEG":<7} {VS} {"CONTRACT":<22} {VS} {"STRIKE":>7} {VS} {"SIDE":<5} {VS} '
                       f'{"ENTRY":>8} {VS} {"BEST":>10} {VS} {"LTP":>8} {VS} '
                       f'{"SL":>9} {VS} {"PNL":>14}  ')
                print(f'{V}{_pad(hdr, W)}{V}')
                print(MIDS)

                for r in snap_rows:
                    pnl_sign, pnl_fmt = _fmt_pnl(r['pnl'], width=12)
                    pnl_col  = GR if r['pnl'] > 0 else (RD if r['pnl'] < 0 else YL)
                    side_col = RD if r['side'] == 'SELL' else GR
                    leg_label = f"{r['leg']}*" if r.get('solo_mode') else r['leg']

                    row = (f"  {WH}{leg_label:<7}{RS} {VS} {CY}{r['tsym']:<22}{RS} {VS} {WH}{int(r['strike']):>7}{RS} {VS} "
                           f"{side_col}{r['side']:<5}{RS} {VS} "
                           f"{WH}{r['entry']:>8.2f}{RS} {VS} "
                           f"{DIM}{r['best_price']:>10.2f}{RS} {VS} "
                           f"{YL}{r['ltp']:>8.2f}{RS} {VS} "
                           f"{MG}{r['sl']:>9.2f}{RS} {VS} "
                           f"  {pnl_col}{pnl_fmt}{RS}  ")
                    print(f'{V}{_pad(row, W)}{V}')

                if any(r.get('solo_mode') for r in snap_rows):
                    print(MIDS)
                    solo_msg = f'  {CY}🎯 SOLO TSL ACTIVE (*):{RS} Partner exited — {SOLO_TSL_PCT*100:.0f}% trailing stop re-anchored to live LTP'
                    print(f'{V}{_pad(solo_msg, W)}{V}')

            print(MID)

            real_sign,  real_fmt  = _fmt_pnl(self.total_realized_pnl, width=10)
            unreal_sign, unreal_fmt = _fmt_pnl(total_unreal, width=10)
            net_sign,   net_fmt   = _fmt_pnl(net, width=10)

            real_col   = GR if self.total_realized_pnl > 0 else (RD if self.total_realized_pnl < 0 else YL)
            unreal_col = GR if total_unreal > 0 else (RD if total_unreal < 0 else YL)
            net_col    = GR if net > 0 else (RD if net < 0 else YL)

            pnl_row = (f'  {DIM}REALIZED:{RS} {real_col}{real_fmt}{RS}  {VS}  '
                       f'{DIM}UNREALIZED:{RS} {unreal_col}{unreal_fmt}{RS}  {VS}  '
                       f'{DIM}NET MTM:{RS} {net_col}{net_fmt} ({net_pct:+.2f}%){RS}  {VS}  '
                       f'{DIM}SL RISK:{RS} {RD}{sl_risk_total:+,.0f}{RS}')
            print(f'{V}{_pad(pnl_row, W)}{V}')
            print(BOT)
            sys.stdout.flush()

        # ── Write live snapshot for web dashboard ─────────────────
        try:
            mcx_snap = {
                "timestamp":       now.strftime("%Y-%m-%d %H:%M:%S"),
                "spot":            spot,
                "atm":             int(atm),
                "expiry":          self.target_opt_expiry_str,
                "is_rolled_over":  self.is_rolled_over,
                "trades_today":    self.trades_today,
                "reversal_latched": self._reversal_latched,
                "cooldown_remaining": max(0, int(POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))),
                "ema":             ema_snap,
                "positions":       snap_rows,
                "realized_pnl":    round(self.total_realized_pnl, 2),
                "unrealized_pnl":  round(total_unreal, 2),
                "net_pnl":         round(net, 2),
                "net_pct":         round(net_pct, 4),
                "sl_risk":         round(sl_risk_total, 2),
                "mtd_pnl":         self.db.data.get("mtd_pnl", self.total_realized_pnl) if hasattr(self, 'db') else self.total_realized_pnl,
                "ytd_pnl":         self.db.data.get("ytd_pnl", self.total_realized_pnl) if hasattr(self, 'db') else self.total_realized_pnl,
                "trade_log":       self.trade_log,
            }
            snap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "live_snapshot_mcx_paper.json")
            with open(snap_path, "w") as sf:
                json.dump(mcx_snap, sf, indent=2)
        except Exception:
            pass

        # ── Telegram live dashboard (every 3 s) ───────────────────
        if now_ts - _last_tg_dash_edit_ts < 3.0 or now_ts < _tg_rate_limited_until:
            return
        _last_tg_dash_edit_ts = now_ts

        ema15 = ema_snap.get("ema_15", spot)
        ema90 = ema_snap.get("ema_90", spot)
        slope = ema_snap.get("slow_slope", 0.0)
        sig   = ema_snap.get("confirmed_signal", 0)
        sig_t = "▲ UP" if sig > 0 else ("▼ DOWN" if sig < 0 else "━ FLAT")

        has_ce = 'CE' in self.positions
        has_pe = 'PE' in self.positions
        if has_ce and has_pe:
            status_str = "🛡️ STRANGLE ACTIVE"
        elif any(r.get('solo_mode') for r in snap_rows):
            status_str = "🎯 SOLO TRAILING"
        elif has_ce or has_pe:
            status_str = "🎯 1-LEG ACTIVE"
        else:
            status_str = "⚙️ SCANNING"

        roll_tag = " ⟳NEXT MO" if self.is_rolled_over else ""
        r_sign   = '+' if self.total_realized_pnl >= 0 else ''
        n_sign   = '+' if net >= 0 else ''
        pnl_badge = "🟢" if net >= 0 else "🔴"

        t  = f"<b>⚡ MCX NATGAS · PAPER v6.0</b>\n"
        t += f"<code>🕐 {now.strftime('%H:%M:%S IST')}</code>\n"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += f"<b>SPOT:</b>   <code>{spot:>8.2f}</code>  <b>ATM:</b> <code>{int(atm)}</code>\n"
        t += f"<b>EXPIRY:</b> <code>{self.target_opt_expiry_str}{roll_tag}</code>\n"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += f"<b>EMA ENGINE</b>  [3–20s persistence]\n<pre>"
        t += f"  EMA15 : {ema15:>8.2f}\n"
        t += f"  EMA90 : {ema90:>8.2f}\n"
        t += f"  SLOPE : {slope:>+8.3f}/m\n"
        t += f"  VR    : {ema_snap.get('vr',1.0):>8.2f}  P={ema_snap.get('persistence_req',5.0):.1f}s\n"
        t += f"  SIGNAL: {sig_t:>8}  HOLD={ema_snap.get('hold_time',0.0):.1f}s\n</pre>"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += f"<b>STATUS:</b> {status_str}  <b>TRADES:</b> <code>{self.trades_today}</code>\n"
        t += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += "<pre>"
        t += f"{'LEG':<6} {'STRIKE':>7} {'ENTRY':>7} {'LTP':>7} {'SL':>8} {'PnL':>10}\n"
        t += f"{'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*8} {'─'*10}\n"
        if snap_rows:
            for r in snap_rows:
                ps  = '+' if r['pnl'] >= 0 else ''
                sl_s = f"{r['sl']:>8.2f}" if r['sl'] > 0 else "       —"
                ltag = f"{r['leg']}*" if r.get('solo_mode') else r['leg']
                t += f"{ltag:<6} {int(r['strike']):>7} {r['entry']:>7.2f} {r['ltp']:>7.2f} {sl_s} {ps}{r['pnl']:>9,.0f}\n"
        else:
            t += "  — No Open Positions —\n"
        t += f"{'─'*48}\n"
        t += f"{'Realized':>16}: {r_sign}₹{self.total_realized_pnl:>10,.2f}\n"
        t += f"{'─'*48}\n"
        t += f"{'NET MTM':>16}: {pnl_badge}{n_sign}₹{net:>9,.2f} ({net_pct:+.2f}%)\n</pre>"
        t += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        t += f"<b>SL:</b> <code>{STRANGLE_SL_PCT*100:.0f}%</code>  <b>TSL:</b> <code>{SOLO_TSL_PCT*100:.0f}%</code>  <b>LOT:</b> <code>1×{LOT_SIZE}u</code>  <b>MODE:</b> <code>PAPER</code>"

        chat_ids = _get_tg_chat_ids()
        for cid in chat_ids:
            msg_id = _last_tg_dash_msg_ids.get(cid)
            if msg_id is not None:
                try:
                    r = requests.post(
                        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText',
                        json={'chat_id': cid, 'message_id': msg_id, 'text': t, 'parse_mode': 'HTML'},
                        timeout=3)
                    if r.status_code == 200:
                        continue
                    if r.status_code == 429:
                        _tg_rate_limited_until = time.time() + r.json().get('parameters', {}).get('retry_after', 30)
                        return
                    _last_tg_dash_msg_ids.pop(cid, None)
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

    # ──────────────────────────────────────────
    # EOD Trade Summary
    # ──────────────────────────────────────────
    def _send_eod_trade_summary(self):
        final_pct = (self.total_realized_pnl / CAPITAL) * 100.0
        s_sign    = '+' if self.total_realized_pnl >= 0 else ''

        for leg_name in ('CE', 'PE'):
            trades = [t for t in self.trade_log if t['leg'] == leg_name]
            if not trades:
                msg = f'<pre>━━━ MCX {leg_name} TRADES ━━━\n  No trades.\n</pre>'
            else:
                leg_tot  = sum(t['pnl'] for t in trades)
                lt_sign  = '+' if leg_tot >= 0 else ''
                msg  = f'<pre>━━━ MCX {leg_name} TRADES ({len(trades)}) ━━━\n'
                msg += f"{'#':<3} {'TIME':<9} {'STRIKE':>7} {'ENTRY':>7} {'EXIT':>7} {'PnL':>10}\n"
                msg += f"{'─'*3} {'─'*9} {'─'*7} {'─'*7} {'─'*7} {'─'*10}\n"
                for i, t in enumerate(trades, 1):
                    ps   = '+' if t['pnl'] >= 0 else ''
                    ttime = t.get('time', '--:--:--')
                    msg += f"{i:<3} {ttime:<9} {t['strike']:>7} {t['entry']:>7.2f} {t['exit']:>7.2f} {ps}{t['pnl']:>9,.0f}\n"
                msg += f"{'─'*51}\n"
                msg += f"{'LEG TOTAL':>32}: {lt_sign}₹{leg_tot:>9,.0f}\n</pre>"
            send_telegram(msg)
            time.sleep(0.4)

        ce_t   = [t for t in self.trade_log if t['leg'] == 'CE']
        pe_t   = [t for t in self.trade_log if t['leg'] == 'PE']
        ce_tot = sum(t['pnl'] for t in ce_t)
        pe_tot = sum(t['pnl'] for t in pe_t)
        msg  = '<pre>━━━ MCX SESSION SUMMARY ━━━\n'
        msg += f"  CE: {len(ce_t):>3} trades | wins: {sum(1 for t in ce_t if t['pnl']>=0):>3} | {'+' if ce_tot>=0 else ''}₹{ce_tot:,.0f}\n"
        msg += f"  PE: {len(pe_t):>3} trades | wins: {sum(1 for t in pe_t if t['pnl']>=0):>3} | {'+' if pe_tot>=0 else ''}₹{pe_tot:,.0f}\n"
        msg += f"  Total trades : {self.trades_today}\n</pre>"
        send_telegram(msg)
        time.sleep(0.4)

        badge = "✅ PROFIT" if self.total_realized_pnl >= 0 else "❌ LOSS"
        final_msg = (f'<pre>━━━ MCX FINAL PNL ━━━\n'
                     f'  Realized PnL : {s_sign}₹{self.total_realized_pnl:,.2f}\n'
                     f'  Return on 2L : {final_pct:+.2f}%\n'
                     f'  {badge}\n</pre>')
        send_telegram(final_msg)

    # ──────────────────────────────────────────
    # Main run loop
    # ──────────────────────────────────────────
    def run(self):
        self.authenticate()
        self._get_mcx_csv()

        DIM = f'{Fore.WHITE}{Style.DIM}'
        CY  = f'{Fore.CYAN}{Style.BRIGHT}'
        GR  = f'{Fore.GREEN}{Style.BRIGHT}'
        RD  = f'{Fore.RED}{Style.BRIGHT}'
        RS  = Style.RESET_ALL

        print()
        print(f'{DIM}{"="*100}{RS}')
        print(f'{CY}  MCX NATURAL GAS PAPER TRADING  v6.0  |  UPV2-ALIGNED EMA  |  16:00 – 23:24 IST{RS}')
        print(f'{CY}  EMA: Fast={EMA_FAST_HL}s  Slow={EMA_SLOW_HL}s  Anchor={EMA_ANCHOR_HL}s  '
              f'Persist=[{PERSISTENCE_MIN}–{PERSISTENCE_MAX}]s  '
              f'SL={STRANGLE_SL_PCT*100:.0f}%  TSL={SOLO_TSL_PCT*100:.0f}%{RS}')
        print(f'{DIM}{"="*100}{RS}')
        print(flush=True)

        exp_note = f"\nTarget Expiry: {self.target_opt_expiry_str}" + \
                   (" (Next Month Rollover)" if self.is_rolled_over else "")
        send_telegram(f'<pre>MCX Natural Gas\nPaper Trading Bot Online (v6.0 UPV2-Aligned)\n'
                      f'Session: 16:00 – 23:24 IST{exp_note}\n'
                      f'SL: {STRANGLE_SL_PCT*100:.0f}%  TSL: {SOLO_TSL_PCT*100:.0f}%</pre>')

        last_wait_msg_ts = 0.0
        ema_snap: Dict[str, Any] = {}
        spot = 0.0
        atm  = 0.0

        while True:
            try:
                now    = get_ist_now()
                now_ts = time.time()

                # ── Session guards ────────────────────────────────
                if now.weekday() == 6:
                    print(f'[{now.strftime("%H:%M")}] Sunday — markets closed.', flush=True)
                    time.sleep(60)
                    continue

                if now.weekday() == 5 and now.hour >= 17:
                    print(f'[{now.strftime("%H:%M")}] Saturday 17:00+ — MCX closed.', flush=True)
                    if self.positions:
                        self._close_all('SATURDAY_CLOSE')
                    time.sleep(60)
                    continue

                # ── Auto square-off ───────────────────────────────
                if now.hour > MCX_EXIT_HOUR or (now.hour == MCX_EXIT_HOUR and now.minute >= MCX_EXIT_MINUTE):
                    if self.positions:
                        print(f'[AUTO] {MCX_EXIT_HOUR}:{MCX_EXIT_MINUTE:02d} IST — squaring off...', flush=True)
                        self._close_all('SESSION_END')
                    self._render_dashboard(spot, atm, ema_snap)
                    final_pct = (self.total_realized_pnl / CAPITAL) * 100.0
                    pnl_col   = GR if self.total_realized_pnl >= 0 else RD
                    s         = '+' if self.total_realized_pnl >= 0 else ''
                    print(f'\n{pnl_col}✅ Session complete. PnL: {s}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%){RS}\n', flush=True)
                    self._send_eod_trade_summary()
                    break

                # ── Pre-market wait ───────────────────────────────
                if now.hour < MCX_ENTRY_HOUR or (now.hour == MCX_ENTRY_HOUR and now.minute < MCX_ENTRY_MINUTE):
                    if now_ts - last_wait_msg_ts > 60.0:
                        last_wait_msg_ts = now_ts
                        print(f'[WAIT] Market opens {MCX_ENTRY_HOUR}:{MCX_ENTRY_MINUTE:02d} IST. '
                              f'Now: {now.strftime("%H:%M:%S")}', flush=True)
                    time.sleep(10)
                    continue

                # ── Spot & ATM ────────────────────────────────────
                spot = self.get_spot()
                if spot <= 50.0:
                    time.sleep(3)
                    continue
                atm = round_to_price(spot, STRIKE_STEP)

                # ── EMA Engine update ─────────────────────────────
                ema_snap      = self.ema_engine.update(spot, now_ts)
                confirmed_sig = ema_snap.get("confirmed_signal", 0)

                # ── Reversal tracker ──────────────────────────────
                self._update_reversal_tracker(spot, confirmed_sig)

                # ═════════════════════════════════════════════════
                # STEP 1: NO POSITIONS → ENTER BALANCED ATM STRADDLE
                # ═════════════════════════════════════════════════
                if not self.positions:
                    time_since_close = now_ts - self.last_any_close_ts
                    if time_since_close >= POST_CLOSE_COOLDOWN:
                        # Defer one leg ONLY on strong, confirmed trend
                        is_strong_trend = (
                            confirmed_sig != 0
                            and ema_snap.get("hold_time", 0.0) >= STRONG_TREND_HOLD_S
                            and abs(ema_snap.get("ema_15", spot) - ema_snap.get("ema_90", spot)) >= STRONG_TREND_SPREAD
                        )

                        if is_strong_trend and confirmed_sig == 1:
                            print(f'[MOMENTUM ENTRY] Bullish (hold>={STRONG_TREND_HOLD_S}s). '
                                  f'Writing PE only at ATM {int(atm)} (CE deferred).', flush=True)
                            self._enter_leg('PE', atm, 'SELL', reason="Strong Bullish — PE only")
                        elif is_strong_trend and confirmed_sig == -1:
                            print(f'[MOMENTUM ENTRY] Bearish (hold>={STRONG_TREND_HOLD_S}s). '
                                  f'Writing CE only at ATM {int(atm)} (PE deferred).', flush=True)
                            self._enter_leg('CE', atm, 'SELL', reason="Strong Bearish — CE only")
                        else:
                            print(f'[ENTRY] Balanced ATM Straddle at {int(atm)}.', flush=True)
                            self._enter_leg('CE', atm, 'SELL', reason="Balanced Straddle")
                            self._enter_leg('PE', atm, 'SELL', reason="Balanced Straddle")
                        self._reversal_latched = False

                    self._render_dashboard(spot, atm, ema_snap)
                    time.sleep(1.0)
                    continue

                # ═════════════════════════════════════════════════
                # STEP 2: 1 LEG OPEN → TRY TO RESTORE STRANGLE
                # (Direction-aware: CE needs bearish fade, PE needs bullish fade)
                # ═════════════════════════════════════════════════
                short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']

                if len(short_legs) == 1:
                    surviving_leg = short_legs[0]
                    missing_leg   = 'CE' if surviving_leg == 'PE' else 'PE'

                    time_since_reentry = now_ts - self.last_reentry_ts
                    time_since_close   = now_ts - self.last_any_close_ts

                    if time_since_reentry >= REENTRY_COOLDOWN_S and time_since_close >= REENTRY_COOLDOWN_S:
                        # Direction-aware reversal check
                        if missing_leg == 'CE':
                            can, rev_reason = ReversionDetector.can_reenter_ce(
                                confirmed_sig, self._reversal_latched)
                        else:
                            can, rev_reason = ReversionDetector.can_reenter_pe(
                                confirmed_sig, self._reversal_latched)

                        if can:
                            print(f'[RE-ENTER] {rev_reason} → restoring strangle. '
                                  f'Entering {missing_leg} at ATM {int(atm)}.', flush=True)
                            if self._enter_leg(missing_leg, atm, 'SELL',
                                               reason=f"Restore strangle ({rev_reason})"):
                                self.last_reentry_ts   = now_ts
                                self._reversal_latched = False
                                # Both legs now in strangle mode — clear solo flags
                                for ln in ('CE', 'PE'):
                                    p = self.positions.get(ln)
                                    if p and 'sl_state' in p:
                                        p['sl_state']['solo_mode']       = False
                                        p['sl_state']['breach_start_ts'] = 0.0
                                self._save_state()

                # ═════════════════════════════════════════════════
                # STEP 3: SL / TSL CHECK FOR ALL OPEN LEGS
                # ═════════════════════════════════════════════════
                legs_to_close: List[Tuple[str, str, float]] = []
                for leg in list(self.positions.keys()):
                    pos = self.positions.get(leg)
                    if not pos or pos['side'] != 'SELL':
                        continue
                    live_ltp         = self._get_leg_ltp(pos)
                    sl_hit, sl_reason = self._update_sl_and_check(leg, live_ltp)

                    if sl_hit:
                        # ── In-place rebalance check (solo TSL + strike == ATM) ──
                        other_leg  = 'PE' if leg == 'CE' else 'CE'
                        other_open = (other_leg in self.positions
                                      and self.positions[other_leg].get('side') == 'SELL')

                        if (not other_open
                                and abs(pos['strike'] - atm) < 0.01
                                and pos.get('sl_state', {}).get('solo_mode', False)):
                            # Guard: do NOT rebalance if EMA is still running against this leg
                            ema_against = ((confirmed_sig == 1 and leg == 'CE') or
                                           (confirmed_sig == -1 and leg == 'PE'))
                            if not ema_against:
                                if self._rebalance_in_place(leg, atm, live_ltp):
                                    continue  # Successfully rebalanced! Skip exit.

                        legs_to_close.append((leg, sl_reason, live_ltp))

                for leg, reason, exit_px in legs_to_close:
                    print(f'[SL ALERT] {reason}', flush=True)
                    self._close_leg(leg, reason, exit_price=exit_px)

                # ── ALWAYS-ON: If 0 short legs remain after closures ──
                active_shorts = [ln for ln, p in self.positions.items() if p['side'] == 'SELL']
                if not active_shorts:
                    self._ensure_always_one_leg_open(atm, confirmed_sig)

                # ═════════════════════════════════════════════════
                # STEP 4: RENDER DASHBOARD
                # ═════════════════════════════════════════════════
                self._render_dashboard(spot, atm, ema_snap)
                time.sleep(1.0)

            except KeyboardInterrupt:
                print('\n[STOP] Keyboard interrupt — squaring off...', flush=True)
                self._close_all('KEYBOARD_INTERRUPT')
                self._render_dashboard(spot, atm, ema_snap)
                final_pct = (self.total_realized_pnl / CAPITAL) * 100.0
                s         = '+' if self.total_realized_pnl >= 0 else ''
                pnl_col   = Fore.GREEN if self.total_realized_pnl >= 0 else Fore.RED
                print(f'\n{pnl_col}✅ Squared off. PnL: {s}₹{self.total_realized_pnl:,.2f} ({final_pct:+.2f}%){Style.RESET_ALL}\n', flush=True)
                self._send_eod_trade_summary()
                break
            except Exception as e:
                import traceback
                print(f'[ERROR] Loop exception: {e}', flush=True)
                traceback.print_exc()
                time.sleep(3.0)


# ─────────────────────────────────────────────
if __name__ == '__main__':
    try:
        bot = NaturalGasPaperBot()
        bot.run()
    except Exception as e:
        print(f'[FATAL] {e}', flush=True)
        send_telegram(f'<pre>MCX Bot Fatal Error (v6.0):\n{e}</pre>')
        sys.exit(1)
