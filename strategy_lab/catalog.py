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
    "nfv1": StrategySpec("nfv1", "NIFTY", 1, "Original user NIFTY engine · legacy launcher", "09:15", "15:34", "15:34", 1, 0, 0, 0, None, False),
    "mcxv1": StrategySpec("mcxv1", "MCX", 1, "Original user Natural Gas engine · legacy launcher", "16:00", "23:24", "23:24", 1, 0, 0, 0, None, False),
    "nfv2": StrategySpec("nfv2", "NIFTY", 2, "Selective range iron condor", "09:45", "14:00", "15:30", 5, 2, 1800, 1000, 2000),
    "mcxv2": StrategySpec("mcxv2", "MCX", 2, "Selective trend credit spread", "16:30", "22:30", "23:15", 5, 2, 1800, 1000, 2000),
    "nfv3": StrategySpec("nfv3", "NIFTY", 3, "Active EMA / RSI / session mean · hedged", "09:20", "15:33", "15:34", 1, 20, 60, 4000, 10000),
    "mcxv3": StrategySpec("mcxv3", "MCX", 3, "ATM straddle · KAMA(10,3,30) / EMA trend · leg stops", "16:05", "23:22", "23:24", 1, 24, 60, 6000, None),
}


def resolve(market, strategy_id=None):
    name = strategy_id or ("nfv2" if market == "NIFTY" else "mcxv2")
    if not isinstance(name, str) or name not in SPECS or SPECS[name].market != market:
        raise ValueError("Choose a strategy belonging to this market")
    return SPECS[name]


def catalog():
    return [asdict(spec) for spec in SPECS.values()]
