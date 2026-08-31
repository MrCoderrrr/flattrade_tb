#!/usr/bin/env python3
"""
V2 PRO LIVE DASHBOARD
On startup:
  1. Fetches today's real executed trade history from Flattrade (order book + trade book)
  2. Fetches live open positions from Flattrade positions API
  3. Computes real realized P&L from closed trades
  4. Layers on live LTP quotes for open positions (unrealized P&L)
  5. Falls back to bot snapshot JSON if API is unavailable
Refreshes every 3 seconds.
"""

import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SNAP_FILE  = os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2.json")
STATE_FILE = os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2.json")

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now() -> datetime: return datetime.now(IST)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ==============================================================================
# FLATTRADE API CONNECTION
# ==============================================================================
api = None
try:
    from api_helper import NorenApiPy
    from creds import USER_ID
    if os.path.exists("token.txt"):
        with open("token.txt", "r") as f:
            tok = f.read().strip()
        api = NorenApiPy()
        api.set_session(userid=str(USER_ID).strip(), password='', usertoken=tok)
        print(f"[DASHBOARD] Connected to Flattrade API as {USER_ID}", flush=True)
except Exception as e:
    print(f"[DASHBOARD] Flattrade API unavailable ({e}), will use snapshot file.", flush=True)
    api = None

_token_cache: Dict[str, str] = {}

# ==============================================================================
# FLATTRADE DATA FETCHERS
# ==============================================================================

def get_spot() -> float:
    if not api: return 0.0
    try:
        q = api.get_quotes(exchange='NSE', token='26000')
        if q and isinstance(q, dict):
            return float(q.get('lp', q.get('ltp', 0.0)))
    except Exception:
        pass
    return 0.0

def get_ltp_for_tsym(tsym: str) -> float:
    """Fetch live LTP for an NFO symbol (with token caching)."""
    if not api or not tsym: return 0.0
    try:
        if tsym not in _token_cache:
            res = api.searchscrip(exchange='NFO', searchtext=tsym)
            if res and isinstance(res, dict) and res.get('values'):
                for item in res['values']:
                    if item.get('tsym') == tsym:
                        _token_cache[tsym] = item.get('token', '')
                        break
        token = _token_cache.get(tsym, '')
        if token:
            q = api.get_quotes(exchange='NFO', token=token)
            if q and isinstance(q, dict):
                return float(q.get('lp', q.get('ltp', 0.0)))
    except Exception:
        pass
    return 0.0

def fetch_flattrade_order_book() -> List[Dict]:
    """
    Fetch today's completed orders from Flattrade order book.
    Returns a list of filled orders for NFO exchange.
    """
    if not api: return []
    try:
        res = api.get_order_book()
        if res and isinstance(res, list):
            today = get_ist_now().date()
            filled = []
            for o in res:
                # NOTE: Flattrade uses 'exch' not 'exchange'
                exch = o.get('exch', o.get('exchange', ''))
                if exch != 'NFO': continue
                if o.get('status', '').upper() not in ('COMPLETE', 'FILLED', 'FIL'): continue
                # Flattrade order time format: "HH:MM:SS DD-MM-YYYY"
                try:
                    norentm = o.get('norentm', o.get('exch_tm', ''))
                    if norentm:
                        # Try both formats
                        for fmt in ("%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S"):
                            try:
                                odate = datetime.strptime(norentm, fmt).date()
                                if odate == today:
                                    filled.append(o)
                                break
                            except ValueError:
                                continue
                    else:
                        filled.append(o)  # include if no timestamp available
                except Exception:
                    filled.append(o)
            return filled
    except Exception as e:
        print(f"[DASHBOARD] Order book fetch error: {e}", flush=True)
    return []

def fetch_flattrade_trade_book() -> List[Dict]:
    """
    Fetch today's executed trade confirmations from Flattrade trade book.
    Trade book shows actual fill price and qty for each execution.
    """
    if not api: return []
    try:
        res = api.get_trade_book()
        if res and isinstance(res, list):
            return [t for t in res if t.get('exch', t.get('exchange', '')) == 'NFO']
    except Exception as e:
        print(f"[DASHBOARD] Trade book fetch error: {e}", flush=True)
    return []

def fetch_flattrade_positions() -> List[Dict]:
    """
    Fetch live open positions from Flattrade positions API.
    Returns NFO positions with net qty, avg price, live LTP, live P&L.
    """
    if not api: return []
    try:
        res = api.get_positions()
        if res and isinstance(res, list):
            return [p for p in res if p.get('exch', p.get('exchange', '')) == 'NFO' and int(p.get('netqty', 0)) != 0]
    except Exception as e:
        print(f"[DASHBOARD] Positions fetch error: {e}", flush=True)
    return []

def compute_realized_pnl_from_trades(trade_book: List[Dict]) -> float:
    """
    Compute realized P&L from trade book by matching BUY vs SELL fills
    on each symbol. Only closed/matched quantities contribute to Realized P&L.
    Open positions do NOT count as realized.
    """
    symbol_fills: Dict[str, Dict] = {}
    for t in trade_book:
        tsym = t.get('tsym', '')
        qty = int(t.get('qty', t.get('fillshares', 0)) or 0)
        price = float(t.get('avgprc', t.get('flprc', 0.0)) or 0.0)
        side = t.get('trantype', t.get('buy_or_sell', 'B')).upper()
        if tsym not in symbol_fills:
            symbol_fills[tsym] = {'buy_qty': 0, 'sell_qty': 0, 'buy_cost': 0.0, 'sell_proceeds': 0.0}
        if side == 'B':
            symbol_fills[tsym]['buy_cost'] += qty * price
            symbol_fills[tsym]['buy_qty'] += qty
        else:
            symbol_fills[tsym]['sell_proceeds'] += qty * price
            symbol_fills[tsym]['sell_qty'] += qty

    realized = 0.0
    for sym, fills in symbol_fills.items():
        matched_qty = min(fills['buy_qty'], fills['sell_qty'])
        if matched_qty > 0 and fills['buy_qty'] > 0 and fills['sell_qty'] > 0:
            avg_buy = fills['buy_cost'] / fills['buy_qty']
            avg_sell = fills['sell_proceeds'] / fills['sell_qty']
            realized += (avg_sell - avg_buy) * matched_qty
    return round(realized, 2)

def build_positions_from_flattrade(ft_positions: List[Dict]) -> Dict[str, Dict]:
    """
    Build a positions dict from Flattrade's live positions API response.
    Enriches with live LTP and P&L.
    """
    positions_view = {}
    for i, p in enumerate(ft_positions):
        tsym = p.get('tsym', '')
        netqty = int(p.get('netqty', 0))
        if netqty == 0: continue

        # Flattrade gives daypnl, urmtom directly
        day_pnl = float(p.get('daypnl', 0.0) or 0.0)
        urmtom   = float(p.get('urmtom', 0.0) or 0.0)
        avg_price = float(p.get('netavgprc', p.get('avgprc', 0.0)) or 0.0)

        # Net negative qty = net short (sold more than bought)
        side = "SELL" if netqty < 0 else "BUY"
        qty_abs = abs(netqty)

        # Live LTP
        ltp = float(p.get('lp', p.get('ltp', 0.0)) or 0.0)
        if ltp <= 0.0:
            ltp = get_ltp_for_tsym(tsym)

        # Compute live P&L if broker didn't give it
        if urmtom != 0.0:
            live_pnl = urmtom
        elif side == "SELL":
            live_pnl = (avg_price - ltp) * qty_abs
        else:
            live_pnl = (ltp - avg_price) * qty_abs

        # Guess leg name and strike from Flattrade symbol format (e.g. NIFTY01SEP26C24050)
        import re
        strike = 0
        base = "CE"
        m = re.search(r'([CP])(\d{4,6})$', tsym)
        if m:
            base = "CE" if m.group(1) == 'C' else "PE"
            strike = int(m.group(2))
        elif "CE" in tsym:
            base = "CE"
        elif "PE" in tsym:
            base = "PE"

        if side == "BUY":
            leg_name = f"{base}_HEDGE"
        else:
            leg_name = base

        if leg_name in positions_view:
            leg_name = f"{leg_name}_{tsym[-4:]}"

        positions_view[leg_name] = {
            "strike": strike,
            "tsym": tsym,
            "base": base,
            "side": side,
            "qty": qty_abs,
            "entry_price": avg_price,
            "live_ltp": ltp,
            "live_pnl": live_pnl,
            "spot_sl_state": None,
        }
    return positions_view


# ==============================================================================
# MAIN DATA LOADER — Priority:
# 1. Flattrade live API (positions + trade book + spot)
# 2. Bot snapshot JSON (if API down or positions empty)
# 3. State file fallback
# ==============================================================================

_ft_realized_pnl: float = 0.0   # cached from trade book (expensive call)
_ft_last_tradebook_fetch: float = 0.0
_ft_trade_book_cache: List[Dict] = []

def load_data() -> Optional[Dict]:
    global _ft_realized_pnl, _ft_last_tradebook_fetch, _ft_trade_book_cache

    now_str = get_ist_now().strftime("%H:%M:%S")
    spot = get_spot() if api else 0.0
    atm = int(round(spot / 50.0) * 50) if spot > 0 else 0

    # --- Try Flattrade live positions ---
    if api:
        try:
            ft_positions = fetch_flattrade_positions()

            # Re-fetch trade book every 30 seconds to compute realized P&L
            if time.time() - _ft_last_tradebook_fetch > 30.0:
                _ft_trade_book_cache = fetch_flattrade_trade_book()
                _ft_realized_pnl = compute_realized_pnl_from_trades(_ft_trade_book_cache)
                _ft_last_tradebook_fetch = time.time()

            positions_view = build_positions_from_flattrade(ft_positions)
            unrealized = sum(p["live_pnl"] for p in positions_view.values())

            # Try to enrich with bot-side indicators and premium TSL state
            bot_snap = {}
            if os.path.exists(SNAP_FILE):
                try:
                    if time.time() - os.path.getmtime(SNAP_FILE) < 30.0:
                        with open(SNAP_FILE, "r") as f:
                            bot_snap = json.load(f)
                except Exception:
                    pass
            elif os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, "r") as f:
                        bot_snap = json.load(f)
                except Exception:
                    pass

            bot_positions = bot_snap.get("positions", {})
            for leg, pos in positions_view.items():
                base = pos["base"]
                b_pos = bot_positions.get(leg) or bot_positions.get(base)
                if b_pos:
                    pos["premium_sl_state"] = b_pos.get("premium_sl_state")
                    if pos["strike"] == 0:
                        pos["strike"] = b_pos.get("strike", 0)

                # Ensure TSL state is clean and accurate for short option legs
                if pos.get("side") == "SELL":
                    entry_p = float(pos.get("entry_price", 0.0))
                    live_p  = float(pos.get("live_ltp", entry_p))
                    if live_p <= 0.0: live_p = entry_p

                    sl_state = pos.get("premium_sl_state")
                    regime = bot_snap.get("regime", "CHOP")
                    trail_pct = 0.05 if regime == "CHOP" else 0.07

                    raw_lowest = float(sl_state.get("lowest_ltp", entry_p)) if (sl_state and isinstance(sl_state, dict)) else entry_p
                    if live_p < raw_lowest:
                        raw_lowest = live_p
                    lowest_p = min(raw_lowest, entry_p)

                    initial_sl = round(entry_p * (1.0 + trail_pct), 2)
                    candidate_sl = round(lowest_p * (1.0 + trail_pct), 2)
                    if lowest_p <= (entry_p * 0.95):
                        candidate_sl = min(candidate_sl, entry_p)

                    cur_sl = min(initial_sl, candidate_sl)

                    pos["premium_sl_state"] = {
                        "entry_price": entry_p,
                        "lowest_ltp": round(lowest_p, 2),
                        "current_sl": cur_sl,
                        "initial_sl": initial_sl,
                        "trail_pct": trail_pct,
                    }

            return {
                "now_str": now_str,
                "mode": bot_snap.get("mode", "LIVE"),
                "paper_mode": False,
                "spot": spot,
                "atm": atm,
                "adx": bot_snap.get("adx", 0.0),
                "kama": bot_snap.get("kama"),
                "regime": bot_snap.get("regime", "LIVE"),
                "trend": bot_snap.get("trend", 0),
                "atr": bot_snap.get("atr", 0.0),
                "dte": bot_snap.get("dte", 0.0),
                "realized_pnl": _ft_realized_pnl,
                "unrealized_pnl": unrealized,
                "positions": positions_view,
                "cooldown_tracker": bot_snap.get("cooldown_tracker", {}),
                "trade_count": len(_ft_trade_book_cache),
                "last_event": f"Flattrade live data | {len(ft_positions)} open positions | {len(_ft_trade_book_cache)} trades today",
                "data_source": "FLATTRADE_LIVE",
            }
        except Exception as e:
            print(f"[DASHBOARD] Flattrade live fetch error: {e}", flush=True)

    # --- Fallback: Bot snapshot JSON ---
    if os.path.exists(SNAP_FILE):
        try:
            if time.time() - os.path.getmtime(SNAP_FILE) < 30.0:
                with open(SNAP_FILE, "r") as f:
                    data = json.load(f)
                data["now_str"] = now_str
                data["data_source"] = "BOT_SNAPSHOT"
                return data
        except Exception:
            pass

    # --- Fallback: State file ---
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
            positions_view = {}
            unrealized = 0.0
            for leg, pos in st.get("positions", {}).items():
                ltp = get_ltp_for_tsym(pos.get("tsym", "")) if api else float(pos.get("entry_price", 0.0))
                if ltp <= 0.0: ltp = float(pos.get("entry_price", 0.0))
                side = pos.get("side", "SELL")
                qty = int(pos.get("qty", 65))
                entry = float(pos.get("entry_price", 0.0))
                pnl = (entry - ltp) * qty if side == "SELL" else (ltp - entry) * qty
                unrealized += pnl
                positions_view[leg] = {**pos, "live_ltp": ltp, "live_pnl": pnl}
            return {
                "now_str": now_str, "mode": st.get("mode", "ACTIVE"),
                "paper_mode": False, "spot": spot, "atm": atm,
                "adx": 0.0, "kama": None, "regime": "—", "trend": 0, "atr": 0.0, "dte": 0.0,
                "realized_pnl": float(st.get("realized_pnl", 0.0)),
                "unrealized_pnl": unrealized, "positions": positions_view,
                "cooldown_tracker": st.get("cooldown_tracker", {}),
                "last_event": "State file fallback (bot may be offline)",
                "data_source": "STATE_FILE",
            }
        except Exception:
            pass

    return None


# ==============================================================================
# RICH RENDERER
# ==============================================================================

def render_rich(snap: Optional[Dict]):
    if not snap:
        return Panel(
            Text("⏳ Waiting for data...\n\nChecking:\n  • Flattrade API\n  • data/state/live_snapshot_v2.json\n  • data/state/algo_state_v2.json", style="bold yellow"),
            title="V2 PRO LIVE DASHBOARD"
        )

    source = snap.get("data_source", "?")
    source_color = "green" if source == "FLATTRADE_LIVE" else ("yellow" if source == "BOT_SNAPSHOT" else "red")

    header = Text(
        f" V2 PRO ALGO — {snap.get('now_str', '')}  |  Mode: {snap.get('mode', '-')}  |  "
        f"{'LIVE' if not snap.get('paper_mode') else 'PAPER'} TRADING  |  Source: {source} ",
        style="bold white on blue"
    )

    top = Table.grid(expand=True)
    for _ in range(4): top.add_column(justify="left")

    spot_str = f"{snap.get('spot', 0):.2f}" if snap.get('spot', 0) > 0 else "[yellow]Fetching...[/yellow]"
    trend_val = snap.get('trend', 0)
    trend_str = "[bold green]▲ BULLISH[/bold green]" if trend_val == 1 else ("[bold red]▼ BEARISH[/bold red]" if trend_val == -1 else "[yellow]— FLAT[/yellow]")
    kama_val = snap.get('kama')
    kama_str = f"₹{kama_val:.2f}" if (kama_val and kama_val > 0) else "[dim]Calculating...[/dim]"
    adx_val = snap.get('adx', 0.0)
    regime = snap.get('regime', '—')

    top.add_row(
        f"[bold]Spot:[/bold] {spot_str}",
        f"[bold]ATM:[/bold] {snap.get('atm', '—')}",
        f"[bold]ADX(9):[/bold] {adx_val:.1f} ({regime})",
        f"[bold]KAMA 1m Trend:[/bold] {trend_str}",
    )

    r_pnl = snap.get('realized_pnl', 0.0)
    u_pnl = snap.get('unrealized_pnl', 0.0)
    tot_pnl = r_pnl + u_pnl
    r_str   = f"[green]₹{r_pnl:,.2f}[/green]"   if r_pnl >= 0  else f"[red]₹{r_pnl:,.2f}[/red]"
    u_str   = f"[green]₹{u_pnl:,.2f}[/green]"   if u_pnl >= 0  else f"[red]₹{u_pnl:,.2f}[/red]"
    tot_str = f"[bold green]₹{tot_pnl:,.2f}[/bold green]" if tot_pnl >= 0 else f"[bold red]₹{tot_pnl:,.2f}[/bold red]"
    trades_today = snap.get('trade_count', '—')

    top.add_row(
        f"[bold]KAMA(13,3,30):[/bold] {kama_str}",
        f"[bold]ATR(5m):[/bold] {snap.get('atr', 0):.2f}",
        f"[bold]Realized:[/bold] {r_str}   [bold]Unrealized:[/bold] {u_str}",
        f"[bold]Total MTM:[/bold] {tot_str}   [dim]({trades_today} trades today)[/dim]",
    )

    # Positions Table
    pos_table = Table(title="📊 Open Positions & Dynamic TSL (Live Flattrade Stream)", expand=True, show_lines=True)
    pos_table.add_column("Leg", style="bold")
    pos_table.add_column("Symbol")
    pos_table.add_column("Side")
    pos_table.add_column("Qty")
    pos_table.add_column("Entry ₹", justify="right")
    pos_table.add_column("Live LTP ₹", justify="right")
    pos_table.add_column("Lowest ₹ (Best)", justify="right", style="bold cyan")
    pos_table.add_column("Active TSL ₹", justify="right", style="bold yellow")
    pos_table.add_column("Buffer to TSL", justify="right")
    pos_table.add_column("P&L ₹", justify="right")

    positions = snap.get("positions", {})
    if positions:
        for leg, pos in positions.items():
            pnl = pos.get("live_pnl", 0.0)
            pnl_str = f"[green]₹{pnl:,.2f}[/green]" if pnl >= 0 else f"[red]₹{pnl:,.2f}[/red]"
            entry_p = float(pos.get("entry_price", 0.0))
            live_p  = float(pos.get("live_ltp", 0.0))
            
            sl_state = pos.get("premium_sl_state") or {}
            if pos.get("side") == "SELL" and sl_state:
                lowest_val = float(sl_state.get("lowest_ltp", live_p))
                lowest_str = f"₹{lowest_val:.2f}"
                tsl_val    = float(sl_state.get("current_sl", entry_p * 1.35))
                tsl_str    = f"₹{tsl_val:.2f}"
                diff       = tsl_val - live_p
                diff_str   = f"[green]+₹{diff:.2f}[/green]" if diff > 0 else f"[bold red]⛔ BREACHED[/bold red]"
            else:
                lowest_str = "—"
                tsl_str    = "[dim]Hedge (No TSL)[/dim]"
                diff_str   = "—"

            side_str = f"[bold red]{pos.get('side','—')}[/bold red]" if pos.get('side') == 'SELL' else f"[bold green]{pos.get('side','—')}[/bold green]"
            pos_table.add_row(
                leg,
                pos.get("tsym", "—"),
                side_str,
                str(pos.get("qty", "—")),
                f"₹{entry_p:.2f}",
                f"₹{live_p:.2f}",
                lowest_str,
                tsl_str,
                diff_str,
                pnl_str,
            )
    else:
        pos_table.add_row("No Open Positions", "—", "—", "—", "—", "—", "—", "—", "—", "—")

    # Anti-Whipsaw Dual-Leg Status Table
    cd_table = Table(title="⚡ Anti-Whipsaw Protection System", expand=True, show_lines=False)
    cd_table.add_column("Feature", style="bold")
    cd_table.add_column("Status")
    cd_table.add_column("Config")
    cd_table.add_row("Premium Trailing SL", "[green]✅ ACTIVE (Per-Second Live Ticks)[/green]", "5% trail (CHOP) / 7% trail (TREND)")
    cd_table.add_row("Dual-Leg Exit", "[green]✅ ENABLED[/green]", "Instantly closes BOTH short legs on any SL hit")
    cd_table.add_row("KAMA 1m Engine", "[green]✅ ENABLED[/green]", "KAMA(13,3,30) calculated on 1-min closes")
    cd_table.add_row("Regime Gateway", "[green]✅ ENABLED[/green]", "ADX(9) + ATR(14) calculated on 5-min bars")

    footer = Text(
        f" [{source_color}]Data: {source}[/{source_color}]  |  Updated: {snap.get('now_str')}  |  Refresh: 3s  |  Press Ctrl+C to close ",
        style="dim"
    )

    layout = Table.grid(expand=True)
    layout.add_row(Panel(header, style="on blue"))
    layout.add_row(Panel(top, title="Market State & P&L"))
    layout.add_row(pos_table)
    layout.add_row(cd_table)
    layout.add_row(Panel(Text(snap.get("last_event", "—"), style="dim"), title="Last Event"))
    layout.add_row(footer)
    return layout


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    print("[DASHBOARD] Starting — fetching Flattrade trade history on boot...", flush=True)

    # Warm up: fetch trade book once on startup so realized P&L is ready immediately
    global _ft_realized_pnl, _ft_last_tradebook_fetch, _ft_trade_book_cache
    if api:
        try:
            _ft_trade_book_cache = fetch_flattrade_trade_book()
            _ft_realized_pnl = compute_realized_pnl_from_trades(_ft_trade_book_cache)
            _ft_last_tradebook_fetch = time.time()
            print(f"[DASHBOARD] Loaded {len(_ft_trade_book_cache)} trades today | Realized P&L: ₹{_ft_realized_pnl:,.2f}", flush=True)
        except Exception as e:
            print(f"[DASHBOARD] Trade book warmup failed: {e}", flush=True)

    if HAS_RICH:
        console = Console()
        with Live(render_rich(load_data()), console=console, refresh_per_second=1, screen=True) as live:
            try:
                while True:
                    live.update(render_rich(load_data()))
                    time.sleep(3.0)
            except KeyboardInterrupt:
                pass
    else:
        try:
            while True:
                os.system('clear' if os.name == 'posix' else 'cls')
                snap = load_data()
                if snap:
                    src = snap.get("data_source", "?")
                    print(f"=== V2 PRO DASHBOARD [{snap.get('now_str')}] | Source: {src} ===")
                    print(f"Spot: {snap.get('spot',0):.2f} | ADX: {snap.get('adx',0):.1f} ({snap.get('regime','—')}) | Trend: {snap.get('trend',0)}")
                    print(f"Realized: ₹{snap.get('realized_pnl',0):,.2f} | Unrealized: ₹{snap.get('unrealized_pnl',0):,.2f} | Total MTM: ₹{snap.get('realized_pnl',0)+snap.get('unrealized_pnl',0):,.2f}")
                    print("-" * 75)
                    for leg, p in snap.get('positions', {}).items():
                        sl = (p.get('spot_sl_state') or {}).get('current_sl', '—')
                        print(f" {leg:<12} {p.get('tsym','—'):<25} {p.get('side','—'):<5} Entry ₹{p.get('entry_price',0):<7.2f} LTP ₹{p.get('live_ltp',0):<7.2f} PnL ₹{p.get('live_pnl',0):<10.2f} SL: {sl}")
                    print(f"\n{snap.get('last_event','—')}")
                else:
                    print("=== V2 PRO DASHBOARD ===")
                    print("Waiting for bot data or Flattrade API connection...")
                time.sleep(3.0)
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
