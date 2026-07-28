"""Unified signal builder registry for the backtest framework.

All 15 signal builders accept the generic interface::

    def signal_builder(
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        vol: pd.Series,
        amount: pd.Series,
        **kwargs,
    ) -> pd.Series

Each returns a Z-score signal Series. Some builders accept optional
keyword arguments (e.g. ``open``, ``moneyflow``, ``margin``).
"""

from __future__ import annotations

import functools
from typing import Callable

from scripts.backtest.signal_builders.moving_average import (
    ma_deviation_z,
    ma_spread_z,
    ma_alignment_z,
)
from scripts.backtest.signal_builders.momentum import (
    roc_composite_z,
    trend_trinity_z,
    breakout_z,
    sustain_z,
)
from scripts.backtest.signal_builders.mean_reversion import (
    resi_rev_z,
    oversold_z,
    time_exh_z,
)
from scripts.backtest.signal_builders.volume_price import (
    divergence_z,
    vol_anomaly_z,
    order_flow_z,
)
from scripts.backtest.signal_builders.candlestick import (
    kbar_score_z,
    shadow_z,
)
from scripts.backtest.signal_builders.composite import (
    regime_composite_z,
    mf_confirmation_z,
    ic_weighted_z,
)
from scripts.backtest.signal_builders.multi_momentum import (
    mom_short_z,
    mom_mid_z,
    mom_long_z,
    mom_all_z,
)

SIGNAL_REGISTRY: dict[str, Callable] = {
    "roc_composite_z": roc_composite_z,
    "trend_trinity_z": trend_trinity_z,
    "breakout_z": breakout_z,
    "sustain_z": sustain_z,
    "resi_rev_z": resi_rev_z,
    "oversold_z": oversold_z,
    "time_exh_z": time_exh_z,
    "divergence_z": divergence_z,
    "vol_anomaly_z": vol_anomaly_z,
    "order_flow_z": order_flow_z,
    "kbar_score_z": kbar_score_z,
    "shadow_z": shadow_z,
    "regime_composite_z": regime_composite_z,
    "mf_confirmation_z": mf_confirmation_z,
    "ic_weighted_z": ic_weighted_z,
    "ma_deviation_120_z": functools.partial(ma_deviation_z, period=120),
    "ma_deviation_250_z": functools.partial(ma_deviation_z, period=250),
    "ma_spread_5x20_z": functools.partial(ma_spread_z, fast=5, slow=20),
    "ma_alignment_z": ma_alignment_z,
    "mom_short_z": mom_short_z,
    "mom_mid_z": mom_mid_z,
    "mom_long_z": mom_long_z,
    "mom_all_z": mom_all_z,
}


def get_signal_builder(name: str) -> Callable:
    if name not in SIGNAL_REGISTRY:
        available = ", ".join(sorted(SIGNAL_REGISTRY))
        raise ValueError(f"Unknown signal builder: {name!r}. Available: {available}")
    return SIGNAL_REGISTRY[name]


def list_signal_builders() -> list[str]:
    return sorted(SIGNAL_REGISTRY)


__all__ = [
    "SIGNAL_REGISTRY",
    "get_signal_builder",
    "list_signal_builders",
]
