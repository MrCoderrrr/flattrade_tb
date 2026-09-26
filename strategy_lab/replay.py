"""Chronological, offline paper replay using the dashboard's actual controller.

Input is a JSONL stream of recorded snapshots, not a CSV of previous strategy
trades. Missing data stays missing. No final-price liquidation or reconstructed
option premium is permitted. See docs/strategy_research.md for the schema and
limits of the resulting observed-sample statistics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory

from .market_data import FeedError
from .models import Bar, Contract, IST, Quote
from .runtime import Controller


ENTRY = {"NIFTY": time(9, 45), "MCX": time(16, 30)}
DEADLINE = {"NIFTY": time(15, 30), "MCX": time(23, 15)}


class DataError(ValueError):
    """Input cannot be replayed without guessing or changing chronology."""


@dataclass(frozen=True)
class Snapshot:
    timestamp: datetime
    market: str
    bars: list[Bar]
    quotes: list[Quote]


def _number(value, name, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"{name} must be a JSON number")
    try:
        valid = math.isfinite(value) and (value >= 0 if zero else value > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise DataError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
    return value


def _integer(value, name, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise DataError(f"{name} must be a {'nonnegative' if zero else 'positive'} integer")
    return value


def _time(value, name):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise DataError(f"{name} must be an ISO datetime") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise DataError(f"{name} must contain a timezone offset")
    return stamp.astimezone(IST)


def _object(row, required, optional=()):
    if not isinstance(row, dict) or set(row) - set(required) - set(optional) or set(required) - set(row):
        raise DataError(f"Object fields must be {', '.join(required)}; optional: {', '.join(optional) or 'none'}")


def parse_snapshot(row) -> Snapshot:
    _object(row, ("timestamp", "market", "bars", "quotes"))
    stamp = _time(row["timestamp"], "snapshot timestamp")
    market = row["market"]
    if market not in ENTRY or not isinstance(row["bars"], list) or not isinstance(row["quotes"], list):
        raise DataError("market must be NIFTY/MCX; bars and quotes must be arrays")
    bars = []
    for item in row["bars"]:
        _object(item, ("timestamp", "open", "high", "low", "close"), ("volume",))
        bar_time = _time(item["timestamp"], "bar timestamp")
        if bar_time.second or bar_time.microsecond or bar_time.minute % 5:
            raise DataError("Bar timestamps must be five-minute bar opens")
        if bar_time + timedelta(minutes=5) > stamp:
            raise DataError("Input includes a future or incomplete bar")
        if bars and bar_time <= bars[-1].timestamp:
            raise DataError("Bars must be strictly chronological without duplicates")
        prices = {key: _number(item[key], key) for key in ("open", "high", "low", "close")}
        if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
            raise DataError("Inconsistent OHLC bounds")
        bars.append(Bar(bar_time, **prices, volume=_number(item.get("volume", 0), "volume", zero=True)))
    quotes, seen_tokens, seen_symbols = [], set(), set()
    for item in row["quotes"]:
        _object(item, ("contract", "timestamp", "bid", "ask", "last"), ("bid_size", "ask_size"))
        c = item["contract"]
        _object(c, ("symbol", "token", "exchange", "expiry", "strike", "option_type", "lot_size", "tick_size"))
        if not all(isinstance(c[field], str) and c[field].strip() for field in ("symbol", "token", "exchange", "option_type")):
            raise DataError("Contract identifiers must be nonempty strings")
        if c["exchange"] != ("NFO" if market == "NIFTY" else "MCX"):
            raise DataError("Contract exchange does not match the snapshot market")
        prefixes = ("NIFTY",) if market == "NIFTY" else ("NATURALGAS", "NATGASMINI")
        if not c["symbol"].startswith(prefixes) or c["option_type"] not in ("CE", "PE"):
            raise DataError("Snapshot needs the stated underlying's CE/PE option contracts")
        try:
            expiry = date.fromisoformat(c["expiry"])
        except (TypeError, ValueError):
            raise DataError("Contract expiry must be an ISO date") from None
        contract = Contract(c["symbol"], c["token"], c["exchange"], expiry,
                            _number(c["strike"], "strike"), c["option_type"],
                            _integer(c["lot_size"], "lot_size"), _number(c["tick_size"], "tick_size"))
        identity = (contract.exchange, contract.token)
        if identity in seen_tokens or contract.symbol in seen_symbols:
            raise DataError("Duplicate contract quote in one snapshot")
        seen_tokens.add(identity)
        seen_symbols.add(contract.symbol)
        quote_time = _time(item["timestamp"], "quote timestamp")
        if quote_time > stamp:
            raise DataError("A quote timestamp cannot be later than its snapshot")
        bid, ask, last = (_number(item[field], field) for field in ("bid", "ask", "last"))
        if bid > ask:
            raise DataError("Crossed bid/ask quote")
        if any(abs(value / contract.tick_size - round(value / contract.tick_size)) > 1e-6 for value in (bid, ask)):
            raise DataError("Bid/ask is off the contract tick")
        quotes.append(Quote(contract, quote_time, bid, ask, last,
                            _integer(item.get("bid_size", 0), "bid_size", zero=True),
                            _integer(item.get("ask_size", 0), "ask_size", zero=True)))
    return Snapshot(stamp, market, bars, quotes)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise DataError(f"Nonfinite JSON constant: {value}")


def load_snapshots(path: str | Path) -> list[Snapshot]:
    snapshots, seen, metadata = [], set(), {}
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                if not line.strip():
                    raise DataError("Blank lines are not snapshots")
                row = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                snapshot = parse_snapshot(row)
                if snapshots and snapshot.timestamp < snapshots[-1].timestamp:
                    raise DataError("Snapshot timestamps must be globally nondecreasing; merge market streams by time first")
                key = (snapshot.timestamp, snapshot.market)
                if key in seen:
                    raise DataError("Duplicate market/timestamp snapshot")
                seen.add(key)
                for quote in snapshot.quotes:
                    contract_key = (snapshot.timestamp.date(), quote.contract.exchange, quote.contract.token)
                    previous = metadata.setdefault(contract_key, quote.contract)
                    if previous != quote.contract:
                        raise DataError("Contract metadata changed within the same day")
                snapshots.append(snapshot)
            except (DataError, json.JSONDecodeError, OverflowError) as exc:
                raise DataError(f"Line {line_number}: {exc}") from None
    if not snapshots:
        raise DataError("No historical snapshots supplied")
    return snapshots


class ReplayFeed:
    """Only previously observed, independently fresh market data is accessible."""
    def __init__(self):
        self.latest: dict[str, Snapshot] = {}

    def update(self, snapshots):
        for snapshot in snapshots:
            self.latest[snapshot.market] = snapshot

    def snapshot(self, market, now, contracts=None):
        observed = self.latest.get(market)
        if observed is None or not 0 <= (now - observed.timestamp).total_seconds() <= 10:
            raise FeedError(f"Replay has no fresh {market} snapshot; prices are not interpolated")
        quotes = observed.quotes
        if contracts:
            required = {(contract.exchange, contract.token) for contract in contracts}
            quotes = [quote for quote in quotes if (quote.contract.exchange, quote.contract.token) in required]
        return observed.bars, quotes


def _coverage_record(snapshot):
    return {"date": snapshot.timestamp.date().isoformat(), "market": snapshot.market,
            "first_snapshot": snapshot.timestamp.isoformat(), "last_snapshot": snapshot.timestamp.isoformat(),
            "snapshot_count": 0, "max_snapshot_gap_seconds": 0., "issues": [],
            "authorized": False, "entries": 0, "closed_trades": 0, "fill_count": 0,
            "estimated_costs": 0., "realized_gross_pnl": 0., "observed_net_pnl": 0., "positions": []}


def _issue(record, issue):
    if issue not in record["issues"]:
        record["issues"].append(issue)


def run_replay(snapshots: list[Snapshot], *, capital=200_000, multiplier=1) -> dict:
    """Run one fixed-parameter sample with a fresh, isolated controller ledger."""
    if not snapshots:
        raise DataError("Cannot replay an empty sample")
    if type(multiplier) is not int or not 1 <= multiplier <= 100:
        raise DataError("Multiplier must be an integer from 1 to 100")
    _number(capital, "capital")
    if capital < 200_000 * multiplier:
        raise DataError("Each multiplier requires at least INR 200,000 of shared capital")
    # Programmatic callers get the same ordering guarantees as the JSONL loader.
    keys = [(row.timestamp, row.market) for row in snapshots]
    if any(right[0] < left[0] for left, right in zip(keys, keys[1:])) or len(keys) != len(set(keys)):
        raise DataError("Snapshots must be chronological with unique market/timestamp keys")
    now = snapshots[0].timestamp
    feed = ReplayFeed()
    coverage, attempted, equity_curve = {}, set(), []
    peak, max_drawdown, stale_points = 0., 0., 0
    allocation = 200_000 * multiplier
    with TemporaryDirectory(prefix="strategy-replay-") as directory:
        controller = Controller(Path(directory), feed=feed, clock=lambda: now)
        try:
            for now, grouped in groupby(snapshots, key=lambda row: row.timestamp):
                rows = list(grouped)
                before = controller.status()
                previous_positions = {market: bool(session["positions"]) for market, session in before["sessions"].items()}
                for snapshot in rows:
                    key = (snapshot.timestamp.date().isoformat(), snapshot.market)
                    record = coverage.setdefault(key, _coverage_record(snapshot))
                    previous_time = datetime.fromisoformat(record["last_snapshot"])
                    gap = (now - previous_time).total_seconds() if record["snapshot_count"] else 0.
                    record["max_snapshot_gap_seconds"] = max(record["max_snapshot_gap_seconds"], gap)
                    # Sparse entry observations can miss different trades even if flat.
                    if gap > 10 and previous_time.time() < DEADLINE[snapshot.market] and now.time() >= ENTRY[snapshot.market]:
                        _issue(record, "snapshot_gap_exceeds_10_seconds")
                    record["last_snapshot"] = now.isoformat()
                    record["snapshot_count"] += 1
                feed.update(rows)
                for snapshot in rows:
                    key = (now.date().isoformat(), snapshot.market)
                    if key not in attempted and now.weekday() < 5 and ENTRY[snapshot.market] <= now.time() < DEADLINE[snapshot.market]:
                        attempted.add(key)
                        try:
                            controller.start(snapshot.market, "paper", multiplier, capital)
                            coverage[key]["authorized"] = True
                        except ValueError as exc:
                            _issue(coverage[key], f"authorization_rejected: {exc}")
                controller.tick()
                status = controller.status()
                stale = False
                for market, session in status["sessions"].items():
                    if not session.get("date"):
                        continue
                    key = (session["date"], market)
                    record = coverage.get(key)
                    if record is None:
                        continue
                    record.update(entries=session["entries"], fill_count=len(session["trades"]),
                                  estimated_costs=session["costs"], realized_gross_pnl=session["realized_pnl"],
                                  observed_net_pnl=session["net_pnl"], positions=session["positions"],
                                  last_state=session["state"], last_reason=session["reason"])
                    if previous_positions[market] and not session["positions"]:
                        record["closed_trades"] += 1
                    if session["positions"] and session.get("valuation_stale"):
                        stale = True
                        _issue(record, "unpriced_open_exposure")
                    if session["positions"] and session["state"] == "EXIT_PENDING":
                        _issue(record, "exit_was_pending_for_missing_or_invalid_data")
                    if session["date"] != now.date().isoformat() and session["positions"]:
                        _issue(record, "unresolved_exposure_carried_to_another_date")
                account = status["account"]
                observed_pnl = account["lifetime_pnl"] + account["daily_pnl"]
                if stale:
                    stale_points += 1
                else:
                    peak = max(peak, observed_pnl)
                    max_drawdown = max(max_drawdown, peak - observed_pnl)
                equity_curve.append({"timestamp": now.isoformat(), "net_pnl": round(observed_pnl, 4),
                                     "equity": round(allocation + observed_pnl, 4), "valuation_stale": stale})
            final = controller.status()
        finally:
            # Shutdown persists unresolved exposure; it never manufactures an exit.
            controller.shutdown()
    days = sorted({snapshot.timestamp.date().isoformat() for snapshot in snapshots})
    for key, record in coverage.items():
        first, last = (_time(record[field], field) for field in ("first_snapshot", "last_snapshot"))
        if first.time() > ENTRY[record["market"]]:
            _issue(record, "entry_window_start_not_observed")
        if last.time() < DEADLINE[record["market"]]:
            _issue(record, "flatten_deadline_not_observed")
        if not record["authorized"]:
            _issue(record, "session_not_authorized")
        if record["positions"]:
            _issue(record, "unresolved_positions_at_end_of_sample")
        record["complete"] = not record["issues"]
        record["booked_net_pnl"] = round(record["realized_gross_pnl"] - record["estimated_costs"], 4)
    daily = []
    for day in days:
        records = [coverage.get((day, market)) for market in ENTRY]
        missing = [market for market in ENTRY if (day, market) not in coverage]
        present = [record for record in records if record is not None]
        complete = not missing and all(record["complete"] for record in present)
        observed = sum(record["observed_net_pnl"] for record in present)
        booked = sum(record["booked_net_pnl"] for record in present)
        daily.append({"date": day, "complete": complete, "missing_markets": missing,
                      "net_pnl": round(observed, 4) if complete else None,
                      "observed_net_pnl": round(observed, 4), "booked_net_pnl": round(booked, 4),
                      "return_pct_on_shared_allocation": round(observed / allocation * 100, 6) if complete else None})
    by_month = defaultdict(list)
    for day in daily:
        by_month[day["date"][:7]].append(day)
    monthly = []
    for month, records in sorted(by_month.items()):
        complete = all(record["complete"] for record in records)
        net = sum(record["observed_net_pnl"] for record in records)
        monthly.append({"month": month, "observed_dates": len(records),
                        "excluded_incomplete_dates": [record["date"] for record in records if not record["complete"]],
                        "observed_net_pnl": round(net, 4),
                        "net_pnl_for_complete_observed_sample": round(net, 4) if complete else None,
                        "return_pct_for_complete_observed_sample": round(net / allocation * 100, 6) if complete else None,
                        "calendar_month_coverage_verified": False,
                        "calendar_month_return_pct": None})
    unresolved = [{"market": market, "session_date": session["date"], "positions": session["positions"],
                   "last_known_net_pnl": session["net_pnl"], "valuation_stale": session.get("valuation_stale", False)}
                  for market, session in final["sessions"].items() if session["positions"]]
    return {"mode": "offline_paper_replay", "performance_status": "UNVALIDATED",
            "capital": capital, "multiplier": multiplier, "return_denominator_shared_allocation": allocation,
            "coverage": {"first_timestamp": snapshots[0].timestamp.isoformat(), "last_timestamp": snapshots[-1].timestamp.isoformat(),
                         "dates": days, "snapshots": len(snapshots), "missing_calendar_days_not_filled": True},
            "entries": sum(record["entries"] for record in coverage.values()),
            "closed_trades": sum(record["closed_trades"] for record in coverage.values()),
            "fill_count": sum(record["fill_count"] for record in coverage.values()),
            "estimated_costs": round(sum(record["estimated_costs"] for record in coverage.values()), 4),
            "booked_net_pnl": round(sum(record["booked_net_pnl"] for record in coverage.values()), 4),
            "observed_last_equity_net_pnl": equity_curve[-1]["net_pnl"],
            "max_observed_equity_drawdown": round(max_drawdown, 4),
            "max_observed_equity_drawdown_pct": round(max_drawdown / allocation * 100, 6),
            "stale_equity_points_excluded_from_drawdown": stale_points,
            "drawdown_is_lower_bound_with_missing_ticks": any(record["issues"] for record in coverage.values()),
            "unresolved_positions": unresolved, "complete_observed_days": sum(day["complete"] for day in daily),
            "sessions": sorted(coverage.values(), key=lambda row: (row["date"], row["market"])),
            "daily": daily, "monthly": monthly, "equity_curve": equity_curve,
            "limitations": ["Fixed candidate parameters; no profitability or future return guarantee.",
                            "Fees and one-tick slippage are research assumptions, not verified broker charges or actual fills.",
                            "Missing prices are never interpolated and terminal positions are never closed at a stale last price.",
                            "Incomplete observed dates are excluded from finalized net/return fields; their partial marks remain visible.",
                            "No exchange-holiday calendar coverage proof: calendar-month returns remain null.",
                            "Observed drawdown cannot capture excursions between recorded quotes."]}


def evaluate(path: str | Path, *, capital=200_000, multiplier=1) -> dict:
    """Evaluate the full record and a chronological 70/30 split by whole dates."""
    snapshots = load_snapshots(path)
    days = sorted({snapshot.timestamp.date() for snapshot in snapshots})
    source_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    code_hash = hashlib.sha256(b"".join((Path(__file__).parent / filename).read_bytes()
                                       for filename in ("models.py", "strategies.py", "runtime.py", "replay.py"))).hexdigest()
    report = {"input_sha256": source_hash, "implementation_sha256": code_hash,
              "parameter_selection": "Fixed implementation; no fitting or optimization performed.",
              "all_data": run_replay(snapshots, capital=capital, multiplier=multiplier)}
    if len(days) < 2:
        report["split"] = {"available": False, "reason": "At least two distinct session dates are required; one date cannot be held out honestly."}
        return report
    split_index = max(1, min(len(days) - 1, math.floor(len(days) * 0.7)))
    test_start = days[split_index]
    training = [snapshot for snapshot in snapshots if snapshot.timestamp.date() < test_start]
    held_out = [snapshot for snapshot in snapshots if snapshot.timestamp.date() >= test_start]
    report["split"] = {"available": True, "method": "Chronological whole-date 70/30 split, rounded to dates; independent fresh ledgers and identical fixed parameters.",
                       "held_out_start": test_start.isoformat(), "training_dates": split_index,
                       "held_out_dates": len(days) - split_index,
                       "caveat": "A held-out label does not prove unseen data provenance, adequate sample size or profitability.",
                       "training": run_replay(training, capital=capital, multiplier=multiplier),
                       "held_out": run_replay(held_out, capital=capital, multiplier=multiplier)}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Chronologically merged JSONL historical snapshots")
    parser.add_argument("--capital", type=float, default=200_000)
    parser.add_argument("--multiplier", type=int, default=1)
    parser.add_argument("--output", type=Path, help="Write the full JSON research report here")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.path, capital=args.capital, multiplier=args.multiplier)
    except (DataError, OSError) as exc:
        parser.error(str(exc))
    serialized = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        print(f"Unvalidated replay report written to {args.output.resolve()}")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
