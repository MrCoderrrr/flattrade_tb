# Complete script
"""mcx_naturalgas_paper.py"""
import os
import sys
import time
import math
import glob
import urllib.request
import zipfile
import io
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

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

# Standard Indian Standard Time (IST = UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    return datetime.now(IST)

TOKEN_FILE = 'token.txt'
STRIKE_STEP = 5.0
MCX_ENTRY_HOUR   = 18     # 18:00 IST (6:00 PM)
MCX_EXIT_HOUR    = 23     # 23:24 IST (11:24 PM auto square-off)
MCX_EXIT_MINUTE  = 24
LOT_SIZE         = 1250   # 1 Lot of Natural Gas = 1250 units
MAX_DAILY_TRADES = 10     # Safeguard against runaway re-entries
DEFAULT_SL_PCT   = 0.10   # 10% Stop Loss
DEFAULT_TSL_PCT  = 0.07   # 7% Trailing Stop Loss

TELEGRAM_TOKEN = '8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE'
CHAT_ID = '6307066850'

_last_tg_dash_msg_id: Optional[int] = None
_last_tg_dash_new_msg_ts: float = 0.0
_last_tg_dash_edit_ts: float = 0.0

def send_telegram(msg: str):
    global _last_tg_dash_msg_id
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        requests.post(url, data={'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}, timeout=5)
        # Reset dashboard message ID so next 1-sec tick posts fresh dashboard below the alert
        _last_tg_dash_msg_id = None
    except Exception:
        pass

def round_to_price(value: float, step: float = STRIKE_STEP) -> float:
    return round(math.floor(value / step + 0.5) * step, 2)

class KAMA:
    @staticmethod
    def compute(closes: List[float], period: int = 10, fast: int = 3, slow: int = 30):
        if len(closes) < period + 1:
            return None, None, 0.0, 0

        kama = [0.0] * len(closes)
        kama[period - 1] = sum(closes[:period]) / period

        fast_sc = 2.0 / (fast + 1.0)
        slow_sc = 2.0 / (slow + 1.0)

        for i in range(period, len(closes)):
            change = abs(closes[i] - closes[i - period])
            volatility = sum(abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1))
            er = (change / volatility) if volatility > 0 else 0.0
            sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
            kama[i] = kama[i - 1] + sc * (closes[i] - kama[i - 1])

        current = float(kama[-1])
        previous = float(kama[-2])
        delta = current - previous
        if delta > 0.1:
            trend = 1
        elif delta < -0.1:
            trend = -1
        else:
            trend = 0
        return current, previous, delta, trend

class NaturalGasPaperBot:
    def __init__(self):
        self.api = NorenApiPy() if NorenApiPy else None
        self.positions: Dict[str, Dict] = {}
        self.kama_prev_delta = 0.0
        self.last_reentry_ts = 0.0
        self.last_double_stop_ts = 0.0
        self.total_realized_pnl = 0.0
        self.trades_today = 0
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcx_state_paper.json")
        self._mcx_master = None
        self._spot_cache = {'ts': 0.0, 'val': 0.0}
        self._last_tg_dash_ts = 0.0
        self._last_console_dash_ts = 0.0
        self.front_month_futs_token: Optional[str] = None
        self.front_month_futs_symbol: Optional[str] = None
        self._load_state()

    def _save_state(self):
        try:
            import json
            state = {
                "date": get_ist_now().strftime("%Y-%m-%d"),
                "positions": self.positions,
                "total_realized_pnl": self.total_realized_pnl,
                "trades_today": self.trades_today,
                "last_reentry_ts": self.last_reentry_ts,
                "last_double_stop_ts": self.last_double_stop_ts
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"[WARN] Failed saving MCX state: {e}")

    def _load_state(self):
        import json
        if not os.path.exists(self.state_file):
            return
        try:
            today_str = get_ist_now().strftime("%Y-%m-%d")
            with open(self.state_file, "r") as f:
                state = json.load(f)
            if state.get("date") == today_str:
                self.positions = state.get("positions", {})
                self.total_realized_pnl = float(state.get("total_realized_pnl", 0.0))
                self.trades_today = int(state.get("trades_today", 0))
                self.last_reentry_ts = float(state.get("last_reentry_ts", 0.0))
                self.last_double_stop_ts = float(state.get("last_double_stop_ts", 0.0))
                print(f"[STATE] Restored MCX state: {len(self.positions)} open legs, Realized PnL: Rs {self.total_realized_pnl:,.2f}, Trades: {self.trades_today}")
        except Exception as e:
            print(f"[WARN] Error loading MCX state: {e}")

    def authenticate(self):
        if not self.api:
            err_detail = f" ({_noren_import_error})" if _noren_import_error else ""
            raise RuntimeError(f"NorenApiPy is not available{err_detail}. Make sure your venv is activated: source venv/bin/activate")

        candidates = [
            TOKEN_FILE,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), TOKEN_FILE),
            '/home/ubuntu/flattrade_tb/flattrade_tb/token.txt',
            '/home/ubuntu/flattrade_tb/token.txt'
        ]
        token_path = None
        for c in candidates:
            if os.path.exists(c) and os.path.getsize(c) > 0:
                token_path = c
                break

        if not token_path:
            raise FileNotFoundError(f'{TOKEN_FILE} missing or empty. Run login.py first.')

        with open(token_path, 'r') as f:
            access_token = f.read().strip()

        self.api.set_session(userid=str(USER_ID).strip(), password='', usertoken=access_token)

        try:
            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get('stat') != 'Ok':
                print('[WARN] Token validation notice: get_limits did not return Ok, proceeding in paper mode.')
        except Exception as e:
            print(f'[WARN] Flattrade session warning: {e}. Proceeding in paper mode.')

        print(f'[OK] Natural Gas PAPER TRADING bot authenticated from {token_path}.')

    def _get_mcx_csv(self):
        if self._mcx_master is not None:
            return self._mcx_master

        import pandas as pd
        today_ist = get_ist_now().strftime('%Y-%m-%d')
        csv_file = f'MCX_symbols_{today_ist}.csv'

        if not os.path.exists(csv_file):
            print(f'[INFO] {csv_file} not found locally. Attempting automatic download from Shoonya...')
            try:
                url = 'https://api.shoonya.com/MCX_symbols.txt.zip'
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=12) as response:
                    with zipfile.ZipFile(io.BytesIO(response.read())) as z:
                        with z.open('MCX_symbols.txt') as f:
                            df = pd.read_csv(f)
                            df.to_csv(csv_file, index=False)
                            print(f'[OK] Downloaded and cached {csv_file}')
            except Exception as e:
                print(f'[WARN] Could not auto-download {csv_file}: {e}')

        if not os.path.exists(csv_file):
            existing = sorted(glob.glob('MCX_symbols_*.csv'), reverse=True)
            if existing:
                csv_file = existing[0]
                print(f'[INFO] Using most recent available MCX symbol file: {csv_file}')
            else:
                print('[ERROR] No MCX_symbols_*.csv file found in directory!')
                return None

        try:
            df = pd.read_csv(csv_file)
            df['ExpiryDate'] = pd.to_datetime(df['Expiry'], format='%d-%b-%Y', errors='coerce')
            self._mcx_master = df

            futs = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'FUTCOM')]
            today_ts = pd.Timestamp(get_ist_now().date())
            future_futs = futs[futs['ExpiryDate'] >= today_ts]
            if future_futs.empty:
                future_futs = futs

            if not future_futs.empty:
                nearest_fut = future_futs.sort_values('ExpiryDate').iloc[0]
                self.front_month_futs_token = str(nearest_fut['Token'])
                self.front_month_futs_symbol = str(nearest_fut['TradingSymbol'])
                print(f'[INFO] Resolved front-month Natural Gas Future: {self.front_month_futs_symbol} (Token: {self.front_month_futs_token})')

            return self._mcx_master
        except Exception as e:
            print(f'[ERROR] Failed loading {csv_file}: {e}')
            return None

    def get_spot(self) -> float:
        now_ts = time.time()
        if now_ts - self._spot_cache['ts'] < 0.95 and self._spot_cache['val'] > 0:
            return self._spot_cache['val']

        if not self.front_month_futs_token:
            self._get_mcx_csv()

        if self.front_month_futs_token and self.api and hasattr(self.api, 'get_quotes'):
            try:
                q = self.api.get_quotes(exchange='MCX', token=self.front_month_futs_token)
                if q and isinstance(q, dict):
                    val = float(q.get('lp', q.get('ltp', 0.0)) or 0.0)
                    if val > 50.0:
                        self._spot_cache = {'ts': now_ts, 'val': val}
                        return val
            except Exception:
                pass

        if self.api and hasattr(self.api, 'searchscrip'):
            try:
                res = self.api.searchscrip(exchange='MCX', searchtext='NATURALGAS')
                if res and isinstance(res, dict) and res.get('values'):
                    for item in res['values']:
                        tsym = str(item.get('tsym', '')).upper()
                        if 'NATURALGAS' in tsym and 'MINI' not in tsym and not tsym.endswith('CE') and not tsym.endswith('PE'):
                            token = item.get('token')
                            q = self.api.get_quotes(exchange='MCX', token=token)
                            if q and isinstance(q, dict):
                                val = float(q.get('lp', q.get('ltp', 0.0)) or 0.0)
                                if val > 50.0:
                                    self._spot_cache = {'ts': now_ts, 'val': val}
                                    return val
            except Exception:
                pass

        return self._spot_cache['val']

    def find_option_symbol(self, strike: float, option_type: str) -> Optional[Dict]:
        import pandas as pd
        df = self._get_mcx_csv()
        if df is None:
            return None

        try:
            today_ts = pd.Timestamp(get_ist_now().date())
            opt_df = df[
                (df['Symbol'] == 'NATURALGAS') &
                (df['Instrument'] == 'OPTFUT') &
                (df['OptionType'] == option_type) &
                (df['StrikePrice'] == float(strike))
            ]

            if opt_df.empty:
                return None

            future_opts = opt_df[opt_df['ExpiryDate'] >= today_ts]
            if future_opts.empty:
                future_opts = opt_df

            nearest = future_opts.sort_values('ExpiryDate').iloc[0]
            token = str(nearest['Token'])
            tsym = str(nearest['TradingSymbol'])

            lp = 0.0
            if self.api and hasattr(self.api, 'get_quotes'):
                try:
                    q = self.api.get_quotes(exchange='MCX', token=token)
                    if q and isinstance(q, dict):
                        lp = float(q.get('lp', q.get('ltp', 0.0)) or 0.0)
                except Exception:
                    pass

            return {'tsym': tsym, 'lp': lp, 'ls': LOT_SIZE, 'token': token}
        except Exception as e:
            print(f'[ERROR] Failed resolving option {strike} {option_type}: {e}')
            return None

    def _get_leg_ltp(self, pos: dict) -> float:
        token = pos.get('token')
        if token and self.api and hasattr(self.api, 'get_quotes'):
            try:
                q = self.api.get_quotes(exchange='MCX', token=token)
                if q and isinstance(q, dict):
                    for field in ('lp', 'ltp', 'c', 'sp1', 'bp1'):
                        val_raw = q.get(field)
                        if val_raw is not None:
                            try:
                                val = float(val_raw)
                                if val > 0:
                                    pos['_last_ltp'] = val
                                    return val
                            except (ValueError, TypeError):
                                pass
            except Exception:
                pass
        return pos.get('_last_ltp', pos['entry_price'])

    def _enter_leg(self, leg: str, strike: float, side: str, loss_stop_pct: float = DEFAULT_SL_PCT, tsl_pct: float = DEFAULT_TSL_PCT):
        if self.trades_today >= MAX_DAILY_TRADES:
            print(f'[GUARD] Max daily trades limit ({MAX_DAILY_TRADES}) reached. Skipping entry.')
            return None

        option_type = 'CE' if leg == 'CE' else 'PE'
        match = self.find_option_symbol(strike, option_type)
        if not match:
            print(f'[WARN] Could not resolve contract for {leg} Strike {strike}.')
            return None

        tsym = match['tsym']
        ltp = float(match.get('lp', 0.0))
        if ltp <= 0:
            print(f'[WARN] LTP is 0 for {tsym}. Skipping entry.')
            return None

        qty = LOT_SIZE
        initial_sl = round(ltp * (1.0 + loss_stop_pct), 2)
        initial_tsl = round(ltp * (1.0 + tsl_pct), 2)

        pos = {
            'leg': leg,
            'tsym': tsym,
            'token': match.get('token', ''),
            'strike': strike,
            'side': side,
            'qty': qty,
            'entry_price': ltp,
            '_last_ltp': ltp,
            'loss_stop_pct': loss_stop_pct,
            'tsl_pct': tsl_pct,
            'sl_state': {
                'lowest_ltp': ltp,
                'current_sl': initial_sl,
                'loss_stop_pct': loss_stop_pct,
                'tsl_pct': tsl_pct
            }
        }
        self.positions[leg] = pos
        self.trades_today += 1
        self._save_state()

        lines = [
            '<pre>',
            '━━━ MCX TRADE OPENED ━━━',
            '',
            f'  {leg:<4} {int(strike):<5} {side} @ {ltp:.2f}',
            f'  SL   {loss_stop_pct*100:.0f}%  →  {initial_sl:.2f}',
            f'  TSL  {tsl_pct*100:.0f}%  →  {initial_tsl:.2f}',
            f'  Qty  {qty}',
            '',
            f'  {tsym}',
            '</pre>'
        ]
        tg = chr(10).join(lines)
        print(f'[PAPER ENTRY] {side} {qty}x {leg} Strike {int(strike)} ({tsym}) @ Rs{ltp:.2f}')
        send_telegram(tg)
        return pos

    def _close_leg(self, leg: str, reason: str):
        pos = self.positions.get(leg)
        if not pos:
            return
        tsym = pos['tsym']
        trade_side = 'BUY' if pos['side'] == 'SELL' else 'SELL'
        ltp = self._get_leg_ltp(pos)

        if pos['side'] == 'SELL':
            pnl = (pos['entry_price'] - ltp) * pos['qty']
        else:
            pnl = (ltp - pos['entry_price']) * pos['qty']

        self.total_realized_pnl += pnl
        sign = '+' if pnl >= 0 else ''

        lines = [
            '<pre>',
            '━━━ MCX TRADE CLOSED ━━━',
            '',
            f'  {leg:<4} {int(pos["strike"]):<5} {reason}',
            f'  Entry  {pos["entry_price"]:.2f}',
            f'  Exit   {ltp:.2f}',
            f'  PnL    {sign}{pnl:,.0f}',
            '',
            f'  Total Realized: {"+" if self.total_realized_pnl >= 0 else ""}{self.total_realized_pnl:,.0f}',
            '</pre>'
        ]
        tg = chr(10).join(lines)
        print(f'[PAPER EXIT] {trade_side} {pos["qty"]}x {tsym} @ Rs{ltp:.2f} | PnL: Rs{pnl:,.2f} | {reason}')
        send_telegram(tg)
        del self.positions[leg]
        if len(self.positions) == 0:
            self.last_double_stop_ts = time.time()
        self._save_state()

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)

    def _update_leg(self, leg: str, live_ltp: float) -> Tuple[bool, str]:
        pos = self.positions.get(leg)
        if not pos or pos['side'] != 'SELL':
            return False, ''
        if live_ltp <= 0:
            return False, ''

        state = pos['sl_state']
        lowest = float(state.get('lowest_ltp', pos['entry_price']))
        if live_ltp < lowest:
            lowest = live_ltp
            state['lowest_ltp'] = round(lowest, 2)

        entry_prem = pos['entry_price']
        initial_sl = round(entry_prem * (1.0 + pos['loss_stop_pct']), 2)
        trail_sl = round(lowest * (1.0 + pos['tsl_pct']), 2)
        target_sl = min(trail_sl, initial_sl)

        if 'current_sl' in state:
            current_sl = min(target_sl, state['current_sl'])
        else:
            current_sl = target_sl
        state['current_sl'] = current_sl

        if live_ltp >= current_sl:
            if current_sl < initial_sl:
                return True, f'TSL Hit on {leg} ({live_ltp:.2f} >= {current_sl:.2f})'
            else:
                return True, f'SL Hit on {leg} ({live_ltp:.2f} >= {current_sl:.2f})'

        return False, ''

    def _print_dashboard(self, spot: float, atm: float):
        global _last_tg_dash_msg_id, _last_tg_dash_new_msg_ts, _last_tg_dash_edit_ts
        now = get_ist_now()
        now_ts = time.time()
        now_s = now.strftime('%H:%M:%S IST')

        if now_ts - self._last_console_dash_ts >= 10.0:
            self._last_console_dash_ts = now_ts
            total_unrealized = 0.0
            print(f"[{now_s}] SPOT: {spot:.2f} | ATM: {int(atm)} | LEGS: {len(self.positions)} | REAL PNL: Rs{self.total_realized_pnl:,.2f}")
            for leg, pos in self.positions.items():
                ltp = self._get_leg_ltp(pos)
                pnl = (pos['entry_price'] - ltp) * pos['qty']
                total_unrealized += pnl
                tsl = pos.get('sl_state', {}).get('current_sl', 0.0)
                print(f'  {leg:2} | {pos["side"]} {int(pos["strike"])} | Entry:{pos["entry_price"]:.2f} LTP:{ltp:.2f} TSL:{tsl:.2f} PnL:Rs{pnl:,.0f}')
            print('-' * 65)

        # Telegram Live Dashboard every 1 second
        if now_ts - _last_tg_dash_edit_ts >= 0.95:
            _last_tg_dash_edit_ts = now_ts
            total_unrealized = 0.0
            leg_data = []
            for leg, pos in self.positions.items():
                ltp = self._get_leg_ltp(pos)
                pnl = (pos['entry_price'] - ltp) * pos['qty']
                total_unrealized += pnl
                tsl = pos.get('sl_state', {}).get('current_sl', 0.0)
                leg_data.append((leg, pos, ltp, pnl, tsl))

            total_pnl = self.total_realized_pnl + total_unrealized
            lines = [
                '<pre>',
                f'MCX NATURAL GAS  [{now.strftime("%H:%M:%S")}]',
                f'Spot {spot:.2f}   ATM {int(atm)}',
                '─────────────────────'
            ]
            if leg_data:
                for leg, pos, ltp, pnl, tsl in leg_data:
                    sign = '+' if pnl >= 0 else ''
                    lines.append(f'{leg:2} SELL {int(pos["strike"]):<4} {sign}{pnl:>7,.0f}')
                    lines.append(f'  E {pos["entry_price"]:>6.2f}  L {ltp:>6.2f}  TSL {tsl:>6.2f}')
            else:
                lines.append('  No Open Positions')

            lines.append('─────────────────────')
            lines.append(f'Realized  {"+" if self.total_realized_pnl >= 0 else ""}{self.total_realized_pnl:>9,.0f}')
            lines.append(f'Unreal    {"+" if total_unrealized >= 0 else ""}{total_unrealized:>9,.0f}')
            lines.append(f'Net MTM   {"+" if total_pnl >= 0 else ""}{total_pnl:>9,.0f}')
            lines.append('</pre>')
            t = chr(10).join(lines)

            # Edit existing message in-place every 1s
            if _last_tg_dash_msg_id is not None and (now_ts - _last_tg_dash_new_msg_ts) < 60.0:
                try:
                    edit_resp = requests.post(
                        f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText',
                        json={'chat_id': CHAT_ID, 'message_id': _last_tg_dash_msg_id, 'text': t, 'parse_mode': 'HTML'},
                        timeout=3
                    )
                    if edit_resp.status_code == 200 and edit_resp.json().get('ok'):
                        return
                except Exception:
                    pass

            # Send a fresh message every 60s or if edit failed
            try:
                send_resp = requests.post(
                    f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                    data={'chat_id': CHAT_ID, 'text': t, 'parse_mode': 'HTML'},
                    timeout=4
                )
                if send_resp.status_code == 200:
                    rjson = send_resp.json()
                    if rjson.get('ok'):
                        _last_tg_dash_msg_id = rjson.get('result', {}).get('message_id')
                        _last_tg_dash_new_msg_ts = now_ts
            except Exception:
                pass

    def _kama_reversal_confirmed(self, current_kama: float, prev_kama: float) -> bool:
        if current_kama is None or prev_kama is None:
            return False
        delta = current_kama - prev_kama
        if abs(delta) < 0.1:
            return False

        if (self.kama_prev_delta > 0 and delta < 0) or (self.kama_prev_delta < 0 and delta > 0):
            self.kama_prev_delta = delta
            return True

        self.kama_prev_delta = delta
        return False

    def find_atm_strike(self, spot: float) -> float:
        return round_to_price(spot, STRIKE_STEP)

    def run(self):
        self.authenticate()
        print('=' * 80)
        print(' NATURAL GAS PAPER TRADING BOT STARTED (18:00 - 23:24 IST) ')
        print('=' * 80)
        send_telegram("<pre>MCX NATURAL GAS\nPaper Trading Bot Online</pre>")

        hist: deque = deque(maxlen=300)
        last_wait_msg_ts = 0.0

        while True:
            try:
                now = get_ist_now()

                if now.weekday() == 6:
                    print(f'[{now.strftime("%H:%M:%S")}] Sunday. Markets closed.')
                    time.sleep(60)
                    continue
                if now.weekday() == 5 and now.hour >= 17:
                    print(f'[{now.strftime("%H:%M:%S")}] Saturday post-17:00 IST. MCX closed.')
                    self._close_all('SATURDAY_CLOSE')
                    time.sleep(60)
                    continue

                if now.hour > MCX_EXIT_HOUR or (now.hour == MCX_EXIT_HOUR and now.minute >= MCX_EXIT_MINUTE):
                    print(f'[AUTO] Exit time reached ({MCX_EXIT_HOUR}:{MCX_EXIT_MINUTE:02d} IST). Liquidating positions...')
                    self._close_all('SESSION_END')
                    send_telegram(f"<pre>MCX Session Completed\nFinal Realized PnL: Rs {self.total_realized_pnl:,.2f}</pre>")
                    break

                if now.hour < MCX_ENTRY_HOUR:
                    if time.time() - last_wait_msg_ts > 60.0:
                        last_wait_msg_ts = time.time()
                        print(f'Waiting for market open ({MCX_ENTRY_HOUR}:00 IST). Current IST: {now.strftime("%H:%M:%S")}')
                    time.sleep(10)
                    continue

                spot = self.get_spot()
                if spot <= 50.0:
                    time.sleep(3)
                    continue

                atm = self.find_atm_strike(spot)
                hist.append(spot)

                reversal = False
                if len(hist) >= 12:
                    current_kama, prev_kama, delta, trend = KAMA.compute(list(hist), period=10, fast=4, slow=30)
                    if current_kama is not None and prev_kama is not None:
                        reversal = self._kama_reversal_confirmed(current_kama, prev_kama)

                if not self.positions:
                    if now.hour >= MCX_ENTRY_HOUR and self.trades_today < MAX_DAILY_TRADES:
                        # Require at least 5 minutes stabilization after a double stop-out
                        time_since_stop = time.time() - getattr(self, "last_double_stop_ts", 0.0)
                        if getattr(self, "last_double_stop_ts", 0.0) == 0.0 or time_since_stop >= 300.0:
                            print(f'[INIT] Opening ATM Straddle at Strike {int(atm)} (Trades today: {self.trades_today}/{MAX_DAILY_TRADES})...')
                            self._enter_leg('CE', atm, 'SELL', loss_stop_pct=DEFAULT_SL_PCT, tsl_pct=DEFAULT_TSL_PCT)
                            self._enter_leg('PE', atm, 'SELL', loss_stop_pct=DEFAULT_SL_PCT, tsl_pct=DEFAULT_TSL_PCT)
                        else:
                            if int(time_since_stop) % 30 == 0:
                                print(f'[WAIT] Double stop cooldown active. Resuming in {int(300 - time_since_stop)}s...')
                else:
                    short_legs = [leg for leg in self.positions if self.positions[leg]['side'] == 'SELL']

                    if len(short_legs) == 1 and reversal:
                        if time.time() - self.last_reentry_ts >= 60.0:
                            missing_leg = 'CE' if 'PE' in short_legs else 'PE'
                            surviving_leg = short_legs[0]
                            surviving_strike = self.positions[surviving_leg]['strike']

                            dist = min(abs(surviving_strike - atm), 25.0)
                            if missing_leg == 'CE':
                                reentry_strike = round_to_price(atm + dist, STRIKE_STEP)
                            else:
                                reentry_strike = round_to_price(atm - dist, STRIKE_STEP)

                            print(f'[REENTRY] KAMA reversal detected! Re-entering {missing_leg} at Strike {int(reentry_strike)} (Surviving: {surviving_leg} {int(surviving_strike)})...')
                            if self._enter_leg(missing_leg, reentry_strike, 'SELL', loss_stop_pct=DEFAULT_SL_PCT, tsl_pct=DEFAULT_TSL_PCT):
                                self.last_reentry_ts = time.time()

                    for leg in list(self.positions.keys()):
                        pos = self.positions.get(leg)
                        if not pos or pos['side'] != 'SELL':
                            continue
                        live_ltp = self._get_leg_ltp(pos)
                        hit, reason = self._update_leg(leg, live_ltp)
                        if hit:
                            print(f'[ALERT] {reason}')
                            self._close_leg(leg, reason)
                            break

                self._print_dashboard(spot, atm)
                time.sleep(1.0)

            except KeyboardInterrupt:
                print("\n[STOP] KeyboardInterrupt received. Squaring off all positions...")
                self._close_all('KEYBOARD_INTERRUPT')
                break
            except Exception as e:
                print(f'[ERROR] Execution loop exception: {e}')
                time.sleep(3.0)

if __name__ == '__main__':
    try:
        bot = NaturalGasPaperBot()
        bot.run()
    except Exception as e:
        print(f'[FATAL] {e}')
        send_telegram(f'<pre>MCX Bot Fatal Error: {e}</pre>')
        sys.exit(1)
