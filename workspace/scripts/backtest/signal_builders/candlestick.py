"""Candlestick signal builders: K-bar composite score and shadow trap
— computed from raw OHLCV Series.

Both signals require ``open`` price, passed via ``**kwargs``.
If ``open`` is not provided, ``close`` is used as a fallback.
"""

from __future__ import annotations

import pandas as pd

from scripts.backtest.signal_builders._common import (
    EPS,
    compute_kbar_features,
)


def kbar_score_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    open_ = kwargs.get("open", close)
    kbar = compute_kbar_features(open_, high, low, close)

    raw = (
        kbar["KMID2"]
        + kbar["KLEN"]
        - kbar["KUP2"]
        + kbar["KLOW2"]
        + kbar["KSFT2"]
    )

    mu = raw.rolling(5, min_periods=3).mean()
    sg = raw.rolling(5, min_periods=3).std() + EPS
    return (raw - mu) / sg


def shadow_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    open_ = kwargs.get("open", close)
    kbar = compute_kbar_features(open_, high, low, close)
    klow2 = kbar["KLOW2"]
    prev_klow2 = klow2.shift(1)

    return ((klow2 > 0.8) & (prev_klow2 > 0.6)).astype(float)
