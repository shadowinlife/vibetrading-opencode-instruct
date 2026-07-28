"""Multi-period momentum composite signal builders."""
from __future__ import annotations
import pandas as pd

EPS = 1e-12


def _rolling_zscore(s: pd.Series, window: int = 250, min_periods: int = 63) -> pd.Series:
    mu = s.rolling(window, min_periods=min_periods).mean()
    sg = s.rolling(window, min_periods=min_periods).std() + EPS
    return (s - mu) / sg


def _multi_period_momentum_z(close: pd.Series, periods: list[int]) -> pd.Series:
    z_scores = []
    for p in periods:
        ret = close.pct_change(p)
        z = _rolling_zscore(ret)
        z_scores.append(z)
    return pd.concat(z_scores, axis=1).mean(axis=1)


def mom_short_z(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series, amount: pd.Series) -> pd.Series:
    """Short-term momentum: 3,5,7,10 day returns."""
    return _multi_period_momentum_z(close, [3, 5, 7, 10])


def mom_mid_z(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series, amount: pd.Series) -> pd.Series:
    """Mid-term momentum: 5,10,15,20 day returns."""
    return _multi_period_momentum_z(close, [5, 10, 15, 20])


def mom_long_z(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series, amount: pd.Series) -> pd.Series:
    """Long-term momentum: 30,60,90 day returns."""
    return _multi_period_momentum_z(close, [30, 60, 90])


def mom_all_z(close: pd.Series, high: pd.Series, low: pd.Series, vol: pd.Series, amount: pd.Series) -> pd.Series:
    """All-period momentum: 3,5,7,10,15,20,30,60,90 day returns."""
    return _multi_period_momentum_z(close, [3, 5, 7, 10, 15, 20, 30, 60, 90])
