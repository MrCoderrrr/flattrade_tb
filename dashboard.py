#!/usr/bin/env python3
import os
import sys
import time
import json
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SNAP_FILE = os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2.json")
STATE_FILE = os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2.json")

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now() -> datetime: return datetime.now(IST)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Fallback Flattrade reader if standalone
api = None
try:
    from api_helper import NorenApiPy
    from creds import USER_ID
    if os.path.exists("token.txt"):
        with open("token.txt", "r") as f:
            tok = f.read().strip()
        api = NorenApiPy()
        api.set_session(userid=str(USER_ID).strip(), password='', usertoken=tok)
except Exception:
    api = None

_quote_token_cache = {}

def get_quote_for_symbol(tsym: str) -> float:
    if not api or not tsym: return 0.0
    try:
        if tsym not in _quote_token_cache:
            res = api.searchscrip(exchange='NFO', searchtext=tsym)
            if res and isinstance(res, dict) and res.get('values'):
                for item in res['values']:
                    if item.get('tsym') == tsym:
                        _quote_token_cache[tsym] = item.get('token')
                        break
        tok = _quote_token_cache.get(tsym)
        if tok:
            q = api.get_quotes(exchange='NFO', token=tok)
            if q and isinstance(q, dict):
                return float(q.get('lp', q.get('ltp', 0.0)))
    except Exception:
        pass
    return 0.0

def get_spot() -> float:
    if not api: return 0.0
    try:
        q = api.get_quotes(exchange='NSE', token='26000')
        if q and isinstance(q, dict):
            return float(q.get('lp', q.get('ltp', 0.0)))
    except Exception:
        pass
    return 0.0

def load_data():
    now_str = get_ist_now().strftime("%H:%M:%S")

    # 1. First priority: Live Snapshot from Bot
    if os.path.exists(SNAP_FILE):
        try:
            mtime = os.path.getmtime(SNAP_FILE)
            if time.time() - mtime < 10.0:
                with open(SNAP_FILE, "r") as f:
                    data = json.load(f)
                    data["now_str"] = now_str
                    return data
        except Exception:
            pass

    # 2. Second priority: Read state file and query broker directly
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
            
            raw_positions = st.get("positions", {})
            positions_view = {}
            unrealized = 0.0
            spot_val = get_spot()

            for leg, pos in raw_positions.items():
                tsym = pos.get("tsym")
                entry_prc = float(pos.get("entry_price", 0.0))
                qty = int(pos.get("qty", 65))
                side = pos.get("side", "SELL")
                
                ltp = get_quote_for_symbol(tsym)
                if ltp <= 0.0:
                    ltp = entry_prc

                if side == "SELL":
                    leg_pnl = (entry_prc - ltp) * qty
                else:
                    leg_pnl = (ltp - entry_prc) * qty

                unrealized += leg_pnl
                positions_view[leg] = {
                    **pos,
                    "live_ltp": ltp,
                    "live_pnl": leg_pnl
                }

            atm_val = int(round(spot_val / 50.0) * 50) if spot_val > 0 else 0

            return {
                "now_str": now_str,
                "mode": st.get("mode", "ACTIVE"),
                "paper_mode": False,
                "spot": spot_val,
                "atm": atm_val,
                "adx": 22.0,
                "kama": spot_val,
                "regime": "LIVE",
                "trend": 0,
                "atr": 35.0,
                "dte": 2.0,
                "realized_pnl": float(st.get("realized_pnl", 0.0)),
                "unrealized_pnl": unrealized,
                "positions": positions_view,
                "cooldown_tracker": st.get("cooldown_tracker", {}),
                "last_event": "Direct broker stream active"
            }
        except Exception:
            pass

    return None

def render_rich(snap):
    if not snap:
        return Panel(Text("Waiting for bot data... (Checking data/state/live_snapshot_v2.json)", style="bold yellow"), title="V2 PRO LIVE DASHBOARD")

    header = Text(
        f" V2 PRO ALGO — {snap.get('now_str', '')}  |  Mode: {snap.get('mode', '-')}  |  "
        f"{'LIVE' if not snap.get('paper_mode', False) else 'PAPER'} TRADING ",
        style="bold white on blue"
    )

    top = Table.grid(expand=True)
    top.add_column(justify="left")
    top.add_column(justify="left")
    top.add_column(justify="left")
    top.add_column(justify="left")
    
    spot_str = f"{snap.get('spot', 0):.2f}" if snap.get('spot', 0) > 0 else "Fetching..."
    atm_str = str(snap.get('atm', 0)) if snap.get('atm', 0) > 0 else "Fetching..."
    
    trend_val = snap.get('trend', 0)
    if trend_val == 1:
        trend_str = "[bold green]BULLISH (+1)[/bold green]"
    elif trend_val == -1:
        trend_str = "[bold red]BEARISH (-1)[/bold red]"
    else:
        trend_str = "[yellow]FLAT (0)[/yellow]"

    kama_val = snap.get('kama')
    kama_str = f"₹{kama_val:.2f}" if (kama_val and kama_val > 0) else "Calculating..."

    top.add_row(
        f"[bold]Spot:[/bold] {spot_str}",
        f"[bold]ATM:[/bold] {atm_str}",
        f"[bold]ADX(9):[/bold] {snap.get('adx', 0):.1f} ({snap.get('regime', '-')})",
        f"[bold]KAMA Trend:[/bold] {trend_str}",
    )
    
    r_pnl = snap.get('realized_pnl', 0.0)
    u_pnl = snap.get('unrealized_pnl', 0.0)
    r_str = f"[green]₹{r_pnl:,.2f}[/green]" if r_pnl >= 0 else f"[red]₹{r_pnl:,.2f}[/red]"
    u_str = f"[green]₹{u_pnl:,.2f}[/green]" if u_pnl >= 0 else f"[red]₹{u_pnl:,.2f}[/red]"
    tot_pnl = r_pnl + u_pnl
    tot_str = f"[bold green]₹{tot_pnl:,.2f}[/bold green]" if tot_pnl >= 0 else f"[bold red]₹{tot_pnl:,.2f}[/bold red]"

    top.add_row(
        f"[bold]KAMA(1m):[/bold] {kama_str}",
        f"[bold]ATR(5m):[/bold] {snap.get('atr', 0):.2f}",
        f"[bold]Realized P&L:[/bold] {r_str}",
        f"[bold]Total MTM:[/bold] {tot_str}",
    )

    pos_table = Table(title="Open Positions (with Spot TSL)", expand=True, show_lines=True)
    pos_table.add_column("Leg", style="bold")
    pos_table.add_column("Strike")
    pos_table.add_column("Side")
    pos_table.add_column("Qty")
    pos_table.add_column("Entry ₹")
    pos_table.add_column("LTP ₹")
    pos_table.add_column("P&L ₹")
    pos_table.add_column("Spot SL Line")
    pos_table.add_column("Breaches")

    positions = snap.get("positions", {})
    if positions:
        for leg, pos in positions.items():
            pnl = pos.get("live_pnl", 0.0)
            pnl_str = f"[green]₹{pnl:,.2f}[/green]" if pnl >= 0 else f"[red]₹{pnl:,.2f}[/red]"
            sl_state = pos.get("spot_sl_state") or {}
            sl_line = f"{sl_state.get('current_sl', '-')}" if sl_state else "-"
            breach = f"{sl_state.get('breach_count', 0)}" if sl_state else "-"
            pos_table.add_row(
                leg,
                str(pos.get("strike", "-")),
                pos.get("side", "-"),
                str(pos.get("qty", "-")),
                f"{pos.get('entry_price', 0):.2f}",
                f"{pos.get('live_ltp', 0):.2f}",
                pnl_str,
                str(sl_line),
                str(breach)
            )
    else:
        pos_table.add_row("No Open Positions", "-", "-", "-", "-", "-", "-", "-", "-")

    cd_table = Table(title="Anti-Whipsaw Protection (Consecutive Tick Engine)", expand=True, show_lines=False)
    cd_table.add_column("Leg", style="bold")
    cd_table.add_column("Status")
    cd_table.add_column("Consecutive Safe Ticks")
    cd_table.add_column("Stopped Spot")
    for leg, cd in snap.get("cooldown_tracker", {}).items():
        active = cd.get("active", False)
        safe_t = cd.get("safe_ticks", 0)
        status_txt = "[yellow]COOLING DOWN[/yellow]" if active else "[green]Active / Ready[/green]"
        progress_txt = f"[bold cyan]{safe_t}/10 Ticks[/bold cyan]" if active else "—"
        stopped_txt = f"₹{cd.get('stopped_spot', 0):.2f}" if active else "—"
        cd_table.add_row(leg, status_txt, progress_txt, stopped_txt)

    footer = Text(f" [Updated at {snap.get('now_str')} | Auto-refresh 3s | Press Ctrl+C to close dashboard] ", style="dim")

    layout = Table.grid(expand=True)
    layout.add_row(Panel(header, style="on blue"))
    layout.add_row(Panel(top, title="Market State & Indicators"))
    layout.add_row(pos_table)
    layout.add_row(cd_table)
    layout.add_row(footer)
    return layout

def main():
    if HAS_RICH:
        console = Console()
        with Live(render_rich(load_data()), console=console, refresh_per_second=1, screen=True) as live:
            try:
                while True:
                    data = load_data()
                    live.update(render_rich(data))
                    time.sleep(3.0)
            except KeyboardInterrupt:
                pass
    else:
        try:
            while True:
                os.system('clear' if os.name == 'posix' else 'cls')
                snap = load_data()
                if snap:
                    print(f"=== V2 PRO DASHBOARD [{snap.get('now_str')}] ===")
                    print(f"Spot: {snap.get('spot')} | KAMA: {snap.get('kama')} | ADX: {snap.get('adx')} | Trend: {snap.get('trend')}")
                    print(f"Realized: ₹{snap.get('realized_pnl',0):,.2f} | Unrealized: ₹{snap.get('unrealized_pnl',0):,.2f}")
                    print("-" * 65)
                    for leg, p in snap.get('positions', {}).items():
                        sl_line = (p.get('spot_sl_state') or {}).get('current_sl', '-')
                        print(f" {leg:<10} Strike {p.get('strike'):<6} Entry ₹{p.get('entry_price'):<6.2f} LTP ₹{p.get('live_ltp'):<6.2f} PnL ₹{p.get('live_pnl',0):<8.2f} SL {sl_line}")
                time.sleep(3.0)
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
