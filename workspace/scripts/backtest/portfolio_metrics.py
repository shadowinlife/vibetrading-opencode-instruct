"""Portfolio-level performance metrics helpers for A-share long-only portfolios.

Computes NAV, cumulative return, drawdown, Sharpe ratio, Calmar ratio,
annualised turnover, and optional benchmark-relative comparisons.

All functions are pure — they accept series/arrays and return dicts or
scalars.  No I/O, no database access.

Reuses the same arithmetic concepts as the single-stock ``metrics.py``
module (Sharpe, max drawdown, Calmar, annualisation) but operates on
portfolio-level daily-return series rather than per-trade holding
periods.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12
TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Core portfolio metrics
# ---------------------------------------------------------------------------

def calc_portfolio_metrics(
    daily_rets: pd.Series | list[float] | np.ndarray,
    *,
    nav_series: list[float] | np.ndarray | None = None,
    turnover_series: pd.Series | list[float] | np.ndarray | None = None,
    benchmark_rets: pd.Series | None = None,
) -> dict:
    """Compute standard portfolio summary metrics from a daily-return series.

    Args:
        daily_rets: Daily portfolio returns (can be a pd.Series, list, or
            ndarray).  Assumed to be sequenced in chronological order.
        nav_series: Optional NAV values (same length as *daily_rets*).
            If omitted, NAV is reconstructed as the cumulative product
            of ``(1 + daily_rets)`` starting from 1.0.
        turnover_series: Optional per-day turnover amounts.  ``None``
            suppresses turnover-derived metrics.
        benchmark_rets: Optional daily benchmark return series indexed
            by date (or a plain Series the same length as *daily_rets*).
            ``None`` suppresses benchmark-relative metrics.

    Returns:
        dict with keys:

        ===================  ================================================
        ``total_return``     Total cumulative return (decimal, e.g. 0.10)
        ``annual_return``    Annualised geometric return
        ``volatility``       Annualised daily-return volatility
        ``sharpe_ratio``     Annualised Sharpe ratio (0 % risk-free rate)
        ``max_drawdown``     Maximum peak-to-trough drawdown (negative)
        ``calmar_ratio``     Annual return / |max drawdown|
        ``start_nav``        Initial NAV
        ``end_nav``          Final NAV
        ``n_days``           Number of return observations
        ``avg_turnover``     Mean daily turnover (only if *turnover_series*
                             supplied)
        ``annual_turnover``  Annualised turnover (only if *turnover_series*
                             supplied; ``250 × avg_turnover``)
        ``excess_return``    Portfolio minus benchmark total return (only
                             if *benchmark_rets* supplied)
        ``tracking_error``   Std dev of daily return differences (only with
                             benchmark)
        ``information_ratio`` Excess annualised return / annualised tracking
                             error (only with benchmark)
        ===================  ================================================
    """
    if isinstance(daily_rets, (list, np.ndarray)):
        daily_rets = pd.Series(daily_rets)
    r = daily_rets.dropna().astype(float)
    n = len(r)

    if nav_series is None:
        reconstructed = list((1.0 + r).cumprod())
        nav_series = [1.0] + reconstructed

    if n == 0:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "volatility": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "calmar_ratio": 0.0,
            "start_nav": 1.0,
            "end_nav": 1.0,
            "n_days": 0,
        }

    total_ret = float(nav_series[-1] - 1.0)
    annual_ret = float(
        (1.0 + total_ret) ** (TRADING_DAYS_PER_YEAR / n) - 1.0
    ) if total_ret > -1.0 else -1.0

    std = float(r.std())
    sharpe = float(r.mean() / std * np.sqrt(TRADING_DAYS_PER_YEAR)) if std > EPS else 0.0
    annual_vol = float(std * np.sqrt(TRADING_DAYS_PER_YEAR))

    # Max drawdown from NAV
    nav_cum = pd.Series(nav_series)
    running_max = nav_cum.cummax()
    max_dd = float((nav_cum / running_max - 1.0).min())
    calmar = float(annual_ret / abs(max_dd)) if abs(max_dd) > EPS else 0.0

    result: dict = {
        "total_return": total_ret,
        "annual_return": annual_ret,
        "volatility": annual_vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "calmar_ratio": calmar,
        "start_nav": float(nav_series[0]),
        "end_nav": float(nav_series[-1]),
        "n_days": n,
    }

    # --- Turnover ---
    if turnover_series is not None:
        if isinstance(turnover_series, (list, np.ndarray)):
            turnover_series = pd.Series(turnover_series)
        avg_t = float(turnover_series.mean())
        result["avg_turnover"] = avg_t
        result["annual_turnover"] = float(avg_t * TRADING_DAYS_PER_YEAR)

    # --- Benchmark-relative ---
    if benchmark_rets is not None:
        b_aligned = _align_benchmark(daily_rets, benchmark_rets)
        if b_aligned is not None:
            excess = daily_rets - b_aligned
            result["excess_return"] = float(
                np.prod(1.0 + excess) - 1.0  # type: ignore[arg-type]
            )
            te = float(excess.std())
            result["tracking_error"] = float(te * np.sqrt(TRADING_DAYS_PER_YEAR))
            result["information_ratio"] = (
                float(excess.mean() / te * np.sqrt(TRADING_DAYS_PER_YEAR))
                if te > EPS else 0.0
            )

    return result


def calc_turnover_metrics(
    turnover_series: pd.Series | list[float] | np.ndarray,
) -> dict:
    """Compute turnover summary from a per-day or per-rebalance turnover series.

    Args:
        turnover_series: Sequence of per-event turnover values (daily or per
            rebalance).

    Returns:
        Dict with ``avg_turnover``, ``annual_turnover`` (scaled to 252 days),
        ``max_turnover``, ``min_turnover`` (if N >= 1), ``total_turnover``.
    """
    if isinstance(turnover_series, (list, np.ndarray)):
        t = pd.Series(turnover_series)
    else:
        t = turnover_series
    if len(t) == 0:
        return {"avg_turnover": 0.0, "annual_turnover": 0.0,
                "total_turnover": 0.0, "n_events": 0}
    avg_t = float(t.mean())
    return {
        "avg_turnover": avg_t,
        "annual_turnover": float(avg_t * TRADING_DAYS_PER_YEAR),
        "total_turnover": float(t.sum()),
        "max_turnover": float(t.max()),
        "min_turnover": float(t.min()),
        "n_events": len(t),
    }


def rebalance_turnover_series(
    rebalance_log: list,
) -> list[float]:
    """Extract per-rebalance turnover from a list of RebalanceLogEntry objects.

    Args:
        rebalance_log: List of ``RebalanceLogEntry`` (or any object with a
            ``.turnover`` attribute).

    Returns:
        List of turnover values in chronological order.
    """
    return [entry.turnover for entry in rebalance_log]


def calc_benchmark_metrics(
    benchmark_rets: pd.Series,
    daily_rets: pd.Series | None = None,
) -> dict:
    """Compute benchmark summary and (optionally) relative metrics.

    Args:
        benchmark_rets: Daily benchmark return series.
        daily_rets: Optional portfolio daily return series for comparison.

    Returns:
        Dict with benchmark ``total_return``, ``annual_return``,
        ``sharpe_ratio``, ``max_drawdown``, ``calmar_ratio``.  If
        *daily_rets* is supplied, also includes ``excess_return``,
        ``tracking_error``, and ``information_ratio``.
    """
    result = calc_portfolio_metrics(benchmark_rets)
    if daily_rets is not None:
        b_aligned = _align_benchmark(daily_rets, benchmark_rets)
        if b_aligned is not None:
            excess = daily_rets - b_aligned
            result["excess_return"] = float(np.prod(1.0 + excess) - 1.0)  # type: ignore[arg-type]
            te = float(excess.std())
            result["tracking_error"] = float(te * np.sqrt(TRADING_DAYS_PER_YEAR))
            result["information_ratio"] = float(
                excess.mean() / te * np.sqrt(TRADING_DAYS_PER_YEAR)
            ) if te > EPS else 0.0
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _align_benchmark(
    portfolio_rets: pd.Series,
    benchmark_rets: pd.Series,
) -> pd.Series | None:
    """Align benchmark returns to the portfolio return index.

    If *benchmark_rets* has a DatetimeIndex, filter to dates present in
    *portfolio_rets.index*.  Otherwise assume positional alignment and
    truncate to the same length.
    """
    if len(benchmark_rets) == 0:
        return None
    if isinstance(benchmark_rets.index, pd.DatetimeIndex) and isinstance(
        portfolio_rets.index, pd.DatetimeIndex
    ):
        common = benchmark_rets.index.intersection(portfolio_rets.index)
        if len(common) == 0:
            return None
        return benchmark_rets.loc[common]
    # Positional alignment
    n = min(len(portfolio_rets), len(benchmark_rets))
    return benchmark_rets.iloc[:n]