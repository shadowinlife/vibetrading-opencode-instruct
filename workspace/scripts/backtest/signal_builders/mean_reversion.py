"""Mean-reversion signal builders: RESI reversal, oversold composite,
time exhaustion — all computed from raw OHLCV Series."""

from __future__ import annotations

import pandas as pd

from scripts.backtest.signal_builders._common import (
    equal_weight_z,
    compute_resi,
    compute_rsv,
    compute_rank,
    compute_imin,
    compute_imax,
)

RESI_WINDOWS = [5, 10, 20]


def resi_rev_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    factors = compute_resi(close, RESI_WINDOWS)
    cols = [f"RESI{w}" for w in RESI_WINDOWS]
    composite = equal_weight_z(factors, cols)
    return -1.0 * composite


def oversold_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    rsv = compute_rsv(close, high, low, [5])
    rank = compute_rank(close, [5])
    factors = {**rsv, **rank}
    composite = equal_weight_z(factors, ["RSV5", "RANK5"])
    return -1.0 * composite


def time_exh_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    imin = compute_imin(low, [20])
    imax = compute_imax(high, [20])
    imxd = {"IMXD20": imax["IMAX20"] - imin["IMIN20"]}
    factors = {**imin, **imxd}
    return equal_weight_z(factors, ["IMIN20", "IMXD20"])
