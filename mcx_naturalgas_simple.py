import os
import sys
import time
import math
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
STRIKE_STEP = 1.0
ENTRY_TIME = "18:00"
EXIT_TIME = "11:24"


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


class NaturalGasBot:
    def __init__(self):
        self.api = NorenApiPy() if NorenApiPy else None
        self.positions: Dict[str, Dict] = {}
        self.kama_prev_delta = 0.0
        self.last_reentry_ts = 0.0
        self.last_snapshot = {}

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

        print("[OK] Natural Gas bot authenticated.")

    def get_spot(self) -> float:
        try:
            res = self.api.searchscrip(exchange="MCX", searchtext="NATGAS")
            if not res or not isinstance(res, dict) or not res.get("values"):
                return 0.0
            for item in res["values"]:
                if "NATGAS" in str(item.get("tsym", "")).upper():
                    q = self.api.get_quotes(exchange="MCX", token=item.get("token"))
                    if q and isinstance(q, dict):
                        val = q.get("lp", q.get("ltp", 0.0))
                        if val:
                            return float(val)
        except Exception:
            pass
        return 0.0

    def find_option_symbol(self, strike: float, option_type: str) -> Optional[Dict]:
        """Find a matching MCX NATGAS option contract for the strike."""
        try:
            search_text = f"NATGAS {int(strike)} {option_type}"
            res = self.api.searchscrip(exchange="MCX", searchtext=search_text)
            if not res or not isinstance(res, dict) or not res.get("values"):
                return None

            for item in res["values"]:
                tsym = str(item.get("tsym", "")).upper()
                if str(int(strike)) in tsym and option_type in tsym:
                    q = self.api.get_quotes(exchange="MCX", token=item.get("token"))
                    lp = float(q.get("lp", q.get("ltp", 0.0))) if q else 0.0
                    return {"tsym": item.get("tsym"), "lp": lp, "ls": int(item.get("ls", 1)), "token": item.get("token")}
        except Exception:
            pass
        return None

    def _enter_leg(self, leg: str, strike: float, side: str, loss_stop_pct: float, tsl_pct: float):
        option_type = "CE" if leg == "CE" else "PE"
        match = self.find_option_symbol(strike, option_type)
        if not match:
            print(f"[WARN] Could not resolve {leg} strike={strike}.")
            return None

        tsym = match["tsym"]
        ltp = float(match.get("lp", 0.0))
        qty = 1250
        try:
            res = self.api.place_order(
                buy_or_sell=str(side),
                product_type="M",
                exchange="MCX",
                tradingsymbol=str(tsym),
                quantity=str(qty),
                discloseqty="0",
                price_type="LMT",
                price=f"{ltp:.2f}",
                trigger_price="0",
                retention="DAY",
                remarks="NATGAS_SIMPLE_V2"
            )
            if not res or not isinstance(res, dict) or res.get("stat") != "Ok":
                err = res.get("emsg", str(res)) if isinstance(res, dict) else str(res)
                print(f"[FAIL] {side} {leg} {tsym}: {err}")
                return None
        except Exception as e:
            print(f"[ERROR] order failed for {leg}: {e}")
            return None

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
        print(f"[ENTRY] {side} {leg} {tsym} @ {ltp:.2f} | SL={loss_stop_pct * 100:.0f}% | TSL={tsl_pct * 100:.0f}%")
        return pos

    def _close_leg(self, leg: str, reason: str):
        pos = self.positions.get(leg)
        if not pos:
            return
        tsym = pos["tsym"]
        trade_side = "BUY" if pos["side"] == "SELL" else "SELL"
        ltp = pos["entry_price"]
        try:
            quote = self.api.get_quotes(exchange="MCX", token=self.find_option_symbol(pos["strike"], "CE" if leg == "CE" else "PE").get("token", "")) if self.find_option_symbol(pos["strike"], "CE" if leg == "CE" else "PE") else None
            if quote:
                ltp = float(quote.get("lp", quote.get("ltp", ltp)))
        except Exception:
            pass

        try:
            self.api.place_order(
                buy_or_sell=str(trade_side),
                product_type="M",
                exchange="MCX",
                tradingsymbol=str(tsym),
                quantity=str(pos["qty"]),
                discloseqty="0",
                price_type="LMT",
                price=f"{ltp:.2f}",
                trigger_price="0",
                retention="DAY",
                remarks=f"NATGAS_CLOSE_{reason}"
            )
        except Exception as e:
            print(f"[WARN] close failed for {leg}: {e}")
        finally:
            print(f"[EXIT] {leg} {tsym} @ {ltp:.2f} | reason={reason}")
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
        print("Natural Gas simple bot started.")

        hist: List[float] = []
        current_kama = None
        prev_kama = None

        while True:
            try:
                IST = timezone(timedelta(hours=5, minutes=30))
                now = datetime.now(IST)
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
                    time.sleep(30)
                    continue

                spot = self.get_spot()
                if spot <= 0:
                    time.sleep(5)
                    continue

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
                        atm = self.find_atm_strike(spot)
                        
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
                            atm = self.find_atm_strike(spot)
                            self._enter_leg(missing_leg, atm, "SELL", 0.02, 0.02)
                            self.last_reentry_ts = time.time()
                            print(f"[REENTRY] KAMA reversal triggered, re-entered {missing_leg} at {atm} to form Straddle")

                    for leg in list(self.positions.keys()):
                        pos = self.positions[leg]
                        if pos["side"] != "SELL":
                            continue
                        quote = self.find_option_symbol(pos["strike"], "CE" if leg == "CE" else "PE")
                        live_ltp = quote["lp"] if quote else pos["entry_price"]
                        hit, reason = self._update_leg(leg, live_ltp)
                        if hit:
                            print(f"[HIT] {reason}")
                            self._close_leg(leg, reason)
                            break

                time.sleep(3)

            except KeyboardInterrupt:
                print("Keyboard interrupt. Closing all positions.")
                self._close_all("MANUAL_STOP")
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(5)


if __name__ == "__main__":
    try:
        bot = NaturalGasBot()
        bot.run()
    except Exception as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
