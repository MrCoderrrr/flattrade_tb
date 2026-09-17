"""
================================================================================
🚀 NIFTY 50 PAPER TRADING ENGINE (VERSION 3.0 - STANDALONE MONOLITH)
================================================================================
Architecture: Single-file, deterministic paper trading bot.
Current Logic: Continuous EMA Crossover (15/90/300) + ADX 300 Regime Detection + DTE Sizing.
"""
import time
import math
import json
import os
import re
import requests
import datetime
from datetime import timezone, timedelta
from typing import List, Dict, Optional, Tuple
from collections import deque

IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
TOKEN_FILE          = 'token.txt'
LOT_SIZE            = 25
STRIKE_STEP         = 50

# Time Windows
ENTRY_HOUR_START    = 9
ENTRY_MINUTE_START  = 15
EXIT_HOUR           = 15
EXIT_MINUTE         = 15

# Telegram
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "6307066850")

# ─────────────────────────────────────────────
# TELEGRAM INTEGRATION
# ─────────────────────────────────────────────
_last_tg_dash_msg_ids = {}

def _get_tg_chat_ids() -> List[str]:
    if isinstance(TELEGRAM_CHAT_ID, list):
        return [str(c).strip() for c in TELEGRAM_CHAT_ID if str(c).strip()]
    if isinstance(TELEGRAM_CHAT_ID, (str, int)):
        return [c.strip() for c in str(TELEGRAM_CHAT_ID).split(",") if c.strip()]
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
# INDICATORS
# ─────────────────────────────────────────────
class ContinuousEMA:
    def __init__(self, half_life_seconds: float):
        self.half_life_seconds = half_life_seconds
        self.value = None
        self.timestamp = None

    def update(self, value: float, timestamp: float) -> float:
        if self.value is None or self.timestamp is None:
            self.value = value
        else:
            alpha = 1.0 - math.exp(-math.log(2) * max(0.0, timestamp - self.timestamp) / self.half_life_seconds)
            self.value += alpha * (value - self.value)
        self.timestamp = timestamp
        return self.value

class RollingVolatility:
    def __init__(self, window: int):
        self.values = deque(maxlen=window)

    def update(self, value: float) -> Optional[float]:
        self.values.append(float(value))
        if len(self.values) < 2: return None
        mean = sum(self.values) / len(self.values)
        return math.sqrt(sum((x - mean) ** 2 for x in self.values) / (len(self.values) - 1))

def adx(prices: list, period: int = 300) -> float:
    actual_period = min(period, len(prices) - 1)
    if actual_period < 1: return 0.0
    # Simplified ADX logic using 1s prices as synthetic bars
    trs, plus, minus = [], [], []
    for i in range(1, len(prices)):
        trs.append(abs(prices[i] - prices[i-1]))
        up = prices[i] - prices[i-1]
        down = prices[i-1] - prices[i]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    tr = sum(trs[-actual_period:]) or 1e-12
    p = 100 * sum(plus[-actual_period:]) / tr
    m = 100 * sum(minus[-actual_period:]) / tr
    return 100 * abs(p - m) / (p + m) if p + m else 0.0

# ─────────────────────────────────────────────
# MAIN BOT CLASS
# ─────────────────────────────────────────────
class NiftyPaperBot:
    def __init__(self):
        self.api = None
        self.state = 'FLAT'
        self.positions = {}
        self.total_realized_pnl = 0.0
        
        # Indicator Engine
        self.emas = {h: ContinuousEMA(h) for h in (15.0, 90.0, 300.0)}
        self.rv = {w: RollingVolatility(w) for w in (60, 300)}
        self.prices = deque(maxlen=301)
        self.slow_history = deque(maxlen=11)
        self.last_spot_time = 0.0
        
        self.adx_val = 0.0
        self.persistence = 0.0
        self._signal = 0
        self._signal_start_ts = None
        
        # Option Cache
        self._option_cache = {}

    def authenticate(self):
        from api_helper import NorenApiPy
        user_id = os.getenv("FLATTRADE_USER_ID") or os.getenv("USER_ID")
        if not user_id:
            try:
                from creds import USER_ID
                user_id = str(USER_ID).strip()
            except ImportError:
                pass
        
        if not user_id or not os.path.exists(TOKEN_FILE):
            print("[ERROR] Missing credentials for Flattrade.", flush=True)
            return False

        self.api = NorenApiPy()
        with open(TOKEN_FILE, 'r') as f:
            token = f.read().strip()
        
        resp = self.api.set_session(userid=user_id, password="", usertoken=token)
        if isinstance(resp, dict) and str(resp.get("stat", "")).lower() in {"ok", "success"}:
            print(f"[OK] Authenticated as {user_id}", flush=True)
            return True
        return False

    def get_quote(self, exchange: str, token: str) -> Optional[float]:
        try:
            res = self.api.get_quotes(exchange=exchange, token=token)
            if isinstance(res, dict) and str(res.get("stat", "")).lower() in {"ok", "success"}:
                return float(res.get("lp", res.get("ltp", 0.0)))
        except:
            pass
        return None

    def get_option_token(self, strike: int, opt_type: str) -> Optional[str]:
        cache_key = f"{strike}_{opt_type}"
        if cache_key in self._option_cache:
            return self._option_cache[cache_key]
        
        try:
            res = self.api.searchscrip(exchange="NFO", searchtext=f"NIFTY {strike} {opt_type}")
            values = res.get("values", [])
            if not values: return None
            
            # Simple nearest expiry filter
            today = datetime.datetime.now(IST).date()
            candidates = []
            for item in values:
                tsym = item.get("tsym", "").upper()
                if not tsym.startswith("NIFTY") or "BANK" in tsym or "FIN" in tsym or "MIDCP" in tsym:
                    continue
                match = re.search(r"(\d{2}[A-Z]{3}\d{2,4})", tsym)
                if match:
                    try:
                        ex_str = match.group(1)
                        fmt = "%d%b%y" if len(ex_str) == 7 else "%d%b%Y"
                        expiry = datetime.datetime.strptime(ex_str, fmt).date()
                        if expiry >= today:
                            candidates.append((expiry, item.get("token")))
                    except: pass
            
            if candidates:
                candidates.sort()
                token = str(candidates[0][1])
                self._option_cache[cache_key] = token
                return token
        except:
            pass
        return None

    def _enter_leg(self, leg_id: str, strike: int, side: str, qty: int):
        opt_type = "CE" if "CE" in leg_id else "PE"
        token = self.get_option_token(strike, opt_type)
        price = self.get_quote("NFO", token) if token else 10.0 # Fallback
        price = price or 10.0
        
        self.positions[leg_id] = {
            'strike': strike,
            'side': side,
            'qty': qty,
            'entry_price': price,
            'token': token
        }
        print(f"[ENTER] {side} {qty}x {leg_id} @ {price}", flush=True)

    def _close_leg(self, leg_id: str, reason: str):
        if leg_id not in self.positions: return
        pos = self.positions[leg_id]
        price = self.get_quote("NFO", pos['token']) if pos['token'] else pos['entry_price']
        price = price or pos['entry_price']
        
        pnl = (price - pos['entry_price']) * pos['qty'] if pos['side'] == 'BUY' else (pos['entry_price'] - price) * pos['qty']
        self.total_realized_pnl += pnl
        print(f"[CLOSE] {pos['side']} {pos['qty']}x {leg_id} @ {price} | PnL: {pnl:.2f} ({reason}, flush=True)")
        del self.positions[leg_id]

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)
        self.state = 'FLAT'

    def update_indicators(self, spot: float):
        ts = time.time()
        if ts - self.last_spot_time >= 1.0:
            self.last_spot_time = ts
            self.prices.append(spot)
            
            for ema in self.emas.values():
                ema.update(spot, ts)
            self.slow_history.append(self.emas[300.0].value)
            
            rv60 = self.rv[60].update(spot)
            rv300 = self.rv[300].update(spot)
            
            if rv60 and rv300 and rv300 > 0:
                vr = rv60 / rv300
                self.persistence = max(3.0, min(30.0, 10.0 / max(vr, 1e-12)))
            else:
                self.persistence = 5.0
                
            self.adx_val = adx(self.prices, 300)
            
            # Signal Gen
            fast, slow = self.emas[15.0].value, self.emas[90.0].value
            if fast and slow and len(self.slow_history) > 1:
                slope = self.slow_history[-1] - self.slow_history[0]
                raw_sig = 1 if (fast > slow and slope > 0) else (-1 if (fast < slow and slope < 0) else 0)
                
                if raw_sig != 0 and raw_sig == self._signal:
                    pass # Keep accumulating time
                else:
                    self._signal = raw_sig
                    self._signal_start_ts = ts if raw_sig != 0 else None

    def _render_dashboard(self, spot: float):
        # UI
        lines = []
        lines.append("="*90)
        lines.append(f"  NIFTY PAPER TRADING BOT  v3.0  |  09:15 - 15:15 IST")
        lines.append("="*90)
        
        fast = self.emas[15.0].value or 0
        slow = self.emas[90.0].value or 0
        slope = (self.slow_history[-1] - self.slow_history[0]) if len(self.slow_history) > 1 else 0
        
        lines.append(f"  SPOT: {spot:.2f} | EMA 15: {fast:.2f} | EMA 90: {slow:.2f} | SLOPE: {slope:.3f}")
        lines.append(f"  ADX: {self.adx_val:.2f} | PERSIST REQ: {self.persistence:.1f}s")
        
        sig_str = "UP" if self._signal > 0 else ("DOWN" if self._signal < 0 else "FLAT")
        hold_time = (time.time() - self._signal_start_ts) if self._signal_start_ts else 0
        lines.append(f"  STATE: {self.state} | SIGNAL: {sig_str} (Held for {hold_time:.1f}s)")
        lines.append("-" * 90)
        
        unrealized = 0.0
        for leg, pos in self.positions.items():
            price = self.get_quote("NFO", pos['token']) if pos['token'] else pos['entry_price']
            price = price or pos['entry_price']
            pnl = (price - pos['entry_price']) * pos['qty'] if pos['side'] == 'BUY' else (pos['entry_price'] - price) * pos['qty']
            unrealized += pnl
            lines.append(f"  {leg:15} | {pos['side']:4} {pos['qty']:3} | ENTRY: {pos['entry_price']:>7.2f} | LIVE: {price:>7.2f} | PnL: {pnl:>7.2f}")
            
        lines.append("-" * 90)
        lines.append(f"  REALIZED: {self.total_realized_pnl:.2f} | UNREALIZED: {unrealized:.2f} | NET: {self.total_realized_pnl + unrealized:.2f}")
        lines.append("="*90)
        
        dash_str = "\n".join(lines)
        os.system('clear' if os.name == 'posix' else 'cls')
        print(dash_str, flush=True)
        
        # Telegram Update
        global _last_tg_dash_msg_ids
        chat_ids = _get_tg_chat_ids()
        if TELEGRAM_TOKEN and chat_ids:
            try:
                for cid in chat_ids:
                    if cid in _last_tg_dash_msg_ids:
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
                                      data={'chat_id': cid, 'message_id': _last_tg_dash_msg_ids[cid], 'text': f"<pre>{dash_str}</pre>", 'parse_mode': 'HTML'}, timeout=2)
                    else:
                        res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                            data={'chat_id': cid, 'text': f"<pre>{dash_str}</pre>", 'parse_mode': 'HTML'}, timeout=2)
                        if res.status_code == 200:
                            _last_tg_dash_msg_ids[cid] = res.json()['result']['message_id']
            except: pass

    def run(self):
        print("Starting NIFTY Engine...", flush=True)
        if not self.authenticate(): return
        
        nifty_token = os.getenv("NIFTY_TOKEN", "26000")
        
        last_wait_msg = 0.0
        while True:
            now = datetime.datetime.now(IST)
            now_ts = time.time()
            
            # Session Control
            if now.hour > EXIT_HOUR or (now.hour == EXIT_HOUR and now.minute >= EXIT_MINUTE):
                if self.positions:
                    self._close_all("SESSION_END")
                    send_telegram(f"Session Ended. Net PnL: {self.total_realized_pnl:.2f}")
                
                if now_ts - last_wait_msg > 60:
                    print(f"[{now.strftime('%H:%M:%S', flush=True)}] NIFTY session closed. Sleeping...")
                    last_wait_msg = now_ts
                time.sleep(10)
                continue
                
            if now.hour < ENTRY_HOUR_START or (now.hour == ENTRY_HOUR_START and now.minute < ENTRY_MINUTE_START):
                if now_ts - last_wait_msg > 60:
                    print(f"[{now.strftime('%H:%M:%S', flush=True)}] Waiting for NIFTY market open at 09:15...")
                    last_wait_msg = now_ts
                time.sleep(10)
                continue

            spot = self.get_quote("NSE", nifty_token)
            if not spot:
                time.sleep(1)
                continue
                
            self.update_indicators(spot)
            
            # Strategy Execution
            if len(self.prices) > 15:
                if self.state == 'FLAT' and self._signal != 0 and self._signal_start_ts and (time.time() - self._signal_start_ts) >= self.persistence:
                    atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
                    self._enter_leg("NIFTY-CE-HEDGE", atm + 1000, "BUY", LOT_SIZE)
                    self._enter_leg("NIFTY-PE-HEDGE", atm - 1000, "BUY", LOT_SIZE)
                    self._enter_leg("NIFTY-CE", atm, "SELL", LOT_SIZE)
                    self._enter_leg("NIFTY-PE", atm, "SELL", LOT_SIZE)
                    self.state = 'OPEN'
                    send_telegram(f"Entered Hedged Straddle at {spot:.2f}")
            
            self._render_dashboard(spot)
            time.sleep(1)

if __name__ == "__main__":
    bot = NiftyPaperBot()
    bot.run()
