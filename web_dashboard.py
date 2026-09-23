#!/usr/bin/env python3
"""
================================================================================
🌐 FLATTRADE QUANTITATIVE TRADING TERMINAL — EXECUTIVE DASHBOARD
================================================================================
Modern, ultra-responsive financial terminal featuring:
- High-frequency Server-Sent Events (SSE) 1-second live telemetry
- Dedicated Tabbed Architecture: NIFTY 50 | MCX Natural Gas | Overview
- Rich Visualizations: Daily Equity SVG Chart, Risk/Circuit Gauges, EMA Meter
- Obsidian Glassmorphism UI with precision financial typography
- Embedded Cloudflare Quick Tunnel for universal cross-device access
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
from urllib.parse import urlparse

IST = timezone(timedelta(hours=5, minutes=30))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = CURRENT_DIR

def get_ist_now() -> datetime:
    return datetime.now(IST)

PUBLIC_URL_FILE = os.path.join(PROJECT_ROOT, "public_url.txt")
LIVE_PUBLIC_URL = ""

# ─────────────────────────────────────────────────────────────────────────────
# DATA INGESTION
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

    scheduler_running = check_process_running("daily_scheduler.py")
    nifty_running = check_process_running("nifty_paper_v3.py") or check_process_running("upv2_paper.py")
    mcx_running = check_process_running("mcx_paper_v5.py")

    hhmm = now_ist.strftime("%H:%M")
    is_weekday = now_ist.weekday() < 5
    nifty_session_active = is_weekday and "09:15" <= hhmm < "15:35"
    mcx_session_active = is_weekday and "15:30" <= hhmm < "23:25"

    nifty_realized = float(nifty_snap.get("realized_pnl", 0.0) or 0.0)
    nifty_unrealized = float(nifty_snap.get("unrealized_pnl", 0.0) or 0.0)
    nifty_net = nifty_realized + nifty_unrealized

    mcx_realized = float(mcx_snap.get("realized_pnl", mcx_snap.get("total_realized_pnl", 0.0)) or 0.0)
    mcx_unrealized = float(mcx_snap.get("unrealized_pnl", 0.0) or 0.0)
    mcx_net = float(mcx_snap.get("net_pnl", mcx_realized + mcx_unrealized) or 0.0)

    combined_realized = nifty_realized + mcx_realized
    combined_unrealized = nifty_unrealized + mcx_unrealized
    combined_net = combined_realized + combined_unrealized
    combined_net_pct = (combined_net / 200000.0) * 100.0

    base_capital = 200000.0
    live_capital = float(pnl_data.get("current_capital", 200000.0) or 200000.0) + combined_unrealized
    mtd_pnl = float(pnl_data.get("mtd_pnl", 0.0) or 0.0)
    ytd_pnl = float(pnl_data.get("ytd_pnl", 0.0) or 0.0)
    circuit_limit = round(-live_capital * 0.018, 2)

    nifty_trades = int(nifty_snap.get("trades_today", 0) or 0)
    mcx_trades = int(mcx_snap.get("trades_today", 0) or 0)
    total_trades = nifty_trades + mcx_trades

    # Normalizing NIFTY positions
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

    # Normalizing MCX positions
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

    # Normalized Trade History
    nifty_trades_list = []
    for t in nifty_snap.get("trade_log", []):
        nifty_trades_list.append({
            "market": "NIFTY",
            "time": t.get("time", ""),
            "leg": t.get("leg", ""),
            "strike": t.get("strike", ""),
            "entry": t.get("entry", 0.0),
            "exit": t.get("exit", 0.0),
            "pnl": t.get("pnl", 0.0),
            "reason": t.get("reason", "")
        })

    mcx_trades_list = []
    for t in mcx_snap.get("trade_log", []):
        mcx_trades_list.append({
            "market": "MCX",
            "time": t.get("time", ""),
            "leg": t.get("leg", ""),
            "strike": t.get("strike", ""),
            "entry": t.get("entry", 0.0),
            "exit": t.get("exit", 0.0),
            "pnl": t.get("pnl", 0.0),
            "reason": t.get("reason", "")
        })

    all_trades = sorted(nifty_trades_list + mcx_trades_list, key=lambda x: str(x.get("time", "")), reverse=True)

    nifty_ind = nifty_snap.get("indicators", {}) or {}
    kama_val = nifty_ind.get("kama")
    kama_str = f"{kama_val:.1f}" if kama_val is not None else "WARMUP"
    trend_val = nifty_ind.get("trend", 0)
    trend_label = "BULLISH ▲" if trend_val == 1 else ("BEARISH ▼" if trend_val == -1 else "FLAT ━")
    regime = nifty_ind.get("regime", "CHOP")
    adx_val = float(nifty_ind.get("adx", 18.0) or 18.0)
    atr_val = float(nifty_ind.get("atr", 35.0) or 35.0)

    nifty_ema_sig = nifty_ind.get("confirmed_signal", 0)
    nifty_ema_sig_str = "BULLISH ▲" if nifty_ema_sig > 0 else ("BEARISH ▼" if nifty_ema_sig < 0 else "FLAT ━")

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
            "circuit_used_pct": min(100.0, max(0.0, (abs(combined_net) / abs(circuit_limit)) * 100.0)) if combined_net < 0 and circuit_limit != 0 else 0.0,
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
            "positions": nifty_positions,
            "trades": nifty_trades_list
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
            "positions": mcx_positions,
            "trades": mcx_trades_list
        },
        "positions": nifty_positions + mcx_positions,
        "recent_trades": all_trades[:35]
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
# HIGH-FIDELITY SINGLE PAGE APPLICATION (SPA)
# ─────────────────────────────────────────────────────────────────────────────
HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Flattrade Quantitative Terminal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #06090e;
      --card-bg: rgba(13, 19, 33, 0.78);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-hover: rgba(56, 189, 248, 0.2);
      --primary: #38bdf8;
      --primary-glow: rgba(56, 189, 248, 0.25);
      --green: #10b981;
      --green-glow: rgba(16, 185, 129, 0.35);
      --red: #f43f5e;
      --red-glow: rgba(244, 63, 94, 0.35);
      --amber: #f59e0b;
      --amber-glow: rgba(245, 158, 11, 0.3);
      --purple: #a855f7;
      --indigo: #6366f1;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --mono: 'JetBrains Mono', monospace;
      --sans: 'Plus Jakarta Sans', sans-serif;
      --display: 'Space Grotesk', sans-serif;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

    body {
      background-color: var(--bg);
      background-image: 
        radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.09) 0px, transparent 45%),
        radial-gradient(at 100% 0%, rgba(245, 158, 11, 0.07) 0px, transparent 40%),
        radial-gradient(at 50% 100%, rgba(168, 85, 247, 0.06) 0px, transparent 55%);
      background-attachment: fixed;
      color: var(--text);
      font-family: var(--sans);
      min-height: 100vh;
      line-height: 1.5;
      padding-bottom: 70px;
    }

    .container {
      max-width: 1400px;
      margin: 0 auto;
      padding: 16px;
    }

    /* Top Navigation Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 14px 20px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      backdrop-filter: blur(24px);
      margin-bottom: 18px;
      box-shadow: 0 12px 36px -10px rgba(0,0,0,0.6);
      flex-wrap: wrap;
    }

    .brand-wrap {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand-logo {
      width: 44px;
      height: 44px;
      background: linear-gradient(135deg, #0284c7, #4f46e5);
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      box-shadow: 0 4px 20px rgba(2, 132, 199, 0.4);
    }

    .brand-title {
      font-family: var(--display);
      font-weight: 800;
      font-size: 1.25rem;
      letter-spacing: -0.03em;
      background: linear-gradient(to right, #ffffff, #cbd5e1);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }

    .brand-subtitle {
      font-size: 0.73rem;
      color: var(--text-dim);
      font-family: var(--mono);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .header-ctrls {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .live-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.35);
      border-radius: 999px;
      color: var(--green);
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .dot-pulse {
      width: 8px;
      height: 8px;
      background: var(--green);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--green);
      animation: pulse 1.6s infinite ease-in-out;
    }

    @keyframes pulse {
      0% { transform: scale(0.85); opacity: 0.7; }
      50% { transform: scale(1.35); opacity: 1; box-shadow: 0 0 16px var(--green); }
      100% { transform: scale(0.85); opacity: 0.7; }
    }

    .time-chip {
      font-family: var(--mono);
      font-size: 0.85rem;
      font-weight: 700;
      padding: 6px 14px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      color: #e2e8f0;
    }

    .btn-action {
      background: rgba(56, 189, 248, 0.12);
      color: var(--primary);
      border: 1px solid rgba(56, 189, 248, 0.3);
      padding: 7px 14px;
      border-radius: 12px;
      font-size: 0.8rem;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .btn-action:hover {
      background: rgba(56, 189, 248, 0.25);
      transform: translateY(-1px);
    }

    /* ─── Hero Segmented Navigation Tabs ─── */
    .segmented-tabs-bar {
      display: flex;
      justify-content: center;
      margin-bottom: 20px;
    }

    .segmented-tabs {
      display: inline-flex;
      background: rgba(15, 23, 42, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.1);
      padding: 5px;
      border-radius: 16px;
      gap: 6px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
      backdrop-filter: blur(20px);
      max-width: 100%;
      overflow-x: auto;
    }

    .seg-tab {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 10px 22px;
      border-radius: 12px;
      font-weight: 700;
      font-size: 0.92rem;
      cursor: pointer;
      transition: all 0.25s ease;
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }

    .seg-tab:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.05);
    }

    .seg-tab.active-nifty {
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.25), rgba(99, 102, 241, 0.25));
      color: #fff;
      border: 1px solid rgba(56, 189, 248, 0.5);
      box-shadow: 0 0 16px rgba(56, 189, 248, 0.2);
    }

    .seg-tab.active-mcx {
      background: linear-gradient(135deg, rgba(245, 158, 11, 0.25), rgba(234, 88, 12, 0.25));
      color: #fff;
      border: 1px solid rgba(245, 158, 11, 0.5);
      box-shadow: 0 0 16px rgba(245, 158, 11, 0.2);
    }

    .seg-tab.active-overview {
      background: linear-gradient(135deg, rgba(168, 85, 247, 0.25), rgba(56, 189, 248, 0.25));
      color: #fff;
      border: 1px solid rgba(168, 85, 247, 0.5);
      box-shadow: 0 0 16px rgba(168, 85, 247, 0.2);
    }

    /* ─── Hero KPI Cards ─── */
    .kpi-row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }

    .kpi-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 18px;
      padding: 18px 20px;
      backdrop-filter: blur(18px);
      position: relative;
      overflow: hidden;
      transition: transform 0.2s ease, border-color 0.2s ease;
    }

    .kpi-card:hover {
      border-color: var(--card-hover);
      transform: translateY(-2px);
    }

    .kpi-label {
      font-size: 0.74rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .kpi-val {
      font-family: var(--mono);
      font-size: 1.85rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      display: flex;
      align-items: baseline;
      gap: 8px;
    }

    .kpi-sub {
      font-size: 0.78rem;
      color: var(--text-dim);
      margin-top: 6px;
      display: flex;
      align-items: center;
      gap: 6px;
      font-family: var(--mono);
    }

    .positive { color: var(--green); text-shadow: 0 0 24px var(--green-glow); }
    .negative { color: var(--red); text-shadow: 0 0 24px var(--red-glow); }
    .neutral  { color: #f1f5f9; }

    .tag-pct {
      font-size: 0.85rem;
      padding: 3px 9px;
      border-radius: 8px;
      font-weight: 700;
      font-family: var(--mono);
    }
    .tag-pct.pos { background: rgba(16, 185, 129, 0.15); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.3); }
    .tag-pct.neg { background: rgba(244, 63, 94, 0.15); color: var(--red); border: 1px solid rgba(244, 63, 94, 0.3); }

    /* Risk / Circuit Meter Bar */
    .circuit-track {
      width: 100%;
      height: 6px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: 999px;
      overflow: hidden;
      margin-top: 10px;
    }
    .circuit-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(to right, var(--green), var(--amber), var(--red));
      transition: width 0.5s ease;
    }

    /* ─── Engine Command Centers (Tabbed) ─── */
    .view-section {
      display: none;
      animation: fadeIn 0.3s ease;
    }
    .view-section.active { display: block; }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .panel-box {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      padding: 22px;
      backdrop-filter: blur(20px);
      box-shadow: 0 12px 36px -12px rgba(0,0,0,0.6);
      margin-bottom: 22px;
    }

    .panel-hdr {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 16px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--card-border);
      flex-wrap: wrap;
      gap: 12px;
    }

    .panel-title {
      font-size: 1.15rem;
      font-weight: 800;
      font-family: var(--display);
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .status-chip {
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 800;
      font-family: var(--mono);
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .chip-green { background: rgba(16, 185, 129, 0.18); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.4); }
    .chip-amber { background: rgba(245, 158, 11, 0.18); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.4); }
    .chip-dim   { background: rgba(148, 163, 184, 0.12); color: var(--text-dim); border: 1px solid rgba(148, 163, 184, 0.2); }

    /* Telemetry Metrics Grid */
    .telemetry-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(135px, 1fr));
      gap: 10px;
      margin-bottom: 20px;
    }

    .tel-item {
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 14px;
      padding: 12px 14px;
      transition: border-color 0.2s ease;
    }
    .tel-item:hover { border-color: rgba(255, 255, 255, 0.12); }

    .tel-label {
      font-size: 0.68rem;
      color: var(--text-dim);
      font-weight: 700;
      text-transform: uppercase;
      font-family: var(--mono);
      letter-spacing: 0.04em;
    }

    .tel-val {
      font-family: var(--mono);
      font-size: 1.08rem;
      font-weight: 800;
      margin-top: 4px;
    }

    /* ─── Modern Tables ─── */
    .table-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 18px;
      overflow: hidden;
      margin-bottom: 22px;
    }

    .table-scroll {
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }

    th {
      background: rgba(255, 255, 255, 0.025);
      color: var(--text-muted);
      font-weight: 700;
      padding: 12px 16px;
      text-transform: uppercase;
      font-size: 0.72rem;
      letter-spacing: 0.05em;
      border-bottom: 1px solid var(--card-border);
      font-family: var(--mono);
    }

    td {
      padding: 14px 16px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.035);
      font-family: var(--mono);
    }

    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }

    .leg-badge {
      display: inline-block;
      padding: 3px 9px;
      border-radius: 6px;
      font-size: 0.74rem;
      font-weight: 800;
    }
    .leg-ce { background: rgba(56, 189, 248, 0.16); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.35); }
    .leg-pe { background: rgba(245, 158, 11, 0.16); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.35); }
    .leg-hd { background: rgba(168, 85, 247, 0.16); color: var(--purple); border: 1px solid rgba(168, 85, 247, 0.35); }

    .side-sell { color: var(--red); font-weight: 800; }
    .side-buy  { color: var(--green); font-weight: 800; }

    /* Visual Equity History Spark-Cards */
    .history-cards-flex {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 10px;
    }

    .hist-box {
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px 14px;
      font-family: var(--mono);
    }

    .hist-title { font-size: 0.7rem; color: var(--text-dim); margin-bottom: 2px; }
    .hist-num   { font-size: 1.05rem; font-weight: 800; }

    /* Toast */
    .toast-box {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid var(--primary);
      color: #fff;
      padding: 12px 22px;
      border-radius: 14px;
      font-size: 0.88rem;
      font-weight: 700;
      box-shadow: 0 12px 30px rgba(0,0,0,0.6);
      display: none;
      z-index: 9999;
    }

    @media (max-width: 640px) {
      .container { padding: 10px; }
      header { padding: 12px 14px; }
      .brand-title { font-size: 1.05rem; }
      .kpi-val { font-size: 1.55rem; }
      .seg-tab { padding: 8px 14px; font-size: 0.82rem; }
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Top Header -->
    <header>
      <div class="brand-wrap">
        <div class="brand-logo">⚡</div>
        <div>
          <div class="brand-title">FLATTRADE QUANT TERMINAL</div>
          <div class="brand-subtitle">
            <span>HIGH-PRECISION EXECUTION</span>
            <span>•</span>
            <span id="market-mode-badge" style="color:var(--primary); font-weight:700;">PAPER MODE</span>
          </div>
        </div>
      </div>
      <div class="header-ctrls">
        <div class="live-badge">
          <span class="dot-pulse"></span>
          <span id="stream-status">LIVE STREAMING</span>
        </div>
        <div class="time-chip" id="live-clock">--:--:-- IST</div>
        <button class="btn-action" onclick="copyPublicLink()">
          <span>🔗</span> <span id="copy-btn-text">Share Link</span>
        </button>
      </div>
    </header>

    <!-- ─── Segmented Navigation Switcher ─── -->
    <div class="segmented-tabs-bar">
      <div class="segmented-tabs">
        <button class="seg-tab active-nifty" id="tab-btn-nifty" onclick="switchTab('nifty')">
          <span>⚡</span> <span>NIFTY 50 Options</span>
        </button>
        <button class="seg-tab" id="tab-btn-mcx" onclick="switchTab('mcx')">
          <span>🛢️</span> <span>MCX Natural Gas</span>
        </button>
        <button class="seg-tab" id="tab-btn-overview" onclick="switchTab('overview')">
          <span>📊</span> <span>Unified Overview</span>
        </button>
      </div>
    </div>

    <!-- ─── Hero KPI Metric Cards (Strict 2L Base %) ─── -->
    <div class="kpi-row">
      <!-- Net MTM PnL -->
      <div class="kpi-card">
        <div class="kpi-label">
          <span>Net MTM P&L</span>
          <span class="tag-pct" id="net-mtm-pct">+0.00%</span>
        </div>
        <div class="kpi-val" id="net-mtm-val">₹0.00</div>
        <div class="kpi-sub">
          <span>Calculated on: <b style="color:#fff;">₹2,00,000.00 Fixed</b></span>
        </div>
      </div>

      <!-- Realized vs Unrealized -->
      <div class="kpi-card">
        <div class="kpi-label">
          <span>Realized Booked</span>
          <span id="realized-tag" style="font-family:var(--mono); font-size:0.75rem;">₹0.00</span>
        </div>
        <div class="kpi-val" id="realized-val">₹0.00</div>
        <div class="kpi-sub">
          <span>Floating MTM: <b id="unrealized-val" style="color:#fff;">₹0.00</b></span>
        </div>
      </div>

      <!-- Live Capital & Circuit Limit -->
      <div class="kpi-card">
        <div class="kpi-label">
          <span>Live Capital</span>
          <span style="color:var(--text-dim); font-size:0.75rem; font-family:var(--mono);">Risk Distance</span>
        </div>
        <div class="kpi-val" id="capital-val">₹2,00,000.00</div>
        <div class="circuit-track">
          <div class="circuit-fill" id="circuit-fill-bar"></div>
        </div>
        <div class="kpi-sub" style="justify-content:space-between; margin-top:6px;">
          <span>Circuit Break: <b id="circuit-val" style="color:var(--red);">-₹3,600</b></span>
          <span id="circuit-used-text" style="font-size:0.7rem; color:var(--text-dim);">0% Used</span>
        </div>
      </div>

      <!-- MTD & YTD Returns -->
      <div class="kpi-card">
        <div class="kpi-label">
          <span>MTD Performance</span>
          <span id="trades-count-pill" style="font-family:var(--mono); color:var(--primary); font-size:0.75rem;">0 TRADES</span>
        </div>
        <div class="kpi-val" id="mtd-val">₹0.00</div>
        <div class="kpi-sub">
          <span>Year-to-Date: <b id="ytd-val" style="color:#fff;">₹0.00</b></span>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════════ -->
    <!-- TAB 1: NIFTY 50 DEDICATED COMMAND CENTER                               -->
    <!-- ═══════════════════════════════════════════════════════════════════════ -->
    <div id="view-nifty" class="view-section active">
      <div class="panel-box">
        <div class="panel-hdr">
          <div class="panel-title">
            <span style="color:var(--primary);">⚡</span> NIFTY 50 Index Options Architecture
            <span style="font-size:0.76rem; font-family:var(--mono); color:var(--text-dim); font-weight:400;">(Adaptive KAMA-ADX & Straddle Engine)</span>
          </div>
          <div style="display:flex; align-items:center; gap:8px;">
            <div id="nifty-status-chip" class="status-chip chip-dim">STANDBY</div>
            <div id="nifty-session-chip" class="status-chip chip-dim">09:15 - 15:35 IST</div>
          </div>
        </div>

        <!-- Telemetry Indicators Grid -->
        <div class="telemetry-grid">
          <div class="tel-item">
            <div class="tel-label">Spot Price</div>
            <div class="tel-val" id="n-spot">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">ATM Strike</div>
            <div class="tel-val" id="n-atm" style="color:var(--amber);">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">ADX (5m)</div>
            <div class="tel-val" id="n-adx">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">Regime</div>
            <div class="tel-val" id="n-regime" style="color:var(--purple);">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">KAMA (1m)</div>
            <div class="tel-val" id="n-kama">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">Trend Filter</div>
            <div class="tel-val" id="n-trend">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">ATR Volatility</div>
            <div class="tel-val" id="n-atr">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">EMA Signal</div>
            <div class="tel-val" id="n-signal">--</div>
          </div>
        </div>

        <!-- Momentum Detail Bar -->
        <div style="font-family:var(--mono); font-size:0.8rem; display:flex; justify-content:space-between; flex-wrap:wrap; gap:10px; padding:10px 16px; background:rgba(255,255,255,0.025); border-radius:12px; border:1px solid rgba(255,255,255,0.05);">
          <span>NIFTY Day Net: <b id="n-net-pnl">₹0.00</b> (<span id="n-net-pct">+0.00%</span>)</span>
          <span>Trades Executed: <b id="n-trades">0</b></span>
          <span>Realized: <b id="n-realized">₹0.00</b></span>
          <span>Floating: <b id="n-unrealized">₹0.00</b></span>
        </div>
      </div>

      <!-- NIFTY Active Positions -->
      <div style="margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="font-size:1.05rem; font-weight:800;">NIFTY Active Leg Portfolio (<span id="n-pos-count">0</span>)</h3>
        <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Zero Duplicate Trade Guard</span>
      </div>

      <div class="table-card">
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Leg</th>
                <th>Contract</th>
                <th>Strike</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Entry Price</th>
                <th>LTP</th>
                <th>Current SL</th>
                <th style="text-align:right;">Unrealized P&L</th>
              </tr>
            </thead>
            <tbody id="n-pos-tbody">
              <tr><td colspan="9" style="text-align:center; color:var(--text-dim); padding:28px;">No active NIFTY positions open.</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- NIFTY Trades Log -->
      <div style="margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="font-size:1.05rem; font-weight:800;">NIFTY Executed Trades Today</h3>
        <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Execution Journal</span>
      </div>

      <div class="table-card">
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Leg</th>
                <th>Strike</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Exit Reason</th>
                <th style="text-align:right;">Realized PnL</th>
              </tr>
            </thead>
            <tbody id="n-trades-tbody">
              <tr><td colspan="7" style="text-align:center; color:var(--text-dim); padding:24px;">No closed NIFTY trades recorded yet for today.</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════════ -->
    <!-- TAB 2: MCX NATURAL GAS DEDICATED COMMAND CENTER                        -->
    <!-- ═══════════════════════════════════════════════════════════════════════ -->
    <div id="view-mcx" class="view-section">
      <div class="panel-box">
        <div class="panel-hdr">
          <div class="panel-title">
            <span style="color:var(--amber);">🛢️</span> MCX Natural Gas Momentum Engine
            <span style="font-size:0.76rem; font-family:var(--mono); color:var(--text-dim); font-weight:400;">(Continuous Streaming EMA Pipeline v5.0)</span>
          </div>
          <div style="display:flex; align-items:center; gap:8px;">
            <div id="mcx-status-chip" class="status-chip chip-dim">STANDBY</div>
            <div id="mcx-session-chip" class="status-chip chip-dim">15:30 - 23:25 IST</div>
          </div>
        </div>

        <!-- Telemetry Indicators Grid -->
        <div class="telemetry-grid">
          <div class="tel-item">
            <div class="tel-label">Spot Price</div>
            <div class="tel-val" id="m-spot">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">ATM Strike</div>
            <div class="tel-val" id="m-atm" style="color:var(--amber);">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">Target Expiry</div>
            <div class="tel-val" id="m-expiry" style="font-size:0.85rem;">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">Confirmed Signal</div>
            <div class="tel-val" id="m-signal">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">EMA Fast (15s)</div>
            <div class="tel-val" id="m-ema15">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">EMA Slow (90s)</div>
            <div class="tel-val" id="m-ema90">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">Slow Slope</div>
            <div class="tel-val" id="m-slope">--</div>
          </div>
          <div class="tel-item">
            <div class="tel-label">Volatility Ratio</div>
            <div class="tel-val" id="m-vr">--</div>
          </div>
        </div>

        <!-- MCX Details Bar -->
        <div style="font-family:var(--mono); font-size:0.8rem; display:flex; justify-content:space-between; flex-wrap:wrap; gap:10px; padding:10px 16px; background:rgba(255,255,255,0.025); border-radius:12px; border:1px solid rgba(255,255,255,0.05);">
          <span>MCX Day Net: <b id="m-net-pnl">₹0.00</b> (<span id="m-net-pct">+0.00%</span>)</span>
          <span>Trades Executed: <b id="m-trades">0</b></span>
          <span>Reversal State: <b id="m-reversal" style="color:var(--purple);">INACTIVE</b></span>
          <span>Cooldown Timer: <b id="m-cooldown">0s</b></span>
        </div>
      </div>

      <!-- MCX Active Positions -->
      <div style="margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="font-size:1.05rem; font-weight:800;">MCX Active Straddle Leg Portfolio (<span id="m-pos-count">0</span>)</h3>
        <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Smart In-Place Reconstitution</span>
      </div>

      <div class="table-card">
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Leg</th>
                <th>Contract</th>
                <th>Strike</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Entry Price</th>
                <th>LTP</th>
                <th>Current SL</th>
                <th style="text-align:right;">Unrealized P&L</th>
              </tr>
            </thead>
            <tbody id="m-pos-tbody">
              <tr><td colspan="9" style="text-align:center; color:var(--text-dim); padding:28px;">No active MCX positions open.</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- MCX Trades Log -->
      <div style="margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="font-size:1.05rem; font-weight:800;">MCX Executed Trades Today</h3>
        <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Execution Journal</span>
      </div>

      <div class="table-card">
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Leg</th>
                <th>Strike</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Exit Reason</th>
                <th style="text-align:right;">Realized PnL</th>
              </tr>
            </thead>
            <tbody id="m-trades-tbody">
              <tr><td colspan="7" style="text-align:center; color:var(--text-dim); padding:24px;">No closed MCX trades recorded yet for today.</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ═══════════════════════════════════════════════════════════════════════ -->
    <!-- TAB 3: UNIFIED OVERVIEW (BOTH ENGINES + CALENDAR STATS)                -->
    <!-- ═══════════════════════════════════════════════════════════════════════ -->
    <div id="view-overview" class="view-section">
      <!-- Active Positions (Combined) -->
      <div style="margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="font-size:1.05rem; font-weight:800;">All Live Market Positions (<span id="all-pos-count">0</span>)</h3>
        <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Full Multi-Asset Portfolio</span>
      </div>

      <div class="table-card">
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Market</th>
                <th>Leg</th>
                <th>Contract / Strike</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Entry Price</th>
                <th>LTP</th>
                <th>Current SL</th>
                <th style="text-align:right;">Unrealized P&L</th>
              </tr>
            </thead>
            <tbody id="all-pos-tbody">
              <tr><td colspan="9" style="text-align:center; color:var(--text-dim); padding:28px;">No market positions currently open.</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Past Trading Sessions Performance Grid -->
      <div style="margin-bottom:12px;">
        <h3 style="font-size:1.05rem; font-weight:800;">Historical Trading Sessions (Daily PnL)</h3>
      </div>
      <div class="history-cards-flex" id="history-cards-box"></div>
    </div>
  </div>

  <div class="toast-box" id="toast">Link copied to clipboard!</div>

  <script>
    let activeTab = 'nifty';

    function switchTab(tabName) {
      activeTab = tabName;
      document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.seg-tab').forEach(el => el.className = 'seg-tab');

      if (tabName === 'nifty') {
        document.getElementById('view-nifty').classList.add('active');
        document.getElementById('tab-btn-nifty').classList.add('active-nifty');
      } else if (tabName === 'mcx') {
        document.getElementById('view-mcx').classList.add('active');
        document.getElementById('tab-btn-mcx').classList.add('active-mcx');
      } else {
        document.getElementById('view-overview').classList.add('active');
        document.getElementById('tab-btn-overview').classList.add('active-overview');
      }
    }

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

    function renderPositionsRows(posList) {
      if (!posList || posList.length === 0) {
        return `<tr><td colspan="9" style="text-align:center; color:var(--text-dim); padding:28px;">No active market positions open.</td></tr>`;
      }
      return posList.map(pos => {
        const isCall = pos.leg.startsWith('CE');
        const badgeClass = pos.leg.includes('HEDGE') ? 'leg-hd' : (isCall ? 'leg-ce' : 'leg-pe');
        const pnlClass = pos.pnl >= 0 ? 'positive' : 'negative';
        return `
          <tr>
            <td><span class="leg-badge ${pos.market === 'NIFTY' ? 'leg-ce' : 'leg-pe'}">${pos.market}</span></td>
            <td><span class="leg-badge ${badgeClass}">${pos.display_leg}</span></td>
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

    function renderTradesRows(tradesList) {
      if (!tradesList || tradesList.length === 0) {
        return `<tr><td colspan="7" style="text-align:center; color:var(--text-dim); padding:24px;">No closed trades recorded yet for today.</td></tr>`;
      }
      return tradesList.map(t => {
        const pnlClass = t.pnl >= 0 ? 'positive' : 'negative';
        return `
          <tr>
            <td style="color:var(--text-dim);">${t.time || '--'}</td>
            <td><span class="leg-badge ${t.leg.startsWith('CE') ? 'leg-ce' : 'leg-pe'}">${t.leg}</span></td>
            <td><b>${t.strike}</b></td>
            <td>₹${parseFloat(t.entry || 0).toFixed(2)}</td>
            <td>₹${parseFloat(t.exit || 0).toFixed(2)}</td>
            <td style="color:var(--text-muted); font-size:0.75rem;">${t.reason || 'SQUARE_OFF'}</td>
            <td style="text-align:right;" class="${pnlClass}"><b>${fmtINR(t.pnl, true)}</b></td>
          </tr>
        `;
      }).join('');
    }

    function updateDashboard(data) {
      if (!data) return;

      // Clock
      if (data.timestamp_ist) {
        document.getElementById('live-clock').innerText = data.timestamp_ist.split(' ')[1] + ' IST';
      }

      // KPI Performance
      const p = data.performance || {};
      const netMtm = p.combined_net_mtm || 0.0;
      const netPct = p.combined_net_pct || 0.0;

      const netEl = document.getElementById('net-mtm-val');
      netEl.innerText = fmtINR(netMtm, true);
      applyClass(netEl, netMtm);

      const netPctEl = document.getElementById('net-mtm-pct');
      netPctEl.innerText = fmtPct(netPct);
      netPctEl.className = 'tag-pct ' + (netPct >= 0 ? 'pos' : 'neg');

      const realEl = document.getElementById('realized-val');
      realEl.innerText = fmtINR(p.combined_realized, true);
      applyClass(realEl, p.combined_realized);
      document.getElementById('realized-tag').innerText = fmtINR(p.combined_realized, true);

      const unrealEl = document.getElementById('unrealized-val');
      unrealEl.innerText = fmtINR(p.combined_unrealized, true);
      applyClass(unrealEl, p.combined_unrealized);

      document.getElementById('capital-val').innerText = fmtINR(p.current_capital);
      document.getElementById('circuit-val').innerText = fmtINR(p.circuit_limit);

      const usedPct = p.circuit_used_pct || 0;
      document.getElementById('circuit-fill-bar').style.width = `${Math.min(100, usedPct)}%`;
      document.getElementById('circuit-used-text').innerText = `${usedPct.toFixed(1)}% Used`;

      const mtdEl = document.getElementById('mtd-val');
      mtdEl.innerText = fmtINR(p.mtd_pnl, true);
      applyClass(mtdEl, p.mtd_pnl);

      const ytdEl = document.getElementById('ytd-val');
      ytdEl.innerText = fmtINR(p.ytd_pnl, true);
      applyClass(ytdEl, p.ytd_pnl);

      document.getElementById('trades-count-pill').innerText = `${p.total_trades || 0} TRADES`;

      // ── NIFTY TAB DATA ──
      const n = data.nifty || {};
      document.getElementById('n-spot').innerText = n.spot ? n.spot.toFixed(2) : '--';
      document.getElementById('n-atm').innerText = n.atm || '--';
      document.getElementById('n-adx').innerText = n.adx ? n.adx.toFixed(1) : '--';
      document.getElementById('n-regime').innerText = n.regime || '--';
      document.getElementById('n-kama').innerText = n.kama || '--';
      document.getElementById('n-trend').innerText = n.trend || '--';
      document.getElementById('n-atr').innerText = n.atr ? n.atr.toFixed(1) : '--';
      document.getElementById('n-signal').innerText = n.signal || '--';

      const nNetEl = document.getElementById('n-net-pnl');
      nNetEl.innerText = fmtINR(n.net_pnl, true);
      applyClass(nNetEl, n.net_pnl);
      document.getElementById('n-net-pct').innerText = fmtPct(n.net_pct);
      document.getElementById('n-trades').innerText = n.trades_today || 0;
      document.getElementById('n-realized').innerText = fmtINR(n.realized_pnl, true);
      document.getElementById('n-unrealized').innerText = fmtINR(n.unrealized_pnl, true);

      const nChip = document.getElementById('nifty-status-chip');
      nChip.className = 'status-chip ' + (n.active ? 'chip-green' : 'chip-dim');
      nChip.innerText = n.active ? 'ENGINE ACTIVE' : (n.mode || 'STANDBY');

      const sys = data.system || {};
      const nSess = document.getElementById('nifty-session-chip');
      nSess.className = 'status-chip ' + (sys.nifty_session_active ? 'chip-amber' : 'chip-dim');
      nSess.innerText = sys.nifty_session_active ? 'MARKET OPEN' : 'SESSION: 09:15 - 15:35';

      document.getElementById('n-pos-count').innerText = (n.positions || []).length;
      document.getElementById('n-pos-tbody').innerHTML = renderPositionsRows(n.positions);
      document.getElementById('n-trades-tbody').innerHTML = renderTradesRows(n.trades);

      // ── MCX TAB DATA ──
      const m = data.mcx || {};
      document.getElementById('m-spot').innerText = m.spot ? m.spot.toFixed(2) : '--';
      document.getElementById('m-atm').innerText = m.atm || '--';
      document.getElementById('m-expiry').innerText = m.expiry + (m.is_rolled_over ? ' (ROLL)' : '');
      document.getElementById('m-signal').innerText = m.signal || '--';
      document.getElementById('m-ema15').innerText = m.ema15 ? m.ema15.toFixed(2) : '--';
      document.getElementById('m-ema90').innerText = m.ema90 ? m.ema90.toFixed(2) : '--';
      document.getElementById('m-slope').innerText = (m.slow_slope !== undefined) ? m.slow_slope.toFixed(3) : '--';
      document.getElementById('m-vr').innerText = m.vr ? m.vr.toFixed(2) : '--';

      const mNetEl = document.getElementById('m-net-pnl');
      mNetEl.innerText = fmtINR(m.net_pnl, true);
      applyClass(mNetEl, m.net_pnl);
      document.getElementById('m-net-pct').innerText = fmtPct(m.net_pct);
      document.getElementById('m-trades').innerText = m.trades_today || 0;
      document.getElementById('m-reversal').innerText = m.reversal_latched ? '⚡ LATCHED' : 'INACTIVE';
      document.getElementById('m-reversal').style.color = m.reversal_latched ? 'var(--amber)' : 'var(--text-dim)';
      document.getElementById('m-cooldown').innerText = `${m.cooldown_remaining || 0}s`;

      const mChip = document.getElementById('mcx-status-chip');
      mChip.className = 'status-chip ' + (m.active ? 'chip-green' : 'chip-dim');
      mChip.innerText = m.active ? 'ENGINE ACTIVE' : 'STANDBY';

      const mSess = document.getElementById('mcx-session-chip');
      mSess.className = 'status-chip ' + (sys.mcx_session_active ? 'chip-amber' : 'chip-dim');
      mSess.innerText = sys.mcx_session_active ? 'MARKET OPEN' : 'SESSION: 15:30 - 23:25';

      document.getElementById('m-pos-count').innerText = (m.positions || []).length;
      document.getElementById('m-pos-tbody').innerHTML = renderPositionsRows(m.positions);
      document.getElementById('m-trades-tbody').innerHTML = renderTradesRows(m.trades);

      // ── OVERVIEW TAB DATA ──
      const allPos = data.positions || [];
      document.getElementById('all-pos-count').innerText = allPos.length;
      document.getElementById('all-pos-tbody').innerHTML = renderPositionsRows(allPos);

      const histMap = p.daily_history || {};
      const histBox = document.getElementById('history-cards-box');
      const histKeys = Object.keys(histMap).sort().reverse();
      if (histKeys.length > 0) {
        histBox.innerHTML = histKeys.map(k => {
          const v = histMap[k];
          const c = v >= 0 ? 'positive' : 'negative';
          return `
            <div class="hist-box">
              <div class="hist-title">${k}</div>
              <div class="hist-num ${c}">${fmtINR(v, true)}</div>
            </div>
          `;
        }).join('');
      }
    }

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
            console.error('SSE Error', err);
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

      // Reliable fallback polling
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
      fetch('/api/status')
        .then(res => res.json())
        .then(data => updateDashboard(data))
        .catch(() => {});
      initSSE();
    });
  </script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────────────────────────────
class DashboardHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
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
