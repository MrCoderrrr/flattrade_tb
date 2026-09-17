"""mcx_naturalgas_paper.py  —  v3.0 (fixed + dashboard)
==========================================================
MCX Natural Gas Naked Short Straddle  |  Paper Trading Mode
Session: 17:00 – 23:24 IST (weekdays)

KEY FIXES over v2:
  1. KAMA reversal signal is LATCHED (not consumed during cooldown).
  2. SL/TSL loop has NO break — all legs checked & closed in one pass.
  3. Re-entry happens BEFORE TSL check in the main loop.
  4. Instant ATM straddle entry whenever positions == 0 (no KAMA wait).
  5. KAMA runs on 1-minute bars (not 1-second ticks) for cleaner signals.
  6. Colorama NIFTY-style dashboard (box-drawing characters, ANSI colour).
"""

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
from typing import Dict, List, Optional, Tuple

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

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
TOKEN_FILE          = 'token.txt'
STRIKE_STEP         = 5.0          # Natural Gas strike step
MCX_ENTRY_HOUR      = 17           # 17:00 IST open
MCX_EXIT_HOUR       = 23
MCX_EXIT_MINUTE     = 24           # 23:24 IST auto square-off
LOT_SIZE            = 1250         # 1 lot = 1250 units
DEFAULT_SL_PCT      = 0.15         # 15% initial stop-loss (fresh straddles)
REENTRY_SL_PCT      = 0.15         # 5% initial stop-loss for reversal re-entry (instant cutoff)
DEFAULT_TSL_PCT     = 0.08         # 8% trailing stop-loss
POST_CLOSE_COOLDOWN = 5.0          # Seconds to wait after any close before re-entry
REVERSAL_MIN_PTS    = 0.80         # Swing reversal threshold (0.80 pts pullback from peak/trough)
MICRO_REVERSAL_PTS  = 0.50         # Fast micro-momentum threshold (0.50 pts with velocity)
REENTRY_COOLDOWN_S  = 10.0         # Min seconds between single-leg re-entries

TELEGRAM_TOKEN = '8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE'
CHAT_ID        = '6307066850'

# Dashboard width (characters)
DASH_W = 90

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


# ─────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────
def round_to_price(value: float, step: float = STRIKE_STEP) -> float:
    return round_to_tick(math.floor(value / step + 0.5) * step)


def round_to_tick(value: float) -> float:
    return round(value * 20.0) / 20.0


def _ansi_len(s: str) -> int:
    """Visual length of a string, stripping ANSI escape codes."""
    return len(re.sub(r'\x1b\[[0-9;]*m', '', s))


def _pad(row: str, width: int) -> str:
    """Right-pad a (possibly ANSI-coloured) row to `width` visual chars."""
    return row + ' ' * max(0, width - _ansi_len(row))


def _fmt_pnl(val: float, width: int = 12) -> Tuple[str, str]:
    """Format PnL with explicit +/- sign, rupee symbol, and right-alignment."""
    if abs(val) < 1e-4:
        val = 0.0
    sign = '+' if val > 0 else ('-' if val < 0 else ' ')
    pnl_str = f"{sign}₹{abs(val):,.2f}"
    return sign, f"{pnl_str:>{width}}"


# ─────────────────────────────────────────────
# Main Bot
# ─────────────────────────────────────────────
class NaturalGasPaperBot:

    def __init__(self):
        self.api                   = NorenApiPy() if NorenApiPy else None
        self.positions: Dict[str, Dict] = {}
        self._reversal_latched     = False       # LATCHED reversal signal (not consumed by cooldown)
        self.last_reentry_ts       = 0.0         # Timestamp of last single-leg re-entry
        self.last_any_close_ts     = 0.0         # Timestamp of last leg close (for POST_CLOSE_COOLDOWN)
        self.total_realized_pnl    = 0.0
        self.trades_today          = 0
        self.state_file            = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   'mcx_state_paper.json')
        self._mcx_master           = None
        self._spot_cache           = {'ts': 0.0, 'val': 0.0}
        self._last_tg_dash_ts      = 0.0
        self._last_console_dash_ts = 0.0
        self.front_month_futs_token: Optional[str] = None
        self.front_month_futs_symbol: Optional[str] = None

        # Reversal tracking state
        self._spot_history: deque  = deque(maxlen=60)
        self._extreme_spot         = 0.0
        self._trend_velocity       = 0.0
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

        print(f'[OK] Natural Gas PAPER TRADING bot authenticated from {token_path}.', flush=True)

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

            futs      = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'FUTCOM')]
            today_ts  = pd.Timestamp(get_ist_now().date())
            future_f  = futs[futs['ExpiryDate'] >= today_ts]
            if future_f.empty:
                future_f = futs
            if not future_f.empty:
                row = future_f.sort_values('ExpiryDate').iloc[0]
                self.front_month_futs_token  = str(row['Token'])
                self.front_month_futs_symbol = str(row['TradingSymbol'])
                print(f'[INFO] Front-month Future: {self.front_month_futs_symbol} '
                      f'(Token: {self.front_month_futs_token})', flush=True)
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
            today_ts = pd.Timestamp(get_ist_now().date())
            opt_df   = df[
                (df['Symbol']      == 'NATURALGAS') &
                (df['Instrument']  == 'OPTFUT') &
                (df['OptionType']  == option_type) &
                (df['StrikePrice'] == float(strike))
            ]
            if opt_df.empty:
                return None
            future_opts = opt_df[opt_df['ExpiryDate'] >= today_ts]
            if future_opts.empty:
                future_opts = opt_df
            row   = future_opts.sort_values('ExpiryDate').iloc[0]
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
        # Return cached value if fetched recently within this tick
        if now_ts - pos.get('_last_ltp_ts', 0.0) < max_age and pos.get('_last_ltp', 0.0) > 0:
            return pos['_last_ltp']

        token = pos.get('token')
        if token and self.api:
            try:
                q = self.api.get_quotes(exchange='MCX', token=token)
                if q and isinstance(q, dict):
                    # Never use 'c' (yesterday's close) for live LTP!
                    for field in ('lp', 'ltp', 'sp1', 'bp1'):
                        raw = q.get(field)
                        if raw is not None:
                            try:
                                val = float(raw)
                                if val > 0:
                                    self._last_spot_for_sim = val

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
            '━━━ MCX TRADE OPENED ━━━',
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
            '━━━ MCX TRADE CLOSED ━━━',
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
            self._extreme_spot = 0.0  # Reset extreme tracking for the new solo leg
        self.last_any_close_ts = time.time()
        self._save_state()

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)

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
            # Initial SL (15%) is active until position moves into profit.
            # Once in profit, trail at tsl_pct (8%).
            state['solo_mode'] = False
            if lowest >= entry_prem:
                target_sl = initial_sl
            else:
                trail_sl  = round_to_tick(lowest * (1.0 + pos['tsl_pct']))
                target_sl = min(trail_sl, initial_sl)

            # Strict ratchet: stop loss can only move down, never up
            current_sl = min(target_sl, state.get('current_sl', initial_sl))
            state['current_sl'] = current_sl

            if live_ltp >= current_sl:
                label = 'TSL Hit' if current_sl < initial_sl else 'SL Hit'
                return True, f'{label} on {leg} ({live_ltp:.2f} >= {current_sl:.2f})'
        else:
            # ── STRANGLE IS OFF (Solo surviving leg) ──
            # "sl is till strangle is on" — Initial 15% SL does NOT apply here.
            # Surviving leg is managed exclusively by tight TSL (8% above lowest price).
            state['solo_mode'] = True
            solo_tsl = round_to_tick(lowest * (1.0 + pos['tsl_pct']))

            # Strict ratchet: can only tighten down
            if 'current_sl' in state:
                current_sl = min(solo_tsl, state['current_sl'])
            else:
                current_sl = solo_tsl
            state['current_sl'] = current_sl

            if live_ltp >= current_sl:
                return True, f'Solo TSL Hit on {leg} ({live_ltp:.2f} >= {current_sl:.2f})'

        return False, ''

    # ── Point-based momentum reversal tracking ───
    def _update_reversal_tracker(self, spot: float):
        """
        Track 1-second spot ticks for momentum reversal.
        Latches self._reversal_latched if price pulls back from extreme.
        """
        self._spot_history.append(spot)
        
        # Only process if we are in a 1-leg (solo) state waiting for re-entry
        short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']
        if len(short_legs) != 1:
            self._extreme_spot = 0.0
            self._trend_velocity = 0.0
            self._reversal_pullback = 0.0
            return
            
        surviving_leg = short_legs[0]
        
        # Calculate velocity over last 10 ticks (~10 seconds)
        hist = list(self._spot_history)
        if len(hist) >= 10:
            self._trend_velocity = hist[-1] - hist[-10]
        else:
            self._trend_velocity = 0.0
            
        # Initialize extreme spot if it's 0
        if self._extreme_spot <= 0:
            self._extreme_spot = spot
            
        # Surviving leg is PE (Call was hit). Market rallied. We want to re-enter Call on a dip.
        if surviving_leg == 'PE':
            # Market is going up, track the highest high
            if spot > self._extreme_spot:
                self._extreme_spot = spot
            
            pullback = self._extreme_spot - spot
            self._reversal_pullback = pullback
            
            # Reversal criteria:
            if pullback >= REVERSAL_MIN_PTS or (pullback >= MICRO_REVERSAL_PTS and self._trend_velocity < 0):
                self._reversal_latched = True
                
        # Surviving leg is CE (Put was hit). Market dumped. We want to re-enter Put on a bounce.
        elif surviving_leg == 'CE':
            # Market is going down, track the lowest low
            if spot < self._extreme_spot:
                self._extreme_spot = spot
                
            pullback = spot - self._extreme_spot
            self._reversal_pullback = pullback
            
            # Reversal criteria:
            if pullback >= REVERSAL_MIN_PTS or (pullback >= MICRO_REVERSAL_PTS and self._trend_velocity > 0):
                self._reversal_latched = True

    def _consume_reversal(self):
        """Mark the latched reversal as consumed after a successful re-entry."""
        self._reversal_latched = False

    # ── Colorama NIFTY-style dashboard ────────
    def _render_dashboard(self, spot: float, atm: float):
        global _last_tg_dash_msg_ids, _last_tg_dash_new_msg_ts, _last_tg_dash_edit_ts, _tg_rate_limited_until

        now    = get_ist_now()
        now_ts = time.time()

        # Build unified snapshot of all positions for this render
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

        # ── Console (every 1 second) ──────────
        if now_ts - self._last_console_dash_ts >= 1.0:
            self._last_console_dash_ts = now_ts

            W   = 98
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

            # Momentum Reversal display
            if self._extreme_spot > 0:
                vel_col = GR if self._trend_velocity > 0 else (RD if self._trend_velocity < 0 else YL)
                sign_str = '+' if self._trend_velocity > 0 else ''
                trend_str = f'Vel: {vel_col}{sign_str}{self._trend_velocity:.2f}{RS} '
                trend_str += f'Pull: {self._reversal_pullback:.2f}'
            else:
                trend_str = f'{DIM}WAITING{RS}'
                
            reversal_tag = f'  {MG}[REVERSAL LATCHED]{RS}' if self._reversal_latched else ''
            cooldown_left = max(0.0, POST_CLOSE_COOLDOWN - (now_ts - self.last_any_close_ts))
            cooldown_tag  = (f'  {YL}[COOLDOWN {cooldown_left:.0f}s]{RS}'
                             if cooldown_left > 0 else '')

            print()
            print(TOP)

            title_l = (f'  {CY}MCX NATGAS PAPER v3.0{RS}  {DIM}│{RS}  '
                       f'{YL}ATM STRADDLE + MOMENTUM{RS}  {DIM}│{RS}  '
                       f'{GR}tail -f natgas_paper.log{RS}')
            title_r = f'{DIM}{now.strftime("%H:%M:%S IST")}{RS}  '
            pad_top = max(1, W - _ansi_len(title_l) - _ansi_len(title_r))
            print(f'{V}{title_l}{" " * pad_top}{title_r}{V}')

            print(MID)
            ind_row = (f'  {DIM}SPOT:{RS} {WH}{spot:>8.2f}{RS}  '
                       f'{DIM}ATM:{RS} {YL}{int(atm):<5}{RS}  '
                       f'{DIM}MOMENTUM:{RS} {trend_str}'
                       f'{reversal_tag}{cooldown_tag}  '
                       f'{DIM}TRADES:{RS} {WH}{self.trades_today}{RS}')
            print(f'{V}{_pad(ind_row, W)}{V}')
            print(MID)

            # Position table
            if not snap_rows:
                msg = f'  {YL}No open positions — waiting for entry...{RS}'
                print(f'{V}{_pad(msg, W)}{V}')
            else:
                hdr = (f'  {"LEG":<6} {VS} {"STRIKE":>7} {VS} {"SIDE":<5} {VS} '
                       f'{"ENTRY":>8} {VS} {"BEST PREM":>10} {VS} {"LTP":>8} {VS} '
                       f'{"CURR SL":>9} {VS} {"PNL":>14}  ')
                print(f'{V}{_pad(hdr, W)}{V}')
                print(MIDS)

                for r in snap_rows:
                    pnl_sign, pnl_fmt = _fmt_pnl(r['pnl'], width=12)
                    pnl_col  = GR if r['pnl'] > 0 else (RD if r['pnl'] < 0 else YL)
                    side_col = RD if r['side'] == 'SELL' else GR
                    leg_label = f"{r['leg']}*" if r.get('solo_mode') else r['leg']

                    row = (f"  {WH}{leg_label:<6}{RS} {VS} {WH}{int(r['strike']):>7}{RS} {VS} "
                           f"{side_col}{r['side']:<5}{RS} {VS} "
                           f"{WH}{r['entry']:>8.2f}{RS} {VS} "
                           f"{DIM}{r['best']:>10.2f}{RS} {VS} "
                           f"{YL}{r['ltp']:>8.2f}{RS} {VS} "
                           f"{MG}{r['sl']:>9.2f}{RS} {VS} "
                           f"  {pnl_col}{pnl_fmt}{RS}  ")
                    print(f'{V}{_pad(row, W)}{V}')

                if any(r.get('solo_mode') for r in snap_rows):
                    print(MIDS)
                    solo_msg = f"  {CY}🎯 SOLO TSL ACTIVE (*):{RS} Strangle OFF — trailing strictly at {DEFAULT_TSL_PCT*100:.0f}% TSL (no initial SL)"
                    print(f'{V}{_pad(solo_msg, W)}{V}')

            print(MID)
            real_sign, real_fmt = _fmt_pnl(self.total_realized_pnl, width=10)
            unreal_sign, unreal_fmt = _fmt_pnl(total_unreal, width=10)
            net_sign, net_fmt = _fmt_pnl(net, width=10)

            real_col   = GR if self.total_realized_pnl > 0 else (RD if self.total_realized_pnl < 0 else YL)
            unreal_col = GR if total_unreal > 0 else (RD if total_unreal < 0 else YL)
            net_col    = GR if net > 0 else (RD if net < 0 else YL)

            r_txt = f"{real_col}{real_fmt}{RS}"
            u_txt = f"{unreal_col}{unreal_fmt}{RS}"
            n_txt = f"{net_col}{net_fmt}{RS}"

            pnl_row = (f"  {DIM}REALIZED:{RS} {r_txt}  {VS}  "
                       f"{DIM}UNREALIZED:{RS} {u_txt}  {VS}  "
                       f"{DIM}NET MTM:{RS} {n_txt}")
            print(f'{V}{_pad(pnl_row, W)}{V}')
            print(BOT)
            sys.stdout.flush()

        # ── Telegram live dashboard (every 3s) ─
        if now_ts - _last_tg_dash_edit_ts >= 3.0:
            if now_ts < _tg_rate_limited_until:
                return
            _last_tg_dash_edit_ts = now_ts

            if self._extreme_spot > 0:
                sign_str = '+' if self._trend_velocity > 0 else ''
                trend_str = f'Vel: {sign_str}{self._trend_velocity:.2f} Pull: {self._reversal_pullback:.2f}'
            else:
                trend_str = 'WAITING'
                
            rev_tag  = ' [REVERSAL]' if self._reversal_latched else ''

            lines = [
                '<pre>',
                f'MCX NATURAL GAS  [{now.strftime("%H:%M:%S")}]',
                f'Spot {spot:.2f}   ATM {int(atm)}',
                f'{trend_str}{rev_tag}',
                '─────────────────────',
            ]
            if snap_rows:
                for r in snap_rows:
                    pnl_sign = '+' if r['pnl'] >= 0 else ''
                    lines.append(f'{r["leg"]:<3} SELL {int(r["strike"]):<4}  PnL {pnl_sign}₹{r["pnl"]:>8,.2f}')
                    lines.append(f'  E {r["entry"]:>6.2f}  L {r["ltp"]:>6.2f}  SL {r["sl"]:>6.2f}')
            else:
                lines.append('  No Open Positions')

            lines += [
                '─────────────────────',
            ]
            r_sign = '+' if self.total_realized_pnl >= 0 else ''
            u_sign = '+' if total_unreal >= 0 else ''
            n_sign = '+' if net >= 0 else ''
            lines += [
                f'Realized  {r_sign}₹{self.total_realized_pnl:>9,.2f}',
                f'Unreal    {u_sign}₹{total_unreal:>9,.2f}',
                f'Net MTM   {n_sign}₹{net:>9,.2f}',
                f'Trades    {self.trades_today}',
                '</pre>',
            ]
            t = '\n'.join(lines)

            chat_ids   = _get_tg_chat_ids()
            is_refresh = (now_ts - _last_tg_dash_new_msg_ts) >= 60.0

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

                if not edited and (msg_id is None or is_refresh):
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

    def _cancel_open_orders_for_symbol(self, tsym: Optional[str] = None):
        """Cancel any pending broker orders matching tsym or PE."""
        if not self.api:
            return
        try:
            orders = self.api.get_order_book()
            if orders and isinstance(orders, list):
                for o in orders:
                    status = str(o.get('status', '')).upper()
                    if status in ('OPEN', 'PENDING', 'TRIGGER_PENDING'):
                        o_tsym = str(o.get('tsym', ''))
                        if (tsym and tsym in o_tsym) or 'PE' in o_tsym:
                            norenordno = o.get('norenordno')
                            if norenordno:
                                print(f"[ACTION] Canceling open broker order {norenordno} ({o_tsym})...", flush=True)
                                self.api.cancel_order(norenordno=norenordno)
        except Exception as e:
            print(f"[WARN] Order book check / cancel: {e}", flush=True)

    def _apply_leg_adjustments(self):
        """
        Applies requested live adjustments on startup:
        1. Closes and removes current PE leg from data and cancels any open PE orders.
        2. Updates any surviving open legs (e.g. CE) to new SL (15%) and TSL (8%).
        """
        # 1. Close PE leg if currently open in positions
        if 'PE' in self.positions:
            pe_pos = self.positions['PE']
            pe_tsym = pe_pos.get('tsym', '')
            print(f"[ACTION] Closing current PE leg ({pe_tsym}) as requested by user...", flush=True)
            self._cancel_open_orders_for_symbol(pe_tsym)
            self._close_leg('PE', 'USER_REQUEST_CLOSE_PE')
            print("[ACTION] PE leg successfully closed and removed from positions & state.", flush=True)
        else:
            self._cancel_open_orders_for_symbol('PE')

        # 2. Update surviving open legs (e.g. CE) with new 15% SL and 8% TSL
        if self.positions:
            is_strangle = ('CE' in self.positions and 'PE' in self.positions)
            for leg, pos in self.positions.items():
                pos["loss_stop_pct"] = DEFAULT_SL_PCT
                pos['tsl_pct']       = DEFAULT_TSL_PCT
                entry_prem          = pos['entry_price']
                new_initial_sl      = round_to_tick(entry_prem * (1.0 + DEFAULT_SL_PCT))
                lowest              = float(pos.get('sl_state', {}).get('lowest_ltp', entry_prem))

                if is_strangle:
                    if lowest >= entry_prem:
                        curr_sl = new_initial_sl
                    else:
                        trail_sl = round_to_tick(lowest * (1.0 + DEFAULT_TSL_PCT))
                        curr_sl = min(trail_sl, new_initial_sl)
                    pos['sl_state'] = {
                        'lowest_ltp':    lowest,
                        'current_sl':    curr_sl,
                        'initial_sl':    new_initial_sl,
                        'loss_stop_pct': DEFAULT_SL_PCT,
                        'tsl_pct':       DEFAULT_TSL_PCT,
                        'solo_mode':     False
                    }
                    print(f"[ACTION] Strangle ON: Updated {leg} SL to {DEFAULT_SL_PCT*100:.0f}% (Initial SL: ₹{new_initial_sl:.2f}), "
                          f"TSL to {DEFAULT_TSL_PCT*100:.0f}% (Current SL: ₹{curr_sl:.2f})", flush=True)
                else:
                    # Strangle is OFF — solo leg operates strictly on 8% TSL
                    solo_tsl = round_to_tick(lowest * (1.0 + DEFAULT_TSL_PCT))
                    pos['sl_state'] = {
                        'lowest_ltp':    lowest,
                        'current_sl':    solo_tsl,
                        'initial_sl':    new_initial_sl,
                        'loss_stop_pct': DEFAULT_SL_PCT,
                        'tsl_pct':       DEFAULT_TSL_PCT,
                        'solo_mode':     True
                    }
                    print(f"[ACTION] Strangle OFF: Updated {leg} to Solo TSL @ {DEFAULT_TSL_PCT*100:.0f}% (Current TSL: ₹{solo_tsl:.2f})", flush=True)
            self._save_state()

    # ── Main run loop ─────────────────────────
    def run(self):
        self.authenticate()
        self._get_mcx_csv()

        DIM = f'{Fore.WHITE}{Style.DIM}'
        CY  = f'{Fore.CYAN}{Style.BRIGHT}'
        RS  = Style.RESET_ALL
        print()
        print(f'{DIM}{"="*92}{RS}')
        print(f'{CY}  MCX NATURAL GAS PAPER TRADING BOT  v3.0  |  17:00 – 23:24 IST{RS}')
        print(f'{DIM}{"="*92}{RS}')
        print(flush=True)

        send_telegram('<pre>MCX Natural Gas\nPaper Trading Bot Online (v3.0)</pre>')

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
                    send_telegram(
                        f'<pre>MCX Session Complete\n'
                        f'Final Realized PnL: ₹{self.total_realized_pnl:,.0f}</pre>')
                    break

                if now.hour < MCX_ENTRY_HOUR:
                    if now_ts - last_wait_msg_ts > 60.0:
                        last_wait_msg_ts = now_ts
                        print(f'[WAIT] Market opens at {MCX_ENTRY_HOUR}:00 IST. '
                              f'Current: {now.strftime("%H:%M:%S")}', flush=True)
                    time.sleep(10)
                    continue

                # ── Spot & ATM ──────────────────────────────
                spot = self.get_spot()
                if spot <= 50.0:
                    time.sleep(3)
                    continue
                atm = round_to_price(spot, STRIKE_STEP)

                # ── Momentum Reversal Tracking ────────────────────────
                self._update_reversal_tracker(spot)

                # ── STEP 1: NO POSITIONS → INSTANT ENTRY ─────
                #    Always enter immediately whenever positions are empty.
                #    Apply a short post-close cooldown to avoid whipsaw.
                if not self.positions:
                    time_since_close = now_ts - self.last_any_close_ts
                    if time_since_close < POST_CLOSE_COOLDOWN:
                        pass  # Brief cooldown (5s) to avoid double-close flicker
                    else:
                        print(f'[INIT] Entering ATM Straddle at {int(atm)} '
                              f'(Trades: {self.trades_today})...', flush=True)
                        self._enter_leg('CE', atm, 'SELL')
                        self._enter_leg('PE', atm, 'SELL')
                        self._consume_reversal()          # Reset any stale latch after fresh entry

                    self._render_dashboard(spot, atm)
                    time.sleep(1.0)
                    continue

                # ── STEP 2: 1 LEG OPEN → MOMENTUM RE-ENTRY FIRST ─
                #    Re-enter the missing leg BEFORE checking TSL.
                #    This ensures re-entry happens while the surviving leg is still alive.
                short_legs = [leg for leg, p in self.positions.items() if p['side'] == 'SELL']

                if len(short_legs) == 1 and self._reversal_latched:
                    time_since_reentry = now_ts - self.last_reentry_ts
                    if time_since_reentry >= REENTRY_COOLDOWN_S:
                        surviving_leg    = short_legs[0]
                        surviving_strike = self.positions[surviving_leg]['strike']
                        missing_leg      = 'CE' if surviving_leg == 'PE' else 'PE'

                        # OTM strangle: enter missing leg at some distance from ATM
                        dist = min(abs(surviving_strike - atm), 25.0)
                        if dist < STRIKE_STEP:
                            # Surviving leg is near ATM; use ATM for fresh straddle
                            reentry_strike = atm
                        else:
                            reentry_strike = round_to_price(
                                atm + dist if missing_leg == 'CE' else atm - dist, STRIKE_STEP)

                        print(f'[REENTRY] Momentum reversal latched! '
                              f'Re-entering {missing_leg} at {int(reentry_strike)} '
                              f'(Surviving: {surviving_leg} {int(surviving_strike)}) '
                              f'with {REENTRY_SL_PCT*100:.0f}% instant SL', flush=True)
                        
                        if self._enter_leg(missing_leg, reentry_strike, 'SELL', loss_stop_pct=REENTRY_SL_PCT):
                            self.last_reentry_ts = now_ts
                            self._consume_reversal()     # Consume latch on successful re-entry

                # ── STEP 3: CHECK TSL/SL FOR ALL LEGS (NO BREAK) ─
                #    Collect all triggered legs first, then close them all.
                #    This prevents a partially-open state triggering premature re-entry.
                legs_to_close: List[Tuple[str, str, float]] = []
                for leg in list(self.positions.keys()):
                    pos      = self.positions.get(leg)
                    if not pos or pos['side'] != 'SELL':
                        continue
                    live_ltp = self._get_leg_ltp(pos)
                    hit, reason = self._update_leg(leg, live_ltp)
                    if hit:
                        legs_to_close.append((leg, reason, live_ltp))

                for leg, reason, exit_px in legs_to_close:
                    print(f'[ALERT] {reason}', flush=True)
                    self._close_leg(leg, reason, exit_price=exit_px)

                # ── STEP 4: Dashboard ────────────────────────
                self._render_dashboard(spot, atm)
                time.sleep(1.0)

            except KeyboardInterrupt:
                print('\n[STOP] KeyboardInterrupt — squaring off all positions...', flush=True)
                self._close_all('KEYBOARD_INTERRUPT')
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
        send_telegram(f'<pre>MCX Bot Fatal Error:\n{e}</pre>')
        sys.exit(1)
