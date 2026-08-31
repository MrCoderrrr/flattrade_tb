#!/usr/bin/env python3
import os
import sys
import time
import json
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SNAP_FILE = os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2.json")
STATE_FILE = os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2.json")

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

def load_data():
    if os.path.exists(SNAP_FILE):
        try:
            with open(SNAP_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
                return {
                    "now_str": datetime.now().strftime("%H:%M:%S"),
                    "mode": st.get("mode", "-"),
                    "paper_mode": False,
                    "spot": 0.0,
                    "atm": 0,
                    "adx": 0.0,
                    "regime": "-",
                    "trend": 0,
                    "atr": 0.0,
                    "dte": 0.0,
                    "realized_pnl": st.get("realized_pnl", 0.0),
                    "unrealized_pnl": 0.0,
                    "positions": st.get("positions", {}),
                    "cooldown_tracker": st.get("cooldown_tracker", {}),
                    "last_event": "Loaded from algo_state_v2.json"
                }
        except Exception:
            pass
    return None

def render_rich(snap):
    if not snap:
        return Panel(Text("Waiting for bot snapshot data...", style="bold yellow"), title="V2 PRO LIVE DASHBOARD")

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
    top.add_row(
        f"[bold]Spot:[/bold] {snap.get('spot', 0):.2f}",
        f"[bold]ATM:[/bold] {snap.get('atm', 0)}",
        f"[bold]ADX:[/bold] {snap.get('adx', 0):.1f} ({snap.get('regime', '-')})",
        f"[bold]KAMA Trend:[/bold] {snap.get('trend', 0)}",
    )
    r_pnl = snap.get('realized_pnl', 0.0)
    u_pnl = snap.get('unrealized_pnl', 0.0)
    r_str = f"[green]₹{r_pnl:,.2f}[/green]" if r_pnl >= 0 else f"[red]₹{r_pnl:,.2f}[/red]"
    u_str = f"[green]₹{u_pnl:,.2f}[/green]" if u_pnl >= 0 else f"[red]₹{u_pnl:,.2f}[/red]"
    tot_pnl = r_pnl + u_pnl
    tot_str = f"[bold green]₹{tot_pnl:,.2f}[/bold green]" if tot_pnl >= 0 else f"[bold red]₹{tot_pnl:,.2f}[/bold red]"

    top.add_row(
        f"[bold]ATR(5m):[/bold] {snap.get('atr', 0):.2f}",
        f"[bold]Total MTM:[/bold] {tot_str}",
        f"[bold]Realized P&L:[/bold] {r_str}",
        f"[bold]Unrealized P&L:[/bold] {u_str}",
    )

    pos_table = Table(title="Open Positions", expand=True, show_lines=True)
    pos_table.add_column("Leg", style="bold")
    pos_table.add_column("Strike")
    pos_table.add_column("Side")
    pos_table.add_column("Qty")
    pos_table.add_column("Entry ₹")
    pos_table.add_column("LTP ₹")
    pos_table.add_column("P&L ₹")
    pos_table.add_column("Spot SL Line")
    pos_table.add_column("Breach Cnt")

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
        pos_table.add_row("-", "-", "-", "-", "-", "-", "-", "-", "-")

    cd_table = Table(title="Cooldowns (Anti-Whipsaw)", expand=True, show_lines=False)
    cd_table.add_column("Leg")
    cd_table.add_column("Active")
    cd_table.add_column("Elapsed (s)")
    for leg, cd in snap.get("cooldown_tracker", {}).items():
        active = cd.get("active", False)
        elapsed = (time.time() - cd["stopped_time"]) if active and cd.get("stopped_time") else 0
        cd_table.add_row(leg, "[yellow]ACTIVE[/yellow]" if active else "inactive", f"{elapsed:.0f}s" if active else "-")

    footer = Text(f" [Live Stream - Press Ctrl+C to exit viewer | Bot keeps running in background] ", style="dim")

    layout = Table.grid(expand=True)
    layout.add_row(Panel(header, style="on blue"))
    layout.add_row(Panel(top, title="Market State & Indicators"))
    layout.add_row(pos_table)
    layout.add_row(cd_table)
    layout.add_row(footer)
    return layout

def render_plain(snap):
    os.system('clear' if os.name == 'posix' else 'cls')
    if not snap:
        print("Waiting for bot snapshot data...")
        return
    print("=" * 70)
    print(f" V2 PRO ALGO — {snap.get('now_str', '')} | Mode: {snap.get('mode', '-')} | {'LIVE' if not snap.get('paper_mode', False) else 'PAPER'} TRADING")
    print("=" * 70)
    print(f"Spot: {snap.get('spot', 0):.2f} | ATM: {snap.get('atm', 0)} | ADX: {snap.get('adx', 0):.1f} ({snap.get('regime', '-')}) | Trend: {snap.get('trend', 0)} | ATR: {snap.get('atr', 0):.2f}")
    r_pnl = snap.get('realized_pnl', 0.0)
    u_pnl = snap.get('unrealized_pnl', 0.0)
    print(f"Realized P&L: ₹{r_pnl:,.2f} | Unrealized: ₹{u_pnl:,.2f} | Total MTM: ₹{r_pnl+u_pnl:,.2f}")
    print("-" * 70)
    print(f"{'LEG':<10} {'STRIKE':<8} {'SIDE':<6} {'ENTRY':<10} {'LTP':<10} {'P&L':<12} {'SL LINE':<10}")
    print("-" * 70)
    for leg, pos in snap.get("positions", {}).items():
        sl_state = pos.get("spot_sl_state") or {}
        sl_line = sl_state.get('current_sl', '-')
        print(f"{leg:<10} {str(pos.get('strike')):<8} {pos.get('side'):<6} {pos.get('entry_price',0):<10.2f} {pos.get('live_ltp',0):<10.2f} {pos.get('live_pnl',0):<12.2f} {str(sl_line):<10}")
    print("=" * 70)
    print("[Press Ctrl+C to close dashboard — Bot will keep running in background]")

def main():
    if HAS_RICH:
        console = Console()
        with Live(render_rich(load_data()), console=console, refresh_per_second=2, screen=True) as live:
            try:
                while True:
                    data = load_data()
                    live.update(render_rich(data))
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    else:
        try:
            while True:
                render_plain(load_data())
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
