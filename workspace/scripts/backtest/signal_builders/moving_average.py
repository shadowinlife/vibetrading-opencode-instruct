"""Moving average signal builders: deviation, spread, and alignment Z-scores."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.backtest.signal_builders._common import EPS, rolling_zscore


def ma_deviation_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    period: int = 120,
    **kwargs,
) -> pd.Series:
    """MA deviation Z-score: (close / MA - 1) normalized."""
    ma = close.rolling(period).mean()
    deviation = pd.Series(close / (ma + EPS) - 1, index=close.index)
    return rolling_zscore(deviation)


def ma_spread_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    fast: int = 5,
    slow: int = 20,
    **kwargs,
) -> pd.Series:
    """Dual MA spread Z-score: (MA_fast / MA_slow - 1) normalized."""
    ma_fast = close.rolling(fast).mean()
    ma_slow = close.rolling(slow).mean()
    spread = pd.Series(ma_fast / (ma_slow + EPS) - 1, index=close.index)
    return rolling_zscore(spread)


def ma_alignment_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    """MA alignment score: count of MAs in bullish order (5>10>20>60),
    normalized to [0,1] then Z-scored."""
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    score = pd.Series(
        (
            (ma5 > ma10).astype(float)
            + (ma10 > ma20).astype(float)
            + (ma20 > ma60).astype(float)
        )
        / 3.0,
        index=close.index,
    )
    return rolling_zscore(score)
