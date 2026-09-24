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
import hashlib
import shutil
import requests
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

IST = timezone(timedelta(hours=5, minutes=30))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = CURRENT_DIR

def get_ist_now() -> datetime:
    return datetime.now(IST)

PUBLIC_URL_FILE = os.path.join(PROJECT_ROOT, "public_url.txt")
TUNNEL_LOG_FILE = os.path.join(PROJECT_ROOT, "tunnel.log")
LIVE_PUBLIC_URL = ""

# ─────────────────────────────────────────────────────────────────────────────
# FLATTRADE AUTHENTICATION & TOKEN SERVICES
# ─────────────────────────────────────────────────────────────────────────────
def get_flattrade_creds():
    try:
        import creds
        return str(creds.API_KEY).strip(), str(creds.API_SECRET).strip(), getattr(creds, "USER_ID", "FZ04111")
    except Exception:
        return "fa5da7cfc3d7459298efeb11d87bab41", "2026.95f32fed8e9f4fa185d572667d1c256e799e1c26d797abcd", "FZ04111"

def extract_request_code(raw_input: str) -> str:
    raw = (raw_input or "").strip()
    if not raw:
        return ""
    if "code=" in raw or "request_code=" in raw or "http://" in raw or "https://" in raw:
        try:
            parsed = urlparse(raw if "://" in raw else f"http://dummy/{raw.lstrip('?')}")
            qs = parse_qs(parsed.query)
            if "code" in qs and qs["code"]:
                return qs["code"][0].strip()
            if "request_code" in qs and qs["request_code"]:
                return qs["request_code"][0].strip()
        except Exception:
            pass
        m = re.search(r"[?&](?:code|request_code)=([a-zA-Z0-9_-]+)", raw)
        if m:
            return m.group(1).strip()
    return raw

def exchange_flattrade_token(url_or_code: str, multiplier: int = 1) -> dict:
    code = extract_request_code(url_or_code)
    if not code:
        return {"status": "error", "message": "No valid request_code or URL detected. Please paste the full redirect URL or code."}

    api_key, api_secret, user_id = get_flattrade_creds()
    raw_token_str = f"{api_key}{code}{api_secret}"
    token_hash = hashlib.sha256(raw_token_str.encode("utf-8")).hexdigest()

    url = "https://authapi.flattrade.in/trade/apitoken"
    payload = {
        "api_key": api_key,
        "request_code": code,
        "api_secret": token_hash
    }

    try:
        resp = requests.post(url, json=payload, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token")
            if token:
                token_file = os.path.join(PROJECT_ROOT, "token.txt")
                with open(token_file, "w") as f:
                    f.write(token.strip())
                if multiplier > 0:
                    mult_file = os.path.join(PROJECT_ROOT, "multiplier.txt")
                    with open(mult_file, "w") as f:
                        f.write(str(int(multiplier)))
                return {
                    "status": "success",
                    "message": "Flattrade session token generated and saved to token.txt successfully!",
                    "token_preview": token[:10] + "..." + token[-6:],
                    "timestamp": get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
                }
            else:
                emsg = data.get("emsg", data.get("message", resp.text))
                return {"status": "error", "message": f"Flattrade rejected code: {emsg}"}
        else:
            return {"status": "error", "message": f"HTTP {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Network error: {str(e)}"}
def get_flattrade_token_status() -> dict:
    token_file = os.path.join(PROJECT_ROOT, "token.txt")
    exists = os.path.exists(token_file) and os.path.getsize(token_file) > 10
    preview = "Not Generated"
    mtime_str = "Never"
    is_today = False
    api_key, _, user_id = get_flattrade_creds()

    if exists:
        try:
            with open(token_file, "r") as f:
                t = f.read().strip()
                if len(t) >= 12:
                    preview = t[:8] + "..." + t[-4:]
            mtime = os.path.getmtime(token_file)
            m_dt = datetime.fromtimestamp(mtime, tz=IST)
            mtime_str = m_dt.strftime("%Y-%m-%d %H:%M IST")
            is_today = (m_dt.date() == get_ist_now().date())
        except Exception:
            pass

    auth_url = f"https://auth.flattrade.in/?app_key={api_key}"
    return {
        "token_exists": exists,
        "token_preview": preview,
        "last_updated": mtime_str,
        "is_today": is_today,
        "user_id": user_id,
        "auth_url": auth_url
    }

def check_process_running(pattern: str) -> bool:
    try:
        res = subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False

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

def _snapshot_date(snapshot: dict):
    """Return the trading date encoded in a snapshot, when available."""
    for key in ("date", "trading_date", "session_date"):
        value = snapshot.get(key)
        if value:
            try:
                return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
            except ValueError:
                pass

    for key in ("timestamp", "updated_at"):
        value = snapshot.get(key)
        if value:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass
    return None

def load_current_snapshot(relative_paths):
    """Load the first snapshot belonging to today's IST trading session."""
    today = get_ist_now().date()
    for relative_path in relative_paths:
        filepath = os.path.join(PROJECT_ROOT, relative_path)
        snapshot = load_json_safe(filepath)
        if not isinstance(snapshot, dict):
            continue

        snapshot_date = _snapshot_date(snapshot)
        if snapshot_date is None:
            # Older snapshot formats only expose a clock. Use file mtime as a
            # safe fallback so an old session cannot leak into today's view.
            try:
                snapshot_date = datetime.fromtimestamp(
                    os.path.getmtime(filepath), tz=IST
                ).date()
            except OSError:
                continue

        if snapshot_date == today:
            return snapshot
    return None

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
    return load_current_snapshot([
        "tradingbot/data/state/live_snapshot_v2_paper.json",
        "tradingbot/data/state/algo_state_v2_paper.json",
        "data/state/live_snapshot_v2_paper.json",
        "data/state/algo_state_v2_paper.json",
        "tradingbot/data/state/live_snapshot_v2.json",
        "tradingbot/data/state/algo_state_v2.json",
        "data/state/live_snapshot_v2.json",
        "data/state/algo_state_v2.json",
    ])

def get_mcx_snapshot():
    return load_current_snapshot([
        "tradingbot/live_snapshot_mcx_paper.json",
        "tradingbot/mcx_state_paper_v5.json",
        "live_snapshot_mcx_paper.json",
        "mcx_state_paper_v5.json",
        "tradingbot/mcx_state_paper.json",
        "mcx_state_paper.json",
    ])

# ─────────────────────────────────────────────────────────────────────────────
# INTRADAY PNL TIME-SERIES ENGINE (09:15 - SESSION CLOSE)
# ─────────────────────────────────────────────────────────────────────────────
INTRADAY_PNL_SERIES = []
INTRADAY_SERIES_DATE = None

def update_intraday_pnl_series(current_net_mtm: float, net_pct: float):
    global INTRADAY_PNL_SERIES, INTRADAY_SERIES_DATE
    now = get_ist_now()
    today_str = str(now.date())

    if INTRADAY_SERIES_DATE != today_str:
        INTRADAY_SERIES_DATE = today_str
        INTRADAY_PNL_SERIES = [{
            "time": "09:15",
            "pnl": 0.0,
            "pct": 0.0,
            "ts": now.replace(hour=9, minute=15, second=0).timestamp()
        }]

    now_ts = now.timestamp()
    if not INTRADAY_PNL_SERIES or (now_ts - INTRADAY_PNL_SERIES[-1].get("ts", 0)) >= 4.0:
        INTRADAY_PNL_SERIES.append({
            "time": now.strftime("%H:%M:%S"),
            "time_short": now.strftime("%H:%M"),
            "pnl": round(current_net_mtm, 2),
            "pct": round(net_pct, 2),
            "ts": now_ts
        })
        if len(INTRADAY_PNL_SERIES) > 1500:
            INTRADAY_PNL_SERIES.pop(0)

    return INTRADAY_PNL_SERIES

def get_history_analytics():
    pnl_data = get_pnl_tracker_data()
    daily_map = dict(pnl_data.get("daily_pnl", {}))

    trade_csv = os.path.join(PROJECT_ROOT, "data", "logs", "trade_book", "trades_v2_paper.csv")
    if os.path.exists(trade_csv):
        try:
            import csv
            with open(trade_csv, "r", encoding="utf-8") as f:
                r = csv.DictReader(f)
                for row in r:
                    ts = row.get("timestamp", "")
                    pnl = row.get("pnl", "")
                    if ts and pnl:
                        dt_str = ts.split()[0]
                        if dt_str not in daily_map:
                            try:
                                daily_map[dt_str] = round(daily_map.get(dt_str, 0.0) + float(pnl), 2)
                            except Exception:
                                pass
        except Exception:
            pass

    all_dates = sorted(daily_map.keys())
    last_30_dates = all_dates[-30:] if len(all_dates) > 30 else all_dates

    last_30_days = []
    day_of_week_map = {
        "Monday": {"total_pnl": 0.0, "count": 0, "wins": 0},
        "Tuesday": {"total_pnl": 0.0, "count": 0, "wins": 0},
        "Wednesday": {"total_pnl": 0.0, "count": 0, "wins": 0},
        "Thursday": {"total_pnl": 0.0, "count": 0, "wins": 0},
        "Friday": {"total_pnl": 0.0, "count": 0, "wins": 0}
    }
    month_map = {}

    for d_str in reversed(last_30_dates):
        val = float(daily_map[d_str])
        pct_2l = (val / 200000.0) * 100.0
        try:
            dt = datetime.strptime(d_str, "%Y-%m-%d")
            weekday = dt.strftime("%A")
            month_key = dt.strftime("%B %Y")
        except Exception:
            weekday = "Weekday"
            month_key = "Recent"

        last_30_days.append({
            "date": d_str,
            "day": weekday,
            "pnl": val,
            "pct": pct_2l,
            "win": (val >= 0)
        })

        if weekday in day_of_week_map:
            day_of_week_map[weekday]["total_pnl"] += val
            day_of_week_map[weekday]["count"] += 1
            if val >= 0:
                day_of_week_map[weekday]["wins"] += 1

        if month_key not in month_map:
            month_map[month_key] = {"total_pnl": 0.0, "days": 0, "wins": 0}
        month_map[month_key]["total_pnl"] += val
        month_map[month_key]["days"] += 1
        if val >= 0:
            month_map[month_key]["wins"] += 1

    dow_list = []
    for day_name in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
        item = day_of_week_map[day_name]
        cnt = item["count"]
        pnl = item["total_pnl"]
        pct = (pnl / 200000.0) * 100.0
        win_rate = (item["wins"] / cnt * 100.0) if cnt > 0 else 0.0
        dow_list.append({
            "day": day_name,
            "total_pnl": pnl,
            "pct": pct,
            "count": cnt,
            "win_rate": win_rate
        })

    month_list = []
    for m_name, m_data in month_map.items():
        pnl = m_data["total_pnl"]
        pct = (pnl / 200000.0) * 100.0
        cnt = m_data["days"]
        wr = (m_data["wins"] / cnt * 100.0) if cnt > 0 else 0.0
        month_list.append({
            "month": m_name,
            "total_pnl": pnl,
            "pct": pct,
            "days": cnt,
            "win_rate": wr
        })

    return {
        "last_30_days": last_30_days,
        "day_of_week": dow_list,
        "monthwise": month_list
    }

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
        "recent_trades": all_trades[:35],
        "intraday_series": update_intraday_pnl_series(combined_net, combined_net_pct),
        "history_analytics": get_history_analytics()
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLOUDFLARE QUICK TUNNEL MANAGER
# ─────────────────────────────────────────────────────────────────────────────
def send_telegram_url_alert(tunnel_url: str = ""):
    bot_token = "8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE"
    chat_id = "6307066850"
    perm_url = "http://54.162.151.193:8000"
    msg = (
        f"👑 *FLATTRADE ALGO PRO DASHBOARD IS LIVE*\n\n"
        f"॥ जय श्री कृष्ण ॥\n\n"
        f"🔒 *Permanent Fixed URL (Never Dies):*\n`{perm_url}`\n\n"
        f"👉 [Click Here To Open Permanent Dashboard]({perm_url})\n\n"
    )
    if tunnel_url:
        msg += f"🌐 *Cloudflare Mirror:*\n`{tunnel_url}`\n\n"
    msg += f"📊 Track real-time NIFTY & MCX performance, live charts, and activate tokens."
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception:
        pass

def extract_url_from_tunnel_log():
    if not os.path.exists(TUNNEL_LOG_FILE):
        return None
    try:
        with open(TUNNEL_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            matches = re.findall(r"https://[-a-zA-Z0-9.]*trycloudflare\.com", content)
            if matches:
                return matches[-1]
    except Exception:
        pass
    return None

def run_tunnel_manager(port: int = 8000):
    global LIVE_PUBLIC_URL
    last_notified_url = ""

    # The tunnel is optional. Do not wake up every few seconds with a failed
    # subprocess attempt when cloudflared is not installed on the host.
    if shutil.which("cloudflared") is None:
        print("[TUNNEL] cloudflared is not installed; using the direct dashboard URL.", flush=True)
        return

    # Load initial URL from public_url.txt if present
    if os.path.exists(PUBLIC_URL_FILE):
        try:
            with open(PUBLIC_URL_FILE, "r") as f:
                saved = f.read().strip()
                if saved.startswith("https://") and "trycloudflare.com" in saved:
                    LIVE_PUBLIC_URL = saved
                    last_notified_url = saved
        except Exception:
            pass

    while True:
        try:
            is_running = check_process_running("cloudflared tunnel")

            url_from_log = extract_url_from_tunnel_log()
            if url_from_log and url_from_log != LIVE_PUBLIC_URL:
                LIVE_PUBLIC_URL = url_from_log
                print(f"[TUNNEL] Active URL established: {LIVE_PUBLIC_URL}", flush=True)
                try:
                    with open(PUBLIC_URL_FILE, "w") as f:
                        f.write(f"{LIVE_PUBLIC_URL}\n")
                except Exception:
                    pass

            if not is_running:
                print(f"[TUNNEL] Launching detached Cloudflare tunnel on port {port}...", flush=True)
                subprocess.Popen(
                    ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}", "--logfile", TUNNEL_LOG_FILE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                time.sleep(3)
                continue

            if LIVE_PUBLIC_URL and LIVE_PUBLIC_URL != last_notified_url:
                last_notified_url = LIVE_PUBLIC_URL
                print(f"[TUNNEL] Notifying Telegram of URL: {LIVE_PUBLIC_URL}", flush=True)
                send_telegram_url_alert(LIVE_PUBLIC_URL)

        except Exception as e:
            print(f"[TUNNEL ERROR] {e}", flush=True)

        time.sleep(3)


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
      --bg: #060911;
      --card-bg: rgba(14, 21, 37, 0.76);
      --card-border: rgba(255, 255, 255, 0.12);
      --card-hover: rgba(56, 189, 248, 0.3);
      --primary: #38bdf8;
      --primary-glow: rgba(56, 189, 248, 0.35);
      --green: #10b981;
      --green-glow: rgba(16, 185, 129, 0.4);
      --red: #f43f5e;
      --red-glow: rgba(244, 63, 94, 0.4);
      --amber: #f59e0b;
      --amber-glow: rgba(245, 158, 11, 0.35);
      --gold: #fbbf24;
      --gold-bright: #fde047;
      --gold-glow: rgba(251, 191, 36, 0.45);
      --purple: #c084fc;
      --purple-glow: rgba(192, 132, 252, 0.35);
      --indigo: #818cf8;
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
        radial-gradient(at 15% 0%, rgba(30, 58, 138, 0.22) 0px, transparent 50%),
        radial-gradient(at 85% 0%, rgba(217, 119, 6, 0.14) 0px, transparent 45%),
        radial-gradient(at 50% 100%, rgba(88, 28, 135, 0.16) 0px, transparent 60%);
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

    /* ─── Royal Auspicious Bar (Jay Shri Krishna) ─── */
    .royal-banner-wrap {
      display: flex;
      justify-content: center;
      margin-bottom: 14px;
    }

    .royal-auspicious-bar {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      padding: 7px 24px;
      background: linear-gradient(135deg, rgba(251, 191, 36, 0.14), rgba(15, 23, 42, 0.85), rgba(245, 158, 11, 0.14));
      border: 1px solid rgba(251, 191, 36, 0.38);
      border-radius: 999px;
      backdrop-filter: blur(28px) saturate(190%);
      -webkit-backdrop-filter: blur(28px) saturate(190%);
      box-shadow: 0 8px 28px -4px rgba(245, 158, 11, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.25);
      animation: royalGlowPulse 4s infinite ease-in-out;
    }

    @keyframes royalGlowPulse {
      0%, 100% { box-shadow: 0 8px 28px -4px rgba(245, 158, 11, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.2); }
      50% { box-shadow: 0 10px 36px 0px rgba(251, 191, 36, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.35); border-color: rgba(251, 191, 36, 0.55); }
    }

    .royal-krishna-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-family: var(--display);
      font-weight: 800;
      font-size: 0.96rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      background: linear-gradient(135deg, #fffbeb 0%, #fef08a 35%, #f59e0b 70%, #d97706 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      filter: drop-shadow(0 2px 10px rgba(245, 158, 11, 0.45));
    }

    .krishna-symbol {
      font-size: 1.15rem;
      -webkit-text-fill-color: initial;
      filter: drop-shadow(0 0 10px rgba(251, 191, 36, 0.6));
    }

    .royal-star {
      color: #fbbf24;
      font-size: 0.72rem;
      opacity: 0.85;
      animation: royalStarTwinkle 2.4s infinite ease-in-out;
    }

    @keyframes royalStarTwinkle {
      0%, 100% { opacity: 0.35; transform: scale(0.85); }
      50% { opacity: 1; transform: scale(1.2); filter: drop-shadow(0 0 8px #fbbf24); }
    }

    .royal-pill {
      font-family: var(--display);
      font-size: 0.74rem;
      font-weight: 800;
      padding: 3px 10px;
      border-radius: 999px;
      background: linear-gradient(135deg, rgba(251, 191, 36, 0.16), rgba(245, 158, 11, 0.22));
      border: 1px solid rgba(251, 191, 36, 0.45);
      color: #fde047;
      letter-spacing: 0.04em;
      box-shadow: 0 0 14px rgba(245, 158, 11, 0.3);
    }

    /* ─── Apple MacBook Glass Solid Header ─── */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 14px 22px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      backdrop-filter: blur(28px) saturate(190%);
      -webkit-backdrop-filter: blur(28px) saturate(190%);
      margin-bottom: 18px;
      box-shadow: 0 16px 40px -12px rgba(0,0,0,0.65), inset 0 1px 0 rgba(255, 255, 255, 0.12);
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
      box-shadow: 0 4px 20px rgba(2, 132, 199, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.2);
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
      background: rgba(16, 185, 129, 0.14);
      border: 1px solid rgba(16, 185, 129, 0.4);
      border-radius: 999px;
      color: var(--green);
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.1);
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
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      color: #e2e8f0;
      backdrop-filter: blur(16px);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
    }

    .btn-action {
      background: rgba(255, 255, 255, 0.06);
      color: var(--text);
      border: 1px solid rgba(255, 255, 255, 0.14);
      padding: 7px 15px;
      border-radius: 12px;
      font-size: 0.8rem;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      backdrop-filter: blur(16px);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.1);
      transition: all 0.22s cubic-bezier(0.16, 1, 0.3, 1);
    }

    .btn-action:hover {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(255, 255, 255, 0.28);
      transform: translateY(-1px);
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.2);
    }

    /* ─── Apple MacBook Glass Solid Segmented Tabs ─── */
    .segmented-tabs-bar {
      display: flex;
      justify-content: center;
      margin-bottom: 20px;
    }

    .segmented-tabs {
      display: inline-flex;
      background: rgba(16, 24, 40, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.12);
      padding: 5px;
      border-radius: 16px;
      gap: 6px;
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55), inset 0 1px 1px rgba(255, 255, 255, 0.1);
      backdrop-filter: blur(28px) saturate(190%);
      -webkit-backdrop-filter: blur(28px) saturate(190%);
      max-width: 100%;
      overflow-x: auto;
    }

    .seg-tab {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      padding: 10px 22px;
      border-radius: 12px;
      font-weight: 700;
      font-size: 0.92rem;
      cursor: pointer;
      transition: all 0.22s cubic-bezier(0.16, 1, 0.3, 1);
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }

    .seg-tab:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.06);
    }

    .seg-tab.active-nifty {
      background: rgba(56, 189, 248, 0.18);
      color: #fff;
      border: 1px solid rgba(56, 189, 248, 0.45);
      box-shadow: 0 4px 18px rgba(56, 189, 248, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    }

    .seg-tab.active-mcx {
      background: rgba(245, 158, 11, 0.18);
      color: #fff;
      border: 1px solid rgba(245, 158, 11, 0.45);
      box-shadow: 0 4px 18px rgba(245, 158, 11, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    }

    .seg-tab.active-overview {
      background: rgba(168, 85, 247, 0.18);
      color: #fff;
      border: 1px solid rgba(168, 85, 247, 0.45);
      box-shadow: 0 4px 18px rgba(168, 85, 247, 0.22), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    }

    /* ─── Apple MacBook Glass Solid KPI Cards ─── */
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
      backdrop-filter: blur(28px) saturate(190%);
      -webkit-backdrop-filter: blur(28px) saturate(190%);
      position: relative;
      overflow: hidden;
      box-shadow: 0 14px 34px -10px rgba(0, 0, 0, 0.65), inset 0 1px 0 rgba(255, 255, 255, 0.11);
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    }

    .kpi-card:hover {
      border-color: rgba(255, 255, 255, 0.24);
      transform: translateY(-2px);
      box-shadow: 0 20px 42px -10px rgba(0, 0, 0, 0.75), inset 0 1px 0 rgba(255, 255, 255, 0.2);
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
      backdrop-filter: blur(28px) saturate(190%);
      -webkit-backdrop-filter: blur(28px) saturate(190%);
      box-shadow: 0 16px 40px -12px rgba(0,0,0,0.65), inset 0 1px 0 rgba(255, 255, 255, 0.11);
      margin-bottom: 22px;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
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

    /* ─── Comprehensive High-End Responsive Mobile Architecture ─── */
    @media (max-width: 768px) {
      .container { padding: 10px 8px 30px; }
      header {
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 14px;
        position: sticky;
        top: 8px;
        z-index: 1000;
      }
      .brand-wrap {
        justify-content: flex-start;
        flex: 1;
        min-width: 0;
      }
      .brand-title { font-size: 1.05rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .brand-subtitle { font-size: 0.65rem; }
      .header-ctrls {
        display: none !important; /* Managed seamlessly inside Hamburger drawer on mobile */
      }

      .royal-banner-wrap { margin-bottom: 12px; }
      .royal-auspicious-bar { padding: 6px 14px; }
      .royal-krishna-badge { font-size: 0.82rem; letter-spacing: 0.05em; }

      .segmented-tabs-bar {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        padding-bottom: 6px;
        justify-content: flex-start;
      }
      .segmented-tabs {
        width: 100%;
        display: flex;
        justify-content: space-between;
        padding: 4px;
        gap: 4px;
      }
      .seg-tab {
        flex: 1;
        padding: 8px 10px;
        font-size: 0.78rem;
        white-space: nowrap;
        text-align: center;
        justify-content: center;
      }

      .kpi-row {
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
        margin-bottom: 16px;
      }
      .kpi-card { padding: 14px; }
      .kpi-val { font-size: 1.45rem; }
      .kpi-sub { font-size: 0.72rem; }

      .panel-box { padding: 14px; border-radius: 16px; margin-bottom: 16px; }
      .panel-hdr { padding-bottom: 12px; margin-bottom: 14px; }
      .panel-title { font-size: 0.98rem; }

      .telemetry-grid {
        grid-template-columns: repeat(2, 1fr);
        gap: 8px;
      }
      .tel-item { padding: 10px 12px; }
      .tel-val { font-size: 1rem; }

      .history-cards-flex {
        grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
        gap: 8px;
      }

      .dow-bar-track { height: 110px; }
      .dow-bar-column { min-width: 48px; }
    }

    @media (max-width: 480px) {
      .container { padding: 8px 6px 24px; }
      .brand-title { font-size: 0.98rem; }
      .royal-krishna-badge { font-size: 0.74rem; }
      .kpi-row {
        grid-template-columns: 1fr;
        gap: 10px;
      }
      .kpi-val { font-size: 1.55rem; }
      .telemetry-grid {
        grid-template-columns: repeat(2, 1fr);
      }
      .segmented-tabs {
        display: flex;
        width: max-content;
      }
      .history-cards-flex {
        grid-template-columns: 1fr;
      }
      .dow-bar-track { height: 95px; }
      .dow-bar-column { min-width: 40px; }
    }

    /* ─── Ultra-Cool Animations & Chart Effects ─── */
    @keyframes radarPulse {
      0% { transform: scale(0.9); opacity: 0.9; box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { transform: scale(1.6); opacity: 0; box-shadow: 0 0 0 16px rgba(16, 185, 129, 0); }
      100% { transform: scale(0.9); opacity: 0; box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }

    @keyframes laserSweep {
      0% { transform: translateX(-100%); opacity: 0.1; }
      50% { opacity: 0.7; }
      100% { transform: translateX(100%); opacity: 0.1; }
    }

    @keyframes neonBorderSweep {
      0% { border-color: rgba(56, 189, 248, 0.2); }
      50% { border-color: rgba(168, 85, 247, 0.5); }
      100% { border-color: rgba(56, 189, 248, 0.2); }
    }

    .pulse-beacon {
      animation: radarPulse 1.8s infinite ease-in-out;
    }

    .laser-line {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 2px;
      background: linear-gradient(to bottom, transparent, var(--primary), #fff, var(--primary), transparent);
      box-shadow: 0 0 15px var(--primary);
      pointer-events: none;
    }

    /* Day of Week Animated Bar */
    .dow-bar-column {
      display: flex;
      flex-direction: column;
      align-items: center;
      flex: 1;
      min-width: 60px;
    }

    .dow-bar-track {
      width: 100%;
      height: 140px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 12px;
      display: flex;
      align-items: flex-end;
      padding: 6px;
      position: relative;
      overflow: hidden;
    }

    .dow-bar-fill {
      width: 100%;
      border-radius: 8px;
      transition: height 0.8s cubic-bezier(0.34, 1.56, 0.64, 1);
      position: relative;
      box-shadow: 0 0 16px rgba(56, 189, 248, 0.3);
    }

    /* Scrollable 30D Feed */
    .scroll-feed-30d {
      display: flex;
      gap: 12px;
      overflow-x: auto;
      padding-bottom: 12px;
      scrollbar-width: thin;
      scrollbar-color: rgba(56, 189, 248, 0.35) rgba(255, 255, 255, 0.02);
    }
    .scroll-feed-30d::-webkit-scrollbar { height: 6px; }
    .scroll-feed-30d::-webkit-scrollbar-thumb { background: rgba(56, 189, 248, 0.4); border-radius: 999px; }

    .day-feed-card {
      min-width: 165px;
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 14px;
      font-family: var(--mono);
      transition: all 0.25s ease;
      flex-shrink: 0;
    }
    .day-feed-card:hover {
      border-color: rgba(56, 189, 248, 0.4);
      transform: translateY(-3px);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
    }

    /* ─── Premium Mobile Hamburger Menu & Slide-out Drawer ─── */
    .btn-hamburger {
      display: none;
      width: 42px;
      height: 42px;
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 12px;
      color: #fff;
      cursor: pointer;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 5px;
      padding: 9px;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.15);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      transition: all 0.2s ease;
      flex-shrink: 0;
    }
    .btn-hamburger:hover {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(56, 189, 248, 0.4);
    }
    .btn-hamburger span {
      display: block;
      width: 100%;
      height: 2px;
      background: #e2e8f0;
      border-radius: 2px;
      transition: transform 0.28s ease, opacity 0.28s ease;
    }
    .btn-hamburger.active span:nth-child(1) {
      transform: translateY(7px) rotate(45deg);
    }
    .btn-hamburger.active span:nth-child(2) {
      opacity: 0;
    }
    .btn-hamburger.active span:nth-child(3) {
      transform: translateY(-7px) rotate(-45deg);
    }

    /* Mobile Drawer Overlay */
    .mobile-drawer-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      z-index: 99990;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .mobile-drawer-overlay.active {
      opacity: 1;
      pointer-events: auto;
    }

    /* Mobile Drawer Panel */
    .mobile-drawer {
      position: fixed;
      top: 0;
      right: 0;
      bottom: 0;
      width: 84%;
      max-width: 320px;
      background: rgba(14, 21, 37, 0.96);
      border-left: 1px solid rgba(255, 255, 255, 0.14);
      box-shadow: -15px 0 45px rgba(0, 0, 0, 0.85);
      backdrop-filter: blur(36px) saturate(200%);
      -webkit-backdrop-filter: blur(36px) saturate(200%);
      z-index: 99995;
      transform: translateX(105%);
      transition: transform 0.32s cubic-bezier(0.16, 1, 0.3, 1);
      display: flex;
      flex-direction: column;
      padding: 22px 18px;
      overflow-y: auto;
    }
    .mobile-drawer.active {
      transform: translateX(0);
    }

    .drawer-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 16px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      margin-bottom: 20px;
    }
    .drawer-title {
      font-family: var(--display);
      font-weight: 800;
      font-size: 1.05rem;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .drawer-close {
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.12);
      color: var(--text-dim);
      width: 32px;
      height: 32px;
      border-radius: 50%;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1rem;
    }

    .drawer-nav-section {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-bottom: 24px;
    }
    .drawer-nav-btn {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 13px 16px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      color: #cbd5e1;
      font-family: var(--display);
      font-size: 0.94rem;
      font-weight: 700;
      cursor: pointer;
      text-align: left;
      transition: all 0.2s ease;
    }
    .drawer-nav-btn:hover, .drawer-nav-btn.active {
      background: rgba(56, 189, 248, 0.16);
      border-color: rgba(56, 189, 248, 0.45);
      color: #fff;
    }
    .drawer-nav-btn.active-nifty {
      background: rgba(56, 189, 248, 0.2);
      border-color: var(--primary);
      color: #fff;
    }
    .drawer-nav-btn.active-mcx {
      background: rgba(245, 158, 11, 0.2);
      border-color: var(--amber);
      color: #fff;
    }
    .drawer-nav-btn.active-overview {
      background: rgba(168, 85, 247, 0.2);
      border-color: #c084fc;
      color: #fff;
    }

    .drawer-actions {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: auto;
      padding-top: 18px;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
    }
    .drawer-action-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 12px;
      border-radius: 12px;
      font-family: var(--display);
      font-weight: 700;
      font-size: 0.88rem;
      cursor: pointer;
    }

    @media (max-width: 768px) {
      .btn-hamburger { display: flex; }
      .segmented-tabs-bar { display: none !important; }
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Royal Auspicious Banner: Jay Shri Krishna -->
    <div class="royal-banner-wrap">
      <div class="royal-auspicious-bar">
        <span class="royal-star">✦</span>
        <div class="royal-krishna-badge">
          <span class="krishna-symbol">🪷</span>
          <span class="krishna-title">॥ जय श्री कृष्ण ॥ &bull; JAY SHRI KRISHNA</span>
          <span class="krishna-symbol">🪷</span>
        </div>
        <span class="royal-star">✦</span>
      </div>
    </div>

    <!-- Top Header -->
    <header>
      <div class="brand-wrap">
        <div class="brand-logo">⚡</div>
        <div>
          <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
            <div class="brand-title">FLATTRADE QUANT TERMINAL</div>
            <span class="royal-pill">॥ जय श्री कृष्ण ॥</span>
          </div>
          <div class="brand-subtitle">
            <span>HIGH-PRECISION ALGORITHMIC ARCHITECTURE</span>
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
        <button class="btn-action" id="btn-auth-header" onclick="openAuthModal()" style="border-color:rgba(168, 85, 247, 0.4); background:rgba(168, 85, 247, 0.12); color:#c084fc;">
          <span>🔑</span> <span id="auth-header-text">Broker Auth</span>
        </button>
        <button class="btn-action" onclick="copyPublicLink()">
          <span>🔗</span> <span id="copy-btn-text">Share Link</span>
        </button>
      </div>

      <!-- Mobile Hamburger Button -->
      <button class="btn-hamburger" id="hamburger-btn" onclick="toggleMobileDrawer()" aria-label="Open Navigation Menu">
        <span></span>
        <span></span>
        <span></span>
      </button>
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
          <span class="tag-pct" id="realized-pct">+0.00%</span>
        </div>
        <div class="kpi-val" id="realized-val">₹0.00</div>
        <div class="kpi-sub">
          <span>Floating MTM: <b id="unrealized-val" style="color:#fff;">₹0.00</b> <span id="unrealized-pct" style="font-weight:700;">(+0.00%)</span></span>
        </div>
      </div>

      <!-- Live Capital & Circuit Limit -->
      <div class="kpi-card">
        <div class="kpi-label">
          <span>Live Capital</span>
          <span id="capital-return-pct" class="tag-pct" style="font-size:0.75rem;">+0.00%</span>
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
          <span class="tag-pct" id="mtd-pct">+0.00%</span>
        </div>
        <div class="kpi-val" id="mtd-val">₹0.00</div>
        <div class="kpi-sub">
          <span>Year-to-Date: <b id="ytd-val" style="color:#fff;">₹0.00</b> <span id="ytd-pct" style="font-weight:700;">(+0.00%)</span></span>
        </div>
      </div>
    </div>

    <!-- ─── Live Intraday P&L Tracking Chart (09:15 to Session Close) ─── -->
    <div class="panel-box" style="padding:18px 20px; margin-bottom:22px; position:relative; overflow:hidden;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; flex-wrap:wrap; gap:8px;">
        <div style="display:flex; align-items:center; gap:10px;">
          <span style="font-size:1.2rem; color:var(--primary);">📈</span>
          <span style="font-family:var(--display); font-weight:800; font-size:1.1rem; letter-spacing:-0.02em;">LIVE INTRADAY P&L TRAJECTORY</span>
          <span class="status-chip chip-green" style="font-size:0.68rem; padding:3px 9px;" id="intraday-live-status">LIVE STREAM</span>
        </div>
        <div style="display:flex; align-items:center; gap:14px; font-family:var(--mono); font-size:0.78rem;">
          <span style="color:var(--text-dim);">Timeline: <b style="color:#fff;">09:15 ➔ 15:35 IST</b></span>
          <span style="color:var(--text-dim);">Live MTM: <b id="chart-cur-mtm" style="color:var(--green); font-size:0.92rem;">₹0.00 (+0.00%)</b></span>
        </div>
      </div>
      
      <!-- Interactive Live Canvas -->
      <div style="position:relative; width:100%; height:230px; background:rgba(0,0,0,0.5); border:1px solid rgba(255,255,255,0.07); border-radius:16px; overflow:hidden;">
        <canvas id="intraday-canvas" style="width:100%; height:100%; display:block;"></canvas>
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
          <span>Trades: <b id="m-trades">0</b></span>
          <span>Realized: <b id="m-realized">₹0.00</b></span>
          <span>Floating: <b id="m-unrealized">₹0.00</b></span>
          <span>Reversal: <b id="m-reversal" style="color:var(--purple);">INACTIVE</b></span>
          <span>Cooldown: <b id="m-cooldown">0s</b></span>
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

    <!-- ─── Universal Historical Performance & Analytics (Visible on all tabs) ─── -->
    <!-- Day-of-Week Cumulative Performance Analysis -->
    <div style="margin-top:28px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
      <h3 style="font-size:1.05rem; font-weight:800; display:flex; align-items:center; gap:8px;">
        <span>📊</span> <span>Day-of-Week Cumulative Performance</span>
      </h3>
      <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Monday – Friday Performance Heat</span>
    </div>

    <div class="panel-box" style="padding:20px; margin-bottom:24px;">
      <div style="display:flex; gap:14px; justify-content:space-between; align-items:flex-end;" id="dow-bars-container">
        <!-- Dynamically populated 5 weekday bars -->
      </div>
    </div>

    <!-- Month-wise Performance Breakdown Cards -->
    <div style="margin-bottom:14px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
      <h3 style="font-size:1.05rem; font-weight:800; display:flex; align-items:center; gap:8px;">
        <span>📅</span> <span>Monthly Performance Breakdown</span>
      </h3>
      <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Month-over-Month Capital Gain (Base: ₹2,00,000)</span>
    </div>
    <div class="history-cards-flex" id="monthwise-cards-box" style="margin-bottom:24px;"></div>

    <!-- Scrollable Last 30 Days Performance Journal -->
    <div style="margin-bottom:14px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
      <h3 style="font-size:1.05rem; font-weight:800; display:flex; align-items:center; gap:8px;">
        <span>🗓️</span> <span>Last 30 Days Performance Feed</span>
      </h3>
      <span style="font-size:0.75rem; color:var(--text-dim); font-family:var(--mono);">Scrollable Daily Session Journal</span>
    </div>
    <div class="scroll-feed-30d" id="history-30d-feed" style="margin-bottom:28px;"></div>
  </div>

  <!-- ─── Broker Authentication Modal (Apple macOS Glass Styling) ─── -->
  <div class="modal-overlay" id="auth-modal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.82); backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px); z-index:99999; align-items:center; justify-content:center; padding:16px;">
    <div style="background:rgba(14, 21, 37, 0.94); border:1px solid rgba(255,255,255,0.16); border-radius:24px; max-width:540px; width:100%; padding:26px; box-shadow:0 30px 80px rgba(0,0,0,0.85), inset 0 1px 0 rgba(255,255,255,0.18); position:relative; backdrop-filter:blur(32px) saturate(190%); -webkit-backdrop-filter:blur(32px) saturate(190%);">
      
      <!-- Apple Window Traffic Light Controls -->
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:18px;">
        <div style="display:flex; align-items:center; gap:8px;">
          <span onclick="closeAuthModal()" style="width:12px; height:12px; border-radius:50%; background:#ef4444; display:inline-block; cursor:pointer;" title="Close"></span>
          <span style="width:12px; height:12px; border-radius:50%; background:#f59e0b; display:inline-block; opacity:0.8;"></span>
          <span style="width:12px; height:12px; border-radius:50%; background:#10b981; display:inline-block; opacity:0.8;"></span>
          <span style="font-size:0.75rem; font-family:var(--mono); color:var(--text-dim); margin-left:8px;">Flattrade Direct Terminal Auth</span>
        </div>
        <button onclick="closeAuthModal()" style="background:rgba(255,255,255,0.06); border:none; color:var(--text-dim); width:28px; height:28px; border-radius:50%; font-size:1rem; cursor:pointer; display:flex; align-items:center; justify-content:center;">✕</button>
      </div>
      
      <div style="display:flex; align-items:center; gap:14px; margin-bottom:16px;">
        <div style="width:44px; height:44px; background:linear-gradient(135deg, #f59e0b, #d97706); border-radius:14px; display:flex; align-items:center; justify-content:center; font-size:1.4rem; box-shadow:0 4px 18px rgba(245, 158, 11, 0.35);">🔑</div>
        <div>
          <h3 style="font-family:var(--display); font-size:1.22rem; font-weight:800; background:linear-gradient(to right, #fff, #fde047); -webkit-background-clip:text; -webkit-text-fill-color:transparent;">Flattrade Daily Token Activation</h3>
          <div style="font-size:0.74rem; color:var(--text-dim); font-family:var(--mono);">Client ID: <b style="color:var(--primary);" id="auth-client-id">FZ04111</b> &bull; Official Web Auth</div>
        </div>
      </div>

      <!-- Current Token Status Box -->
      <div style="display:flex; align-items:center; justify-content:space-between; background:rgba(255,255,255,0.035); border:1px solid var(--card-border); border-radius:14px; padding:12px 16px; margin-bottom:18px; font-family:var(--mono); font-size:0.8rem; box-shadow:inset 0 1px 0 rgba(255,255,255,0.05);">
        <div>
          <div style="font-size:0.68rem; color:var(--text-dim); text-transform:uppercase;">Daily Token State</div>
          <div style="font-weight:700; margin-top:2px;" id="modal-token-preview">Checking...</div>
        </div>
        <span class="status-chip chip-dim" id="modal-token-chip">CHECKING</span>
      </div>

      <!-- STEP 1: Open Flattrade Login Page -->
      <div style="background:rgba(255,255,255,0.025); border:1px solid rgba(255,255,255,0.08); border-radius:16px; padding:16px; margin-bottom:14px;">
        <div style="font-size:0.75rem; font-weight:800; color:var(--primary); text-transform:uppercase; letter-spacing:0.06em; margin-bottom:6px;">STEP 1: Log in on Flattrade</div>
        <div style="font-size:0.74rem; color:var(--text-dim); margin-bottom:12px; line-height:1.4;">
          Click below to open Flattrade in a new tab. Log in securely with your User ID, Password, and TOTP:
        </div>
        <a id="btn-flattrade-link" href="https://auth.flattrade.in/?app_key=fa5da7cfc3d7459298efeb11d87bab41" target="_blank" style="display:flex; align-items:center; justify-content:center; gap:8px; width:100%; background:linear-gradient(135deg, rgba(56, 189, 248, 0.18), rgba(99, 102, 241, 0.22)); border:1px solid rgba(56, 189, 248, 0.45); color:#fff; text-decoration:none; padding:12px 16px; border-radius:12px; font-weight:800; font-size:0.92rem; box-shadow:0 4px 18px rgba(56, 189, 248, 0.25); transition:all 0.2s;">
          <span>🌐</span> <span>Open Flattrade Official Login Page ↗</span>
        </a>
      </div>

      <!-- STEP 2: Paste Redirect URL or Code -->
      <div style="background:rgba(255,255,255,0.025); border:1px solid rgba(255,255,255,0.08); border-radius:16px; padding:16px; margin-bottom:14px;">
        <div style="font-size:0.75rem; font-weight:800; color:var(--gold); text-transform:uppercase; letter-spacing:0.06em; margin-bottom:6px;">STEP 2: Paste Redirect URL or Code</div>
        <div style="font-size:0.74rem; color:var(--text-dim); margin-bottom:12px; line-height:1.4;">
          After logging in, copy the URL from your browser address bar (or just the code) and paste it below:
        </div>
        <input type="text" id="auth-input-code" placeholder="Paste redirected URL (e.g. https://127.0.0.1/?code=...) or code" style="width:100%; background:rgba(0,0,0,0.6); border:1px solid rgba(255,255,255,0.18); border-radius:12px; padding:12px 14px; color:#fff; font-family:var(--mono); font-size:0.85rem; outline:none; transition:border-color 0.2s;" onfocus="this.style.borderColor='var(--gold)';" onblur="this.style.borderColor='rgba(255,255,255,0.18)';" onkeypress="if(event.key === 'Enter') submitAuthToken();">

        <button id="btn-submit-token" onclick="submitAuthToken()" style="width:100%; margin-top:14px; background:linear-gradient(135deg, #10b981, #059669); color:#fff; border:none; padding:13px; border-radius:12px; font-weight:800; font-size:0.95rem; cursor:pointer; display:flex; align-items:center; justify-content:center; gap:8px; box-shadow:0 4px 20px rgba(16, 185, 129, 0.4), inset 0 1px 0 rgba(255,255,255,0.25); transition:all 0.2s;">
          <span>⚡ Extract & Activate Token</span>
        </button>
      </div>

      <!-- Output feedback message -->
      <div id="auth-feedback-box" style="display:none; font-family:var(--mono); font-size:0.82rem; padding:12px 14px; border-radius:12px;"></div>
    </div>
  </div>

  <!-- ─── Mobile Slide-out Drawer Navigation ─── -->
  <div class="mobile-drawer-overlay" id="drawer-overlay" onclick="closeMobileDrawer()"></div>
  <aside class="mobile-drawer" id="mobile-drawer" aria-label="Mobile Navigation">
    <div class="drawer-header">
      <div class="drawer-title">
        <span style="font-size:1.2rem;">⚡</span>
        <span>Navigation Menu</span>
      </div>
      <button class="drawer-close" onclick="closeMobileDrawer()" aria-label="Close menu">✕</button>
    </div>

    <!-- Live Status Pill in Drawer -->
    <div style="background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08); border-radius:14px; padding:12px 14px; margin-bottom:18px; display:flex; justify-content:space-between; align-items:center;">
      <div style="display:flex; align-items:center; gap:8px;">
        <span class="dot-pulse"></span>
        <span style="font-size:0.75rem; font-family:var(--mono); font-weight:700; color:var(--green);" id="drawer-stream-status">LIVE STREAMING</span>
      </div>
      <div style="font-family:var(--mono); font-size:0.8rem; font-weight:700; color:#fff;" id="drawer-live-clock">--:--:-- IST</div>
    </div>

    <!-- Navigation Section Tabs -->
    <div style="font-size:0.7rem; font-weight:800; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:10px; padding-left:4px;">
      Trading Engines
    </div>
    <div class="drawer-nav-section">
      <button class="drawer-nav-btn active-nifty" id="drawer-btn-nifty" onclick="switchTab('nifty')">
        <span style="font-size:1.1rem;">⚡</span>
        <div style="flex:1;">
          <div>NIFTY 50 Options</div>
          <div style="font-size:0.7rem; color:var(--text-dim); font-family:var(--mono); font-weight:400;">KAMA-ADX & Straddle</div>
        </div>
      </button>

      <button class="drawer-nav-btn" id="drawer-btn-mcx" onclick="switchTab('mcx')">
        <span style="font-size:1.1rem;">🛢️</span>
        <div style="flex:1;">
          <div>MCX Natural Gas</div>
          <div style="font-size:0.7rem; color:var(--text-dim); font-family:var(--mono); font-weight:400;">EMA Trend Pullback</div>
        </div>
      </button>

      <button class="drawer-nav-btn" id="drawer-btn-overview" onclick="switchTab('overview')">
        <span style="font-size:1.1rem;">📊</span>
        <div style="flex:1;">
          <div>Unified Overview</div>
          <div style="font-size:0.7rem; color:var(--text-dim); font-family:var(--mono); font-weight:400;">Multi-Asset Positions</div>
        </div>
      </button>
    </div>

    <!-- Quick Actions in Drawer -->
    <div class="drawer-actions">
      <button class="drawer-action-btn" onclick="closeMobileDrawer(); openAuthModal();" style="background:linear-gradient(135deg, rgba(168, 85, 247, 0.25), rgba(147, 51, 234, 0.35)); border:1px solid rgba(168, 85, 247, 0.45); color:#f3e8ff;">
        <span>🔑</span> <span id="drawer-auth-text">Flattrade Broker Auth</span>
      </button>

      <button class="drawer-action-btn" onclick="copyPublicLink(); closeMobileDrawer();" style="background:rgba(255, 255, 255, 0.05); border:1px solid rgba(255, 255, 255, 0.12); color:#e2e8f0;">
        <span>🔗</span> <span>Share Public Link</span>
      </button>

      <div style="text-align:center; margin-top:10px; font-size:0.72rem; color:var(--gold); font-family:var(--display); font-weight:700;">
        ॥ जय श्री कृष्ण ॥
      </div>
    </div>
  </aside>

  <div class="toast-box" id="toast">Link copied to clipboard!</div>

  <script>
    let activeTab = 'nifty';

    function toggleMobileDrawer() {
      const drawer = document.getElementById('mobile-drawer');
      const overlay = document.getElementById('drawer-overlay');
      const btn = document.getElementById('hamburger-btn');
      const isOpen = drawer.classList.contains('active');
      if (isOpen) {
        closeMobileDrawer();
      } else {
        drawer.classList.add('active');
        overlay.classList.add('active');
        if (btn) btn.classList.add('active');
        document.body.style.overflow = 'hidden';
      }
    }

    function closeMobileDrawer() {
      const drawer = document.getElementById('mobile-drawer');
      const overlay = document.getElementById('drawer-overlay');
      const btn = document.getElementById('hamburger-btn');
      if (drawer) drawer.classList.remove('active');
      if (overlay) overlay.classList.remove('active');
      if (btn) btn.classList.remove('active');
      document.body.style.overflow = '';
    }

    function switchTab(tabName) {
      activeTab = tabName;
      document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.seg-tab').forEach(el => el.className = 'seg-tab');
      document.querySelectorAll('.drawer-nav-btn').forEach(el => el.className = 'drawer-nav-btn');

      if (tabName === 'nifty') {
        document.getElementById('view-nifty').classList.add('active');
        const tabEl = document.getElementById('tab-btn-nifty');
        if (tabEl) tabEl.classList.add('active-nifty');
        const dBtn = document.getElementById('drawer-btn-nifty');
        if (dBtn) dBtn.classList.add('active-nifty');
      } else if (tabName === 'mcx') {
        document.getElementById('view-mcx').classList.add('active');
        const tabEl = document.getElementById('tab-btn-mcx');
        if (tabEl) tabEl.classList.add('active-mcx');
        const dBtn = document.getElementById('drawer-btn-mcx');
        if (dBtn) dBtn.classList.add('active-mcx');
      } else {
        document.getElementById('view-overview').classList.add('active');
        const tabEl = document.getElementById('tab-btn-overview');
        if (tabEl) tabEl.classList.add('active-overview');
        const dBtn = document.getElementById('drawer-btn-overview');
        if (dBtn) dBtn.classList.add('active-overview');
      }
      closeMobileDrawer();
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
        const legPct2L = (pos.pnl / 200000.0) * 100.0;
        const premPct = pos.entry > 0 ? (((pos.side === 'SELL' ? (pos.entry - pos.ltp) : (pos.ltp - pos.entry)) / pos.entry) * 100.0) : 0.0;
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
            <td style="text-align:right;" class="${pnlClass}">
              <div style="font-weight:800; font-size:0.95rem;">${fmtINR(pos.pnl, true)}</div>
              <div style="font-size:0.73rem; font-weight:700; opacity:0.9; margin-top:2px;">${fmtPct(legPct2L)} on 2L <span style="opacity:0.75;">(${fmtPct(premPct)} prem)</span></div>
            </td>
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
        const tradePct2L = (t.pnl / 200000.0) * 100.0;
        return `
          <tr>
            <td style="color:var(--text-dim);">${t.time || '--'}</td>
            <td><span class="leg-badge ${t.leg.startsWith('CE') ? 'leg-ce' : 'leg-pe'}">${t.leg}</span></td>
            <td><b>${t.strike}</b></td>
            <td>₹${parseFloat(t.entry || 0).toFixed(2)}</td>
            <td>₹${parseFloat(t.exit || 0).toFixed(2)}</td>
            <td style="color:var(--text-muted); font-size:0.75rem;">${t.reason || 'SQUARE_OFF'}</td>
            <td style="text-align:right;" class="${pnlClass}">
              <div style="font-weight:800;">${fmtINR(t.pnl, true)}</div>
              <div style="font-size:0.72rem; font-weight:700; opacity:0.85;">${fmtPct(tradePct2L)} on 2L</div>
            </td>
          </tr>
        `;
      }).join('');
    }

    // ── Live Intraday Canvas Chart State & Engine ──
    let chartSeries = [];
    let chartCurNet = 0.0;
    let chartCurPct = 0.0;
    let chartAnimFrame = null;
    let chartMouseX = null;

    function renderIntradayChart(series, curNet, curPct) {
      if (Array.isArray(series)) {
        chartSeries = series;
      }
      if (curNet !== undefined && !isNaN(curNet)) chartCurNet = parseFloat(curNet);
      if (curPct !== undefined && !isNaN(curPct)) chartCurPct = parseFloat(curPct);

      const curNetEl = document.getElementById('chart-cur-mtm');
      if (curNetEl) {
        curNetEl.innerText = `${fmtINR(chartCurNet, true)} (${fmtPct(chartCurPct)})`;
        curNetEl.style.color = chartCurNet >= 0 ? 'var(--green)' : 'var(--red)';
      }
    }

    function drawIntradayCanvas() {
      const canvas = document.getElementById('intraday-canvas');
      if (!canvas) return;

      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = rect.width;
      const h = rect.height;
      if (!w || !h) return;

      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }

      const ctx = canvas.getContext('2d');
      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);

      const padLeft = 68;
      const padRight = 24;
      const padTop = 22;
      const padBottom = 26;
      const plotW = Math.max(10, w - padLeft - padRight);
      const plotH = Math.max(10, h - padTop - padBottom);

      // Fixed grid: 09:15 (555m) to 15:35 (935m) = 380 min
      const startMin = 555;
      const endMin = 935;
      const totalMinSpan = endMin - startMin; // 380

      function timeStrToMin(timeStr) {
        if (!timeStr) return startMin;
        const parts = timeStr.split(':').map(Number);
        const hh = parts[0] || 0;
        const mm = parts[1] || 0;
        const ss = parts[2] || 0;
        return hh * 60 + mm + ss / 60.0;
      }

      function minToX(m) {
        const ratio = Math.max(0.0, Math.min(1.0, (m - startMin) / totalMinSpan));
        return padLeft + ratio * plotW;
      }

      // Determine Y range (PnL in INR)
      let minPnl = 0.0;
      let maxPnl = 0.0;
      for (const pt of chartSeries) {
        if (pt.pnl < minPnl) minPnl = pt.pnl;
        if (pt.pnl > maxPnl) maxPnl = pt.pnl;
      }
      if (chartCurNet < minPnl) minPnl = chartCurNet;
      if (chartCurNet > maxPnl) maxPnl = chartCurNet;

      // Add headroom
      const spread = Math.max(1000, maxPnl - minPnl);
      const headRoom = spread * 0.18;
      const yMin = minPnl - headRoom;
      const yMax = maxPnl + headRoom;
      const yRange = yMax - yMin || 1;

      function pnlToY(pnl) {
        const ratio = (pnl - yMin) / yRange;
        return padTop + (1.0 - ratio) * plotH;
      }

      const zeroY = pnlToY(0.0);

      // 1. Vertical time grid lines & labels (Fixed 09:15 to 15:35)
      const timeTicks = [
        { label: '09:15', m: 555 },
        { label: '10:30', m: 630 },
        { label: '11:30', m: 690 },
        { label: '12:30', m: 750 },
        { label: '13:30', m: 810 },
        { label: '14:30', m: 870 },
        { label: '15:35', m: 935 }
      ];

      ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
      ctx.lineWidth = 1;
      ctx.fillStyle = 'rgba(148, 163, 184, 0.7)';
      ctx.font = '10px "JetBrains Mono", monospace';
      ctx.textAlign = 'center';

      timeTicks.forEach(tick => {
        const x = minToX(tick.m);
        ctx.beginPath();
        ctx.setLineDash([3, 4]);
        ctx.moveTo(x, padTop);
        ctx.lineTo(x, padTop + plotH);
        ctx.stroke();

        ctx.fillText(tick.label, x, h - 8);
      });

      // 2. Horizontal Zero Line (Dash + Glow)
      ctx.beginPath();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
      ctx.lineWidth = 1.2;
      ctx.moveTo(padLeft, zeroY);
      ctx.lineTo(padLeft + plotW, zeroY);
      ctx.stroke();

      // Zero label on Y axis
      ctx.setLineDash([]);
      ctx.textAlign = 'right';
      ctx.fillStyle = '#94a3b8';
      ctx.fillText('₹0 (0%)', padLeft - 6, zeroY + 3.5);

      // Upper guideline
      const upperPnl = maxPnl > 0 ? maxPnl : 1000;
      const upperY = pnlToY(upperPnl);
      if (Math.abs(upperY - zeroY) > 22) {
        ctx.strokeStyle = 'rgba(16, 185, 129, 0.15)';
        ctx.beginPath();
        ctx.moveTo(padLeft, upperY);
        ctx.lineTo(padLeft + plotW, upperY);
        ctx.stroke();

        const upperPct = (upperPnl / 200000.0) * 100.0;
        ctx.fillStyle = '#10b981';
        ctx.fillText(`+₹${Math.round(upperPnl)} (${fmtPct(upperPct)})`, padLeft - 6, upperY + 3.5);
      }

      // Lower guideline
      const lowerPnl = minPnl < 0 ? minPnl : -1000;
      const lowerY = pnlToY(lowerPnl);
      if (Math.abs(lowerY - zeroY) > 22) {
        ctx.strokeStyle = 'rgba(244, 63, 94, 0.15)';
        ctx.beginPath();
        ctx.moveTo(padLeft, lowerY);
        ctx.lineTo(padLeft + plotW, lowerY);
        ctx.stroke();

        const lowerPct = (lowerPnl / 200000.0) * 100.0;
        ctx.fillStyle = '#f43f5e';
        ctx.fillText(`-₹${Math.abs(Math.round(lowerPnl))} (${fmtPct(lowerPct)})`, padLeft - 6, lowerY + 3.5);
      }

      // 3. Build trajectory points
      let points = [];
      if (chartSeries.length > 0) {
        for (const pt of chartSeries) {
          const m = timeStrToMin(pt.time || pt.time_short);
          const px = minToX(m);
          const py = pnlToY(pt.pnl);
          points.push({ x: px, y: py, pnl: pt.pnl, pct: pt.pct, time: pt.time });
        }
      }

      if (points.length === 0) {
        const startX = minToX(startMin);
        const startY = pnlToY(0.0);
        points.push({ x: startX, y: startY, pnl: 0, pct: 0, time: '09:15' });
      }

      points.sort((a, b) => a.x - b.x);

      // 4. Fill gradient under the curve down to zeroY
      if (points.length > 1) {
        const lastPt = points[points.length - 1];
        const isPos = chartCurNet >= 0;
        const grad = ctx.createLinearGradient(0, padTop, 0, padTop + plotH);
        if (isPos) {
          grad.addColorStop(0, 'rgba(16, 185, 129, 0.28)');
          grad.addColorStop(0.6, 'rgba(16, 185, 129, 0.06)');
          grad.addColorStop(1, 'rgba(16, 185, 129, 0.0)');
        } else {
          grad.addColorStop(0, 'rgba(244, 63, 94, 0.0)');
          grad.addColorStop(0.6, 'rgba(244, 63, 94, 0.06)');
          grad.addColorStop(1, 'rgba(244, 63, 94, 0.28)');
        }

        ctx.beginPath();
        ctx.moveTo(points[0].x, zeroY);
        ctx.lineTo(points[0].x, points[0].y);
        for (let i = 1; i < points.length; i++) {
          ctx.lineTo(points[i].x, points[i].y);
        }
        ctx.lineTo(lastPt.x, zeroY);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();

        // 5. Stroke glowing curve
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        for (let i = 1; i < points.length; i++) {
          ctx.lineTo(points[i].x, points[i].y);
        }
        ctx.lineWidth = 2.6;
        ctx.strokeStyle = isPos ? '#10b981' : '#f43f5e';
        ctx.shadowColor = isPos ? 'rgba(16, 185, 129, 0.65)' : 'rgba(244, 63, 94, 0.65)';
        ctx.shadowBlur = 12;
        ctx.stroke();
        ctx.shadowBlur = 0;
      }

      // 6. Refined Calm Market Dot & Organic Breathing Glow
      const lastPt = points[points.length - 1];
      const nowMs = Date.now();
      const isCurPos = chartCurNet >= 0;

      // Smooth slow organic breathing phase (3.4s cycle)
      const breath = (Math.sin(nowMs / 540) + 1) / 2;

      // Subtle Soft Ambient Radial Nebula behind dot
      const glowR = 18 + breath * 14;
      const nebula = ctx.createRadialGradient(lastPt.x, lastPt.y, 2, lastPt.x, lastPt.y, glowR);
      nebula.addColorStop(0, isCurPos ? `rgba(16, 185, 129, ${0.35 + 0.15 * breath})` : `rgba(244, 63, 94, ${0.35 + 0.15 * breath})`);
      nebula.addColorStop(0.5, isCurPos ? `rgba(16, 185, 129, ${0.08 * breath})` : `rgba(244, 63, 94, ${0.08 * breath})`);
      nebula.addColorStop(1, 'rgba(0, 0, 0, 0)');
      ctx.fillStyle = nebula;
      ctx.beginPath();
      ctx.arc(lastPt.x, lastPt.y, glowR, 0, Math.PI * 2);
      ctx.fill();

      // Smooth Concentric Expanding Ripple Waves (Slow water-drop effect)
      // Wave 1 (2.8s period)
      const wave1 = (nowMs % 2800) / 2800;
      const r1 = 6 + wave1 * 26;
      const a1 = (1 - wave1) * 0.45;
      ctx.beginPath();
      ctx.arc(lastPt.x, lastPt.y, r1, 0, Math.PI * 2);
      ctx.strokeStyle = isCurPos ? `rgba(16, 185, 129, ${a1})` : `rgba(244, 63, 94, ${a1})`;
      ctx.lineWidth = 1.6 * (1 - wave1 * 0.6);
      ctx.stroke();

      // Wave 2 (staggered by 1.4s)
      const wave2 = ((nowMs + 1400) % 2800) / 2800;
      const r2 = 6 + wave2 * 26;
      const a2 = (1 - wave2) * 0.45;
      ctx.beginPath();
      ctx.arc(lastPt.x, lastPt.y, r2, 0, Math.PI * 2);
      ctx.strokeStyle = isCurPos ? `rgba(16, 185, 129, ${a2})` : `rgba(244, 63, 94, ${a2})`;
      ctx.lineWidth = 1.6 * (1 - wave2 * 0.6);
      ctx.stroke();

      // Inner Breathing Halo Ring
      ctx.beginPath();
      ctx.arc(lastPt.x, lastPt.y, 6.5 + breath * 2.2, 0, Math.PI * 2);
      ctx.strokeStyle = isCurPos ? `rgba(52, 211, 153, ${0.65 + 0.35 * breath})` : `rgba(251, 113, 133, ${0.65 + 0.35 * breath})`;
      ctx.lineWidth = 1.8;
      ctx.stroke();

      // Radiant Core Jewel Dot
      ctx.beginPath();
      ctx.arc(lastPt.x, lastPt.y, 4.2, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = isCurPos ? '#10b981' : '#f43f5e';
      ctx.shadowBlur = 12 + breath * 6;
      ctx.fill();
      ctx.shadowBlur = 0;

      // Subtle Vertical Tracking Guide
      ctx.beginPath();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = isCurPos ? 'rgba(16, 185, 129, 0.25)' : 'rgba(244, 63, 94, 0.25)';
      ctx.lineWidth = 1;
      ctx.moveTo(lastPt.x, padTop);
      ctx.lineTo(lastPt.x, padTop + plotH);
      ctx.stroke();
      ctx.setLineDash([]);

      // Floating Live Tag above tip
      const tagText = `${chartCurNet >= 0 ? '+' : ''}₹${Math.abs(chartCurNet).toFixed(2)} (${fmtPct(chartCurPct)})`;
      ctx.font = 'bold 11px "JetBrains Mono", monospace';
      const tagW = ctx.measureText(tagText).width + 16;
      const tagH = 22;
      let tagX = Math.max(padLeft + 8, Math.min(w - padRight - tagW, lastPt.x - tagW / 2));
      let tagY = lastPt.y - 28;
      if (tagY < padTop + 2) tagY = lastPt.y + 14;

      ctx.fillStyle = 'rgba(15, 23, 42, 0.9)';
      ctx.strokeStyle = isCurPos ? 'rgba(16, 185, 129, 0.6)' : 'rgba(244, 63, 94, 0.6)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(tagX, tagY, tagW, tagH, 6) : ctx.rect(tagX, tagY, tagW, tagH);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = isCurPos ? '#10b981' : '#f43f5e';
      ctx.textAlign = 'center';
      ctx.fillText(tagText, tagX + tagW / 2, tagY + 15);

      // 8. Interactive crosshair on hover
      if (chartMouseX !== null && chartMouseX >= padLeft && chartMouseX <= lastPt.x) {
        let closest = points[0];
        let minD = Math.abs(points[0].x - chartMouseX);
        for (let i = 1; i < points.length; i++) {
          const d = Math.abs(points[i].x - chartMouseX);
          if (d < minD) { minD = d; closest = points[i]; }
        }
        if (closest) {
          ctx.beginPath();
          ctx.setLineDash([2, 2]);
          ctx.strokeStyle = 'rgba(56, 189, 248, 0.65)';
          ctx.moveTo(closest.x, padTop);
          ctx.lineTo(closest.x, padTop + plotH);
          ctx.moveTo(padLeft, closest.y);
          ctx.lineTo(padLeft + plotW, closest.y);
          ctx.stroke();
          ctx.setLineDash([]);

          ctx.beginPath();
          ctx.arc(closest.x, closest.y, 4.5, 0, Math.PI * 2);
          ctx.fillStyle = '#38bdf8';
          ctx.fill();

          const tStr = `${closest.time || ''} • ${fmtINR(closest.pnl, true)} (${fmtPct(closest.pct)})`;
          ctx.font = '10px "JetBrains Mono", monospace';
          const tw = ctx.measureText(tStr).width + 14;
          let tx = Math.max(padLeft, Math.min(w - padRight - tw, closest.x - tw / 2));
          let ty = closest.y - 20;
          if (ty < padTop + 2) ty = closest.y + 14;

          ctx.fillStyle = 'rgba(6, 9, 14, 0.95)';
          ctx.strokeStyle = '#38bdf8';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect ? ctx.roundRect(tx, ty, tw, 20, 5) : ctx.rect(tx, ty, tw, 20);
          ctx.fill();
          ctx.stroke();

          ctx.fillStyle = '#ffffff';
          ctx.textAlign = 'center';
          ctx.fillText(tStr, tx + tw / 2, ty + 13);
        }
      }

      ctx.restore();
    }

    function startCanvasAnimationLoop() {
      function loop() {
        drawIntradayCanvas();
        chartAnimFrame = requestAnimationFrame(loop);
      }
      if (!chartAnimFrame) {
        loop();
      }
    }

    function setupCanvasInteractivity() {
      const canvas = document.getElementById('intraday-canvas');
      if (!canvas) return;
      canvas.addEventListener('mousemove', (e) => {
        const rect = canvas.getBoundingClientRect();
        chartMouseX = e.clientX - rect.left;
      });
      canvas.addEventListener('mouseleave', () => {
        chartMouseX = null;
      });
      window.addEventListener('resize', () => {
        drawIntradayCanvas();
      });
    }

    // ── Day-of-Week Cumulative Bar Chart Renderer ──
    function renderDOWChart(dowList) {
      const container = document.getElementById('dow-bars-container');
      if (!container) return;

      const defaultDOW = [
        { day: 'Monday', total_pnl: 0, pct: 0, count: 0, win_rate: 0 },
        { day: 'Tuesday', total_pnl: 0, pct: 0, count: 0, win_rate: 0 },
        { day: 'Wednesday', total_pnl: 0, pct: 0, count: 0, win_rate: 0 },
        { day: 'Thursday', total_pnl: 0, pct: 0, count: 0, win_rate: 0 },
        { day: 'Friday', total_pnl: 0, pct: 0, count: 0, win_rate: 0 }
      ];

      const list = (Array.isArray(dowList) && dowList.length > 0) ? dowList : defaultDOW;

      let maxAbs = 1000;
      list.forEach(d => {
        if (Math.abs(d.total_pnl) > maxAbs) maxAbs = Math.abs(d.total_pnl);
      });

      container.innerHTML = list.map(d => {
        const isPos = d.total_pnl >= 0;
        const heightPct = Math.max(12, Math.min(100, Math.round((Math.abs(d.total_pnl) / maxAbs) * 100)));
        const colorClass = isPos ? 'positive' : 'negative';
        const bgGradient = isPos
          ? 'linear-gradient(to top, rgba(16, 185, 129, 0.25), #10b981)'
          : 'linear-gradient(to top, rgba(244, 63, 94, 0.25), #f43f5e)';
        const glow = isPos ? '0 0 16px rgba(16, 185, 129, 0.4)' : '0 0 16px rgba(244, 63, 94, 0.4)';
        const shortDay = d.day.slice(0, 3).toUpperCase();

        return `
          <div class="dow-bar-column">
            <div style="font-family:var(--mono); font-size:0.75rem; font-weight:800; margin-bottom:6px;" class="${colorClass}">
              ${fmtINR(d.total_pnl, true)}
              <div style="font-size:0.68rem; font-weight:700; opacity:0.85;">${fmtPct(d.pct)} on 2L</div>
            </div>
            <div class="dow-bar-track">
              <div class="dow-bar-fill" style="height:${heightPct}%; background:${bgGradient}; box-shadow:${glow};"></div>
            </div>
            <div style="font-family:var(--mono); font-weight:800; font-size:0.85rem; margin-top:8px; color:#fff;">${shortDay}</div>
            <div style="font-family:var(--mono); font-size:0.7rem; color:var(--text-dim); margin-top:2px;">
              ${d.count}d • <span style="color:${d.win_rate >= 50 ? 'var(--green)' : 'var(--amber)'}; font-weight:700;">${Math.round(d.win_rate)}% Win</span>
            </div>
          </div>
        `;
      }).join('');
    }

    // ── Monthly Performance Breakdown Cards Renderer ──
    function renderMonthwiseCards(monthList) {
      const container = document.getElementById('monthwise-cards-box');
      if (!container) return;

      if (!Array.isArray(monthList) || monthList.length === 0) {
        container.innerHTML = `<div style="grid-column:1/-1; text-align:center; color:var(--text-dim); padding:20px; font-family:var(--mono);">No monthly historical records available yet.</div>`;
        return;
      }

      container.innerHTML = monthList.map(m => {
        const isPos = m.total_pnl >= 0;
        const cClass = isPos ? 'positive' : 'negative';
        const tagClass = isPos ? 'pos' : 'neg';
        const wr = Math.round(m.win_rate || 0);

        return `
          <div class="hist-box" style="padding:16px; border-radius:16px; position:relative; overflow:hidden;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <div class="hist-title" style="font-size:0.8rem; font-weight:700; color:var(--text-muted);">${m.month}</div>
              <span class="tag-pct ${tagClass}" style="font-size:0.75rem;">${fmtPct(m.pct)} on 2L</span>
            </div>
            <div class="hist-num ${cClass}" style="font-size:1.35rem; margin-bottom:8px;">
              ${fmtINR(m.total_pnl, true)}
            </div>
            <div style="font-size:0.74rem; color:var(--text-dim); display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <span>${m.days} Trading Days</span>
              <span style="color:${wr >= 50 ? 'var(--green)' : 'var(--amber)'}; font-weight:700;">${wr}% Win Rate</span>
            </div>
            <div class="circuit-track" style="height:4px; margin-top:0;">
              <div style="height:100%; width:${wr}%; background:${wr >= 50 ? 'var(--green)' : 'var(--amber)'}; border-radius:999px;"></div>
            </div>
          </div>
        `;
      }).join('');
    }

    // ── Last 30 Days Scrollable Daily Journal Renderer ──
    function render30DayFeed(last30Days) {
      const container = document.getElementById('history-30d-feed');
      if (!container) return;

      if (!Array.isArray(last30Days) || last30Days.length === 0) {
        container.innerHTML = `<div style="color:var(--text-dim); padding:20px; font-family:var(--mono);">No 30-day session logs recorded yet.</div>`;
        return;
      }

      container.innerHTML = last30Days.map(d => {
        const isPos = d.pnl >= 0;
        const cClass = isPos ? 'positive' : 'negative';
        const tagClass = isPos ? 'pos' : 'neg';
        const badgeText = d.pnl > 0 ? 'WIN' : (d.pnl < 0 ? 'LOSS' : 'FLAT');
        const badgeBg = d.pnl > 0 ? 'rgba(16, 185, 129, 0.15)' : (d.pnl < 0 ? 'rgba(244, 63, 94, 0.15)' : 'rgba(148, 163, 184, 0.12)');
        const badgeBorder = d.pnl > 0 ? 'rgba(16, 185, 129, 0.3)' : (d.pnl < 0 ? 'rgba(244, 63, 94, 0.3)' : 'rgba(148, 163, 184, 0.2)');
        const badgeColor = d.pnl > 0 ? 'var(--green)' : (d.pnl < 0 ? 'var(--red)' : 'var(--text-dim)');

        return `
          <div class="day-feed-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <span style="font-size:0.75rem; color:var(--text-muted); font-weight:700;">${d.date}</span>
              <span style="font-size:0.65rem; font-weight:800; padding:2px 6px; border-radius:6px; background:${badgeBg}; border:1px solid ${badgeBorder}; color:${badgeColor};">${badgeText}</span>
            </div>
            <div style="font-size:0.72rem; color:var(--text-dim); margin-bottom:6px;">${d.day}</div>
            <div class="hist-num ${cClass}" style="font-size:1.05rem; margin-bottom:4px;">
              ${fmtINR(d.pnl, true)}
            </div>
            <div style="font-size:0.72rem; font-weight:700;" class="${cClass}">
              ${fmtPct(d.pct)} on 2L
            </div>
          </div>
        `;
      }).join('');
    }

    function updateDashboard(data) {
      if (!data) return;

      // Clock
      if (data.timestamp_ist) {
        const timeStr = data.timestamp_ist.split(' ')[1] + ' IST';
        const clk = document.getElementById('live-clock');
        if (clk) clk.innerText = timeStr;
        const dClk = document.getElementById('drawer-live-clock');
        if (dClk) dClk.innerText = timeStr;
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

      // Realized with % on 2L
      const realPct = (p.combined_realized / 200000.0) * 100.0;
      const realEl = document.getElementById('realized-val');
      realEl.innerText = fmtINR(p.combined_realized, true);
      applyClass(realEl, p.combined_realized);
      const realPctEl = document.getElementById('realized-pct');
      if (realPctEl) {
        realPctEl.innerText = fmtPct(realPct);
        realPctEl.className = 'tag-pct ' + (realPct >= 0 ? 'pos' : 'neg');
      }

      // Unrealized with % on 2L
      const unrealPct = (p.combined_unrealized / 200000.0) * 100.0;
      const unrealEl = document.getElementById('unrealized-val');
      unrealEl.innerText = fmtINR(p.combined_unrealized, true);
      applyClass(unrealEl, p.combined_unrealized);
      const unrealPctEl = document.getElementById('unrealized-pct');
      if (unrealPctEl) {
        unrealPctEl.innerText = `(${fmtPct(unrealPct)})`;
        unrealPctEl.style.color = unrealPct >= 0 ? 'var(--green)' : 'var(--red)';
      }

      // Capital return % on 2L
      document.getElementById('capital-val').innerText = fmtINR(p.current_capital);
      document.getElementById('circuit-val').innerText = fmtINR(p.circuit_limit);
      const capRetPct = ((p.current_capital - 200000.0) / 200000.0) * 100.0;
      const capRetEl = document.getElementById('capital-return-pct');
      if (capRetEl) {
        capRetEl.innerText = `${fmtPct(capRetPct)} Growth`;
        capRetEl.className = 'tag-pct ' + (capRetPct >= 0 ? 'pos' : 'neg');
      }

      const usedPct = p.circuit_used_pct || 0;
      document.getElementById('circuit-fill-bar').style.width = `${Math.min(100, usedPct)}%`;
      document.getElementById('circuit-used-text').innerText = `${usedPct.toFixed(1)}% Used`;

      // MTD with % on 2L
      const mtdPct = (p.mtd_pnl / 200000.0) * 100.0;
      const mtdEl = document.getElementById('mtd-val');
      mtdEl.innerText = fmtINR(p.mtd_pnl, true);
      applyClass(mtdEl, p.mtd_pnl);
      const mtdPctEl = document.getElementById('mtd-pct');
      if (mtdPctEl) {
        mtdPctEl.innerText = fmtPct(mtdPct);
        mtdPctEl.className = 'tag-pct ' + (mtdPct >= 0 ? 'pos' : 'neg');
      }

      // YTD with % on 2L
      const ytdPct = (p.ytd_pnl / 200000.0) * 100.0;
      const ytdEl = document.getElementById('ytd-val');
      ytdEl.innerText = fmtINR(p.ytd_pnl, true);
      applyClass(ytdEl, p.ytd_pnl);
      const ytdPctEl = document.getElementById('ytd-pct');
      if (ytdPctEl) {
        ytdPctEl.innerText = `(${fmtPct(ytdPct)})`;
        ytdPctEl.style.color = ytdPct >= 0 ? 'var(--green)' : 'var(--red)';
      }

      // This legacy badge was removed from the current header markup. Keep the
      // refresh loop alive when an optional element is not present.
      const tradesCountPill = document.getElementById('trades-count-pill');
      if (tradesCountPill) {
        tradesCountPill.innerText = `${p.total_trades || 0} TRADES`;
      }

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

      const nRealPct = (n.realized_pnl / 200000.0) * 100.0;
      const nUnrealPct = (n.unrealized_pnl / 200000.0) * 100.0;
      document.getElementById('n-realized').innerText = `${fmtINR(n.realized_pnl, true)} (${fmtPct(nRealPct)})`;
      document.getElementById('n-unrealized').innerText = `${fmtINR(n.unrealized_pnl, true)} (${fmtPct(nUnrealPct)})`;

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

      const mRealPct = (m.realized_pnl / 200000.0) * 100.0;
      const mUnrealPct = (m.unrealized_pnl / 200000.0) * 100.0;
      const mRealEl = document.getElementById('m-realized');
      if (mRealEl) mRealEl.innerText = `${fmtINR(m.realized_pnl, true)} (${fmtPct(mRealPct)})`;
      const mUnrealEl = document.getElementById('m-unrealized');
      if (mUnrealEl) mUnrealEl.innerText = `${fmtINR(m.unrealized_pnl, true)} (${fmtPct(mUnrealPct)})`;

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

      // ── RENDER LIVE CHARTS & ADVANCED ANALYTICS ──
      renderIntradayChart(data.intraday_series, netMtm, netPct);

      const ha = data.history_analytics || {};
      renderDOWChart(ha.day_of_week);
      renderMonthwiseCards(ha.monthwise);
      render30DayFeed(ha.last_30_days);
    }

    function initSSE() {
      const streamStatus = document.getElementById('stream-status');
      let es = null;
      let reconnectTimer = null;
      let reconnectDelay = 1000;
      let lastStreamMessageAt = 0;
      let pollInFlight = false;

      function setStreamStatus(label, color) {
        if (!streamStatus) return;
        streamStatus.innerText = label;
        streamStatus.style.color = color;
      }

      function scheduleReconnect() {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 10000);
      }

      function connect() {
        if (es) es.close();
        es = new EventSource('/api/stream');
        es.onopen = () => {
          reconnectDelay = 1000;
          lastStreamMessageAt = Date.now();
          setStreamStatus('LIVE STREAMING', 'var(--green)');
        };
        es.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data);
            updateDashboard(data);
            lastStreamMessageAt = Date.now();
          } catch(err) {
            console.error('SSE Error', err);
            // Let the watchdog polling path recover even if a malformed or
            // partially rendered update reaches the browser.
            lastStreamMessageAt = 0;
          }
        };
        es.onerror = () => {
          setStreamStatus('RECONNECTING...', 'var(--amber)');
          es.close();
          scheduleReconnect();
        };
      }
      connect();

      // Watchdog polling also handles a half-open/stalled SSE connection.
      setInterval(() => {
        const streamHealthy = es && es.readyState === EventSource.OPEN &&
          lastStreamMessageAt > 0 && (Date.now() - lastStreamMessageAt) < 5000;
        if (streamHealthy || pollInFlight) return;

        pollInFlight = true;
        fetch('/api/status', { cache: 'no-store' })
          .then(res => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json();
          })
          .then(data => {
            updateDashboard(data);
            setStreamStatus('POLLING FALLBACK', 'var(--amber)');
          })
          .catch(() => {})
          .finally(() => { pollInFlight = false; });
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

    // ── Broker Auth Modal Handlers ──
    function openAuthModal() {
      const modal = document.getElementById('auth-modal');
      modal.style.display = 'flex';
      fetchAuthStatus();
      setTimeout(() => {
        const inp = document.getElementById('auth-input-code');
        if (inp) inp.focus();
      }, 100);
    }

    function closeAuthModal() {
      document.getElementById('auth-modal').style.display = 'none';
      document.getElementById('auth-feedback-box').style.display = 'none';
    }

    function fetchAuthStatus() {
      fetch('/api/auth/status')
        .then(res => res.json())
        .then(d => {
          if (d.user_id) document.getElementById('auth-client-id').innerText = d.user_id;
          if (d.auth_url) document.getElementById('btn-flattrade-link').href = d.auth_url;

          const previewEl = document.getElementById('modal-token-preview');
          const chipEl = document.getElementById('modal-token-chip');
          const headerBtnText = document.getElementById('auth-header-text');
          const drawerAuthText = document.getElementById('drawer-auth-text');

          if (d.token_exists && d.is_today) {
            previewEl.innerText = `${d.token_preview} (Updated ${d.last_updated})`;
            chipEl.className = 'status-chip chip-green';
            chipEl.innerText = 'VALID TODAY';
            if (headerBtnText) headerBtnText.innerText = 'Token Active';
            if (drawerAuthText) drawerAuthText.innerText = 'Token Active (Flattrade)';
          } else if (d.token_exists) {
            previewEl.innerText = `${d.token_preview} (Expired ${d.last_updated})`;
            chipEl.className = 'status-chip chip-amber';
            chipEl.innerText = 'EXPIRED / RENEW';
            if (headerBtnText) headerBtnText.innerText = 'Renew Token';
            if (drawerAuthText) drawerAuthText.innerText = 'Renew Flattrade Token';
          } else {
            previewEl.innerText = 'No Token Found';
            chipEl.className = 'status-chip chip-dim';
            chipEl.innerText = 'LOGIN NEEDED';
            if (headerBtnText) headerBtnText.innerText = 'Login Needed';
            if (drawerAuthText) drawerAuthText.innerText = 'Login to Flattrade';
          }
        })
        .catch(() => {});
    }

    function submitAuthToken() {
      const input = document.getElementById('auth-input-code');
      const val = (input.value || '').trim();
      const feedback = document.getElementById('auth-feedback-box');
      const btn = document.getElementById('btn-submit-token');

      if (!val) {
        feedback.style.display = 'block';
        feedback.style.background = 'rgba(244, 63, 94, 0.15)';
        feedback.style.color = 'var(--red)';
        feedback.style.border = '1px solid rgba(244, 63, 94, 0.3)';
        feedback.innerText = 'Please paste the redirected URL or code from Flattrade first!';
        return;
      }

      btn.disabled = true;
      btn.style.opacity = '0.6';
      btn.innerHTML = '<span>⏳ Extracting Token & Activating...</span>';

      fetch('/api/auth/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url_or_code: val })
      })
      .then(res => res.json())
      .then(data => {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.innerHTML = '<span>⚡ Extract & Activate Token</span>';
        feedback.style.display = 'block';

        if (data.status === 'success') {
          feedback.style.background = 'rgba(16, 185, 129, 0.15)';
          feedback.style.color = 'var(--green)';
          feedback.style.border = '1px solid rgba(16, 185, 129, 0.3)';
          feedback.innerHTML = `✅ <b>Success!</b> ${data.message}<br><small>Token: ${data.token_preview}</small>`;
          input.value = '';
          fetchAuthStatus();
        } else {
          feedback.style.background = 'rgba(244, 63, 94, 0.15)';
          feedback.style.color = 'var(--red)';
          feedback.style.border = '1px solid rgba(244, 63, 94, 0.3)';
          feedback.innerHTML = `❌ <b>Failed:</b> ${data.message}`;
        }
      })
      .catch(err => {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.innerHTML = '<span>⚡ Extract & Activate Token</span>';
        feedback.style.display = 'block';
        feedback.style.background = 'rgba(244, 63, 94, 0.15)';
        feedback.style.color = 'var(--red)';
        feedback.style.border = '1px solid rgba(244, 63, 94, 0.3)';
        feedback.innerText = `Network error: ${err.message}`;
      });
    }

    window.addEventListener('DOMContentLoaded', () => {
      setupCanvasInteractivity();
      startCanvasAnimationLoop();
      fetch('/api/status')
        .then(res => res.json())
        .then(data => updateDashboard(data))
        .catch(() => {});
      fetchAuthStatus();
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
        parsed = urlparse(self.path)
        path = parsed.path
        self.send_response(200)
        if path.startswith("/api/"):
            self.send_header("Content-Type", "application/json")
        else:
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
            try:
                data = get_aggregated_dashboard_state()
                payload = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                import traceback
                traceback.print_exc()
                err_payload = json.dumps({"status": "error", "message": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_payload)

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

        elif path == "/api/auth/status":
            data = get_flattrade_token_status()
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(payload)

        elif path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/token":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                post_body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
                data = json.loads(post_body)
            except Exception:
                data = {}

            url_or_code = data.get("url_or_code", "")
            multiplier = int(data.get("multiplier", 1) or 1)
            result = exchange_flattrade_token(url_or_code, multiplier)

            payload = json.dumps(result).encode("utf-8")
            self.send_response(200 if result.get("status") == "success" else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
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
