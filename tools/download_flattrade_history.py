#!/usr/bin/env python3
"""Download read-only NIFTY and India VIX minute candles with resumable day files.

Broker timestamps are preserved exactly. Daily summary features are end-of-day
research data and must not be used as same-day intraday model inputs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
URL = "https://piconnect.flattrade.in/PiConnectAPI/TPSeries"
INSTRUMENTS = {"nifty_spot_1m": "26000", "india_vix_1m": "26017"}
FIELDS = ("Timestamp", "Open", "High", "Low", "Close", "Volume")
# NSE circular NSE/CMTR/70319: Diwali Muhurat trading, 13:45-14:45 IST.
SHORT_SESSIONS = {date(2025, 10, 21): 50}
# NSE circular NSE/CMTR/72349: live Sunday session for the Union Budget.
TRADING_WEEKENDS = {date(2026, 2, 1)}


def minimum_bars(day: date) -> int:
    return SHORT_SESSIONS.get(day, 300)


def fetch(uid: str, token: str, instrument: str, day: date):
    start = datetime(day.year, day.month, day.day, 9, 14, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 31, tzinfo=IST)
    payload = {"uid": uid, "exch": "NSE", "token": instrument,
               "st": str(int(start.timestamp())), "et": str(int(end.timestamp())),
               "intrv": "1"}
    body = ("jData=" + json.dumps(payload, separators=(",", ":")) +
            "&jKey=" + quote_plus(token)).encode()
    request = Request(URL, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("Flattrade session expired; renew it in dashboard Settings") from None
        raise RuntimeError(f"Flattrade HTTP {exc.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Flattrade request failed: {type(exc).__name__}") from None


def normalize(raw, day: date):
    if isinstance(raw, dict):
        message = str(raw.get("emsg", "unknown broker error"))
        if "no data" in message.lower():
            return None
        raise RuntimeError(message[:180])
    if not isinstance(raw, list):
        raise RuntimeError("Unexpected Flattrade candle response")
    found = {}
    for item in raw:
        try:
            stamp = datetime.strptime(item["time"], "%d-%m-%Y %H:%M:%S")
            if stamp.date() != day:
                raise ValueError("Candle outside requested day")
            numbers = [float(item[key]) for key in ("into", "inth", "intl", "intc")]
            if (not all(math.isfinite(n) and n > 0 for n in numbers)
                    or numbers[1] < max(numbers[0], numbers[3])
                    or numbers[2] > min(numbers[0], numbers[3])):
                raise ValueError("Invalid OHLC")
            volume = float(item.get("intv") or 0)
            if not math.isfinite(volume) or volume < 0:
                raise ValueError("Invalid volume")
            row = dict(zip(FIELDS, [stamp.isoformat(sep=" ", timespec="seconds"),
                                    *numbers, volume]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Malformed candle on {day}: {exc}") from None
        if stamp in found and found[stamp] != row:
            raise RuntimeError(f"Conflicting duplicate candle on {day}")
        found[stamp] = row
    rows = [found[stamp] for stamp in sorted(found)]
    if len(rows) < minimum_bars(day):
        raise RuntimeError(f"Incomplete session on {day}: {len(rows)} candles")
    return rows


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def read_day(path: Path, day: date):
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if (len(rows) < minimum_bars(day)
            or any(not row["Timestamp"].startswith(day.isoformat()) for row in rows)):
        raise RuntimeError(f"Saved file failed validation: {path.name}")
    stamps = [row["Timestamp"] for row in rows]
    if stamps != sorted(set(stamps)):
        raise RuntimeError(f"Saved file has duplicate/unsorted timestamps: {path.name}")
    return rows


def daily_features(day: date, spot: list[dict], vix: list[dict] | None):
    opens = [float(row["Open"]) for row in spot]
    highs = [float(row["High"]) for row in spot]
    lows = [float(row["Low"]) for row in spot]
    closes = [float(row["Close"]) for row in spot]
    returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    # Sum of intraday one-minute squared returns approximates one day's variance.
    realized = 100 * math.sqrt(252 * sum(value * value for value in returns))
    result = {"Date": day.isoformat(),
              "Session_Type": "muhurat_short" if day in SHORT_SESSIONS else "regular",
              "Spot_Bars": len(spot),
              "Spot_Open": opens[0], "Spot_High": max(highs),
              "Spot_Low": min(lows), "Spot_Close": closes[-1],
              "Spot_Return_Pct": 100 * (closes[-1] / opens[0] - 1),
              "Spot_Range_Pct": 100 * (max(highs) - min(lows)) / opens[0],
              "Spot_Realized_Vol_Annualized_Pct": realized,
              "VIX_Bars": len(vix) if vix else 0,
              "VIX_Open": "", "VIX_High": "", "VIX_Low": "", "VIX_Close": ""}
    if vix:
        result.update(VIX_Open=float(vix[0]["Open"]),
                      VIX_High=max(float(row["High"]) for row in vix),
                      VIX_Low=min(float(row["Low"]) for row in vix),
                      VIX_Close=float(vix[-1]["Close"]))
    return result


def save_summary(output: Path, start: date, end: date):
    days = [start + timedelta(days=offset) for offset in range((end-start).days + 1)]
    candidates = [day for day in days if day.weekday() < 5 or day in TRADING_WEEKENDS]
    daily = []
    spot_missing = []
    vix_missing = []
    for day in candidates:
        spot_path = output / "nifty_spot_1m" / f"nifty_spot_1m_{day}.csv"
        vix_path = output / "india_vix_1m" / f"india_vix_1m_{day}.csv"
        if not spot_path.is_file():
            spot_missing.append(str(day))
            continue
        spot = read_day(spot_path, day)
        vix = read_day(vix_path, day) if vix_path.is_file() else None
        if vix is None:
            vix_missing.append(str(day))
        daily.append(daily_features(day, spot, vix))
    target = output / "daily_volatility.csv"
    temporary = target.with_suffix(".tmp")
    if daily:
        with temporary.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(daily[0]))
            writer.writeheader()
            writer.writerows(daily)
        temporary.replace(target)
    manifest = {"source": "Flattrade TPSeries", "interval": "1 minute",
                "timestamp_note": "Raw broker candle labels; no minute shift",
                "daily_feature_note": "End-of-day research only; exclude same-day values from intraday model features",
                "start": str(start), "end": str(end), "weekdays": len(candidates),
                "spot_days": len(daily), "vix_days": len(daily)-len(vix_missing),
                "spot_bars": sum(int(row["Spot_Bars"]) for row in daily),
                "vix_bars": sum(int(row["VIX_Bars"]) for row in daily),
                "spot_missing_weekdays": spot_missing,
                "short_session_dates": [str(day) for day in SHORT_SESSIONS
                                        if start <= day <= end],
                "trading_weekends": [str(day) for day in TRADING_WEEKENDS
                                     if start <= day <= end],
                "vix_missing_on_spot_days": vix_missing}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    today = datetime.now(IST).date()
    parser.add_argument("--start", type=date.fromisoformat,
                        default=today-timedelta(days=365))
    parser.add_argument("--end", type=date.fromisoformat, default=today-timedelta(days=1))
    parser.add_argument("--output", type=Path,
                        default=Path("data/market_history/nifty_1y"))
    parser.add_argument("--delay", type=float, default=0.7,
                        help="Seconds between calls; 0.7 stays below documented 200/min limit")
    args = parser.parse_args()
    if args.start > args.end or args.end >= today or args.delay < 0.4:
        parser.error("Use past dates with start <= end and delay >= 0.4 seconds")
    uid = os.environ.get("FLATTRADE_USER_ID", "").strip()
    token_path = Path(os.environ.get("FLATTRADE_TOKEN_FILE", "token.txt"))
    if not uid or not token_path.is_file():
        parser.error("Set FLATTRADE_USER_ID and FLATTRADE_TOKEN_FILE")
    token = token_path.read_text().strip()
    if len(token) <= 10:
        parser.error("Flattrade token file is empty")
    args.output.mkdir(parents=True, exist_ok=True)
    no_data = 0
    calls = 0
    dates = [args.start + timedelta(days=i) for i in range((args.end-args.start).days+1)]
    for day in dates:
        if day.weekday() >= 5 and day not in TRADING_WEEKENDS:
            continue
        for name, instrument in INSTRUMENTS.items():
            target = args.output / name / f"{name}_{day}.csv"
            if target.is_file():
                read_day(target, day)
                continue
            for attempt in range(3):
                try:
                    raw = fetch(uid, token, instrument, day)
                    calls += 1
                    rows = normalize(raw, day)
                    if rows is None:
                        no_data += 1
                    else:
                        write_csv(target, rows)
                    break
                except RuntimeError as exc:
                    if "Session Expired" in str(exc) or "session expired" in str(exc).lower():
                        raise
                    if attempt == 2:
                        print(f"ERROR {day} {name}: {exc}", flush=True)
                    else:
                        time.sleep(2 * (attempt + 1))
                finally:
                    time.sleep(args.delay)
        if day.day in (1, 15) or day == args.end:
            print(f"Progress through {day}: {calls} requests, {no_data} no-data responses", flush=True)
    manifest = save_summary(args.output, args.start, args.end)
    print("DONE " + json.dumps({key: manifest[key] for key in
          ("spot_days", "vix_days", "spot_bars", "vix_bars", "weekdays")}), flush=True)


if __name__ == "__main__":
    main()
