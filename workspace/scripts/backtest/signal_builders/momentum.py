"""Momentum-family signal builders: ROC composite, trend trinity, breakout,
sustainability — all computed from raw OHLCV Series."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.backtest.signal_builders._common import (
    EPS,
    rolling_zscore,
    equal_weight_z,
    compute_roc,
    compute_ma,
    compute_beta,
    compute_rsqr,
    compute_max,
    compute_qtlu,
    compute_cntp_cntn_cntd,
    compute_sump_sumn_sumd,
)

ROC_WINDOWS = [5, 10, 20, 30, 60]
MA_WINDOWS = [5, 10, 20, 30, 60]
BETA_WINDOWS = [20, 60]
RSQR_WINDOWS = [20, 60]
MAX_WINDOWS = [20, 60]
QTLU_WINDOWS = [20, 60]


def roc_composite_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    factors = compute_roc(close, ROC_WINDOWS)
    cols = [f"ROC{w}" for w in ROC_WINDOWS]
    return equal_weight_z(factors, cols)


def trend_trinity_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    ma_factors = compute_ma(close, MA_WINDOWS)
    beta_factors = compute_beta(close, BETA_WINDOWS)
    rsqr_factors = compute_rsqr(close, RSQR_WINDOWS)

    ma_cols = [f"MA{w}" for w in MA_WINDOWS]
    beta_cols = [f"BETA{w}" for w in BETA_WINDOWS]
    rsqr_cols = [f"RSQR{w}" for w in RSQR_WINDOWS]

    ma_z = equal_weight_z(ma_factors, ma_cols)
    beta_z = equal_weight_z(beta_factors, beta_cols)
    rsqr_z = equal_weight_z(rsqr_factors, rsqr_cols)

    return ma_z * 0.4 + beta_z * 0.3 + rsqr_z * 0.3


def breakout_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    max_factors = compute_max(high, close, MAX_WINDOWS)
    qtlu_factors = compute_qtlu(close, QTLU_WINDOWS)

    max_cols = [f"MAX{w}" for w in MAX_WINDOWS]
    qtlu_cols = [f"QTLU{w}" for w in QTLU_WINDOWS]

    max_z = equal_weight_z(max_factors, max_cols)
    qtlu_z = equal_weight_z(qtlu_factors, qtlu_cols)

    return (max_z + qtlu_z) * 0.5


def sustain_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    _, _, cntd = compute_cntp_cntn_cntd(close, [20])
    _, _, sumd = compute_sump_sumn_sumd(close, [20])

    cntd20_z = rolling_zscore(cntd["CNTD20"])
    sumd20_z = rolling_zscore(sumd["SUMD20"])

    return pd.Series(
        np.minimum(cntd20_z.to_numpy(), sumd20_z.to_numpy()),
        index=close.index,
    )
