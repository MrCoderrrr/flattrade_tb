"""Single source of truth for the requested strategy and risk parameters."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    paper_mode: bool = True
    ema_half_lives: tuple[float, float, float] = (15.0, 90.0, 300.0)
    rv_windows: tuple[int, int] = (60, 300)
    persistence_range: tuple[float, float] = (3.0, 30.0)
    adx_window_seconds: int = 300
    atr_period: int = 14
    account_reference: float = 195784.0
    combined_loss_limit_pct: float = 0.04
    stale_quote_seconds: float = 5.0
    recovery_seconds: float = 1.0
    recovery_ticks: int = 10
    ivr_window: int = 20
    ivr_size_factor: float = 0.5
    mcx_stop_loss_pct: float = 0.15
    mcx_max_flips: int = 4
    mcx_flip_cooldown_seconds: float = 45.0
    mcx_loss_limit_pct: float = 0.03
    mcx_k: float = 2.5
    nifty_dte_profiles: tuple[float, float, float] = (0.60, 1.00, 0.50)
    nifty_high_dte_persistence: float = 5.0
    nifty_low_dte_persistence: float = 5.0
    nifty_adx_threshold: float = 25.0
    nifty_trend_k: float = 3.0
    nifty_recenter_k: float = 1.5
    nifty_deceleration_k: float = 1.0
    nifty_low_dte_cutoff: str = "14:00"
    nifty_flatten_time: str = "15:15"


DEFAULT_CONFIG = EngineConfig()
