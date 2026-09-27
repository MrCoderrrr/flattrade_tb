"""Read-only NIFTY research: one-second capture, day-held-out learning and replay.

This module never submits broker orders or changes controller strategy state.
Historical CSV provenance is not assumed. Model metrics are research diagnostics,
not forecasts of realizable returns.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import threading
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path

import numpy as np

from .models import IST


FEATURES = (
    "spot_return_1m", "ema_8_21_gap", "kama_10_slope", "dmi_strength_14",
    "rsi_14", "atr_14_fraction", "realized_vol_10", "efficiency_10",
    "channel_position_20", "atm_straddle_fraction", "atm_book_spread",
    "put_call_oi_imbalance", "put_call_iv_skew", "days_to_expiry",
    "session_fraction",
)
CLASSES = ("DOWN", "FLAT", "UP")
HORIZON = 5  # completed one-minute observations, never crossing a session
MIN_TRAIN_DAYS = 20
MIN_TEST_DAYS = 5


def _num(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _valid_book(side):
    bid, ask = _num(side.get("bid")), _num(side.get("ask"))
    return bid > 0 and ask >= bid and (ask - bid) / max((ask + bid) / 2, 0.01) <= 0.20


def _atm(rows, spot):
    ordered = sorted(rows, key=lambda row: abs(_num(row.get("strike")) - spot))
    return next((row for row in ordered if _valid_book(row.get("ce") or {})
                 and _valid_book(row.get("pe") or {})), None)


def _expiry(value):
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except (TypeError, ValueError):
            pass
    return ""


class MLResearch:
    def __init__(self, root: Path, chain=None, clock=None):
        self.path = Path(root) / "data" / "strategy_lab" / "ml.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS snapshots(
            timestamp TEXT PRIMARY KEY, day TEXT NOT NULL, source TEXT NOT NULL,
            quality TEXT NOT NULL, spot REAL NOT NULL, atm REAL NOT NULL,
            expiry TEXT NOT NULL, payload TEXT NOT NULL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS snapshot_day ON snapshots(day,quality)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS trade_audit(
            file TEXT PRIMARY KEY, day TEXT NOT NULL, rows INTEGER NOT NULL,
            reported_pnl REAL NOT NULL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS artifacts(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.commit()
        self.lock = threading.RLock()
        self.chain = chain
        self.clock = clock or (lambda: datetime.now(IST))
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = ""
        self.attempted_day = None

    def close(self):
        self.stop()
        with self.lock:
            self.db.close()

    def _put(self, timestamp, source, quality, spot, atm, expiry, payload, compress=False):
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        stored = zlib.compress(encoded, level=3) if compress else encoded.decode()
        with self.lock, self.db:
            self.db.execute("""INSERT OR REPLACE INTO snapshots
                (timestamp,day,source,quality,spot,atm,expiry,payload)
                VALUES (?,?,?,?,?,?,?,?)""",
                (timestamp.isoformat(), timestamp.date().isoformat(), source, quality,
                 spot, atm, expiry, stored))

    def capture(self, snapshot):
        """Record each clock second, preserving stale/absent quote status."""
        now = datetime.fromisoformat(snapshot["as_of"]).astimezone(IST).replace(microsecond=0)
        spot = _num(snapshot.get("spot"))
        rows = snapshot.get("rows") or []
        atm = _atm(rows, spot) if spot > 0 else None
        fresh = bool(snapshot.get("ready") and atm and all(
            (atm.get(kind) or {}).get("age_seconds") is not None
            and 0 <= _num(atm[kind]["age_seconds"], -1) <= 10
            for kind in ("ce", "pe")))
        compact = []
        for row in rows:
            item = {"strike": row.get("strike")}
            for kind in ("ce", "pe"):
                item[kind] = {field: (row.get(kind) or {}).get(field)
                              for field in ("bid", "ask", "bid_size", "ask_size", "oi", "iv", "age_seconds")}
            compact.append(item)
        payload = {"rows": compact, "dte": max(0, (datetime.fromisoformat(snapshot["expiry"]).date()
                    - now.date()).days) if snapshot.get("expiry") else 0}
        self._put(now, "broker_websocket", "live_observed" if fresh else "live_stale",
                  spot, _num(atm["strike"]) if atm else 0, snapshot.get("expiry") or "",
                  payload, compress=True)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()

        def run():
            while not self.stop_event.is_set():
                now = self.clock().astimezone(IST)
                try:
                    if (self.chain and now.weekday() < 5
                            and "09:15" <= now.strftime("%H:%M") < "15:40"):
                        self.capture(self.chain.snapshot())
                        self.last_error = ""
                    if (now.weekday() < 5 and now.strftime("%H:%M") >= "15:42"
                            and self.attempted_day != now.date().isoformat()):
                        self.attempted_day = now.date().isoformat()
                        retention = (now.date() - timedelta(days=90)).isoformat()
                        with self.lock, self.db:
                            self.db.execute("DELETE FROM snapshots WHERE source='broker_websocket' "
                                            "AND day < ?", (retention,))
                        with self.lock:
                            has_today = self.db.execute(
                                "SELECT 1 FROM snapshots WHERE day=? AND quality='live_observed' LIMIT 1",
                                (self.attempted_day,)).fetchone()
                        if has_today:
                            self.train()
                            self.last_error = ""
                except Exception as exc:
                    self.last_error = type(exc).__name__ + ": " + str(exc)[:160]
                self.stop_event.wait(max(0.05, 1 - (self.clock().microsecond / 1_000_000)))

        self.thread = threading.Thread(target=run, name="nifty-ml-capture", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None

    def import_directory(self, chain_dir: Path, trade_dir: Path | None = None,
                         spot_dir: Path | None = None):
        """Idempotently import historical rows; constant ATM IV/OI days are quarantined."""
        chain_dir = Path(chain_dir)
        if spot_dir is None:
            spot_dir = chain_dir.parent / "backtest" / "spot_1m"
        if trade_dir is None:
            trade_dir = chain_dir.parent / "logs" / "trade_book"
        result = {}
        for file in sorted(chain_dir.glob("nifty_oc_*.csv")):
            spots = {}
            spot_file = Path(spot_dir) / file.name.replace("nifty_oc_", "nifty_spot_1m_")
            if spot_file.is_file():
                with spot_file.open(newline="") as handle:
                    spots = {row["Timestamp"]: {key.lower(): _num(row.get(key))
                             for key in ("Open", "High", "Low", "Close")}
                             for row in csv.DictReader(handle)}
            with file.open(newline="") as handle:
                groups = [(stamp, list(rows)) for stamp, rows in groupby(
                    csv.DictReader(handle), key=lambda row: row["Timestamp"])]
            atm_rows = [next((row for row in rows if row["Strike"] == row["ATM"]), None)
                        for _, rows in groups]
            atm_rows = [row for row in atm_rows if row]
            varying = (len({_num(row["CE_IV"]) for row in atm_rows}) >= 5
                       and len({_num(row["CE_OI"]) for row in atm_rows}) >= 5)
            quality = "historical_variable" if varying else "quarantined_static_fields"
            stored = 0
            for stamp, group in groups:
                try:
                    when = datetime.fromisoformat(stamp).replace(tzinfo=IST)
                except ValueError:
                    continue
                spot = _num(group[0].get("Spot"))
                if spot <= 0:
                    continue
                rows = []
                for item in group:
                    strike = _num(item.get("Strike"))
                    if strike <= 0:
                        continue
                    sides = {}
                    for kind in ("CE", "PE"):
                        sides[kind.lower()] = {
                            "bid": _num(item.get(kind + "_Bid")),
                            "ask": _num(item.get(kind + "_Ask")),
                            "iv": _num(item.get(kind + "_IV")),
                            "oi": _num(item.get(kind + "_OI")),
                            "bid_size": _num(item.get(kind + "_BidQty")),
                            "ask_size": _num(item.get(kind + "_AskQty")),
                        }
                    rows.append({"strike": strike, **sides})
                payload = {"rows": rows, "bar": spots.get(stamp),
                           "dte": _num(group[0].get("DTE"))}
                self._put(when, "legacy_csv_unverified", quality, spot,
                          _num(group[0].get("ATM")), _expiry(group[0].get("Expiry")), payload)
                stored += 1
            result[file.name] = {"minutes": stored, "quality": quality}
        if Path(trade_dir).is_dir():
            for file in sorted(Path(trade_dir).glob("trade_log*.csv")):
                with file.open(newline="") as handle:
                    rows = list(csv.DictReader(handle))
                if not rows:
                    continue
                day = rows[0].get("Timestamp", "")[:10]
                if not day or any(row.get("Timestamp", "")[:10] != day for row in rows):
                    continue
                pnl = sum(_num(row.get("PnL")) for row in rows)
                with self.lock, self.db:
                    self.db.execute("INSERT OR REPLACE INTO trade_audit VALUES (?,?,?,?)",
                                    (file.name, day, len(rows), pnl))
        return result

    def status(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM artifacts WHERE key='report'").fetchone()
            counts = [dict(day=day, source=source, quality=quality, snapshots=count)
                      for day, source, quality, count in self.db.execute(
                          "SELECT day,source,quality,COUNT(*) FROM snapshots "
                          "GROUP BY day,source,quality ORDER BY day DESC")]
            trade_rows = [dict(file=file, day=day, rows=count, reported_pnl=pnl)
                          for file, day, count, pnl in self.db.execute(
                              "SELECT file,day,rows,reported_pnl FROM trade_audit ORDER BY day DESC,file")]
        return {"report": json.loads(row[0]) if row else None, "data_days": counts,
                "legacy_trade_files": trade_rows, "collector_error": self.last_error,
                "collector_running": bool(self.thread and self.thread.is_alive()),
                "collector_interval_seconds": 1, "model_controls_orders": False}

    def _minutes(self):
        with self.lock:
            latest_row = self.db.execute(
                "SELECT MAX(day) FROM snapshots WHERE quality IN "
                "('historical_variable','live_observed')").fetchone()
            if not latest_row[0]:
                return {}
            cutoff = (datetime.fromisoformat(latest_row[0]).date()
                      - timedelta(days=60)).isoformat()
            cursor = self.db.execute("""
                SELECT s.timestamp,s.day,s.source,s.spot,s.atm,s.expiry,s.payload,
                       minutes.low_spot,minutes.high_spot
                FROM snapshots s JOIN (
                    SELECT MAX(timestamp) AS last_stamp, MIN(spot) AS low_spot,
                           MAX(spot) AS high_spot
                    FROM snapshots
                    WHERE quality IN ('historical_variable','live_observed') AND day>=?
                    GROUP BY SUBSTR(timestamp,1,16)
                ) minutes ON s.timestamp=minutes.last_stamp
                ORDER BY s.timestamp""", (cutoff,))
            by_day = defaultdict(list)
            for stamp, day, source, spot, atm, expiry, payload, low, high in cursor:
                decoded = zlib.decompress(payload) if isinstance(payload, bytes) else payload
                item = {"time": datetime.fromisoformat(stamp), "spot": spot, "atm": atm,
                        "expiry": expiry, "source": source, **json.loads(decoded)}
                if source == "broker_websocket":
                    item["bar"] = {"open": spot, "high": high, "low": low, "close": spot}
                by_day[day].append(item)
        return dict(by_day)

    def train(self):
        return train_research(self)


def _ema(values, period):
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _kama(values):
    result = [values[0]]
    slow, fast = 2 / 31, 2 / 3
    for i, value in enumerate(values[1:], 1):
        if i < 10:
            result.append(result[-1])
            continue
        movement = sum(abs(values[j] - values[j - 1]) for j in range(i - 9, i + 1))
        efficiency = abs(value - values[i - 10]) / movement if movement else 0
        smoothing = (slow + efficiency * (fast - slow)) ** 2
        result.append(result[-1] + smoothing * (value - result[-1]))
    return result


def _bar(sample):
    bar = sample.get("bar") or {}
    price = sample["spot"]
    return (_num(bar.get("high"), price), _num(bar.get("low"), price))


def _dmi_and_atr(samples, end):
    """DMI strength and ATR use completed bars strictly before the decision."""
    trs, plus, minus = [], [], []
    for i in range(max(1, end - 27), end):
        high, low = _bar(samples[i])
        prev_high, prev_low = _bar(samples[i - 1])
        prev_close = samples[i - 1]["spot"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up, down = high - prev_high, prev_low - low
        plus.append(up if up > down and up > 0 else 0)
        minus.append(down if down > up and down > 0 else 0)
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0
    dx = []
    for i in range(14, len(trs) + 1):
        tr = sum(trs[i - 14:i])
        p = sum(plus[i - 14:i]) / tr if tr else 0
        m = sum(minus[i - 14:i]) / tr if tr else 0
        dx.append(abs(p - m) / (p + m) if p + m else 0)
    return (sum(dx[-14:]) / len(dx[-14:]) if dx else 0), atr


def _features_for_day(samples):
    samples = [sample for sample in samples if sample["spot"] > 0
               and _atm(sample.get("rows") or [], sample["spot"])]
    samples.sort(key=lambda row: row["time"])
    if len(samples) < 60:
        return []
    spots = [sample["spot"] for sample in samples]
    ema8, ema21, kama = _ema(spots, 8), _ema(spots, 21), _kama(spots)
    frames = []
    for i in range(30, len(samples)):
        current = samples[i]
        if (current["time"] - samples[i - 10]["time"]).total_seconds() > 15 * 60:
            continue
        spot = spots[i]
        atm = _atm(current["rows"], spot)
        ce, pe = atm["ce"], atm["pe"]
        dmi, atr = _dmi_and_atr(samples, i)
        changes = [spots[j] - spots[j - 1] for j in range(i - 13, i + 1)]
        gain = sum(max(0, change) for change in changes)
        loss = sum(max(0, -change) for change in changes)
        rsi = gain / (gain + loss) if gain + loss else 0.5
        recent = [math.log(spots[j] / spots[j - 1]) for j in range(i - 9, i + 1)]
        path = sum(abs(spots[j] - spots[j - 1]) for j in range(i - 9, i + 1))
        low, high = min(spots[i - 20:i]), max(spots[i - 20:i])
        mid_ce, mid_pe = (_num(ce["bid"]) + _num(ce["ask"])) / 2, (
            _num(pe["bid"]) + _num(pe["ask"])) / 2
        spread = (_num(ce["ask"]) - _num(ce["bid"])
                  + _num(pe["ask"]) - _num(pe["bid"])) / max(mid_ce + mid_pe, 0.01)
        oi_ce, oi_pe = _num(ce.get("oi")), _num(pe.get("oi"))
        minute = current["time"].hour * 60 + current["time"].minute - (9 * 60 + 15)
        features = [
            (spot / spots[i - 1] - 1), (ema8[i] - ema21[i]) / spot,
            (kama[i] - kama[i - 3]) / spot, dmi, rsi,
            atr / spot, float(np.std(recent)), abs(spot - spots[i - 10]) / path if path else 0,
            (spot - low) / (high - low) if high > low else 0.5,
            (mid_ce + mid_pe) / spot, spread,
            (oi_pe - oi_ce) / (oi_pe + oi_ce) if oi_pe + oi_ce else 0,
            (_num(pe.get("iv")) - _num(ce.get("iv"))) / 100,
            min(30, _num(current.get("dte"))) / 30,
            max(0, min(1, minute / 385)),
        ]
        if not all(math.isfinite(x) for x in features):
            continue
        frame = {"sample": current, "features": features, "class": None,
                 "risk_ce": None, "risk_pe": None, "atm_strike": _num(atm["strike"])}
        if i + HORIZON < len(samples) and (
                samples[i + HORIZON]["time"] - current["time"]).total_seconds() <= 8 * 60:
            future = samples[i + HORIZON]["spot"] - spot
            threshold = max(0.25 * atr, spot * 0.00015)
            frame["class"] = 2 if future > threshold else 0 if future < -threshold else 1
            for kind in ("ce", "pe"):
                asks = []
                for next_sample in samples[i + 1:i + HORIZON + 1]:
                    next_row = next((row for row in next_sample["rows"]
                                    if _num(row["strike"]) == frame["atm_strike"]), None)
                    book = next_row.get(kind) if next_row else None
                    if not book or not _valid_book(book):
                        break
                    asks.append(_num(book["ask"]))
                if len(asks) == HORIZON:
                    frame["risk_" + kind] = min(3.0, max(0, max(asks) /
                                                        _num(atm[kind]["ask"]) - 1))
        frames.append(frame)
    return frames


def _standardize(x, center=None, scale=None):
    if center is None:
        center = np.median(x, axis=0)
        scale = np.percentile(x, 75, axis=0) - np.percentile(x, 25, axis=0)
        scale = np.where(scale > 1e-9, scale, np.maximum(np.abs(center) * 0.1, 1e-4))
    z = np.clip((x - center) / scale, -5, 5)
    return np.c_[np.ones(len(z)), z], center, scale


def _fit(frames_by_day, days):
    latest = max(days)
    labeled = [(day, frame) for day in days for frame in frames_by_day[day]
               if frame["class"] is not None]
    if len(labeled) < 100:
        return None
    x = np.asarray([frame["features"] for _, frame in labeled], dtype=float)
    y = np.asarray([frame["class"] for _, frame in labeled], dtype=int)
    z, center, scale = _standardize(x)
    day_counts = Counter(day for day, _ in labeled)
    raw_weights = {day: 1 + 0.5 * (1 - min(60, (datetime.fromisoformat(latest)
                    - datetime.fromisoformat(day)).days) / 60) for day in days}
    weights = np.asarray([raw_weights[day] / day_counts[day] for day, _ in labeled])
    weights /= weights.sum()
    target = np.eye(3)[y]
    coef = np.zeros((z.shape[1], 3))
    regularizer = np.r_[0, np.repeat(0.025, z.shape[1] - 1)]
    for _ in range(450):
        logits = z @ coef
        logits -= logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        gradient = z.T @ ((probabilities - target) * weights[:, None])
        coef -= 0.18 * (gradient + regularizer[:, None] * coef)
    risk_models = {}
    for kind in ("ce", "pe"):
        selected = np.asarray([frame["risk_" + kind] is not None
                               for _, frame in labeled], dtype=bool)
        if selected.sum() < 50:
            risk_models[kind] = None
            continue
        subset_z, subset_w = z[selected], weights[selected]
        subset_w = subset_w / subset_w.sum()
        values = np.asarray([frame["risk_" + kind] for _, frame in labeled
                             if frame["risk_" + kind] is not None], dtype=float)
        values = np.clip(values, 0, np.percentile(values, 98))
        ridge = np.diag(np.r_[0.001, np.repeat(0.1, z.shape[1] - 1)])
        risk_coef = np.linalg.solve((subset_z.T * subset_w) @ subset_z + ridge,
                                    (subset_z.T * subset_w) @ values)
        residual = values - subset_z @ risk_coef
        risk_models[kind] = {"coef": risk_coef.tolist(),
                             "upper_residual": float(np.percentile(residual, 75))}
    return {"features": FEATURES, "classes": CLASSES, "center": center.tolist(),
            "scale": scale.tolist(), "coef": coef.tolist(), "risk": risk_models,
            "trained_days": days, "rows": len(labeled),
            "majority_class": int(np.bincount(y, minlength=3).argmax()),
            "max_day_weight_share": round(max(raw_weights.values()) /
                                          sum(raw_weights.values()), 4)}


def _predict(model, features):
    z, _, _ = _standardize(np.asarray([features], dtype=float),
                           np.asarray(model["center"]), np.asarray(model["scale"]))
    logits = (z @ np.asarray(model["coef"]))[0]
    probabilities = np.exp(logits - logits.max())
    probabilities /= probabilities.sum()
    risk = {}
    for kind in ("ce", "pe"):
        item = model["risk"][kind]
        risk[kind] = max(0.03, min(0.6, float(z[0] @ np.asarray(item["coef"]))
                                + item["upper_residual"])) if item else 0.25
    return probabilities.tolist(), risk


def _evaluation(model, frames):
    test = [frame for frame in frames if frame["class"] is not None]
    if not test:
        return {"samples": 0, "accuracy": None, "baseline_accuracy": None}
    predictions = [_predict(model, frame["features"])[0] for frame in test]
    predicted = [int(np.argmax(p)) for p in predictions]
    actual = [frame["class"] for frame in test]
    majority = model["majority_class"]
    recall = [sum(p == c and a == c for p, a in zip(predicted, actual)) /
              sum(a == c for a in actual) for c in range(3) if c in actual]
    return {"samples": len(test),
            "accuracy": round(sum(p == a for p, a in zip(predicted, actual)) /
                              len(actual), 4),
            "balanced_accuracy": round(sum(recall) / len(recall), 4),
            "baseline_accuracy": round(sum(a == majority for a in actual) /
                                       len(actual), 4),
            "class_counts": {CLASSES[c]: actual.count(c) for c in range(3)},
            "confusion": [[sum(a == i and p == j for a, p in zip(actual, predicted))
                           for j in range(3)] for i in range(3)]}


def _action_examples(frames):
    """Five-minute, original-strike premium outcomes with no missing quote path.

    These are independent action-value labels, not a full portfolio backtest.
    Minute data can miss intraminute stop crossings; live one-second records
    improve that resolution as they accumulate.
    """
    from .nifty_v5 import stop_parameters
    examples = []
    for index in range(len(frames)-HORIZON):
        path = frames[index:index+HORIZON+1]
        if any((b['sample']['time']-a['sample']['time']).total_seconds() != 60
               for a,b in zip(path,path[1:])):
            continue
        current = path[0]
        strike = current['atm_strike']
        for kind in ('ce','pe'):
            books = [_book_at(item['sample'],strike,kind) for item in path]
            if any(book is None for book in books):
                continue
            first = books[0]
            stop_pct,trail_pct = stop_parameters(
                {'quality':current['features'][7],'volatility_ratio':1.},True)
            spot = current['sample']['spot']
            spread = (_num(first['ask'])-_num(first['bid']))/max(_num(first['ask']),.01)
            for action in ('hold','reenter'):
                opening = _num(first['ask']) if action == 'hold' else _num(first['bid'])
                if opening <= 0:
                    continue
                best = opening
                exit_ask = None
                for book in books[1:]:
                    ask = _num(book['ask'])
                    best = min(best,ask)
                    exit_ask = ask
                    if ask >= min(opening*(1+stop_pct),best*(1+trail_pct)):
                        break
                feature = current['features'] + [1. if kind=='ce' else -1.,
                           1. if action=='reenter' else 0.,spread,opening/spot]
                # Hold advantage compares closing now with closing on the
                # observed path; re-entry includes the bid/ask crossing.
                advantage = opening-exit_ask
                examples.append({'day':current['sample']['time'].date().isoformat(),
                                 'time':current['sample']['time'].isoformat(),
                                 'features':feature,'advantage':advantage,
                                 'kind':kind,'action':action})
    return examples


def _fit_action_model(examples, training_days):
    selected = [item for item in examples if item['day'] in training_days]
    if len(selected) < 100:
        return None
    x = np.asarray([item['features'] for item in selected],dtype=float)
    y = np.asarray([item['advantage'] for item in selected],dtype=float)
    z,center,scale = _standardize(x)
    latest = max(training_days)
    counts = Counter(item['day'] for item in selected)
    day_weight = {day:1+.5*(1-min(60,(datetime.fromisoformat(latest)-
                    datetime.fromisoformat(day)).days)/60) for day in training_days}
    weight = np.asarray([day_weight[item['day']]/counts[item['day']]
                         for item in selected],dtype=float)
    weight /= weight.sum()
    ridge = np.diag(np.r_[.001,np.repeat(.2,z.shape[1]-1)])
    coef = np.linalg.solve((z.T*weight)@z+ridge,(z.T*weight)@y)
    return {'center':center.tolist(),'scale':scale.tolist(),'coef':coef.tolist(),
            'trained_days':training_days,'samples':len(selected),
            'max_day_weight_share':round(max(day_weight.values())/sum(day_weight.values()),4)}


def _action_research(frames, eligible, prior, test_day):
    examples = [item for day in eligible for item in _action_examples(frames[day])]
    model = _fit_action_model(examples,prior)
    test = [item for item in examples if item['day']==test_day]
    if model is None or not test:
        return {'status':'insufficient_quote_paths','training_days':prior,
                'test_day':test_day,'test_samples':len(test)},None
    x = np.asarray([item['features'] for item in test],dtype=float)
    z,_,_ = _standardize(x,np.asarray(model['center']),np.asarray(model['scale']))
    predicted = z@np.asarray(model['coef'])
    actual = np.asarray([item['advantage'] for item in test],dtype=float)
    baseline = np.median([item['advantage'] for item in examples if item['day'] in prior])
    report = {'status':'exploratory' if len(prior)<MIN_TRAIN_DAYS else 'paper_validation',
              'training_days':prior,'test_day':test_day,'train_samples':model['samples'],
              'test_samples':len(test),'sign_accuracy':round(float(np.mean((predicted>0)==(actual>0))),4),
              'baseline_sign_accuracy':round(float(np.mean((baseline>0)==(actual>0))),4),
              'mae_points':round(float(np.mean(np.abs(predicted-actual))),4),
              'baseline_mae_points':round(float(np.mean(np.abs(baseline-actual))),4),
              'max_training_day_weight':model['max_day_weight_share'],
              'horizon_minutes':HORIZON,'paper_only':True,
              'limitation':'Quote-path action labels omit brokerage, margin, portfolio interaction, and intraminute stops on historical CSV days.'}
    return report,_fit_action_model(examples,[day for day in eligible if day >= prior[0]])


def _book_at(sample, strike, kind):
    row = next((row for row in sample["rows"] if _num(row["strike"]) == strike), None)
    book = row.get(kind) if row else None
    if not book or _num(book.get("bid")) <= 0 or _num(book.get("ask")) < _num(book.get("bid")):
        return None
    if sample["source"] == "broker_websocket":
        age = book.get("age_seconds")
        if age is None or _num(age, -1) < 0 or _num(age) > 10:
            return None
    return book


def _replay(model, frames, managed=True):
    """One hedged paper basket, executable quote sides, prior-minute decisions."""
    positions, events, equity = {}, [], []
    cash, start_credit, entry_time, reentries = 0.0, None, None, 0
    previous = None
    confirmations = {"UP": 0, "DOWN": 0, "RESET": 0}
    incomplete = False

    def transact(sample, name, side, strike, qty, action):
        nonlocal cash
        book = _book_at(sample, strike, side)
        if not book:
            return False
        price = _num(book["ask"] if qty > 0 else book["bid"])
        cash -= qty * price
        if name in positions:
            positions[name]["qty"] += qty
            if not positions[name]["qty"]:
                del positions[name]
        else:
            positions[name] = {"qty": qty, "strike": strike, "side": side,
                               "entry": price, "best_ask": price}
        events.append({"time": sample["time"].isoformat(), "action": action,
                       "leg": name, "price": round(price, 2), "strike": strike})
        return True

    for frame in frames:
        sample = frame["sample"]
        clock = sample["time"].strftime("%H:%M")
        if not "09:35" <= clock <= "15:25":
            previous = frame
            continue
        if not positions and start_credit is None:
            if not previous:
                previous = frame
                continue
            atm = frame["atm_strike"]
            candidates = [row["strike"] for row in sample["rows"]]
            ce_wing = min(candidates, key=lambda strike: abs(strike - (atm + 1000)))
            pe_wing = min(candidates, key=lambda strike: abs(strike - (atm - 1000)))
            if (abs(ce_wing - atm - 1000) > 100 or abs(atm - pe_wing - 1000) > 100
                    or not all(_book_at(sample, strike, side) for strike, side in (
                        (atm, "ce"), (atm, "pe"), (ce_wing, "ce"), (pe_wing, "pe")))):
                previous = frame
                continue
            # Long wings first, then short ATM legs. The basket has bounded payoff.
            for name, side, strike, qty in (
                    ("CE_WING", "ce", ce_wing, 1), ("PE_WING", "pe", pe_wing, 1),
                    ("CE", "ce", atm, -1), ("PE", "pe", atm, -1)):
                transact(sample, name, side, strike, qty, "ENTRY")
            start_credit = cash
            entry_time = sample["time"].isoformat()
            if start_credit <= 0:
                incomplete = True
                break
        elif positions:
            marks = {}
            for name, leg in positions.items():
                book = _book_at(sample, leg["strike"], leg["side"])
                if not book:
                    incomplete = True
                    break
                marks[name] = _num(book["ask"] if leg["qty"] < 0 else book["bid"])
            if incomplete:
                break
            unrealized = cash + sum(leg["qty"] * marks[name]
                                    for name, leg in positions.items())
            equity.append({"time": sample["time"].isoformat(),
                           "points": round(unrealized, 2)})
            if managed:
                fresh_previous = (previous if previous and 0 <
                                  (sample["time"] - previous["sample"]["time"]).total_seconds()
                                  <= 120 else None)
                probabilities, risk = (_predict(model, fresh_previous["features"])
                                       if fresh_previous else ([0, 1, 0], {"ce": 0.25, "pe": 0.25}))
                for name in ("CE", "PE"):
                    if name in positions:
                        leg = positions[name]
                        leg["best_ask"] = min(leg["best_ask"], marks[name])
                active = [name for name in ("CE", "PE") if name in positions]
                if len(active) == 2:
                    # Per-leg caps remain even if the classifier has low conviction.
                    losses = {}
                    for name in active:
                        leg = positions[name]
                        width = max(0.18, min(0.55, 1.35 * risk[name.lower()]))
                        losses[name] = (marks[name] - leg["entry"]) / leg["entry"] - width
                    loser = max(losses, key=losses.get)
                    signal = ("CE" if probabilities[2] > 0.62
                              and probabilities[2] - probabilities[0] > 0.14 else
                              "PE" if probabilities[0] > 0.62
                              and probabilities[0] - probabilities[2] > 0.14 else None)
                    confirmations["UP"] = confirmations["UP"] + 1 if signal == "CE" else 0
                    confirmations["DOWN"] = confirmations["DOWN"] + 1 if signal == "PE" else 0
                    if losses[loser] > 0:
                        transact(sample, loser, positions[loser]["side"],
                                 positions[loser]["strike"], 1, "MODEL_RISK_STOP")
                    elif signal and confirmations["UP" if signal == "CE" else "DOWN"] >= 2:
                        transact(sample, signal, positions[signal]["side"],
                                 positions[signal]["strike"], 1, "MODEL_TREND_EXIT")
                elif len(active) == 1:
                    name = active[0]
                    leg = positions[name]
                    trend = max(probabilities[0], probabilities[2])
                    trail = max(0.06, min(0.30, 0.55 * risk[name.lower()]
                                         * (1.25 if fresh_previous and fresh_previous["features"][7] < 0.25 else 0.85
                                            if trend > 0.65 else 1)))
                    trail_hit = (leg["best_ask"] < leg["entry"]
                                 and marks[name] >= leg["best_ask"] + trail * leg["entry"])
                    reset = trend < 0.52 or trail_hit
                    confirmations["RESET"] = confirmations["RESET"] + 1 if reset else 0
                    missing = "PE" if name == "CE" else "CE"
                    if confirmations["RESET"] >= 2 and reentries < 2:
                        strike = positions[name]["strike"]
                        if (abs(sample["spot"] - strike) <= 150
                                and _book_at(sample, strike, missing.lower())):
                            transact(sample, missing, missing.lower(), strike, -1,
                                     "MODEL_REENTER_MISSING")
                            reentries += 1
                            confirmations["RESET"] = 0
                    # A winning leg can reverse violently; the hard stop still applies.
                    if name in positions and (marks[name] - leg["entry"]) / leg["entry"] > 0.55:
                        transact(sample, name, leg["side"], leg["strike"], 1,
                                 "HARD_LEG_STOP")
            if unrealized <= -50:
                events.append({"time": sample["time"].isoformat(),
                               "action": "SESSION_RISK_EXIT", "points": round(unrealized, 2)})
                clock = "15:25"
            if clock >= "15:25":
                for name in list(positions):
                    leg = positions[name]
                    if not transact(sample, name, leg["side"], leg["strike"],
                                    -leg["qty"], "SESSION_EXIT"):
                        incomplete = True
                        break
                if not incomplete:
                    equity.append({"time": sample["time"].isoformat(), "points": round(cash, 2)})
                break
        previous = frame
    if positions:
        incomplete = True
    return {"entry_time": entry_time, "complete": bool(entry_time and not incomplete),
            "pnl_points": round(cash, 2) if entry_time and not incomplete else None,
            "initial_credit_points": round(start_credit, 2) if start_credit is not None else None,
            "reentries": reentries, "events": events[:100], "equity": equity[::max(1, len(equity)//180)]}


def train_research(research: MLResearch):
    days = research._minutes()
    frames = {day: _features_for_day(samples) for day, samples in days.items()}
    eligible = sorted(day for day, items in frames
                      .items() if sum(item["class"] is not None for item in items) >= 100)
    if len(eligible) < 3:
        report = {"status": "insufficient_sessions", "eligible_days": eligible,
                  "message": "At least three distinct usable sessions are needed for an n-1 test."}
        with research.lock, research.db:
            research.db.execute("INSERT OR REPLACE INTO artifacts VALUES ('report',?)",
                                (json.dumps(report),))
        return report
    test_day = eligible[-1]
    cutoff = (datetime.fromisoformat(test_day) - timedelta(days=60)).date().isoformat()
    prior = [day for day in eligible[:-1] if day >= cutoff]
    model = _fit(frames, prior)
    if model is None:
        report = {"status": "insufficient_training_rows", "eligible_days": eligible}
    else:
        evaluation = _evaluation(model, frames[test_day])
        replay = _replay(model, frames[test_day], managed=True)
        baseline = _replay(model, frames[test_day], managed=False)
        action_report, action_candidate = _action_research(frames,eligible,prior,test_day)
        future_model = _fit(frames, [day for day in eligible if day >= cutoff])
        rolling = []
        for day in eligible[-MIN_TEST_DAYS:]:
            earlier = [item for item in eligible if cutoff <= item < day]
            if len(earlier) < 2:
                continue
            prior_model = model if day == test_day else _fit(frames, earlier)
            if prior_model:
                score = _evaluation(prior_model, frames[day])
                rolling.append({"day": day, "train_days": len(earlier),
                                "accuracy": score["accuracy"],
                                "baseline_accuracy": score["baseline_accuracy"]})
        report = {"status": "exploratory" if len(prior) < MIN_TRAIN_DAYS
                  or len(rolling) < MIN_TEST_DAYS else "paper_validation",
                  "train_days": prior, "test_day": test_day, "train_rows": model["rows"],
                  "test": evaluation, "rolling_out_of_sample": rolling, "replay": replay,
                  "action_model": action_report,
                  "hold_baseline_pnl_points": baseline["pnl_points"],
                  "candidate_trained_through": future_model["trained_days"][-1]
                  if future_model else None,
                  "feature_names": FEATURES, "horizon_minutes": HORIZON,
                  "max_training_day_weight": model["max_day_weight_share"],
                  "promotion_ready": False,
                  "promotion_reason": (f"Requires at least {MIN_TRAIN_DAYS} independent training "
                                       f"days and {MIN_TEST_DAYS} rolling out-of-sample days; "
                                       "brokerage/taxes and execution quality remain unverified."),
                  "pnl_units": "quoted premium points per one option unit, before brokerage and taxes"}
        with research.lock, research.db:
            research.db.execute("INSERT OR REPLACE INTO artifacts VALUES ('candidate',?)",
                                (json.dumps(future_model),))
            if action_candidate:
                research.db.execute("INSERT OR REPLACE INTO artifacts VALUES ('action_candidate',?)",
                                    (json.dumps(action_candidate),))
    with research.lock, research.db:
        research.db.execute("INSERT OR REPLACE INTO artifacts VALUES ('report',?)",
                            (json.dumps(report),))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("import", "train", "status"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--option-chain", type=Path)
    parser.add_argument("--trades", type=Path)
    parser.add_argument("--spot", type=Path)
    args = parser.parse_args(argv)
    research = MLResearch(args.root)
    try:
        if args.action == "import":
            if not args.option_chain:
                parser.error("--option-chain is required for import")
            print(json.dumps(research.import_directory(args.option_chain, args.trades,
                                                       args.spot), indent=2))
        elif args.action == "train":
            print(json.dumps(research.train(), indent=2))
        else:
            print(json.dumps(research.status(), indent=2))
    finally:
        research.close()


if __name__ == "__main__":
    main()
