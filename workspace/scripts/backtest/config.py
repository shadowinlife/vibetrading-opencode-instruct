"""Strategy configuration dataclass for backtest engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyConfig:
    """Configuration for a single backtest strategy.

    Supports both pure alpha158 signals AND moneyflow-confirmed signals
    via the optional confirm_col / confirm_positive / confirmation_threshold fields.

    Phase 5 execution-model flags (all opt-in, default disabled):
        t_plus_1: Enforce A-share T+1 settlement (cannot sell on entry day).
        price_limit: Enforce ±10% daily price-limit check (skip trade if limit hit).
        max_turnover_ratio: Cap position size by daily turnover (e.g. 0.1 = 10% ADV).
        slippage_model: Slippage model name (None, "fixed", "linear_impact").

    Cost model (applied independently in engine):
        commission_rate: Per-side brokerage commission (default 0.0003 = 0.03%).
            Applied to both buy and sell sides.
        stamp_tax_rate: Sell-side stamp tax (default 0.001 = 0.1%).
            Applied only to sell side. A-share: 0.05% since 2023-08-28.
        Round-trip cost = 2 * commission_rate + stamp_tax_rate.
    """

    name: str
    signal_col: str
    entry_z: float = 1.0
    exit_z: float = -0.5
    stop_loss: float = -0.15
    max_hold: int = 126
    vol_target: bool = False
    confirm_col: str | None = None
    confirm_positive: bool = True
    confirmation_threshold: float = 0.0

    # --- Phase 5: Execution model opt-in flags ---
    t_plus_1: bool = False
    price_limit: bool = False
    max_turnover_ratio: float | None = None
    slippage_model: str | None = None
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.001