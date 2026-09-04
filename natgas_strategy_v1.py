"""
MCX Natural Gas Naked Short Straddle (v1.1 - fixed)
Architecture: Based on NIFTY v2 safety patterns. Unhedged naked selling.
Real Capital Risk: High. Paper trade heavily before running live.
Features: KAMA Reversal Re-entry, Premium Trailing Stop-Loss, Circuit Breaker, Rate-limited verified execution.

FIX NOTES (v1.1):
- Root cause of "data not fetching": FlattradeBroker used to log a "falling back to
  simulated data" warning when the token was missing/invalid but never actually cleared
  self.api. MarketData then still saw a live (but unauthenticated) api object, skipped
  its simulated-data path, and every real quote call failed/returned nothing -> spot
  stayed at 0.0 and is_stale stayed True forever.
- Fix: added an explicit self.authenticated flag on FlattradeBroker, set True only when
  set_session + get_limits actually succeed. MarketData now checks broker.authenticated
  (not "is self.api truthy") everywhere it decides between real API calls and simulated
  fallback data.
"""
import sys, os, time, math, signal, json, threading, datetime, select
from typing import Dict, Any, Tuple, Optional, List
from datetime import datetime as dt_module, timedelta, timezone
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
    return dt_module.now(timezone.utc) + timedelta(hours=5, minutes=30)

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
        # FIX: explicit authentication flag. Do not infer "is data live" from
        # "does self.api exist" -- self.api can exist (object constructed) while the
        # session itself is unauthenticated/invalid.
        self.authenticated = False

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
            log_warn("Token missing. Live data will be unavailable — falling back to simulated data.")
            if not self.paper_trading: raise RuntimeError("Token missing for LIVE mode.")
            # FIX: self.authenticated stays False -> MarketData will correctly use its
            # simulated fallback path instead of silently failing real API calls.
        else:
            with open(token_file, "r") as f:
                token = f.read().strip()
            self.api.set_session(userid=str(USER_ID).strip(), password="", usertoken=token)
            limits = self.api.get_limits()
            if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
                if not self.paper_trading: raise RuntimeError("Token invalid or expired. Login again.")
                else:
                    log_warn("Paper mode: Token invalid. Falling back to simulated data.")
                    # FIX: previously this branch left self.api set to a live-but-unauthenticated
                    # object, so downstream code thought a real feed was available and never
                    # fell back to simulated prices. self.authenticated remains False here,
                    # which is now the single source of truth MarketData checks.
            else:
                cash = float(limits.get('cash', 0.0))
                payin = float(limits.get('payin', 0.0))
                self.live_capital = cash + payin
                self.authenticated = True
                log_info(f"Flattrade MCX session authenticated. (Live Capital: {self.live_capital})")

        if self.paper_trading:
            log_warn("PAPER TRADING MODE — Execution fills are simulated (but data is live if authenticated).")

    def place_option_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0.0, product_type: str = "M", order_type: str = "MKT", remarks: str = "") -> Dict[str, Any]:
        if self.paper_trading or not self.authenticated:
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

    def _get_mcx_master(self) -> pd.DataFrame:
        if hasattr(self, '_mcx_master_df'):
            return self._mcx_master_df
        
        today = get_ist_now().strftime("%Y-%m-%d")
        cache_file = os.path.join(PROJECT_ROOT, f"MCX_symbols_{today}.csv")
        
        import urllib.request, zipfile, io, pandas as pd
        if not os.path.exists(cache_file):
            try:
                log_info("Downloading latest MCX Symbol Master from exchange network...")
                url = "https://api.shoonya.com/MCX_symbols.txt.zip"
                response = urllib.request.urlopen(url)
                with zipfile.ZipFile(io.BytesIO(response.read())) as z:
                    with z.open("MCX_symbols.txt") as f:
                        df = pd.read_csv(f)
                        df.to_csv(cache_file, index=False)
            except Exception as e:
                log_warn(f"Failed to download MCX master: {e}")
                return pd.DataFrame()
        
        try:
            self._mcx_master_df = pd.read_csv(cache_file)
            return self._mcx_master_df
        except Exception:
            return pd.DataFrame()

    def _init_spot_token(self):
        # FIX: check broker.authenticated, not "is self.broker.api truthy".
        if not getattr(self.broker, 'authenticated', False):
            self.spot_token = "SIMULATED"
            self.latest_spot = 220.0
            self.latest_atm = 220
            log_warn("MarketData: broker not authenticated -> using SIMULATED price feed.")
            return

        try:
            df = self._get_mcx_master()
            if not df.empty:
                # Find Futures (FUTCOM) for NATURALGAS
                fut_df = df[(df['Symbol'] == 'NATURALGAS') & (df['Instrument'] == 'FUTCOM')]
                if not fut_df.empty:
                    # Sort by expiry to get nearest
                    fut_df = fut_df.copy()
                    fut_df['ExpiryDate'] = pd.to_datetime(fut_df['Expiry'], format='%d-%b-%Y')
                    fut_df = fut_df[fut_df['ExpiryDate'].dt.date >= get_ist_now().date()]
                    fut_df = fut_df.sort_values('ExpiryDate')
                    
                    if not fut_df.empty:
                        best_cand = fut_df.iloc[0]
                        self.spot_token = str(best_cand['Token'])
                        self.spot_tsym = str(best_cand['TradingSymbol']).upper()
                        log_info(f"Resolved Spot Symbol (Nearest Expiry {best_cand['Expiry']}): {self.spot_tsym} (Token: {self.spot_token})")
                    else:
                        log_warn("No valid unexpired futures found for NATURALGAS in master.")
            else:
                log_warn("MCX master dataframe is empty. Cannot resolve spot token.")
        except Exception as e:
            log_warn(f"Failed to resolve Natural Gas spot token via master: {e}")

        if not getattr(self, 'spot_token', None):
            log_warn("Could not resolve a live spot token even though authenticated -> falling back to SIMULATED price feed.")
            self.spot_token = "SIMULATED"
            self.latest_spot = 220.0
            self.latest_atm = 220

    def get_spot_and_atm(self) -> Tuple[float, int, bool]:
        # FIX: same authenticated-flag check as above, plus explicit SIMULATED short-circuit.
        if self.spot_token == "SIMULATED" or not getattr(self.broker, 'authenticated', False):
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
        # FIX: authenticated-flag check instead of api-truthy check.
        if self.spot_token == "SIMULATED" or not getattr(self.broker, 'authenticated', False):
            # Fallback deterministic pricing only if API is completely unavailable
            dist = abs(self.latest_spot - strike)
            return {"lp": max(0.1, 15.0 - (dist * 0.5)), "tsym": f"NATGAS{strike}{option_type}"}

        # Actual searchscrip cache could be added here, but searchscrip is heavy.
        # MCX tokens format: Use searchscrip to find token precisely.
        # MCX tokens format: Use downloaded master file instead of searchscrip
        try:
            opt_letter = "CE" if option_type.upper() == "CE" else "PE" # In OptionType col, it's CE or PE
            
            df = self._get_mcx_master()
            if not df.empty:
                # Filter for NATURALGAS options matching the strike and option type
                opt_df = df[(df['Symbol'] == 'NATURALGAS') & 
                            (df['Instrument'] == 'OPTFUT') & 
                            (df['OptionType'] == opt_letter) & 
                            (df['StrikePrice'] == float(strike))]
                
                if not opt_df.empty:
                    # Sort by expiry to get nearest
                    opt_df = opt_df.copy()
                    opt_df['ExpiryDate'] = pd.to_datetime(opt_df['Expiry'], format='%d-%b-%Y')
                    opt_df = opt_df[opt_df['ExpiryDate'].dt.date >= get_ist_now().date()]
                    opt_df = opt_df.sort_values('ExpiryDate')
                    
                    if not opt_df.empty:
                        best_cand = opt_df.iloc[0]
                        token = str(best_cand['Token'])
                        tsym = str(best_cand['TradingSymbol']).upper()
                        
                        q = self.broker.api.get_quotes(exchange="MCX", token=token)
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
                    loaded_cd = state.get("cooldown_tracker", {})
                    # FIX: merge onto the CE/PE default template instead of a raw overwrite.
                    # A previous run's state file missing "CE"/"PE" keys used to cause a
                    # KeyError later in run() when reading self.cooldown_tracker[leg].
                    for leg in ("CE", "PE"):
                        if leg in loaded_cd:
                            self.cooldown_tracker[leg] = loaded_cd[leg]
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
        if not getattr(self.broker, "authenticated", False) or getattr(self.broker, "paper_trading", False):
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
        # FIX: only treat this as a "live" verification path if we're actually
        # authenticated -- otherwise skip straight to the paper-trading confirmation.
        is_live = (not getattr(self.broker, "paper_trading", False)) and getattr(self.broker, "authenticated", False)

        if is_live:
            for check_attempt in range(4):
                time.sleep(0.5)
                if hasattr(api, "single_order_history"):
                    try:
                        history = api.single_order_history(orderno=str(ord_id))
                        if history and isinstance(history, list) and len(history) > 0:
                            latest = history[-1]
                            status = str(latest.get("status", "")).upper()
                            if status in ("REJECTED", "CANCELLED"): return False, f"REJECTED: {latest.get('rejreason', 'Rejected')}"
                            if status in ("COMPLETE", "FILLED"): return True, "COMPLETE"
                            if status in ("OPEN", "PENDING", "TRIGGER_PENDING"):
                                if check_attempt == 3:
                                    # NOTE: still "OPEN" after all retries means the order is
                                    # resting, not filled. We return True/"OPEN" here to match
                                    # original behavior (treat as placed), but this is worth
                                    # tightening further if you see phantom fills in live mode.
                                    return True, "OPEN"
                                continue
                    except: pass
            return False, "INCONCLUSIVE_IN_LIVE_MODE"

        if getattr(self.broker, "paper_trading", False): return True, "Paper fill confirmed"
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
        current_ltp = ltp

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
        current_ltp = ltp

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
        log_info("Starting MCX NatGas Naked Straddle Strategy (v1.1)...")
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

                if not is_new_1m_bar and getattr(self, "current_indicators", None) is None:
                    time.sleep(1.0)
                    continue

                # ── TICK-LEVEL CYCLE (TSL) & 1-MIN CYCLE (KAMA) ──
                self._ltp_cache.clear()

                if is_new_1m_bar or getattr(self, "current_indicators", None) is None:
                    self.current_indicators = self.market_data.compute_indicators()

                indicators = self.current_indicators
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

                        if PREM_SL_DEBOUNCE_BARS <= 1:
                            if ltp >= new_tsl["active_stop"]:
                                new_tsl["breach_count"] = PREM_SL_DEBOUNCE_BARS
                            else:
                                new_tsl["breach_count"] = 0
                        else:
                            if ltp >= new_tsl["active_stop"] and is_new_1m_bar:
                                new_tsl["breach_count"] += 1
                            elif is_new_1m_bar:
                                new_tsl["breach_count"] = 0

                        p["tsl_state"] = new_tsl

                        if new_tsl["breach_count"] >= max(1, PREM_SL_DEBOUNCE_BARS):
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

                        if is_new_1m_bar:
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
                # Sleep 1 second for continuous tick-level TSL evaluation
                time.sleep(1.0)

            except Exception as e:
                log_warn(f"Exception in main loop: {e}")
                time.sleep(1)

    def _render_dashboard(self, spot: float, atm: int, indicators: dict, unrealized: float):
        try:
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

            trend_str = "▲ UP" if indicators.get("kama_trend") == 1 else ("▼ DOWN" if indicators.get("kama_trend") == -1 else "━ FLAT")
            trend_col = c_green if indicators.get("kama_trend") == 1 else (c_red if indicators.get("kama_trend") == -1 else c_yellow)
            kama_str = f"{indicators.get('kama'):.2f}" if indicators.get("kama") else "WARMUP"
            
            print()
            print(TOP)
            feed_str = "LIVE" if getattr(self.broker, "authenticated", False) else "SIMULATED"
            title_left = f"  {c_cyan}MCX NATURAL GAS STRADDLE (V1.1){res}  {c_dim}│{res}  {c_yellow}NAKED SHORT{res}  {c_dim}│{res}  {c_green}TYPE 'zxc' TO STOP{res}"
            title_right = f"{c_dim}{get_ist_now().strftime('%H:%M:%S')}{res}  "
            pad = max(0, W - ansi_len(title_left) - ansi_len(title_right))
            print(f"{V}{title_left}{' ' * pad}{title_right}{V}")
            
            ind_bar = (f"  {c_dim}SPOT:{res} {c_white}{spot:>6.1f}{res}  {c_dim}ATM:{res} {c_yellow}{atm:<5}{res}  "
                       f"{c_dim}KAMA(1m):{res} {c_white}{kama_str:>8}{res} {trend_col}{trend_str}{res}  "
                       f"{c_dim}FEED:{res} {c_white}{feed_str}{res}  "
                       f"{c_dim}CAPITAL:{res} {c_white}₹{self.risk_manager.capital:,.0f}{res}")
            pad_ind = max(0, W - ansi_len(ind_bar))
            print(MID)
            print(f"{V}{ind_bar}{' ' * pad_ind}{V}")
            print(MID)

            if not self.positions:
                msg = f"  {c_yellow}No open positions. State: {self.mode}{res}"
                print(f"{V}{msg}{' ' * max(0, W - ansi_len(msg))}{V}")
            else:
                hdr = f"  {'LEG':<5} {VS} {'STRIKE':>7} {VS} {'SIDE':<5} {VS} {'QTY':>4} {VS} {'ENTRY':>7} {VS} {'BEST PREM':>10} {VS} {'LTP':>7} {VS} {'TSL':>10} {VS} {'PNL':>10}  "
                print(f"{V}{hdr}{' ' * max(0, W - ansi_len(hdr))}{V}")
                print(MID_S)

                for leg, p in self.positions.items():
                    ltp = self._get_ltp(p["strike"], leg)
                    pnl = (p["entry_price"] - ltp) * p["qty"]
                    tsl = p["tsl_state"]
                    
                    side_col = c_red if p["side"] == "SELL" else c_green
                    pnl_col = c_green if pnl >= 0 else c_red
                    sign = "+" if pnl >= 0 else ""
                    armed_str = "ARMED" if tsl["is_armed"] else "UNARMED"
                    tsl_disp = f"{tsl['active_stop']:.1f} ({armed_str})"
                    
                    best_prem = tsl.get("lowest_premium_seen", p["entry_price"])
                    best_prem_str = f"{best_prem:.1f}"
                    
                    row = (f"  {c_white}{leg:<5}{res} {VS} {c_white}{p['strike']:>7}{res} {VS} {side_col}{p['side']:<5}{res} {VS} "
                           f"{c_white}{p['qty']:>4}{res} {VS} "
                           f"{c_white}{p['entry_price']:>7.2f}{res} {VS} {c_dim}{best_prem_str:>10}{res} {VS} "
                           f"{c_yellow}{ltp:>7.2f}{res} {VS} {c_mag}{tsl_disp:>10}{res} {VS} "
                           f"{pnl_col}{sign}₹{pnl:>8,.2f}{res}  ")
                    print(f"{V}{row}{' ' * max(0, W - ansi_len(row))}{V}")

            print(MID)
            tot = self.realized_pnl + unrealized
            cb = self.risk_manager.circuit_breaker_loss_limit
            
            pnl_r_col = c_green if self.realized_pnl >= 0 else c_red
            pnl_u_col = c_green if unrealized >= 0 else c_red
            pnl_t_col = c_green if tot >= 0 else c_red
            
            pnl_str = (f"  {c_dim}REALIZED:{res} {pnl_r_col}₹{self.realized_pnl:,.1f}{res}  {c_dim}│{res}  "
                       f"{c_dim}UNREAL:{res} {pnl_u_col}₹{unrealized:,.1f}{res}  {c_dim}│{res}  "
                       f"{c_dim}NET:{res} {pnl_t_col}₹{tot:,.1f}{res}  {c_dim}│{res}  "
                       f"{c_dim}CB LIMIT:{res} {c_red}₹{cb:,.1f}{res}")
            print(f"{V}{pnl_str}{' ' * max(0, W - ansi_len(pnl_str))}{V}")
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

    print(f"\n{Fore.CYAN}=== MCX NATURAL GAS STRADDLE v1.1 ==={Style.RESET_ALL}")
    print(f"{Fore.YELLOW}WARNING: This is a NAKED short options strategy. Risk is technically undefined.{Style.RESET_ALL}")

    print(f"\n{Fore.GREEN}{Style.BRIGHT}✅ PAPER TRADING ENFORCED GLOBALLY")
    print(f"  ─ Live execution is currently disabled for this session.")
    print(f"  ─ All trades will be simulated with zero real-money risk.{Style.RESET_ALL}\n")
    PAPER_TRADING_MODE = True

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