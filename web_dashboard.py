#!/usr/bin/env python3
"""
================================================================================
🌐 REAL-TIME FINANCIAL WEB DASHBOARD & CLOUDFLARE PUBLIC TUNNEL
================================================================================
Provides a public, mobile-responsive, real-time trading dashboard for NIFTY 50
and MCX Natural Gas algorithmic paper engines.

Features:
- Single-page application with dark terminal aesthetic (glassmorphism UI)
- Server-Sent Events (SSE) live streaming every 1 second (zero lag)
- Auto-starts and manages Cloudflare Quick Tunnel (public HTTPS URL)
- Displays Live MTM PnL (% calculated on ₹2,00,000 base), Capital, MTD/YTD
- Live Spot, ATM, ADX, KAMA, Streaming EMA Momentum indicators
- Real-time Positions, Stop Loss / Trailing SL status, and Trade Book
================================================================================
"""

import os
import sys
import time
import json
import re
import socket
import select
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

IST = timezone(timedelta(hours=5, minutes=30))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = CURRENT_DIR

def get_ist_now() -> datetime:
    return datetime.now(IST)

PUBLIC_URL_FILE = os.path.join(PROJECT_ROOT, "public_url.txt")
LIVE_PUBLIC_URL = ""

# ─────────────────────────────────────────────────────────────────────────────
# DATA INGESTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def load_json_safe(filepath: str, default=None):
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def get_pnl_tracker_data():
    candidates = [
        os.path.join(PROJECT_ROOT, "pnl_tracker.json"),
        os.path.join(PROJECT_ROOT, "data", "logs", "pnl_tracker.json"),
        os.path.join(PROJECT_ROOT, "data", "state", "pnl_tracker.json")
    ]
    for p in candidates:
        d = load_json_safe(p)
        if d:
            return d
    return {
        "current_capital": 200000.0,
        "today_pnl": 0.0,
        "mtd_pnl": 0.0,
        "ytd_pnl": 0.0,
        "daily_pnl": {}
    }

def get_nifty_snapshot():
    candidates = [
        os.path.join(PROJECT_ROOT, "data", "state", "live_snapshot_v2_paper.json"),
        os.path.join(PROJECT_ROOT, "data", "state", "algo_state_v2_paper.json")
    ]
    for p in candidates:
        d = load_json_safe(p)
        if d:
            return d
    return None

def get_mcx_snapshot():
    candidates = [
        os.path.join(PROJECT_ROOT, "live_snapshot_mcx_paper.json"),
        os.path.join(PROJECT_ROOT, "mcx_state_paper_v5.json")
    ]
    for p in candidates:
        d = load_json_safe(p)
        if d:
            return d
    return None

def check_process_running(pattern: str) -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True).strip()
        pids = [int(p) for p in out.splitlines() if int(p) != os.getpid()]
        return len(pids) > 0
    except Exception:
        return False

def get_aggregated_dashboard_state() -> dict:
    now_ist = get_ist_now()
    pnl_data = get_pnl_tracker_data()
    nifty_snap = get_nifty_snapshot() or {}
    mcx_snap = get_mcx_snapshot() or {}

    # Check active engines
    scheduler_running = check_process_running("daily_scheduler.py")
    nifty_running = check_process_running("nifty_paper_v3.py") or check_process_running("upv2_paper.py")
    mcx_running = check_process_running("mcx_paper_v5.py")

    # Determine Active Session
    hhmm = now_ist.strftime("%H:%M")
    is_weekday = now_ist.weekday() < 5
    nifty_session_active = is_weekday and "09:15" <= hhmm < "15:35"
    mcx_session_active = is_weekday and "15:30" <= hhmm < "23:25"

    # Aggregated PnL Calculation
    nifty_realized = float(nifty_snap.get("realized_pnl", 0.0) or 0.0)
    nifty_unrealized = float(nifty_snap.get("unrealized_pnl", 0.0) or 0.0)
    nifty_net = nifty_realized + nifty_unrealized

    mcx_realized = float(mcx_snap.get("realized_pnl", mcx_snap.get("total_realized_pnl", 0.0)) or 0.0)
    mcx_unrealized = float(mcx_snap.get("unrealized_pnl", 0.0) or 0.0)
    mcx_net = float(mcx_snap.get("net_pnl", mcx_realized + mcx_unrealized) or 0.0)

    # Combined today net MTM
    combined_realized = nifty_realized + mcx_realized
    combined_unrealized = nifty_unrealized + mcx_unrealized
    combined_net = combined_realized + combined_unrealized
    combined_net_pct = (combined_net / 200000.0) * 100.0

    # Capital metrics
    base_capital = 200000.0
    live_capital = float(pnl_data.get("current_capital", 200000.0) or 200000.0) + combined_unrealized
    mtd_pnl = float(pnl_data.get("mtd_pnl", 0.0) or 0.0)
    ytd_pnl = float(pnl_data.get("ytd_pnl", 0.0) or 0.0)
    circuit_limit = round(-live_capital * 0.018, 2)

    # Trades count
    nifty_trades = int(nifty_snap.get("trades_today", 0) or 0)
    mcx_trades = int(mcx_snap.get("trades_today", 0) or 0)
    total_trades = nifty_trades + mcx_trades

    # Active positions normalization
    nifty_positions = []
    raw_nifty_pos = nifty_snap.get("positions", {})
    if isinstance(raw_nifty_pos, dict):
        for leg, pos in raw_nifty_pos.items():
            entry = float(pos.get("entry_price", 0.0) or 0.0)
            ltp = float(pos.get("ltp", pos.get("_last_ltp", entry)) or entry)
            strike = pos.get("strike", 0)
            side = pos.get("side", "SELL")
            qty = pos.get("qty", 65)
            pnl = float(pos.get("pnl", ((entry - ltp) if side == "SELL" else (ltp - entry)) * qty) or 0.0)
            sl_state = pos.get("dual_sl_state", {})
            current_sl = float(sl_state.get("current_premium_sl", 0.0) or 0.0)
            is_solo = bool(sl_state.get("solo_mode", False))

            nifty_positions.append({
                "market": "NIFTY",
                "leg": leg,
                "display_leg": f"{leg}*" if is_solo else (f"{leg[:2]}(H)" if "HEDGE" in leg else leg),
                "strike": strike,
                "side": side,
                "qty": qty,
                "entry": entry,
                "ltp": ltp,
                "sl": current_sl,
                "pnl": pnl,
                "tsym": pos.get("tsym", f"NIFTY {strike} {leg[:2]}"),
                "is_solo": is_solo
            })

    mcx_positions = []
    raw_mcx_pos = mcx_snap.get("positions", [])
    if isinstance(raw_mcx_pos, list):
        for pos in raw_mcx_pos:
            mcx_positions.append({
                "market": "MCX",
                "leg": pos.get("leg", "CE"),
                "display_leg": f"{pos.get('leg')}*" if pos.get("solo_mode") else pos.get("leg"),
                "strike": pos.get("strike", 0),
                "side": pos.get("side", "SELL"),
                "qty": pos.get("qty", 1250),
                "entry": float(pos.get("entry", 0.0) or 0.0),
                "ltp": float(pos.get("ltp", 0.0) or 0.0),
                "sl": float(pos.get("sl", 0.0) or 0.0),
                "pnl": float(pos.get("pnl", 0.0) or 0.0),
                "tsym": pos.get("tsym", f"NATGAS {pos.get('strike')} {pos.get('leg')}"),
                "is_solo": bool(pos.get("solo_mode", False))
            })
    elif isinstance(raw_mcx_pos, dict):
        for leg, pos in raw_mcx_pos.items():
            entry = float(pos.get("entry_price", 0.0) or 0.0)
            ltp = float(pos.get("_last_ltp", entry) or entry)
            sl_state = pos.get("sl_state", {})
            mcx_positions.append({
                "market": "MCX",
                "leg": leg,
                "display_leg": f"{leg}*" if sl_state.get("solo_mode") else leg,
                "strike": pos.get("strike", 0),
                "side": pos.get("side", "SELL"),
                "qty": pos.get("qty", 1250),
                "entry": entry,
                "ltp": ltp,
                "sl": float(sl_state.get("current_sl", 0.0) or 0.0),
                "pnl": float(((entry - ltp) if pos.get("side") == "SELL" else (ltp - entry)) * pos.get("qty", 1250)),
                "tsym": pos.get("tsym", leg),
                "is_solo": bool(sl_state.get("solo_mode", False))
            })

    # Combined trade log
    trade_history = []
    nifty_trades_log = nifty_snap.get("trade_log", [])
    if isinstance(nifty_trades_log, list):
        for t in nifty_trades_log:
            trade_history.append({
                "market": "NIFTY",
                "time": t.get("time", ""),
                "leg": t.get("leg", ""),
                "strike": t.get("strike", ""),
                "entry": t.get("entry", 0.0),
                "exit": t.get("exit", 0.0),
                "pnl": t.get("pnl", 0.0),
                "reason": t.get("reason", "")
            })

    mcx_trades_log = mcx_snap.get("trade_log", [])
    if isinstance(mcx_trades_log, list):
        for t in mcx_trades_log:
            trade_history.append({
                "market": "MCX",
                "time": t.get("time", ""),
                "leg": t.get("leg", ""),
                "strike": t.get("strike", ""),
                "entry": t.get("entry", 0.0),
                "exit": t.get("exit", 0.0),
                "pnl": t.get("pnl", 0.0),
                "reason": t.get("reason", "")
            })

    trade_history.sort(key=lambda x: str(x.get("time", "")), reverse=True)

    # NIFTY indicators
    nifty_ind = nifty_snap.get("indicators", {}) or {}
    kama_val = nifty_ind.get("kama")
    kama_str = f"{kama_val:.1f}" if kama_val is not None else "WARMUP"
    trend_val = nifty_ind.get("trend", 0)
    trend_label = "BULLISH ▲" if trend_val == 1 else ("BEARISH ▼" if trend_val == -1 else "FLAT ━")
    regime = nifty_ind.get("regime", "CHOP")
    adx_val = nifty_ind.get("adx", 18.0) or 18.0
    atr_val = nifty_ind.get("atr", 35.0) or 35.0

    nifty_ema_sig = nifty_ind.get("confirmed_signal", 0)
    nifty_ema_sig_str = "BULLISH ▲" if nifty_ema_sig > 0 else ("BEARISH ▼" if nifty_ema_sig < 0 else "FLAT ━")

    # MCX indicators
    mcx_ema = mcx_snap.get("ema", {}) or {}
    mcx_sig = mcx_ema.get("confirmed_signal", 0)
    mcx_sig_str = "BULLISH ▲" if mcx_sig > 0 else ("BEARISH ▼" if mcx_sig < 0 else "FLAT ━")

    return {
        "status": "success",
        "timestamp_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "time_str": now_ist.strftime("%H:%M:%S"),
        "public_url": LIVE_PUBLIC_URL,
        "system": {
            "scheduler_running": scheduler_running,
            "nifty_running": nifty_running,
            "mcx_running": mcx_running,
            "nifty_session_active": nifty_session_active,
            "mcx_session_active": mcx_session_active,
        },
        "performance": {
            "combined_net_mtm": combined_net,
            "combined_net_pct": combined_net_pct,
            "combined_realized": combined_realized,
            "combined_unrealized": combined_unrealized,
            "current_capital": live_capital,
            "base_capital": base_capital,
            "circuit_limit": circuit_limit,
            "mtd_pnl": mtd_pnl,
            "ytd_pnl": ytd_pnl,
            "total_trades": total_trades,
            "daily_history": pnl_data.get("daily_pnl", {})
        },
        "nifty": {
            "active": nifty_running,
            "mode": nifty_snap.get("mode", "STANDBY"),
            "spot": float(nifty_snap.get("spot", 0.0) or 0.0),
            "atm": int(nifty_snap.get("atm", 0) or 0),
            "realized_pnl": nifty_realized,
            "unrealized_pnl": nifty_unrealized,
            "net_pnl": nifty_net,
            "net_pct": (nifty_net / 200000.0) * 100.0,
            "trades_today": nifty_trades,
            "regime": regime,
            "adx": adx_val,
            "kama": kama_str,
            "trend": trend_label,
            "atr": atr_val,
            "ema15": nifty_ind.get("ema_15", 0.0),
            "ema90": nifty_ind.get("ema_90", 0.0),
            "ema300": nifty_ind.get("ema_300", 0.0),
            "slow_slope": nifty_ind.get("slow_slope", 0.0),
            "vr": nifty_ind.get("vr", 1.0),
            "signal": nifty_ema_sig_str,
            "hold_time": nifty_ind.get("hold_time", 0.0),
            "positions": nifty_positions
        },
        "mcx": {
            "active": mcx_running,
            "spot": float(mcx_snap.get("spot", 0.0) or 0.0),
            "atm": int(mcx_snap.get("atm", 0) or 0),
            "expiry": mcx_snap.get("expiry", "Front Month"),
            "is_rolled_over": bool(mcx_snap.get("is_rolled_over", False)),
            "realized_pnl": mcx_realized,
            "unrealized_pnl": mcx_unrealized,
            "net_pnl": mcx_net,
            "net_pct": (mcx_net / 200000.0) * 100.0,
            "trades_today": mcx_trades,
            "reversal_latched": bool(mcx_snap.get("reversal_latched", False)),
            "cooldown_remaining": mcx_snap.get("cooldown_remaining", 0),
            "ema15": mcx_ema.get("ema_15", 0.0),
            "ema90": mcx_ema.get("ema_90", 0.0),
            "ema300": mcx_ema.get("ema_300", 0.0),
            "slow_slope": mcx_ema.get("slow_slope", 0.0),
            "vr": mcx_ema.get("vr", 1.0),
            "signal": mcx_sig_str,
            "hold_time": mcx_ema.get("hold_time", 0.0),
            "positions": mcx_positions
        },
        "positions": nifty_positions + mcx_positions,
        "recent_trades": trade_history[:25]
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLOUDFLARE QUICK TUNNEL MANAGER
# ─────────────────────────────────────────────────────────────────────────────
def run_tunnel_manager(port: int = 8000):
    global LIVE_PUBLIC_URL
    while True:
        try:
            print(f"[TUNNEL] Launching Cloudflare Tunnel for 127.0.0.1:{port}...", flush=True)
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            for line in iter(proc.stdout.readline, ""):
                # Search for trycloudflare URL
                match = re.search(r"https://[-a-zA-Z0-9.]*trycloudflare\.com", line)
                if match:
                    url = match.group(0)
                    LIVE_PUBLIC_URL = url
                    print(f"\n=======================================================", flush=True)
                    print(f"🚀 LIVE PUBLIC DASHBOARD LINK ESTABLISHED:", flush=True)
                    print(f"👉 {url}", flush=True)
                    print(f"=======================================================\n", flush=True)
                    try:
                        with open(PUBLIC_URL_FILE, "w") as f:
                            f.write(f"{url}\n")
                    except Exception:
                        pass
            proc.wait()
        except Exception as e:
            print(f"[TUNNEL ERROR] {e}", flush=True)
        time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# EMBEDDED FRONTEND HTML / CSS / JAVASCRIPT
# ─────────────────────────────────────────────────────────────────────────────
HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Flattrade Quantitative Trading Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #070a12;
      --card-bg: rgba(15, 23, 42, 0.72);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-hover: rgba(255, 255, 255, 0.12);
      --primary: #38bdf8;
      --green: #10b981;
      --green-glow: rgba(16, 185, 129, 0.25);
      --red: #f43f5e;
      --red-glow: rgba(244, 63, 94, 0.25);
      --amber: #f59e0b;
      --purple: #a855f7;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --mono: 'JetBrains Mono', monospace;
      --sans: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

    body {
      background-color: var(--bg);
      background-image: 
        radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.08) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(168, 85, 247, 0.06) 0px, transparent 50%),
        radial-gradient(at 50% 50%, rgba(16, 185, 129, 0.04) 0px, transparent 60%);
      background-attachment: fixed;
      color: var(--text);
      font-family: var(--sans);
      min-height: 100vh;
      line-height: 1.5;
      padding-bottom: 60px;
    }

    .container {
      max-width: 1440px;
      margin: 0 auto;
      padding: 16px 20px;
    }

    /* Top Navigation Bar */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      padding: 14px 20px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      backdrop-filter: blur(20px);
      margin-bottom: 20px;
      box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5);
    }

    .logo-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .logo-badge {
      width: 42px;
      height: 42px;
      background: linear-gradient(135deg, #38bdf8, #6366f1);
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      box-shadow: 0 4px 15px rgba(56, 189, 248, 0.4);
    }

    .logo-title {
      font-weight: 800;
      font-size: 1.15rem;
      letter-spacing: -0.02em;
      background: linear-gradient(to right, #fff, #cbd5e1);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .logo-sub {
      font-size: 0.75rem;
      color: var(--text-dim);
      font-family: var(--mono);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .live-pulse {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      border-radius: 9999px;
      color: var(--green);
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 600;
    }

    .pulse-dot {
      width: 8px;
      height: 8px;
      background: var(--green);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--green);
      animation: pulse 1.8s infinite;
    }

    @keyframes pulse {
      0% { transform: scale(0.9); opacity: 0.8; }
      50% { transform: scale(1.3); opacity: 1; box-shadow: 0 0 14px var(--green); }
      100% { transform: scale(0.9); opacity: 0.8; }
    }

    .clock-pill {
      font-family: var(--mono);
      font-size: 0.85rem;
      font-weight: 600;
      padding: 6px 14px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      color: #e2e8f0;
    }

    .btn-share {
      background: rgba(56, 189, 248, 0.15);
      color: var(--primary);
      border: 1px solid rgba(56, 189, 248, 0.3);
      padding: 7px 14px;
      border-radius: 10px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .btn-share:hover {
      background: rgba(56, 189, 248, 0.25);
      transform: translateY(-1px);
    }

    /* Hero Key Stats Grid */
    .hero-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }

    .metric-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 18px 20px;
      backdrop-filter: blur(16px);
      position: relative;
      overflow: hidden;
      transition: transform 0.2s ease, border-color 0.2s ease;
    }

    .metric-card:hover {
      border-color: var(--card-hover);
      transform: translateY(-2px);
    }

    .metric-label {
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    .metric-value {
      font-family: var(--mono);
      font-size: 1.8rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      display: flex;
      align-items: baseline;
      gap: 8px;
    }

    .metric-sub {
      font-size: 0.78rem;
      color: var(--text-dim);
      margin-top: 6px;
      display: flex;
      align-items: center;
      gap: 6px;
      font-family: var(--mono);
    }

    .positive { color: var(--green); text-shadow: 0 0 20px var(--green-glow); }
    .negative { color: var(--red); text-shadow: 0 0 20px var(--red-glow); }
    .neutral  { color: #e2e8f0; }

    .pct-tag {
      font-size: 0.85rem;
      padding: 2px 8px;
      border-radius: 6px;
      font-weight: 600;
      font-family: var(--mono);
    }
    .pct-tag.pos { background: rgba(16, 185, 129, 0.15); color: var(--green); }
    .pct-tag.neg { background: rgba(244, 63, 94, 0.15); color: var(--red); }

    /* Session Badges Row */
    .session-banner {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 10px 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 20px;
      font-size: 0.85rem;
    }

    .session-pills {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .status-chip {
      padding: 4px 10px;
      border-radius: 8px;
      font-size: 0.75rem;
      font-weight: 700;
      font-family: var(--mono);
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .chip-active { background: rgba(16, 185, 129, 0.2); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.4); }
    .chip-standby { background: rgba(245, 158, 11, 0.15); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.3); }
    .chip-idle { background: rgba(148, 163, 184, 0.15); color: var(--text-dim); border: 1px solid rgba(148, 163, 184, 0.2); }

    /* Section Tabs */
    .nav-tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 12px;
      margin-bottom: 20px;
      overflow-x: auto;
    }

    .tab-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      padding: 8px 18px;
      border-radius: 10px;
      font-weight: 600;
      font-size: 0.88rem;
      cursor: pointer;
      transition: all 0.2s;
      white-space: nowrap;
    }

    .tab-btn:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.05);
    }

    .tab-btn.active {
      color: #fff;
      background: rgba(56, 189, 248, 0.15);
      border-color: rgba(56, 189, 248, 0.3);
    }

    /* Strategy Dual Cards Layout */
    .engine-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 24px;
    }

    @media (max-width: 980px) {
      .engine-grid { grid-template-columns: 1fr; }
    }

    .engine-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 18px;
      padding: 20px;
      backdrop-filter: blur(16px);
      box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5);
    }

    .engine-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--card-border);
    }

    .engine-title {
      font-size: 1.05rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .engine-ind-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }

    @media (max-width: 600px) {
      .engine-ind-grid { grid-template-columns: repeat(2, 1fr); }
    }

    .ind-box {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 10px;
      padding: 8px 12px;
    }

    .ind-name {
      font-size: 0.68rem;
      color: var(--text-dim);
      font-weight: 600;
      text-transform: uppercase;
      font-family: var(--mono);
    }

    .ind-val {
      font-family: var(--mono);
      font-size: 0.95rem;
      font-weight: 700;
      margin-top: 2px;
    }

    /* Position Tables */
    .table-container {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      overflow-x: auto;
      margin-bottom: 24px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }

    th {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text-muted);
      font-weight: 600;
      padding: 12px 16px;
      text-transform: uppercase;
      font-size: 0.72rem;
      letter-spacing: 0.05em;
      border-bottom: 1px solid var(--card-border);
      font-family: var(--mono);
    }

    td {
      padding: 12px 16px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      font-family: var(--mono);
    }

    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }

    .badge-leg {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 700;
    }
    .badge-ce { background: rgba(56, 189, 248, 0.15); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.3); }
    .badge-pe { background: rgba(245, 158, 11, 0.15); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.3); }
    .badge-hedge { background: rgba(168, 85, 247, 0.15); color: var(--purple); border: 1px solid rgba(168, 85, 247, 0.3); }

    .side-sell { color: var(--red); font-weight: 700; }
    .side-buy  { color: var(--green); font-weight: 700; }

    /* Performance Calendar Cards */
    .history-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }

    .history-card {
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px 14px;
      font-family: var(--mono);
    }

    .hist-date {
      font-size: 0.72rem;
      color: var(--text-dim);
      margin-bottom: 4px;
    }

    .hist-pnl {
      font-size: 1.1rem;
      font-weight: 700;
    }

    /* Toast Notification */
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid var(--primary);
      color: #fff;
      padding: 12px 20px;
      border-radius: 12px;
      font-size: 0.85rem;
      font-weight: 600;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      display: none;
      z-index: 1000;
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header>
      <div class="logo-group">
        <div class="logo-badge">⚡</div>
        <div>
          <div class="logo-title">FLATTRADE QUANT TERMINAL</div>
          <div class="logo-sub">
            <span>PRECISION EXECUTION ENGINE</span>
            <span>•</span>
            <span id="market-mode-badge">PAPER TRADING</span>
          </div>
        </div>
      </div>
      <div class="header-actions">
        <div class="live-pulse">
          <span class="pulse-dot"></span>
          <span id="stream-status">LIVE STREAMING</span>
        </div>
        <div class="clock-pill" id="live-clock">--:--:-- IST</div>
        <button class="btn-share" onclick="copyPublicLink()">
          <span>🔗</span> <span id="copy-btn-text">Share Link</span>
        </button>
      </div>
    </header>

    <!-- Session Status Bar -->
    <div class="session-banner">
      <div class="session-pills">
        <span style="font-size:0.75rem; color:var(--text-dim); font-weight:600;">ACTIVE SERVICES:</span>
        <span class="status-chip chip-active" id="chip-scheduler">SCHEDULER: ON</span>
        <span class="status-chip" id="chip-nifty">NIFTY (09:15-15:35)</span>
        <span class="status-chip" id="chip-mcx">MCX (15:30-23:25)</span>
      </div>
      <div style="font-family:var(--mono); font-size:0.8rem; color:var(--text-muted);" id="session-window-text">
        Loading session parameters...
      </div>
    </div>

    <!-- Hero Metrics Grid -->
    <div class="hero-grid">
      <!-- Net MTM Card -->
      <div class="metric-card">
        <div class="metric-label">
          <span>Net MTM P&L</span>
          <span class="pct-tag" id="net-mtm-pct">+0.00%</span>
        </div>
        <div class="metric-value" id="net-mtm-val">₹0.00</div>
        <div class="metric-sub">
          <span>Base Capital: <b>₹2,00,000</b></span>
        </div>
      </div>

      <!-- Realized vs Unrealized Card -->
      <div class="metric-card">
        <div class="metric-label">
          <span>Realized Booked</span>
          <span id="realized-tag" style="font-family:var(--mono); font-size:0.75rem;">₹0.00</span>
        </div>
        <div class="metric-value" id="realized-val">₹0.00</div>
        <div class="metric-sub">
          <span>Floating MTM: <b id="unrealized-val">₹0.00</b></span>
        </div>
      </div>

      <!-- Current Account Capital -->
      <div class="metric-card">
        <div class="metric-label">
          <span>Live Capital</span>
          <span style="color:var(--text-dim); font-size:0.75rem; font-family:var(--mono);">Max Drawdown</span>
        </div>
        <div class="metric-value" id="capital-val">₹2,00,000.00</div>
        <div class="metric-sub">
          <span>Circuit Break: <b id="circuit-val" style="color:var(--red);">-₹3,600</b></span>
        </div>
      </div>

      <!-- MTD & YTD Returns -->
      <div class="metric-card">
        <div class="metric-label">
          <span>MTD Performance</span>
          <span id="trades-count-pill" style="font-family:var(--mono); color:var(--primary); font-size:0.75rem;">0 TRADES</span>
        </div>
        <div class="metric-value" id="mtd-val">₹0.00</div>
        <div class="metric-sub">
          <span>Year-to-Date: <b id="ytd-val">₹0.00</b></span>
        </div>
      </div>
    </div>

    <!-- Dual Strategy Engine Cards -->
    <div class="engine-grid">
      <!-- NIFTY Engine -->
      <div class="engine-card">
        <div class="engine-header">
          <div class="engine-title">
            <span style="color:var(--primary);">●</span> NIFTY 50 Options
            <span style="font-size:0.75rem; font-family:var(--mono); color:var(--text-dim); font-weight:400;">(Adaptive KAMA-ADX)</span>
          </div>
          <div id="nifty-status-badge" class="status-chip chip-standby">STANDBY</div>
        </div>

        <div class="engine-ind-grid">
          <div class="ind-box">
            <div class="ind-name">Spot Price</div>
            <div class="ind-val" id="nifty-spot">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">ATM Strike</div>
            <div class="ind-val" id="nifty-atm" style="color:var(--amber);">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">ADX (5m)</div>
            <div class="ind-val" id="nifty-adx">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">Regime</div>
            <div class="ind-val" id="nifty-regime" style="color:var(--purple);">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">KAMA (1m)</div>
            <div class="ind-val" id="nifty-kama">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">Trend</div>
            <div class="ind-val" id="nifty-trend">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">EMA Slope</div>
            <div class="ind-val" id="nifty-slope">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">EMA Signal</div>
            <div class="ind-val" id="nifty-signal">--</div>
          </div>
        </div>

        <div style="font-family:var(--mono); font-size:0.8rem; display:flex; justify-content:space-between; padding:8px 12px; background:rgba(255,255,255,0.02); border-radius:8px;">
          <span>NIFTY Day PnL: <b id="nifty-day-pnl">₹0.00</b></span>
          <span>Trades: <b id="nifty-trades">0</b></span>
        </div>
      </div>

      <!-- MCX Natural Gas Engine -->
      <div class="engine-card">
        <div class="engine-header">
          <div class="engine-title">
            <span style="color:var(--amber);">●</span> MCX Natural Gas
            <span style="font-size:0.75rem; font-family:var(--mono); color:var(--text-dim); font-weight:400;">(Streaming EMA Momentum)</span>
          </div>
          <div id="mcx-status-badge" class="status-chip chip-standby">STANDBY</div>
        </div>

        <div class="engine-ind-grid">
          <div class="ind-box">
            <div class="ind-name">NG Spot</div>
            <div class="ind-val" id="mcx-spot">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">ATM Strike</div>
            <div class="ind-val" id="mcx-atm" style="color:var(--amber);">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">Expiry</div>
            <div class="ind-val" id="mcx-expiry" style="font-size:0.8rem;">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">EMA Signal</div>
            <div class="ind-val" id="mcx-signal">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">EMA 15s</div>
            <div class="ind-val" id="mcx-ema15">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">EMA 90s</div>
            <div class="ind-val" id="mcx-ema90">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">Slow Slope</div>
            <div class="ind-val" id="mcx-slope">--</div>
          </div>
          <div class="ind-box">
            <div class="ind-name">Vol Ratio (VR)</div>
            <div class="ind-val" id="mcx-vr">--</div>
          </div>
        </div>

        <div style="font-family:var(--mono); font-size:0.8rem; display:flex; justify-content:space-between; padding:8px 12px; background:rgba(255,255,255,0.02); border-radius:8px;">
          <span>MCX Day PnL: <b id="mcx-day-pnl">₹0.00</b></span>
          <span>Trades: <b id="mcx-trades">0</b></span>
        </div>
      </div>
    </div>

    <!-- Active Positions Section -->
    <div style="margin-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
      <h3 style="font-size:1.05rem; font-weight:700;">Live Positions (<span id="pos-count">0</span>)</h3>
      <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Auto-Updating Every 1s</span>
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>Market</th>
            <th>Leg</th>
            <th>Contract / Strike</th>
            <th>Side</th>
            <th>Qty</th>
            <th>Entry</th>
            <th>LTP</th>
            <th>Current SL</th>
            <th style="text-align:right;">P&L (MTM)</th>
          </tr>
        </thead>
        <tbody id="positions-tbody">
          <tr>
            <td colspan="9" style="text-align:center; color:var(--text-dim); padding:28px;">
              No active market positions currently open.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Recent Closed Trades -->
    <div style="margin-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
      <h3 style="font-size:1.05rem; font-weight:700;">Today's Executed Trades</h3>
      <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Full Execution Log</span>
    </div>

    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Market</th>
            <th>Leg</th>
            <th>Strike</th>
            <th>Entry Price</th>
            <th>Exit Price</th>
            <th>Exit Reason</th>
            <th style="text-align:right;">Realized PnL</th>
          </tr>
        </thead>
        <tbody id="trades-tbody">
          <tr>
            <td colspan="8" style="text-align:center; color:var(--text-dim); padding:24px;">
              No closed trades recorded yet for today.
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Daily PnL History Cards -->
    <div style="margin-bottom:12px;">
      <h3 style="font-size:1.05rem; font-weight:700;">Past Trading Sessions</h3>
    </div>
    <div class="history-grid" id="history-grid"></div>
  </div>

  <div class="toast" id="toast">Link copied to clipboard!</div>

  <script>
    function fmtINR(val, plus=false) {
      if (val === undefined || val === null || isNaN(val)) return '₹0.00';
      const num = parseFloat(val);
      const sign = num > 0 ? (plus ? '+' : '') : (num < 0 ? '-' : '');
      return `${sign}₹${Math.abs(num).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    }

    function fmtPct(val) {
      if (val === undefined || val === null || isNaN(val)) return '+0.00%';
      const num = parseFloat(val);
      return `${num >= 0 ? '+' : ''}${num.toFixed(2)}%`;
    }

    function applyClass(el, val) {
      el.classList.remove('positive', 'negative', 'neutral');
      if (val > 0.001) el.classList.add('positive');
      else if (val < -0.001) el.classList.add('negative');
      else el.classList.add('neutral');
    }

    function updateDashboard(data) {
      if (!data) return;

      // Clock
      if (data.timestamp_ist) {
        document.getElementById('live-clock').innerText = data.timestamp_ist.split(' ')[1] + ' IST';
      }

      // Performance
      const p = data.performance || {};
      const netMtm = p.combined_net_mtm || 0.0;
      const netPct = p.combined_net_pct || 0.0;

      const netEl = document.getElementById('net-mtm-val');
      netEl.innerText = fmtINR(netMtm, true);
      applyClass(netEl, netMtm);

      const netPctEl = document.getElementById('net-mtm-pct');
      netPctEl.innerText = fmtPct(netPct);
      netPctEl.className = 'pct-tag ' + (netPct >= 0 ? 'pos' : 'neg');

      // Realized & Unrealized
      const realEl = document.getElementById('realized-val');
      realEl.innerText = fmtINR(p.combined_realized, true);
      applyClass(realEl, p.combined_realized);
      document.getElementById('realized-tag').innerText = fmtINR(p.combined_realized, true);

      const unrealEl = document.getElementById('unrealized-val');
      unrealEl.innerText = fmtINR(p.combined_unrealized, true);
      applyClass(unrealEl, p.combined_unrealized);

      // Capital
      document.getElementById('capital-val').innerText = fmtINR(p.current_capital);
      document.getElementById('circuit-val').innerText = fmtINR(p.circuit_limit);

      // MTD / YTD
      const mtdEl = document.getElementById('mtd-val');
      mtdEl.innerText = fmtINR(p.mtd_pnl, true);
      applyClass(mtdEl, p.mtd_pnl);

      const ytdEl = document.getElementById('ytd-val');
      ytdEl.innerText = fmtINR(p.ytd_pnl, true);
      applyClass(ytdEl, p.ytd_pnl);

      document.getElementById('trades-count-pill').innerText = `${p.total_trades || 0} TRADES`;

      // Status chips
      const sys = data.system || {};
      const chipSch = document.getElementById('chip-scheduler');
      chipSch.className = 'status-chip ' + (sys.scheduler_running ? 'chip-active' : 'chip-idle');
      chipSch.innerText = sys.scheduler_running ? 'SCHEDULER: ON' : 'SCHEDULER: OFF';

      const chipNifty = document.getElementById('chip-nifty');
      chipNifty.className = 'status-chip ' + (sys.nifty_running ? 'chip-active' : (sys.nifty_session_active ? 'chip-standby' : 'chip-idle'));
      chipNifty.innerText = sys.nifty_running ? 'NIFTY: RUNNING' : 'NIFTY (09:15-15:35)';

      const chipMcx = document.getElementById('chip-mcx');
      chipMcx.className = 'status-chip ' + (sys.mcx_running ? 'chip-active' : (sys.mcx_session_active ? 'chip-standby' : 'chip-idle'));
      chipMcx.innerText = sys.mcx_running ? 'MCX: RUNNING' : 'MCX (15:30-23:25)';

      const winText = document.getElementById('session-window-text');
      if (sys.nifty_session_active) winText.innerText = '⚡ NIFTY Session Window Active (09:15 - 15:35 IST)';
      else if (sys.mcx_session_active) winText.innerText = '⚡ MCX Natural Gas Session Window Active (15:30 - 23:25 IST)';
      else winText.innerText = '💤 Market Session Closed • Standing by for Next Open';

      // NIFTY Data
      const n = data.nifty || {};
      document.getElementById('nifty-spot').innerText = n.spot ? n.spot.toFixed(2) : '--';
      document.getElementById('nifty-atm').innerText = n.atm || '--';
      document.getElementById('nifty-adx').innerText = n.adx ? n.adx.toFixed(1) : '--';
      document.getElementById('nifty-regime').innerText = n.regime || '--';
      document.getElementById('nifty-kama').innerText = n.kama || '--';
      document.getElementById('nifty-trend').innerText = n.trend || '--';
      document.getElementById('nifty-slope').innerText = (n.slow_slope !== undefined) ? n.slow_slope.toFixed(3) : '--';
      document.getElementById('nifty-signal').innerText = n.signal || '--';

      const nDayEl = document.getElementById('nifty-day-pnl');
      nDayEl.innerText = fmtINR(n.net_pnl, true);
      applyClass(nDayEl, n.net_pnl);
      document.getElementById('nifty-trades').innerText = n.trades_today || 0;

      const nBadge = document.getElementById('nifty-status-badge');
      nBadge.className = 'status-chip ' + (n.active ? 'chip-active' : 'chip-standby');
      nBadge.innerText = n.active ? 'ACTIVE' : n.mode || 'STANDBY';

      // MCX Data
      const m = data.mcx || {};
      document.getElementById('mcx-spot').innerText = m.spot ? m.spot.toFixed(2) : '--';
      document.getElementById('mcx-atm').innerText = m.atm || '--';
      document.getElementById('mcx-expiry').innerText = m.expiry + (m.is_rolled_over ? ' (ROLLOVER)' : '');
      document.getElementById('mcx-signal').innerText = m.signal || '--';
      document.getElementById('mcx-ema15').innerText = m.ema15 ? m.ema15.toFixed(2) : '--';
      document.getElementById('mcx-ema90').innerText = m.ema90 ? m.ema90.toFixed(2) : '--';
      document.getElementById('mcx-slope').innerText = (m.slow_slope !== undefined) ? m.slow_slope.toFixed(3) : '--';
      document.getElementById('mcx-vr').innerText = m.vr ? m.vr.toFixed(2) : '--';

      const mDayEl = document.getElementById('mcx-day-pnl');
      mDayEl.innerText = fmtINR(m.net_pnl, true);
      applyClass(mDayEl, m.net_pnl);
      document.getElementById('mcx-trades').innerText = m.trades_today || 0;

      const mBadge = document.getElementById('mcx-status-badge');
      mBadge.className = 'status-chip ' + (m.active ? 'chip-active' : 'chip-standby');
      mBadge.innerText = m.active ? 'ACTIVE' : 'STANDBY';

      // Positions Table
      const posList = data.positions || [];
      document.getElementById('pos-count').innerText = posList.length;
      const posTbody = document.getElementById('positions-tbody');
      if (posList.length === 0) {
        posTbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color:var(--text-dim); padding:28px;">No active market positions currently open.</td></tr>`;
      } else {
        posTbody.innerHTML = posList.map(pos => {
          const isCall = pos.leg.startsWith('CE');
          const badgeClass = pos.leg.includes('HEDGE') ? 'badge-hedge' : (isCall ? 'badge-ce' : 'badge-pe');
          const pnlClass = pos.pnl >= 0 ? 'positive' : 'negative';
          return `
            <tr>
              <td><b style="color:${pos.market === 'NIFTY' ? 'var(--primary)' : 'var(--amber)'}">${pos.market}</b></td>
              <td><span class="badge-leg ${badgeClass}">${pos.display_leg}</span></td>
              <td><b>${pos.tsym || pos.strike}</b></td>
              <td><span class="${pos.side === 'SELL' ? 'side-sell' : 'side-buy'}">${pos.side}</span></td>
              <td>${pos.qty}</td>
              <td>₹${parseFloat(pos.entry).toFixed(2)}</td>
              <td><b style="color:#fff;">₹${parseFloat(pos.ltp).toFixed(2)}</b></td>
              <td style="color:var(--text-muted);">${pos.sl > 0 ? '₹' + parseFloat(pos.sl).toFixed(2) : '—'}</td>
              <td style="text-align:right;" class="${pnlClass}"><b>${fmtINR(pos.pnl, true)}</b></td>
            </tr>
          `;
        }).join('');
      }

      // Trades Table
      const tradesList = data.recent_trades || [];
      const trTbody = document.getElementById('trades-tbody');
      if (tradesList.length === 0) {
        trTbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--text-dim); padding:24px;">No closed trades recorded yet for today.</td></tr>`;
      } else {
        trTbody.innerHTML = tradesList.map(t => {
          const pnlClass = t.pnl >= 0 ? 'positive' : 'negative';
          return `
            <tr>
              <td style="color:var(--text-dim);">${t.time || '--'}</td>
              <td><b>${t.market}</b></td>
              <td><span class="badge-leg ${t.leg.startsWith('CE') ? 'badge-ce' : 'badge-pe'}">${t.leg}</span></td>
              <td>${t.strike}</td>
              <td>₹${parseFloat(t.entry || 0).toFixed(2)}</td>
              <td>₹${parseFloat(t.exit || 0).toFixed(2)}</td>
              <td style="color:var(--text-muted); font-size:0.75rem;">${t.reason || 'SQUARE_OFF'}</td>
              <td style="text-align:right;" class="${pnlClass}"><b>${fmtINR(t.pnl, true)}</b></td>
            </tr>
          `;
        }).join('');
      }

      // Daily History
      const histMap = p.daily_history || {};
      const histGrid = document.getElementById('history-grid');
      const histKeys = Object.keys(histMap).sort().reverse();
      if (histKeys.length > 0) {
        histGrid.innerHTML = histKeys.map(k => {
          const v = histMap[k];
          const c = v >= 0 ? 'positive' : 'negative';
          return `
            <div class="history-card">
              <div class="hist-date">${k}</div>
              <div class="hist-pnl ${c}">${fmtINR(v, true)}</div>
            </div>
          `;
        }).join('');
      }
    }

    // Connect to SSE stream
    function initSSE() {
      const streamStatus = document.getElementById('stream-status');
      let es = null;

      function connect() {
        es = new EventSource('/api/stream');
        es.onopen = () => {
          streamStatus.innerText = 'LIVE STREAMING';
          streamStatus.style.color = 'var(--green)';
        };
        es.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data);
            updateDashboard(data);
          } catch(err) {
            console.error('SSE JSON error', err);
          }
        };
        es.onerror = () => {
          streamStatus.innerText = 'RECONNECTING...';
          streamStatus.style.color = 'var(--amber)';
          es.close();
          setTimeout(connect, 2000);
        };
      }
      connect();

      // Fallback Polling (in case SSE drops or client proxy blocks SSE)
      setInterval(() => {
        if (!es || es.readyState !== EventSource.OPEN) {
          fetch('/api/status')
            .then(res => res.json())
            .then(data => updateDashboard(data))
            .catch(() => {});
        }
      }, 2000);
    }

    function copyPublicLink() {
      const url = window.location.href;
      navigator.clipboard.writeText(url).then(() => {
        const toast = document.getElementById('toast');
        toast.style.display = 'block';
        setTimeout(() => { toast.style.display = 'none'; }, 2500);
      });
    }

    window.addEventListener('DOMContentLoaded', () => {
      // First quick fetch
      fetch('/api/status')
        .then(res => res.json())
        .then(data => updateDashboard(data))
        .catch(() => {});

      // Start SSE
      initSSE();
    });
  </script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# HTTP REQUEST HANDLER
# ─────────────────────────────────────────────────────────────────────────────
class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence access logs to keep terminal neat
        return

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))

        elif path == "/api/status":
            data = get_aggregated_dashboard_state()
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(payload)

        elif path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            try:
                while True:
                    data = get_aggregated_dashboard_state()
                    payload = f"data: {json.dumps(data)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        else:
            self.send_response(404)
            self.end_headers()


def start_server(port: int = 8000):
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHTTPHandler)
    print(f"[WEB DASHBOARD] Listening at http://0.0.0.0:{port}", flush=True)

    # Launch tunnel in background thread
    t = threading.Thread(target=run_tunnel_manager, args=(port,), daemon=True)
    t.start()

    server.serve_forever()


if __name__ == "__main__":
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    start_server(port)
