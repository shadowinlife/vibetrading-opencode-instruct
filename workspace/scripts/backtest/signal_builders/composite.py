"""Composite signal builders: regime-aware blending, moneyflow confirmation
gate, and IC-weighted dynamic signal.

``regime_composite_z`` and ``ic_weighted_z`` compute all 158 Alpha158 factors
internally from raw OHLCV via ``scripts.alpha158.compute_alpha158``.

``mf_confirmation_z`` requires external moneyflow and margin DataFrames
passed as keyword arguments; returns NaN series with a warning if absent.
"""

from __future__ import annotations

import warnings
from typing import cast

import numpy as np
import pandas as pd

from scripts.backtest.signal_builders._common import EPS

_SUB_SIGNALS = [
    "S_MOMENTUM", "S_REVERSAL", "S_VOLATILITY",
    "S_VOLUME", "S_TREND", "S_SENTIMENT",
]


def _build_all_factors_and_groups(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    open_: pd.Series | None = None,
) -> pd.DataFrame:
    """Compute all 158 Alpha158 factors and return DataFrame with group sub-signals."""
    from scripts.alpha158 import compute_alpha158

    if open_ is None:
        open_ = close

    idx = close.index
    df_ohlcv = pd.DataFrame({
        "trade_date": pd.Series(range(len(close)), index=idx),
        "open": open_.values,
        "high": high.values,
        "low": low.values,
        "close": close.values,
        "vol": vol.values,
        "amount": amount.values,
    })

    factors = compute_alpha158(df_ohlcv)
    factors.index = idx

    skip = {"trade_date", "ts_code"}
    feat_cols = [c for c in factors.columns if c not in skip]

    z_dict: dict[str, pd.Series] = {}
    for col in feat_cols:
        mu = factors[col].rolling(250, min_periods=63).mean()
        sg = factors[col].rolling(250, min_periods=63).std() + EPS
        z_dict[f"{col}_Z"] = (factors[col] - mu) / sg

    z_df = pd.concat(z_dict, axis=1)
    z_cols = list(z_df.columns)

    momentum = [z for z in z_cols if any(k in z for k in ["ROC", "MA", "BETA", "RSQR", "MAX", "QTLU", "RANK", "CNTP", "CNTD", "SUMP", "SUMD", "CORR"])]
    reversal = [z for z in z_cols if any(k in z for k in ["RESI", "MIN", "QTLD", "RSV", "CNTN", "IMAX", "IMIN", "SUMN"])]
    volatility = [z for z in z_cols if any(k in z for k in ["STD", "VSTD", "WVMA", "KLEN", "KUP", "KLOW"])]
    volume_g = [z for z in z_cols if any(k in z for k in ["VMA", "VSUMP", "VSUMN", "VSUMD", "KMID2", "KSFT2"])]
    trend_g = [z for z in z_cols if any(k in z for k in ["KMID_", "KSFT_", "CORD", "IMXD"]) or z in ["KMID_Z", "KSFT_Z"]]
    sentiment = [z for z in z_cols if any(k in z for k in ["OPEN0", "HIGH0", "LOW0", "VWAP0"])]

    def sm(cols: list[str]) -> pd.Series:
        return z_df[cols].mean(axis=1) if cols else pd.Series(0.0, index=z_df.index)

    groups = pd.concat({
        "S_MOMENTUM": sm(momentum),
        "S_REVERSAL": -sm(reversal),
        "S_VOLATILITY": -sm(volatility),
        "S_VOLUME": sm(volume_g),
        "S_TREND": sm(trend_g),
        "S_SENTIMENT": sm(sentiment),
        "_close": pd.Series(close.values, index=close.index),
    }, axis=1)

    return pd.concat([factors, z_df, groups], axis=1)


def regime_composite_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    open_ = kwargs.get("open")
    df = _build_all_factors_and_groups(close, high, low, vol, amount, open_)

    trend_ret = pd.Series(
        df["_close"].pct_change(5).to_numpy()
        + df["_close"].pct_change(20).to_numpy(),
        index=df.index,
    ).rolling(20).mean()
    regime = 1.0 / (1.0 + np.exp(-3.0 * np.nan_to_num(trend_ret, nan=0.0)))

    trend_comp = (
        0.30 * df["S_MOMENTUM"].fillna(0)
        + 0.25 * df["S_TREND"].fillna(0)
        + 0.20 * df["S_VOLUME"].fillna(0)
        + 0.15 * df["S_SENTIMENT"].fillna(0)
        + 0.10 * df["S_VOLATILITY"].fillna(0)
    )
    rev_comp = (
        0.35 * df["S_REVERSAL"].fillna(0)
        + 0.25 * df["S_VOLUME"].fillna(0)
        + 0.20 * df["S_SENTIMENT"].fillna(0)
        + 0.20 * df["S_VOLATILITY"].fillna(0)
    )
    composite = regime * trend_comp + (1.0 - regime) * rev_comp

    rm = composite.rolling(250, min_periods=63).mean()
    rs = composite.rolling(250, min_periods=63).std() + EPS
    return (composite - rm) / rs


def mf_confirmation_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    moneyflow = kwargs.get("moneyflow")
    margin = kwargs.get("margin")

    if moneyflow is None or margin is None:
        warnings.warn(
            "mf_confirmation_z requires 'moneyflow' and 'margin' DataFrames "
            "as keyword arguments. Returning NaN series.",
            stacklevel=2,
        )
        return pd.Series(np.nan, index=close.index)

    open_ = kwargs.get("open")
    df = _build_all_factors_and_groups(close, high, low, vol, amount, open_)

    trend_ret = pd.Series(
        df["_close"].pct_change(5).to_numpy()
        + df["_close"].pct_change(20).to_numpy(),
        index=df.index,
    ).rolling(20).mean()
    regime = 1.0 / (1.0 + np.exp(-3.0 * np.nan_to_num(trend_ret, nan=0.0)))

    trend_comp = (
        0.30 * df["S_MOMENTUM"].fillna(0)
        + 0.25 * df["S_TREND"].fillna(0)
        + 0.20 * df["S_VOLUME"].fillna(0)
        + 0.15 * df["S_SENTIMENT"].fillna(0)
        + 0.10 * df["S_VOLATILITY"].fillna(0)
    )
    rev_comp = (
        0.35 * df["S_REVERSAL"].fillna(0)
        + 0.25 * df["S_VOLUME"].fillna(0)
        + 0.20 * df["S_SENTIMENT"].fillna(0)
        + 0.20 * df["S_VOLATILITY"].fillna(0)
    )
    composite = regime * trend_comp + (1.0 - regime) * rev_comp
    rm = composite.rolling(250, min_periods=63).mean()
    rs = composite.rolling(250, min_periods=63).std() + EPS
    df["COMPOSITE_Z"] = (composite - rm) / rs

    mf_cols = ["buy_lg_amount", "sell_lg_amount", "daily_amount", "net_d5_amount"]
    for col in mf_cols:
        if col in moneyflow.columns:
            df[f"_mf_{col}"] = moneyflow[col].reindex(df.index).fillna(0)
        else:
            df[f"_mf_{col}"] = 0.0

    buy = df["_mf_buy_lg_amount"]
    sell = df["_mf_sell_lg_amount"]
    amt = df["_mf_daily_amount"].replace(0, 1.0)
    mf_raw = (buy - sell) / (amt + EPS)
    mu_mf = mf_raw.rolling(20, min_periods=10).mean()
    sg_mf = mf_raw.rolling(20, min_periods=10).std() + EPS
    mf_big_z = (mf_raw - mu_mf) / sg_mf

    if "rzmre" in margin.columns:
        rzmre = margin["rzmre"].reindex(df.index).replace(0, np.nan)
    else:
        rzmre = pd.Series(np.nan, index=df.index)
    margin_chg = rzmre.pct_change(5, fill_method=None).fillna(0)
    mu_mg = margin_chg.rolling(20, min_periods=10).mean()
    sg_mg = margin_chg.rolling(20, min_periods=10).std() + EPS
    margin_buy_z = (margin_chg - mu_mg) / sg_mg

    d5 = df["_mf_net_d5_amount"]
    mf_gate_d5 = (d5 > 0).astype(float)

    votes = (
        (mf_big_z.fillna(0) > 0).astype(int)
        + (margin_buy_z.fillna(0) > 0).astype(int)
        + (mf_gate_d5.fillna(0) > 0).astype(int)
    )
    mf_gate = (votes >= 2).astype(int)

    result = df["COMPOSITE_Z"].where(mf_gate == 1, 0.0)
    return pd.Series(result, index=close.index)


def ic_weighted_z(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vol: pd.Series,
    amount: pd.Series,
    **kwargs,
) -> pd.Series:
    open_ = kwargs.get("open")
    df = _build_all_factors_and_groups(close, high, low, vol, amount, open_)
    fwd_shift = 5

    fwd_ret5 = (df["_close"].shift(-5) - df["_close"]) / (df["_close"] + EPS)

    window_ic = 63
    min_periods_ic = 30
    f_ranked = fwd_ret5.rolling(window_ic, min_periods=min_periods_ic).rank(pct=False)

    def _rolling_spearman_ic(sub_signal: pd.Series) -> pd.Series:
        s_ranked = sub_signal.rolling(window_ic, min_periods=min_periods_ic).rank(pct=False)
        return cast(
            pd.Series,
            s_ranked.rolling(window_ic, min_periods=min_periods_ic).corr(f_ranked),
        )

    ic_dict: dict[str, pd.Series] = {}
    for s in _SUB_SIGNALS:
        ic_raw = _rolling_spearman_ic(cast(pd.Series, df[s]))
        ic_dict[f"IC_{s}"] = ic_raw.shift(fwd_shift)

    ir_dict: dict[str, pd.Series] = {}
    for s in _SUB_SIGNALS:
        ic_raw = ic_dict[f"IC_{s}"]
        ic_mean = ic_raw.rolling(window_ic, min_periods=min_periods_ic).mean()
        ic_std = ic_raw.rolling(window_ic, min_periods=min_periods_ic).std().fillna(1.0)
        ir_dict[f"IR_{s}"] = ic_mean / (ic_std + EPS)

    ir_df = pd.concat(ir_dict, axis=1)
    ir_cols = list(ir_df.columns)
    ir_matrix = ir_df.fillna(0.0).to_numpy(copy=True)
    ir_max = ir_matrix.max(axis=1, keepdims=True)
    exp_ir = np.exp(ir_matrix - ir_max)
    weights = exp_ir / exp_ir.sum(axis=1, keepdims=True)

    signal_matrix = df[_SUB_SIGNALS].fillna(0.0).to_numpy(copy=True)
    ic_composite = pd.Series((signal_matrix * weights).sum(axis=1), index=df.index)

    rm = ic_composite.rolling(250, min_periods=63).mean()
    rs = ic_composite.rolling(250, min_periods=63).std() + EPS
    return (ic_composite - rm) / rs
