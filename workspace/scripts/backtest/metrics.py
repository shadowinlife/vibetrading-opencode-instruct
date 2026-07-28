"""Backtest performance metrics: calc_metrics()."""

from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


def calc_metrics(returns: pd.Series, trade_count: int, hold_days: list[int]) -> dict:
    r = returns.dropna()
    if len(r) == 0:
        return {
            "total_ret": 0.0,
            "annual_ret": 0.0,
            "sharpe": 0.0,
            "max_dd": 0.0,
            "calmar": 0.0,
            "trade_count": trade_count,
            "median_hold_days": 0.0,
            "max_hold_days": 0,
        }
    cum = (1 + r).cumprod()
    total_ret = float(cum.iloc[-1] - 1)
    annual_ret = float((1 + total_ret) ** (252 / len(r)) - 1) if len(r) > 0 and 1 + total_ret > 0 else -1.0
    std = float(r.std())
    sharpe = float(r.mean() / std * np.sqrt(252)) if std > EPS else 0.0
    running_max = cum.cummax()
    max_dd = float((cum / running_max - 1).min())
    calmar = float(annual_ret / abs(max_dd)) if abs(max_dd) > EPS else 0.0
    return {
        "total_ret": total_ret,
        "annual_ret": annual_ret,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "calmar": calmar,
        "trade_count": trade_count,
        "median_hold_days": float(np.median(hold_days)) if hold_days else 0.0,
        "max_hold_days": int(max(hold_days)) if hold_days else 0,
    }