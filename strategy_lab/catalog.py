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
    session_loss_per_unit: float | None
    trade_risk_per_unit: float | None
    enabled: bool = True


SPECS = {
    "nfv1": StrategySpec("nfv1", "NIFTY", 1, "KAMA/EMA confirmed ATM strangle · adaptive premium stops · 1000-point wings", "09:18", "15:34", "15:34", 1, 1000, 30, None, 100000),
    "mcxv1": StrategySpec("mcxv1", "MCX", 1, "Original KAMA ATM straddle · unhedged · paper adaptation", "16:00", "23:22", "23:24", 1, 1000, 5, None, None),
    "nfv3": StrategySpec("nfv3", "NIFTY", 3, "ATM short straddle · 1000-point call/put wings · indicator leg exits · continuous re-entry", "09:18", "15:34", "15:34", 1, 1000, 60, None, 65000),
    "nfv5": StrategySpec("nfv5", "NIFTY", 5, "One-second flow · ATM straddle · 1000-point wings · adaptive leg stops · paper", "09:18", "15:34", "15:34", 1, 60, 30, 4000, 100000),
    "mcxv3": StrategySpec("mcxv3", "MCX", 3, "ATM straddle · KAMA(10,3,30) / EMA trend · leg stops", "16:05", "23:22", "23:24", 1, 1000, 5, None, None),
}


def resolve(market, strategy_id=None):
    name = strategy_id or ("nfv3" if market == "NIFTY" else "mcxv3")
    if not isinstance(name, str) or name not in SPECS or SPECS[name].market != market:
        raise ValueError("Choose a strategy belonging to this market")
    return SPECS[name]


def catalog():
    return [asdict(spec) for spec in SPECS.values()]
