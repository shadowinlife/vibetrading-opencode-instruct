"""Volume-price signal builders: divergence, volume anomaly, order flow
— all computed from raw OHLCV Series."""

from __future__ import annotations

import pandas as pd

from scripts.backtest.signal_builders._common import (
    EPS,
    rolling_zscore,
    equal_weight_z,
    compute_corr,
    compute_cord,
    compute_vma_vstd,
    compute_wvma,
    compute_vsump_vsumn_vsumd,
)

VOLUME_ANOMALY_CAP = 5.0


def divergence_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    corr = compute_corr(close, vol, [20])
    cord = compute_cord(close, vol, [20])

    corr20_z = rolling_zscore(corr["CORR20"])
    cord20_z = rolling_zscore(cord["CORD20"])

    return pd.Series(
        [min(a, -b) for a, b in zip(corr20_z, cord20_z)],
        index=close.index,
    )


def vol_anomaly_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    vma, vstd = compute_vma_vstd(vol, [20])
    wvma = compute_wvma(close, vol, [20])

    factors = {**vma, **vstd, **wvma}
    raw = equal_weight_z(factors, ["VMA20", "VSTD20", "WVMA20"])
    return raw.clip(lower=-VOLUME_ANOMALY_CAP, upper=VOLUME_ANOMALY_CAP)


def order_flow_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    _, _, vsumd = compute_vsump_vsumn_vsumd(vol, [20])
    return rolling_zscore(vsumd["VSUMD20"])
