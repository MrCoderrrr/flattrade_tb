"""CSV trade audit trail with stable, explicit columns."""
import csv
from pathlib import Path
from .models import Trade, TriggerType

TRADE_LOG_COLUMNS = ("timestamp", "strategy_name", "dte_bucket", "state_at_entry",
                     "trigger_type", "entry_price", "exit_price", "pnl",
                     "adx_at_entry", "vr_ratio")


class TradeLogger:
    def __init__(self, path: str | Path = "trade_log.csv"):
        self.path = Path(path)
        if not self.path.exists():
            with self.path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS).writeheader()

    def log(self, trade: Trade, pnl: float = 0.0, *, dte_bucket: str = "",
            state_at_entry: str = "", entry_price: float | None = None,
            exit_price: float | None = None, adx_at_entry: float | None = None,
            vr_ratio: float | None = None) -> None:
        if not isinstance(trade.order.trigger_type, TriggerType):
            raise ValueError("invalid trigger_type")
        row = {"timestamp": trade.timestamp.isoformat(), "strategy_name": trade.order.strategy,
               "dte_bucket": dte_bucket, "state_at_entry": state_at_entry,
               "trigger_type": trade.order.trigger_type.value,
               "entry_price": trade.fill_price if entry_price is None else entry_price,
               "exit_price": "" if exit_price is None else exit_price, "pnl": pnl,
               "adx_at_entry": "" if adx_at_entry is None else adx_at_entry,
               "vr_ratio": "" if vr_ratio is None else vr_ratio}
        with self.path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=TRADE_LOG_COLUMNS).writerow(row)
