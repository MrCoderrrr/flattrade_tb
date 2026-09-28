"""Stable strategy names and per-version execution settings."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StrategySpec:
    id: str
    market: str
    version: int
    description: str
    entry_start: str
    entry_end: str
    flatten: str
    bar_minutes: int
    max_entries: int
    cooldown_seconds: int
    session_loss_per_unit: float
    trade_risk_per_unit: float | None
    enabled: bool = True


SPECS = {
    "nfv1": StrategySpec("nfv1", "NIFTY", 1, "Original-style KAMA ATM strangle · 1000-point wings · paper adaptation", "09:18", "15:33", "15:34", 1, 30, 60, 4000, 100000),
    "mcxv1": StrategySpec("mcxv1", "MCX", 1, "Original KAMA ATM straddle · unhedged · paper adaptation", "16:00", "23:22", "23:24", 1, 30, 60, 6000, None),
    "nfv2": StrategySpec("nfv2", "NIFTY", 2, "Selective range iron condor", "09:45", "14:00", "15:30", 5, 2, 1800, 1000, 2000),
    "mcxv2": StrategySpec("mcxv2", "MCX", 2, "Selective trend credit spread", "16:30", "22:30", "23:15", 5, 2, 1800, 1000, 2000),
    "nfv3": StrategySpec("nfv3", "NIFTY", 3, "Active EMA / RSI / session mean · hedged", "09:20", "15:33", "15:34", 1, 20, 60, 4000, 10000),
    "nfv4": StrategySpec("nfv4", "NIFTY", 4, "Closed-candle structure · breakout/retest/rejection · hedged paper", "09:31", "15:15", "15:34", 1, 12, 60, 4000, 8000),
    "nfv5": StrategySpec("nfv5", "NIFTY", 5, "One-second flow · ATM straddle · 1000-point wings · adaptive leg stops · paper", "09:20", "15:33", "15:34", 1, 60, 30, 4000, 100000),
    "mcxv3": StrategySpec("mcxv3", "MCX", 3, "ATM straddle · KAMA(10,3,30) / EMA trend · leg stops", "16:05", "23:22", "23:24", 1, 24, 60, 6000, None),
}

# Retired versions remain addressable for historical journals and replay, but
# are no longer offered for new dashboard sessions.
ARCHIVED_IDS = frozenset({"nfv2", "nfv4", "mcxv2"})


def resolve(market, strategy_id=None):
    name = strategy_id or ("nfv2" if market == "NIFTY" else "mcxv2")
    if not isinstance(name, str) or name not in SPECS or SPECS[name].market != market:
        raise ValueError("Choose a strategy belonging to this market")
    return SPECS[name]


def catalog():
    return [{**asdict(spec), "available": spec.id not in ARCHIVED_IDS}
            for spec in SPECS.values()]
