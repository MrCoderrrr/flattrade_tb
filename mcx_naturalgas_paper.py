import os
import sys
import time
import math
import requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from creds import USER_ID
except Exception:
    USER_ID = os.getenv("USER_ID", "")

try:
    from api_helper import NorenApiPy
except Exception:
    NorenApiPy = None

TOKEN_FILE = "token.txt"
STRIKE_STEP = 5.0
ENTRY_TIME = "18:00"
EXIT_TIME = "11:24"

# --- Telegram Configuration ---
TELEGRAM_TOKEN = "8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE"
CHAT_ID = "6307066850"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=3)
    except Exception as e:
        print(f"[WARN] Failed to send Telegram message: {e}")

def round_to_price(value: float, step: float = STRIKE_STEP) -> float:
    return round(math.floor(value / step + 0.5) * step, 2)


class KAMA:
    @staticmethod
    def compute(closes: List[float], period: int = 10, fast: int = 3, slow: int = 30):
        if len(closes) < period + 1:
            return None, None, 0.0, 0.0

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
        self.total_realized_pnl = 0.0

    def authenticate(self):
        if not self.api:
            raise RuntimeError("NorenApiPy is not available. Make sure api_helper.py is present.")

        if not os.path.exists(TOKEN_FILE):
            raise FileNotFoundError(f"{TOKEN_FILE} missing. Run login.py first.")

        with open(TOKEN_FILE, "r") as f:
            access_token = f.read().strip()

        self.api.set_session(userid=str(USER_ID).strip(), password="", usertoken=access_token)
        limits = self.api.get_limits()
        if not limits or not isinstance(limits, dict) or limits.get("stat") != "Ok":
            raise RuntimeError("Token invalid or expired.")

        print("[OK] Natural Gas PAPER TRADING bot authenticated.")

    def get_spot(self) -> float:
        try:
            res = self.api.searchscrip(exchange="MCX", searchtext="NATURALGAS")
            if not res or not isinstance(res, dict) or not res.get("values"):
                return 0.0
            for item in res["values"]:
                tsym = str(item.get("tsym", "")).upper()
                if "NATURALGAS" in tsym and "MINI" not in tsym:
                    q = self.api.get_quotes(exchange="MCX", token=item.get("token"))
                    if q and isinstance(q, dict):
                        val = q.get("lp", q.get("ltp", 0.0))
                        if val:
                            return float(val)
        except Exception:
            pass
        return 0.0

    def find_option_symbol(self, strike: float, option_type: str) -> Optional[Dict]:
        """Find a matching MCX NATURALGAS option contract for the strike using master CSV."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        csv_file = f"MCX_symbols_{today_str}.csv"
        
        if not os.path.exists(csv_file):
            print(f"[WARN] {csv_file} missing. Run download_mcx.py first.")
            return None
            
        try:
            import pandas as pd
            if getattr(self, '_mcx_master', None) is None:
                df = pd.read_csv(csv_file)
                # Convert Expiry to datetime for sorting
                df['ExpiryDate'] = pd.to_datetime(df['Expiry'], format='%d-%b-%Y', errors='coerce')
                self._mcx_master = df
                
            df = self._mcx_master
            # Filter for Natgas Options matching strike and type
            opt_df = df[
                (df['Symbol'] == 'NATURALGAS') & 
                (df['Instrument'] == 'OPTFUT') & 
                (df['OptionType'] == option_type) & 
                (df['StrikePrice'] == strike)
            ]
            
            if opt_df.empty:
                return None
                
            # Filter out expired contracts and find the nearest expiry
            today = pd.Timestamp.now().normalize()
            future_opts = opt_df[opt_df['ExpiryDate'] >= today]
            if future_opts.empty:
                future_opts = opt_df # Fallback to any if all are technically expired today
                
            nearest = future_opts.sort_values('ExpiryDate').iloc[0]
            token = str(nearest['Token'])
            tsym = str(nearest['TradingSymbol'])
            
            # Get live quote to ensure we have LTP
            q = self.api.get_quotes(exchange="MCX", token=token)
            lp = float(q.get("lp", q.get("ltp", 0.0))) if q else 0.0
            
            return {"tsym": tsym, "lp": lp, "ls": 1250, "token": token}
            
        except Exception as e:
            print(f"[ERROR] Failed to resolve option symbol: {e}")
            return None

    def _enter_leg(self, leg: str, strike: float, side: str, loss_stop_pct: float, tsl_pct: float):
        option_type = "CE" if leg == "CE" else "PE"
        match = self.find_option_symbol(strike, option_type)
        if not match:
            print(f"[WARN] Could not resolve {leg} strike={strike}.")
            return None

        tsym = match["tsym"]
        ltp = float(match.get("lp", 0.0))
        if ltp <= 0:
            print(f"[WARN] LTP is 0 or invalid for {tsym}. Cannot simulate paper trade.")
            return None
            
        qty = 1250 # 1 Lot of Natgas

        # ---- PAPER TRADING (No API Order) ----
        print(f"[PAPER FILL] Entered {side} {qty}x {tsym} @ Rs{ltp:.2f}")

        pos = {
            "leg": leg,
            "tsym": tsym,
            "strike": strike,
            "side": side,
            "qty": qty,
            "entry_price": ltp,
            "loss_stop_pct": loss_stop_pct,
            "tsl_pct": tsl_pct,
            "premium_sl_state": {
                "lowest_ltp": ltp,
                "loss_stop_pct": loss_stop_pct,
                "tsl_pct": tsl_pct,
                "loss_stop": ltp * (1 + loss_stop_pct),
                "tsl": ltp * (1 + tsl_pct),
                "imported_at": time.time(),
            },
        }
        self.positions[leg] = pos
        msg = f"🟢 *PAPER ENTRY* ({leg})\n`{side} {qty}x {tsym}` @ ₹{ltp:.2f}\n*SL:* {loss_stop_pct * 100:.0f}% | *TSL:* {tsl_pct * 100:.0f}%"
        print(f"[PAPER ENTRY] {side} {leg} {tsym} @ Rs{ltp:.2f} | SL={loss_stop_pct * 100:.0f}% | TSL={tsl_pct * 100:.0f}%")
        send_telegram(msg)
        return pos

    def _close_leg(self, leg: str, reason: str):
        pos = self.positions.get(leg)
        if not pos:
            return
        tsym = pos["tsym"]
        trade_side = "BUY" if pos["side"] == "SELL" else "SELL"
        ltp = pos["entry_price"]
        
        try:
            match = self.find_option_symbol(pos["strike"], "CE" if leg == "CE" else "PE")
            if match:
                quote = self.api.get_quotes(exchange="MCX", token=match.get("token", ""))
                if quote:
                    ltp = float(quote.get("lp", quote.get("ltp", ltp)))
        except Exception:
            pass

        # ---- PAPER TRADING (No API Order) ----
        if pos["side"] == "SELL":
            pnl = (pos["entry_price"] - ltp) * pos["qty"]
        else:
            pnl = (ltp - pos["entry_price"]) * pos["qty"]
            
        self.total_realized_pnl += pnl
        
        icon = "🔴" if pnl < 0 else "🟢"
        msg = f"{icon} *PAPER EXIT* ({leg})\n`{trade_side} {pos['qty']}x {tsym}` @ ₹{ltp:.2f}\n*PnL:* ₹{pnl:.2f}\n*Reason:* {reason}"
        
        print(f"[PAPER EXIT] Closed {trade_side} {pos['qty']}x {tsym} @ Rs{ltp:.2f} | PnL: Rs{pnl:.2f} | Reason: {reason}")
        send_telegram(msg)
        del self.positions[leg]

    def _close_all(self, reason: str):
        for leg in list(self.positions.keys()):
            self._close_leg(leg, reason)

    def _update_leg(self, leg: str, live_ltp: float):
        pos = self.positions.get(leg)
        if not pos or pos["side"] != "SELL":
            return False, ""

        state = pos["premium_sl_state"]
        lowest = float(state.get("lowest_ltp", pos["entry_price"]))
        if live_ltp < lowest:
            lowest = live_ltp
        state["lowest_ltp"] = round(lowest, 2)

        loss_stop = lowest * (1.0 + pos["loss_stop_pct"])
        tsl_stop = lowest * (1.0 + pos["tsl_pct"])
        state["loss_stop"] = round(loss_stop, 2)
        state["tsl"] = round(tsl_stop, 2)

        if live_ltp >= loss_stop:
            return True, f"Stop loss hit on {leg} at {live_ltp:.2f} >= {loss_stop:.2f}"
        if live_ltp >= tsl_stop:
            return True, f"TSL hit on {leg} at {live_ltp:.2f} >= {tsl_stop:.2f}"
        return False, ""
        
    def _print_dashboard(self, spot: float, atm: float):
        """Prints local dashboard and sends rich Telegram update optimised for Samsung S25 Ultra."""
        now_s = datetime.now().strftime("%H:%M:%S")
        total_unrealized = 0.0
        leg_lines = []

        for leg, pos in self.positions.items():
            match = self.find_option_symbol(pos["strike"], "CE" if leg == "CE" else "PE")
            live_ltp = float(match.get("lp", pos["entry_price"])) if match else pos["entry_price"]
            pnl = (pos["entry_price"] - live_ltp) * pos["qty"] if pos["side"] == "SELL" else (live_ltp - pos["entry_price"]) * pos["qty"]
            total_unrealized += pnl
            state = pos.get("premium_sl_state", {})
            tsl = state.get("tsl", 0.0)
            p_icon = "🟢" if pnl >= 0 else "🔴"
            leg_lines.append(
                f"{p_icon} `{leg:<4}` 🔽SELL `{int(pos['strike'])}` | E:`{pos['entry_price']:.2f}` → `{live_ltp:.2f}` | TSL:`{tsl:.2f}` | *₹{pnl:+.0f}*"
            )
            # Also print local console
            print(f" -> {leg:2} | {pos['side']} {pos['qty']}x {pos['strike']} | Entry: Rs{pos['entry_price']:.2f} | LTP: Rs{live_ltp:.2f} | TSL: Rs{tsl:.2f} | Unrealized: Rs{pnl:.2f}")

        total_pnl = self.total_realized_pnl + total_unrealized
        pnl_icon  = "🟢" if total_pnl >= 0 else "🔴"
        print(f"\n[{now_s}] SPOT: {spot:.2f} | ATM: {atm} | OPEN LEGS: {len(self.positions)} | REALIZED PNL: Rs{self.total_realized_pnl:.2f}")
        print("-" * 80)

        # --- Build Telegram message ---
        lines = [
            f"⛽ *MCX NATGAS PAPER* `{now_s}`",
            f"",
            f"💹 *Spot:* `{spot:.2f}`  |  *ATM:* `{int(atm)}`",
            f"📋 *Open Legs:* `{len(self.positions)}`",
            f"",
        ]
        if leg_lines:
            lines.append("*── POSITIONS ──*")
            lines.extend(leg_lines)
            lines.append("")

        lines += [
            f"*── PnL ──*",
            f"✅ *Realized:* `₹{self.total_realized_pnl:+,.2f}`",
            f"📌 *Unrealized:* `₹{total_unrealized:+,.2f}`",
            f"{pnl_icon} *Net MTM:* `₹{total_pnl:+,.2f}`",
        ]
        send_telegram("\n".join(lines))



    def _kama_reversal_confirmed(self, current_kama: float, prev_kama: float):
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
        print("="*80)
        print(" NATURAL GAS PAPER TRADING BOT STARTED ")
        print("="*80)
        send_telegram("✅ *Natural Gas Paper Trading Bot Started*")

        hist: List[float] = []
        current_kama = None
        prev_kama = None
        last_dash_ts = 0

        while True:
            try:
                IST = timezone(timedelta(hours=5, minutes=30))
                now = datetime.now(IST)
                
                # Check weekends
                if now.weekday() >= 5:
                    print("Weekend. Exit.")
                    self._close_all("WEEKEND")
                    break

                # Auto-square off at 11:24 PM (23:24 IST)
                if now.hour > 23 or (now.hour == 23 and now.minute >= 24):
                    print("[AUTO] Exit time reached (11:24 PM IST). Closing all positions.")
                    self._close_all("SESSION_END")
                    break

                # Pre-market wait until 6:00 PM
                if now.hour < 18:
                    if time.time() - last_dash_ts > 60:
                        print(f"Waiting for market open (18:00 IST). Current time: {now.strftime('%H:%M:%S')}")
                        last_dash_ts = time.time()
                    time.sleep(5)
                    continue

                spot = self.get_spot()
                if spot <= 0:
                    time.sleep(5)
                    continue
                    
                atm = self.find_atm_strike(spot)

                hist.append(spot)
                trend = 0
                if len(hist) >= 12:
                    current_kama, prev_kama, delta, trend = KAMA.compute(hist, period=10, fast=3, slow=30)
                    if current_kama is not None and prev_kama is not None:
                        reversal = self._kama_reversal_confirmed(current_kama, prev_kama)
                    else:
                        reversal = False
                else:
                    reversal = False

                # If we have no positions, start with a Straddle
                if not self.positions:
                    if now.hour >= 18:
                        # Straddle Entry (Sell CE and PE at ATM)
                        self._enter_leg("CE", atm, "SELL", 0.10, 0.05)
                        self._enter_leg("PE", atm, "SELL", 0.10, 0.05)
                        print(f"[INIT] ATM straddle opened at {atm}")
                else:
                    # We have a position.
                    short_legs = [leg for leg in self.positions if self.positions[leg]["side"] == "SELL"]
                    if len(short_legs) == 1 and reversal:
                        if time.time() - self.last_reentry_ts < 60:
                            pass
                        else:
                            missing_leg = "CE" if "PE" in short_legs else "PE"
                            self._enter_leg(missing_leg, atm, "SELL", 0.02, 0.02)
                            self.last_reentry_ts = time.time()
                            print(f"[REENTRY] KAMA reversal triggered, re-entered {missing_leg} at {atm} to form Straddle")

                    for leg in list(self.positions.keys()):
                        pos = self.positions[leg]
                        if pos["side"] != "SELL":
                            continue
                        match = self.find_option_symbol(pos["strike"], "CE" if leg == "CE" else "PE")
                        live_ltp = float(match.get("lp", pos["entry_price"])) if match else pos["entry_price"]
                        
                        hit, reason = self._update_leg(leg, live_ltp)
                        if hit:
                            print(f"[HIT] {reason}")
                            self._close_leg(leg, reason)
                            break
                            
                # Send Telegram dashboard every 3 seconds
                if time.time() - last_dash_ts >= 3:
                    self._print_dashboard(spot, atm)
                    last_dash_ts = time.time()

                time.sleep(3)

            except KeyboardInterrupt:
                print("\nKeyboard interrupt. Closing all positions.")
                self._close_all("MANUAL_STOP")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(5)


if __name__ == "__main__":
    try:
        bot = NaturalGasPaperBot()
        bot.run()
    except Exception as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
