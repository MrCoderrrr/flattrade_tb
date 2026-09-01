"""
MCX Natural Gas Naked Short Straddle (v1.0)
Architecture: Based on NIFTY v2 safety patterns. Unhedged naked selling.
Real Capital Risk: High. Paper trade heavily before running live.
Features: KAMA Reversal Re-entry, Premium Trailing Stop-Loss, Circuit Breaker, Rate-limited verified execution.
"""
import sys, os, time, math, signal, json, threading, datetime, select
from typing import Dict, Any, Tuple, Optional, List
from datetime import datetime as dt_module, timedelta
from colorama import init, Fore, Style

# Third-party for dashboard/math
import numpy as np
import pandas as pd

try:
    from api_helper import NorenApiPy
except ImportError:
    NorenApiPy = None

init(autoreset=True)

# -----------------------------------------------------------------------------
# Configuration Constants
# -----------------------------------------------------------------------------
PROJECT_ROOT            = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_FILE          = os.path.join(PROJECT_ROOT, "natgas_trades.csv")
LIVE_SNAP_FILE          = os.path.join(PROJECT_ROOT, "natgas_snapshot.json")
KILL_SWITCH_FILE        = os.path.join(PROJECT_ROOT, "stop_natgas.flag")
STATE_FILE              = os.path.join(PROJECT_ROOT, "natgas_state.json")

# Session config
MARKET_START_HOUR       = 18
MARKET_START_MINUTE     = 30
SQUARE_OFF_HOUR         = 23
SQUARE_OFF_MINUTE       = 24
LOT_SIZE                = 1250
STRIKE_STEP             = 5.0

# Premium Trailing Stop-Loss config
INITIAL_SL_PCT          = 0.20  # 20% initial hard stop
TRAIL_ACTIVATION_PCT    = 0.25  # Arms at -25%
TRAIL_DISTANCE_PCT      = 0.15  # Trails at +15% from bottom
REENTRY_SL_PCT          = 0.10  # 10% tighter stop for re-entries
PREM_SL_DEBOUNCE_BARS   = 1

# KAMA Re-entry Logic (1-min bars)
KAMA_PERIOD             = 10
KAMA_FAST               = 3
KAMA_SLOW               = 30
KAMA_MIN_REVERSAL_PTS   = 0.5   # 0.5 points move on MCX
KAMA_CONSECUTIVE_BARS   = 2

# Caps & Backoff
MAX_REENTRIES_PER_LEG   = 2
MAX_REENTRIES_TOTAL     = 3
BACKOFF_BASE_SEC        = 60

# Global Risk
PORTFOLIO_CIRCUIT_PCT   = 2.5   # -2.5% of capital (wider due to unhedged/noise)
FEED_STALE_TIMEOUT_S    = 20    # 20s staleness timeout
RECONCILIATION_INTERVAL_S = 30  # Diff broker vs local every 30s

# Execution Engine Safety
RATE_LIMIT_SEC          = 1.05
MAX_ENTRY_RETRIES       = 3
MAX_EXIT_RETRIES        = 5
LIMIT_SLIPPAGE_PCT      = 0.02
LIMIT_SLIPPAGE_MIN_PTS  = 0.5

# Global runtime flags
_EMERGENCY_STOP_TRIGGERED = False
CONFIRM_BEFORE_TRADE = False
_EMERGENCY_STOP_LOCK = threading.Lock()
PAPER_TRADING_MODE = True
CAPITAL = 250_000 # Example placeholder for paper mode, user is prompted in live

def get_ist_now() -> dt_module:
    return dt_module.utcnow() + timedelta(hours=5, minutes=30)

def log_info(msg: str):
    print(f"[{get_ist_now().strftime('%H:%M:%S')} INFO]  {msg}")

def log_warn(msg: str):
    print(f"[{get_ist_now().strftime('%H:%M:%S')} WARN]  {Fore.YELLOW}{msg}{Style.RESET_ALL}")

def log_alert(msg: str):
    print(f"[{get_ist_now().strftime('%H:%M:%S')} ALERT] {Fore.RED}{Style.BRIGHT}{msg}{Style.RESET_ALL}")

def log_trade(msg: str):
    print(f"[{get_ist_now().strftime('%H:%M:%S')} TRADE] {Fore.CYAN}{Style.BRIGHT}{msg}{Style.RESET_ALL}")


class FlattradeBroker:
    def __init__(self, paper_trading: bool):
        self.paper_trading = paper_trading
        self.api = None
        self.order_counter = 1000
        
        # Always authenticate to fetch live data, even in paper mode
        from creds import USER_ID
        try:
            from api_helper import NorenApiPy
            self.api = NorenApiPy()
            log_info("Using NorenApiPy() factory.")
        except ImportError:
            from api_helper import get_norenapi
            self.api = get_norenapi()
            log_warn("NorenApiPy not found, used get_norenapi() fallback.") # Bug 2 fix
            
        token_file = "token.txt" if os.path.exists("token.txt") else os.path.join(PROJECT_ROOT, "token.txt")
        if not os.path.exists(token_file):
            log_warn("Token missing. Live data will be unavailable.")
            if not self.paper_trading: raise RuntimeError("Token missing for LIVE mode.")
        else:
            with open(token_file, "r") as f:
                token = f.read().strip()
            self.api.set_session(userid=str(USER_ID).strip(), password="", usertoken=token)
            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
                if not self.paper_trading: raise RuntimeError("Token invalid or expired. Login again.")
                else: log_warn("Paper mode: Token invalid. Falling back to simulated data.")
            else:
                cash = float(limits.get('cash', 0.0))
                payin = float(limits.get('payin', 0.0))
                self.live_capital = cash + payin
                log_info(f"Flattrade MCX session authenticated. (Live Capital: {self.live_capital})")
                
        if self.paper_trading:
            log_warn("PAPER TRADING MODE — Execution fills are simulated (but data is live if authenticated).")

    def place_option_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0.0, product_type: str = "M", order_type: str = "MKT", remarks: str = "") -> Dict[str, Any]:
        if self.paper_trading or not self.api:
            self.order_counter += 1
            ord_id = f"ORD_{int(time.time())}_{self.order_counter}"
            order_info = {
                "stat": "Ok",
                "norenordno": ord_id,
                "trantype": transaction_type,
                "prctyp": order_type,
                "remarks": remarks,
                "tsym": symbol,
                "qty": quantity
            }
            return order_info

        action = transaction_type[0].upper()
        if price > 0.0:
            buffer_pts = max(0.5, price * LIMIT_SLIPPAGE_PCT)
            if action == 'B':
                raw_lmt = price + buffer_pts
                lmt_price = round(math.ceil(raw_lmt / 0.1) * 0.1, 2)
            else:
                raw_lmt = max(0.1, price - buffer_pts)
                lmt_price = max(0.1, round(math.floor(raw_lmt / 0.1) * 0.1, 2))
            prc_str = f"{lmt_price:.2f}"
        else:
            prc_str = "0"
            
        try:
            res = self.api.place_order(
                buy_or_sell=str(action),
                product_type=str(product_type),
                exchange="MCX",
                tradingsymbol=str(symbol),
                quantity=str(quantity),
                discloseqty="0",
                price_type=order_type,
                price=prc_str,
                trigger_price="0",
                retention="DAY",
                remarks=remarks or "API_NATGAS_V1"
            )
            return res
        except Exception as e:
            return {"stat": "Not_Ok", "emsg": str(e)}


class MarketData:
    def __init__(self, broker: FlattradeBroker):
        self.broker = broker
        self.spot_token = None
        self.spot_tsym = None
        self.latest_spot = 0.0
        self.latest_atm = 0
        self.bars_1m = []
        self.last_completed_1m_key = ""
        self.kama_history = []
        self._init_spot_token()
        
    def _init_spot_token(self):
        if not getattr(self.broker, 'api', None):
            self.spot_token = "SIMULATED"
            self.latest_spot = 220.0 # Example
            self.latest_atm = 220
            return
            
        try:
            res = self.broker.api.searchscrip(exchange="MCX", searchtext="NATURALGAS")
            if res and isinstance(res, list):
                candidates = []
                for item in res:
                    tsym = str(item.get("tsym", "")).upper()
                    if "NATURALGAS" in tsym and "MINI" not in tsym and "FUT" in tsym:
                        exd = item.get("exd")
                        if exd:
                            try:
                                dt = datetime.datetime.strptime(exd, "%d-%b-%Y")
                                candidates.append((dt, item))
                            except ValueError:
                                pass
                if candidates:
                    candidates.sort(key=lambda x: x[0])
                    now_date = get_ist_now().date()
                    valid_cands = [c for c in candidates if c[0].date() >= now_date]
                    best_cand = valid_cands[0][1] if valid_cands else candidates[-1][1]
                    self.spot_token = best_cand.get("token")
                    self.spot_tsym = str(best_cand.get("tsym", "")).upper()
                    log_info(f"Resolved Spot Symbol (Nearest Expiry {best_cand.get('exd')}): {self.spot_tsym} (Token: {self.spot_token})") # Bug 5 fix
        except Exception as e:
            log_warn(f"Failed to resolve Natural Gas token: {e}")
            
    def get_spot_and_atm(self) -> Tuple[float, int, bool]:
        if not getattr(self.broker, 'api', None) or self.spot_token == "SIMULATED":
            return self.latest_spot, self.latest_atm, False
            
        if not self.spot_token: return self.latest_spot, self.latest_atm, True
        
        try:
            res = self.broker.api.get_quotes(exchange="MCX", token=self.spot_token)
            if res and isinstance(res, dict) and str(res.get('stat', '')).lower() in ('ok', 'success'):
                raw_lp = res.get('lp', res.get('ltp', 0.0))
                spot = float(raw_lp)
                if spot > 0:
                    self.latest_spot = spot
                    self.latest_atm = int(math.floor(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)
                    return self.latest_spot, self.latest_atm, False
        except Exception as e:
            log_warn(f"Spot quote error: {e}")
        return self.latest_spot, self.latest_atm, True
        
    def fetch_live_tick(self) -> Tuple[float, int, bool, bool]:
        spot, atm, is_stale = self.get_spot_and_atm()
        now = get_ist_now()
        current_min_key = now.strftime("%Y-%m-%d %H:%M")
        is_new_1m_bar = (current_min_key != self.last_completed_1m_key)
        
        if is_new_1m_bar:
            if self.last_completed_1m_key:
                self.bars_1m.append({"timestamp": self.last_completed_1m_key, "close": spot})
                if len(self.bars_1m) > max(KAMA_SLOW, KAMA_PERIOD) + 5:
                    self.bars_1m.pop(0)
            self.last_completed_1m_key = current_min_key
            
        return spot, atm, is_new_1m_bar, is_stale
        
    def compute_indicators(self) -> Dict[str, Any]:
        if len(self.bars_1m) < KAMA_SLOW + 1:
            return {"kama": None, "kama_diff": 0.0, "kama_trend": 0}
            
        closes = np.array([b["close"] for b in self.bars_1m])
        kama_arr = self._kama(closes, KAMA_PERIOD, KAMA_FAST, KAMA_SLOW)
        
        curr_kama = kama_arr[-1]
        prev_kama = kama_arr[-2]
        diff = curr_kama - prev_kama
        
        trend = 1 if diff > 0 else (-1 if diff < 0 else 0)
        
        return {
            "kama": float(curr_kama),
            "kama_diff": float(diff),
            "kama_trend": int(trend)
        }
        
    def _kama(self, close, n=10, fast=3, slow=30):
        kama = np.zeros_like(close)
        kama[:] = np.nan
        if len(close) < n: return kama
        abs_diff = np.abs(np.diff(close))
        volatility = np.zeros_like(close)
        for i in range(n, len(close)):
            volatility[i] = np.sum(abs_diff[i-n:i])
        change = np.abs(close - np.roll(close, n))
        er = np.zeros_like(close)
        with np.errstate(divide='ignore', invalid='ignore'):
            er = np.where(volatility != 0, change / volatility, 0)
        fast_c = 2 / (fast + 1)
        slow_c = 2 / (slow + 1)
        sc = (er * (fast_c - slow_c) + slow_c) ** 2
        kama[n-1] = close[n-1]
        for i in range(n, len(close)):
            kama[i] = kama[i-1] + sc[i] * (close[i] - kama[i-1])
        return kama
        
    def get_live_quote(self, strike: int, option_type: str) -> Dict[str, Any]:
        if not getattr(self.broker, 'api', None) or self.spot_token == "SIMULATED":
            # Fallback deterministic pricing only if API is completely unavailable
            dist = abs(self.latest_spot - strike)
            return {"lp": max(0.1, 15.0 - (dist * 0.5)), "tsym": f"NATGAS{strike}{option_type}"}
            
        # Actual searchscrip cache could be added here, but searchscrip is heavy.
        # MCX tokens format: Use searchscrip to find token precisely.
        try:
            res = self.broker.api.searchscrip(exchange="MCX", searchtext=f"NATURALGAS {strike} {option_type}")
            if res and isinstance(res, list):
                for item in res:
                    tsym = str(item.get("tsym", "")).upper()
                    if "MINI" not in tsym and str(int(strike)) in tsym and option_type in tsym: # Bug 1: Correct strike/type matching
                        q = self.broker.api.get_quotes(exchange="MCX", token=item.get("token"))
                        if q:
                            lp = float(q.get("lp", q.get("ltp", 0.0)))
                            return {"tsym": tsym, "lp": lp}
        except Exception as e:
            log_warn(f"Quote fetch error for {strike} {option_type}: {e}")
        return {}


class RiskManager:
    def __init__(self, capital: float):
        self.capital = capital
        self.circuit_breaker_loss_limit = -1 * capital * (PORTFOLIO_CIRCUIT_PCT / 100.0)
        
    def check_portfolio_circuit_breaker(self, realized_pnl: float, unrealized_pnl: float) -> tuple:
        total_pnl = realized_pnl + unrealized_pnl
        if total_pnl <= self.circuit_breaker_loss_limit:
            return True, f"Global Circuit Breaker Hit! PnL {total_pnl:.2f} <= Limit {self.circuit_breaker_loss_limit:.2f}"
        return False, ""
        
    def update_premium_tsl(self, current_state: dict, live_premium: float, is_reentry: bool) -> dict:
        lowest_seen = min(current_state.get("lowest_premium_seen", live_premium), live_premium)
        entry_prem = current_state["entry_premium"]
        
        # Determine activation
        armed = current_state.get("is_armed", False)
        if not armed:
            if lowest_seen <= entry_prem * (1 - TRAIL_ACTIVATION_PCT):
                armed = True
                
        # Calculate active stop
        if armed:
            active_stop = lowest_seen * (1 + TRAIL_DISTANCE_PCT)
        else:
            sl_pct = REENTRY_SL_PCT if is_reentry else INITIAL_SL_PCT
            active_stop = entry_prem * (1 + sl_pct)
            
        return {
            "entry_premium": entry_prem,
            "lowest_premium_seen": lowest_seen,
            "is_armed": armed,
            "active_stop": active_stop,
            "breach_count": current_state.get("breach_count", 0),
            "is_reentry": is_reentry
        }


class ExecutionEngine:
    def __init__(self, broker: FlattradeBroker, market_data: MarketData, risk_manager: RiskManager):
        self.broker = broker
        self.market_data = market_data
        self.risk_manager = risk_manager
        
        self.positions: Dict[str, Any] = {}
        self.realized_pnl = 0.0
        self.mode = "WAIT_DATA" 
        self.is_running = True
        
        self.cooldown_tracker: Dict[str, Dict[str, Any]] = {
            "CE": {"stopped_time": 0.0, "kama_at_stop": 0.0, "kama_trend_at_stop": 0, "active": False, "reentries": 0},
            "PE": {"stopped_time": 0.0, "kama_at_stop": 0.0, "kama_trend_at_stop": 0, "active": False, "reentries": 0}
        }
        self.total_reentries_today = 0
        self.last_reconciliation = 0
        self.last_feed_tick = 0
        self._ltp_cache = {}
        
        self._load_state()
        
    def _save_state(self):
        try:
            state = {
                "date": str(get_ist_now().date()),
                "realized_pnl": self.realized_pnl,
                "positions": self.positions,
                "cooldown_tracker": self.cooldown_tracker,
                "total_reentries_today": getattr(self, "total_reentries_today", 0)
            }
            with open(STATE_FILE, "w") as sf:
                json.dump(state, sf)
        except Exception:
            pass

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as sf:
                    state = json.load(sf)
                if state.get("date") == str(get_ist_now().date()):
                    self.realized_pnl = state.get("realized_pnl", 0.0)
                    self.positions = state.get("positions", {})
                    self.cooldown_tracker = state.get("cooldown_tracker", {})
                    self.total_reentries_today = state.get("total_reentries_today", 0)
                    if self.positions:
                        self.mode = "RUNNING"
                        
                    # Reconcile immediately
                    actual_pos = self._get_live_exchange_positions()
                    if actual_pos is not None:
                        for l, p in list(self.positions.items()):
                            if p['tsym'] not in actual_pos or actual_pos[p['tsym']] == 0:
                                log_alert(f"Startup Recon: {l} closed on exchange, removing locally.")
                                del self.positions[l]
            except Exception:
                pass

    def _get_live_exchange_positions(self) -> dict:
        api = getattr(self.broker, "api", None)
        if not api or getattr(self.broker, "paper_trading", False):
            return {p["tsym"]: p["qty"] for p in self.positions.values()} if self.positions else {}
        
        try:
            pos_resp = api.get_positions()
            if pos_resp and isinstance(pos_resp, list):
                live_book = {}
                for p in pos_resp:
                    netqty = int(p.get("netqty", 0))
                    if netqty != 0:
                        live_book[p.get("tsym")] = abs(netqty)
                return live_book
        except Exception as e:
            log_warn(f"Failed to fetch live positions: {e}")
        return None

    def _log_trade(self, action: str, leg: str, strike: int, side: str, qty: int, price: float, pnl: float = None, reason: str = ""):
        try:
            if not os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE, "w") as f: f.write("timestamp,action,leg,strike,side,qty,price,pnl,reason\n")
            with open(TRADE_LOG_FILE, "a") as f:
                ts = get_ist_now().strftime("%Y-%m-%d %H:%M:%S")
                pnl_str = f"{pnl:.2f}" if pnl is not None else ""
                f.write(f"{ts},{action},{leg},{strike},{side},{qty},{price:.2f},{pnl_str},{reason}\n")
        except: pass

    def _get_ltp(self, strike: int, option_type: str) -> float:
        key = f"{strike}_{option_type}"
        if key not in self._ltp_cache:
            q = self.market_data.get_live_quote(strike, option_type)
            self._ltp_cache[key] = float(q.get("lp", 0.0))
        return self._ltp_cache[key]

    def _verify_order_status(self, ord_id: str, tsym: str, side: str, qty: int) -> Tuple[bool, str]:
        api = getattr(self.broker, "api", self.broker)
        is_live = not getattr(self.broker, "paper_trading", False)
        
        for check_attempt in range(4):
            if is_live: time.sleep(0.5)
            if hasattr(api, "single_order_history"):
                try:
                    history = api.single_order_history(orderno=str(ord_id))
                    if history and isinstance(history, list) and len(history) > 0:
                        latest = history[-1]
                        status = str(latest.get("status", "")).upper()
                        if status in ("REJECTED", "CANCELLED"): return False, f"REJECTED: {latest.get('rejreason', 'Rejected')}"
                        if status in ("COMPLETE", "FILLED"): return True, "COMPLETE"
                        if status in ("OPEN", "PENDING", "TRIGGER_PENDING"):
                            if check_attempt == 3 or not is_live: return True, "OPEN"
                            continue
                except: pass
        if getattr(self.broker, "paper_trading", False): return True, f"Paper fill confirmed"
        return False, "INCONCLUSIVE_IN_LIVE_MODE"


    def _ask_user_confirm(self, leg: str, side: str, strike: int, qty: int, ltp: float) -> bool:
        if not CONFIRM_BEFORE_TRADE: return True
        log_info(f"CONFIRMATION REQUIRED: {side} {qty}x {leg} {strike} @ {ltp:.2f}")
        try:
            if sys.stdin.isatty():
                ans = input(f"Proceed with {leg} {side}? (Y/N): ").strip().upper()
                return ans == 'Y'
            else:
                conf_file = "confirm_trade.txt"
                if os.path.exists(conf_file):
                    with open(conf_file, "r") as f: ans = f.read().strip().upper()
                    os.remove(conf_file)
                    return ans == 'Y'
                return False
        except Exception:
            return False

    def _enter_leg(self, leg: str, strike: int, side: str, is_reentry: bool = False) -> bool:
        if leg in self.positions:
            log_warn(f"Skipping {leg} entry — position already exists in state.")
            return False

        q = self.market_data.get_live_quote(strike, leg)
        tsym = q.get("tsym", f"NATGAS{strike}{leg}")
        if not tsym: return False
        
        ltp = float(q.get("lp", 0.0))
        if ltp <= 0:
            log_warn(f"Skipping {leg} entry — LTP is 0 or unavailable.")
            return False
            
        if not self._ask_user_confirm(leg, side, strike, LOT_SIZE, ltp):
            log_info(f"User rejected {leg} entry.")
            return False
            
        placed_successfully = False
        qty = LOT_SIZE
        
        for attempt in range(MAX_ENTRY_RETRIES):
            time.sleep(RATE_LIMIT_SEC)
            
            # Recalculate slippage bound each attempt
            q_retry = self.market_data.get_live_quote(strike, leg)
            current_ltp = float(q_retry.get("lp", ltp)) if q_retry else ltp
            slippage = max(LIMIT_SLIPPAGE_MIN_PTS, current_ltp * LIMIT_SLIPPAGE_PCT)
            limit_price = round(current_ltp - slippage if side == "SELL" else current_ltp + slippage, 1)
            
            res = self.broker.place_option_order(
                symbol=tsym, transaction_type="SELL" if side == "SELL" else "BUY",
                quantity=qty, price=limit_price, order_type="LMT", remarks="API_NATGAS_ENT"
            )
            
            if res and res.get("stat") == "Ok":
                ord_id = res.get("norenordno", res.get("NOrdNo", res.get("order_id", "OK")))
                confirmed, detail = self._verify_order_status(ord_id, tsym, side, qty)
                if confirmed:
                    placed_successfully = True
                    break
                    
        if not placed_successfully:
            log_warn(f"Failed to enter {leg} after {MAX_ENTRY_RETRIES} attempts.")
            return False
            
        pos_info = {
            "strike": strike, "tsym": tsym, "side": side, "qty": qty, "entry_price": current_ltp,
            "tsl_state": {
                "entry_premium": current_ltp,
                "lowest_premium_seen": current_ltp,
                "is_armed": False,
                "active_stop": current_ltp * (1 + (REENTRY_SL_PCT if is_reentry else INITIAL_SL_PCT)),
                "breach_count": 0,
                "is_reentry": is_reentry
            }
        }
        self.positions[leg] = pos_info
        self._log_trade("ENTRY", leg, strike, side, qty, current_ltp, reason="SIGNAL")
        self._save_state()
        return True

    def _exit_leg(self, leg: str, reason: str = "") -> bool:
        pos = self.positions.get(leg)
        if not pos: return True
        
        strike, tsym, side, qty = pos["strike"], pos["tsym"], pos["side"], pos["qty"]
        close_side = "BUY" if side == "SELL" else "SELL"
        
        ltp = self._get_ltp(strike, leg)
        placed_successfully = False
        
        for attempt in range(MAX_EXIT_RETRIES):
            time.sleep(RATE_LIMIT_SEC)
            q_retry = self.market_data.get_live_quote(strike, leg)
            current_ltp = float(q_retry.get("lp", ltp)) if q_retry else ltp
            slippage = max(LIMIT_SLIPPAGE_MIN_PTS, current_ltp * LIMIT_SLIPPAGE_PCT)
            limit_price = round(current_ltp + slippage if close_side == "BUY" else current_ltp - slippage, 1)
            
            res = self.broker.place_option_order(
                symbol=tsym, transaction_type=close_side,
                quantity=qty, price=limit_price, order_type="LMT", remarks="API_NATGAS_EXT"
            )
            
            if res and res.get("stat") == "Ok":
                ord_id = res.get("norenordno", res.get("NOrdNo", res.get("order_id", "OK")))
                confirmed, detail = self._verify_order_status(ord_id, tsym, close_side, qty)
                if confirmed:
                    placed_successfully = True
                    break
                    
        if placed_successfully:
            pnl = (pos["entry_price"] - current_ltp) * qty if side == "SELL" else (current_ltp - pos["entry_price"]) * qty
            self.realized_pnl += pnl
            self._log_trade("EXIT", leg, strike, side, qty, current_ltp, pnl=pnl, reason=reason)
            del self.positions[leg]
            self._save_state()
            return True
        else:
            log_alert(f"CRITICAL: Failed to exit {leg} after {MAX_EXIT_RETRIES} attempts. Position retained in state for retry.")
            return False


    def _exit_all_positions(self, reason="SQUARE_OFF"):
        for leg in list(self.positions.keys()):
            self._exit_leg(leg, reason=reason)
            
    def trigger_emergency_shutdown(self, reason: str):
        global _EMERGENCY_STOP_TRIGGERED
        with _EMERGENCY_STOP_LOCK:
            if _EMERGENCY_STOP_TRIGGERED: return
            _EMERGENCY_STOP_TRIGGERED = True
            
        log_alert(f"EXECUTING GLOBAL EMERGENCY LIQUIDATION (Reason: {reason})! Closing all positions...")
        self.mode = "HALTED"
        self._exit_all_positions(reason=reason)
        self._save_state()
        log_info("Emergency liquidation complete. Bot is halted.")

    def run(self):
        log_info("Starting MCX NatGas Naked Straddle Strategy (v1.0)...")
        log_info(f"Execution Safety: {RATE_LIMIT_SEC}s rate limit | Stale {FEED_STALE_TIMEOUT_S}s | Kill Switch 'zxc'")
        
        while self.is_running and not _EMERGENCY_STOP_TRIGGERED:
            try:
                now = get_ist_now()
                current_time = time.time()
                
                # Check Weekends
                if now.weekday() >= 5:
                    log_info("Weekend detected. MCX Nat Gas is closed. Exiting.")
                    break
                
                # Auto Square-off Time Check
                if now.hour > SQUARE_OFF_HOUR or (now.hour == SQUARE_OFF_HOUR and now.minute >= SQUARE_OFF_MINUTE):
                    log_alert(f"Auto Square-Off Time Reached ({SQUARE_OFF_HOUR:02d}:{SQUARE_OFF_MINUTE:02d}). Liquidating all positions...")
                    self._exit_all_positions(reason="SESSION_END")
                    self.mode = "SESSION_DONE"
                    self._save_state()
                    print(f"\n{Fore.GREEN}✅ Session Completed Successfully. Final Realized PnL: ₹{self.realized_pnl:,.2f}{Style.RESET_ALL}\n")
                    break
                    
                # ── 1. STRICT 1-MINUTE EXECUTION CADENCE ──
                spot, atm, is_new_1m_bar, is_stale = self.market_data.fetch_live_tick()
                if not is_stale:
                    self.last_feed_tick = current_time
                    
                # Bug 4 Fix: Move safety checks to run every tick
                if self.last_feed_tick > 0 and (current_time - self.last_feed_tick) > FEED_STALE_TIMEOUT_S:
                    log_alert(f'🛑 FEED STALE FOR >{FEED_STALE_TIMEOUT_S}s! Emergency Halt.')
                    self.trigger_emergency_shutdown(reason="FEED_STALE")
                    break
                    
                if os.path.exists(KILL_SWITCH_FILE):
                    log_alert('🛑 KILL SWITCH ENGAGED! Emergency Halt.')
                    self.trigger_emergency_shutdown(reason="KILL_SWITCH")
                    break
                    
                if current_time - self.last_reconciliation > RECONCILIATION_INTERVAL_S:
                    self.last_reconciliation = current_time
                    actual_pos = self._get_live_exchange_positions()
                    if actual_pos is not None:
                        for l, p in list(self.positions.items()):
                            if p['tsym'] not in actual_pos or actual_pos[p['tsym']] == 0:
                                log_alert(f'⚠️ RECONCILIATION MISMATCH: {l} closed on exchange!')
                                del self.positions[l]
                                
                unrealized = 0.0
                if self.positions:
                    for l, p in self.positions.items():
                        ltp = self._get_ltp(p["strike"], l)
                        unrealized += (p["entry_price"] - ltp) * p["qty"]
                        
                cb_triggered, cb_msg = self.risk_manager.check_portfolio_circuit_breaker(self.realized_pnl, unrealized)
                if cb_triggered:
                    log_alert(cb_msg)
                    self.trigger_emergency_shutdown(reason="CIRCUIT_BREAKER_HALT")
                    break

                if not is_new_1m_bar:
                    now_curr = get_ist_now()
                    sec_into_min = now_curr.second + (now_curr.microsecond / 1_000_000.0)
                    sleep_sec = max(0.1, 60.0 - sec_into_min + 0.05)
                    time.sleep(min(sleep_sec, 0.5))
                    continue
                    
                # 1-min Evaluation Cycle Starts (Trading decisions)
                self._ltp_cache.clear()
                indicators = self.market_data.compute_indicators()
                kama_val = indicators.get("kama")
                kama_trend = indicators.get("kama_trend")
                
                # ── 2. STATE MACHINE ──
                
                # WAIT_DATA: Pre-market
                if self.mode == "WAIT_DATA":
                    if now.hour > MARKET_START_HOUR or (now.hour == MARKET_START_HOUR and now.minute >= MARKET_START_MINUTE):
                        self.mode = "ENTRY_PHASE"
                    else:
                        self._render_dashboard(spot, atm, indicators, unrealized)
                        continue
                        
                # ENTRY_PHASE: Enter Naked Straddle
                if self.mode == "ENTRY_PHASE":
                    log_info(f"Market Start Time ({MARKET_START_HOUR:02d}:{MARKET_START_MINUTE:02d}) reached. Entering ATM Straddle at {atm}...")
                    ce_ok = self._enter_leg("CE", atm, "SELL")
                    pe_ok = self._enter_leg("PE", atm, "SELL")
                    
                    if ce_ok and pe_ok:
                        self.mode = "RUNNING"
                    else:
                        log_alert("Failed to fill both legs of straddle. Rolling back filled legs...")
                        self._exit_all_positions(reason="PARTIAL_FILL_ROLLBACK")
                        if self.positions:
                            log_alert("CRITICAL: Partial fill rollback failed to close all legs. Halting new entries.")
                            self.mode = "PARTIAL_FILL_STUCK"
                
                # PARTIAL_FILL_STUCK: A leg refused to close during rollback
                elif self.mode == "PARTIAL_FILL_STUCK":
                    log_alert("CRITICAL (PARTIAL_FILL_STUCK): Attempting to exit stuck legs...")
                    self._exit_all_positions(reason="STUCK_LEG_RETRY")
                    if not self.positions:
                        log_info("Stuck legs cleared. Reverting to ENTRY_PHASE.")
                        self.mode = "ENTRY_PHASE"
                        
                # RUNNING: Manage Positions & Re-entries
                elif self.mode == "RUNNING":
                    # Manage Trailing Stops
                    for leg in list(self.positions.keys()):
                        p = self.positions[leg]
                        ltp = self._get_ltp(p["strike"], leg)
                        
                        tsl_state = p["tsl_state"]
                        new_tsl = self.risk_manager.update_premium_tsl(tsl_state, ltp, tsl_state.get("is_reentry", False))
                        
                        if ltp >= new_tsl["active_stop"]:
                            new_tsl["breach_count"] += 1
                        else:
                            new_tsl["breach_count"] = 0
                            
                        p["tsl_state"] = new_tsl
                        
                        if new_tsl["breach_count"] >= PREM_SL_DEBOUNCE_BARS:
                            reason = "TRAIL_SL_HIT" if new_tsl["is_armed"] else "INITIAL_SL_HIT"
                            log_alert(f"🛑 {leg} stop triggered ({reason}) at {ltp:.2f} (Stop: {new_tsl['active_stop']:.2f}). Exiting leg.")
                            if self._exit_leg(leg, reason=reason):
                                self.cooldown_tracker[leg] = {
                                    "stopped_time": current_time,
                                    "kama_at_stop": kama_val or 0.0,
                                    "kama_trend_at_stop": kama_trend,
                                    "active": True,
                                    "reentries": self.cooldown_tracker[leg].get("reentries", 0)
                                }
                    
                    # Manage Re-entries
                    for leg in ("CE", "PE"):
                        cd = self.cooldown_tracker.get(leg)
                        if not cd or not cd.get("active", False): continue
                        if leg in self.positions: continue
                        
                        if cd.get("reentries", 0) >= MAX_REENTRIES_PER_LEG: continue
                        if self.total_reentries_today >= MAX_REENTRIES_TOTAL: continue
                        
                        backoff = BACKOFF_BASE_SEC * (2 ** cd.get("reentries", 0))
                        if current_time - cd["stopped_time"] < backoff: continue
                        
                        if not kama_val: continue
                        
                        # KAMA Reversal Check
                        kama_diff = kama_val - cd["kama_at_stop"]
                        # We want KAMA to reverse. 
                        # If CE (Bearish) was stopped, price went UP. KAMA was pointing UP. We want it to turn DOWN.
                        # So we want kama_trend == -1 and kama_diff < -KAMA_MIN_REVERSAL_PTS
                        is_reversal = False
                        if leg == "CE":
                            if kama_trend == -1 and kama_diff <= -KAMA_MIN_REVERSAL_PTS: is_reversal = True
                        elif leg == "PE":
                            if kama_trend == 1 and kama_diff >= KAMA_MIN_REVERSAL_PTS: is_reversal = True
                            
                        cd["consecutive_reversal_bars"] = cd.get("consecutive_reversal_bars", 0) + 1 if is_reversal else 0
                        
                        if cd["consecutive_reversal_bars"] >= KAMA_CONSECUTIVE_BARS:
                            log_info(f"🔄 KAMA Reversal Confirmed for {leg}! Re-entering...")
                            if self._enter_leg(leg, atm, "SELL", is_reentry=True):
                                cd["active"] = False
                                cd["reentries"] += 1
                                cd["consecutive_reversal_bars"] = 0
                                self.total_reentries_today += 1
                                self._save_state()
                                
                self._render_dashboard(spot, atm, indicators, unrealized)
                
            except Exception as e:
                log_warn(f"Exception in main loop: {e}")
                time.sleep(1)

    def _render_dashboard(self, spot: float, atm: int, indicators: dict, unrealized: float):
        try:
            os.system('cls' if os.name == 'nt' else 'clear')
            
            # Save external snapshot
            snap = {
                "date": str(get_ist_now().date()),
                "time": get_ist_now().strftime("%H:%M:%S"),
                "spot": spot,
                "atm": atm,
                "kama": indicators.get("kama"),
                "kama_trend": indicators.get("kama_trend"),
                "positions": {l: {"strike": p["strike"], "qty": p["qty"], "entry": p["entry_price"], "tsl": p["tsl_state"]} for l, p in self.positions.items()},
                "pnl": {"realized": self.realized_pnl, "unrealized": unrealized, "total": self.realized_pnl + unrealized}
            }
            with open(LIVE_SNAP_FILE, "w") as sf:
                json.dump(snap, sf)
                
            W = 114
            TOP = f"{Fore.GREEN}╔{'═' * W}╗{Style.RESET_ALL}"
            MID = f"{Fore.GREEN}╠{'═' * W}╣{Style.RESET_ALL}"
            BOT = f"{Fore.GREEN}╚{'═' * W}╝{Style.RESET_ALL}"
            V = f"{Fore.GREEN}║{Style.RESET_ALL}"
            
            c_cyan = Fore.CYAN + Style.BRIGHT
            c_yellow = Fore.YELLOW
            res = Style.RESET_ALL
            
            title = f"  {c_cyan}MCX NATURAL GAS STRADDLE (V1.0){res}  {c_yellow}NAKED SHORT{res}  {Fore.GREEN}TYPE 'zxc' TO STOP{res}"
            time_str = get_ist_now().strftime("%H:%M:%S")
            title_pad = W - len(title) + 12 - len(time_str) - 2
            print(TOP)
            print(f"{V}{title}{' ' * max(0, title_pad)}{time_str}  {V}")
            print(MID)
            
            kama_val = indicators.get("kama")
            k_str = f"{kama_val:.1f}" if kama_val else "WARMUP"
            tr = indicators.get("kama_trend", 0)
            tr_str = "UP" if tr == 1 else ("DOWN" if tr == -1 else "FLAT")
            
            metrics = f"  SPOT: {spot:.1f}  ATM: {atm}  KAMA(1m): {k_str} ({tr_str})  CAPITAL: ₹{self.risk_manager.capital:,.0f}"
            print(f"{V}{metrics}{' ' * max(0, W - len(metrics))} {V}")
            print(MID)
            
            if not self.positions:
                st = f"  No open positions. State: {self.mode}"
                print(f"{V}{st}{' ' * max(0, W - len(st))} {V}")
            else:
                for leg, p in self.positions.items():
                    tsl = p["tsl_state"]
                    armed_str = "ARMED" if tsl["is_armed"] else "UNARMED"
                    ltp = self._get_ltp(p["strike"], leg)
                    pnl = (p["entry_price"] - ltp) * p["qty"]
                    row = f"  {leg} {p['strike']} | Entry: {p['entry_price']:.1f} | Live: {ltp:.1f} | Stop: {tsl['active_stop']:.1f} ({armed_str}) | PnL: ₹{pnl:,.1f}"
                    print(f"{V}{row}{' ' * max(0, W - len(row))} {V}")
                    
            print(MID)
            tot = self.realized_pnl + unrealized
            cb = self.risk_manager.circuit_breaker_loss_limit
            summary = f"  REALIZED: ₹{self.realized_pnl:,.1f}  UNREAL: ₹{unrealized:,.1f}  NET: ₹{tot:,.1f}  CB LIMIT: ₹{cb:,.1f}"
            print(f"{V}{summary}{' ' * max(0, W - len(summary))} {V}")
            print(BOT)
            
        except Exception as e:
            log_warn(f"Dashboard snap fail: {e}")

# -----------------------------------------------------------------------------
# Input Listener for Kill Switch
# -----------------------------------------------------------------------------
def input_listener():
    try:
        while True:
            if _EMERGENCY_STOP_TRIGGERED: break
            if select.select([sys.stdin], [], [], 1.0)[0]:
                line = sys.stdin.readline()
                if not line: break
                if "zxc" in line.strip().lower():
                    log_alert("Keyboard Kill Switch 'zxc' detected!")
                    with open(KILL_SWITCH_FILE, "w") as f: f.write("KILL")
                    break
    except Exception:
        pass


def signal_handler(sig, frame):
    log_alert(f"Caught signal {sig}! Engaging Kill Switch.")
    with open(KILL_SWITCH_FILE, "w") as f: f.write("KILL")

# -----------------------------------------------------------------------------
# Main & Setup
# -----------------------------------------------------------------------------
def prompt_user_variables():
    global PAPER_TRADING_MODE, CAPITAL
    
    if not sys.stdin.isatty():
        log_info("Non-interactive mode detected. Proceeding with PAPER mode defaults.")
        PAPER_TRADING_MODE = True
        return
        
    print(f"\n{Fore.CYAN}=== MCX NATURAL GAS STRADDLE v1.0 ==={Style.RESET_ALL}")
    print(f"{Fore.YELLOW}WARNING: This is a NAKED short options strategy. Risk is technically undefined.{Style.RESET_ALL}")
    
    mode_in = input(f"Select Mode [PAPER/LIVE] (Default: PAPER): ").strip().upper()
    if mode_in == "LIVE":
        PAPER_TRADING_MODE = False
    else:
        PAPER_TRADING_MODE = True
        

            
    print(f"\n✅ Deploying in {'PAPER' if PAPER_TRADING_MODE else 'LIVE'} Mode...\n")

if __name__ == "__main__":
    import os
    if os.path.exists(KILL_SWITCH_FILE):
        try: os.remove(KILL_SWITCH_FILE)
        except: pass

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        prompt_user_variables()
        broker = FlattradeBroker(paper_trading=PAPER_TRADING_MODE)
        
        if not PAPER_TRADING_MODE and hasattr(broker, 'live_capital') and broker.live_capital > 0:
            CAPITAL = broker.live_capital
            log_info(f"Capital auto-fetched from Flattrade: ₹{CAPITAL:,.2f}")
            
        market_data = MarketData(broker)
        risk_manager = RiskManager(capital=CAPITAL)
        
        threading.Thread(target=input_listener, daemon=True).start()
        
        engine = ExecutionEngine(broker, market_data, risk_manager)
        engine.run()
        
    except Exception as e:
        log_alert(f"Fatal setup error: {e}")
        sys.exit(1)
