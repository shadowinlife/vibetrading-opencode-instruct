"""Portfolio simulator core loop for v1.

Simulates a daily long-only, equal-weight portfolio with:
- explicit cash accounting
- 0.15 % one-way transaction costs on position-weight deltas
- immediate entry for newly selected stocks on rebalance day
- mid-period freed capital moves to cash until the next rebalance
- cash + invested weights never exceed 1.0

This module is additive — it does **not** modify ``scripts/backtest/engine.py``
or any existing single-stock modules.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.backtest.portfolio_types import NavSummary, PortfolioConfig, RebalanceLogEntry

ONE_WAY_COST = 0.0015


def _build_ret_lookup(
    price_df: pd.DataFrame, all_dates: pd.DatetimeIndex
) -> dict[pd.Timestamp, dict[str, float]]:
    """Index daily_ret by (trade_date, ts_code) for O(1) lookup."""
    grouped = price_df.set_index(["trade_date", "ts_code"])["daily_ret"]
    lookup: dict[pd.Timestamp, dict[str, float]] = {}
    for ts in all_dates:
        try:
            day_series = grouped.loc[ts]
            if isinstance(day_series, pd.Series):
                lookup[ts] = day_series.to_dict()  # type: ignore[arg-type]
            elif not day_series.empty:
                lookup[ts] = {str(day_series.index[0]): float(day_series.iloc[0])}
            else:
                lookup[ts] = {}
        except KeyError:
            lookup[ts] = {}
    return lookup


def _normalise_weights(position_weights: dict[str, float], cash_weight: float) -> tuple[dict[str, float], float]:
    """Scale position weights and cash so they sum to 1.0."""
    total = sum(position_weights.values()) + cash_weight
    if total > 1e-15:
        for code in position_weights:
            position_weights[code] /= total
        cash_weight /= total
    else:
        position_weights.clear()
        cash_weight = 1.0
    return position_weights, cash_weight


def _compute_cost_and_turnover(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    cost_rate: float,
) -> tuple[float, float]:
    """Return (total_cost, gross_turnover) for the weight deltas."""
    all_codes = set(current_weights.keys()) | set(target_weights.keys())
    cost = 0.0
    turnover = 0.0
    for code in all_codes:
        delta = abs(target_weights.get(code, 0.0) - current_weights.get(code, 0.0))
        cost += delta * cost_rate
        turnover += delta
    return cost, turnover


def _compute_nav_summary(
    rets: pd.Series, nav_series: list[float], date_series: list[pd.Timestamp]
) -> NavSummary:
    """Derive NavSummary from a daily-return series."""
    if len(rets) < 2:
        return NavSummary(
            start_date=str(date_series[0].date()),
            end_date=str(date_series[-1].date()),
            initial_nav=1.0,
            final_nav=1.0,
            total_return=0.0,
            annual_return=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            calmar_ratio=0.0,
            daily_nav=[],
        )

    cum_ret = float(nav_series[-1] - 1.0)
    trading_days = len(rets)
    annual_ret = float(
        (1.0 + cum_ret) ** (252.0 / trading_days) - 1.0
    ) if cum_ret > -1.0 else -1.0

    std = float(rets.std())
    sharpe = float(rets.mean() / std * np.sqrt(252.0)) if std > 1e-12 else 0.0

    cum_series = (1.0 + rets).cumprod()
    max_dd = float((cum_series / cum_series.cummax() - 1.0).min())
    calmar = float(annual_ret / abs(max_dd)) if abs(max_dd) > 1e-12 else 0.0

    daily_nav_records = [
        {"date": str(d.date()), "nav": float(n)}
        for d, n in zip(date_series, nav_series)
    ]

    return NavSummary(
        start_date=str(date_series[0].date()),
        end_date=str(date_series[-1].date()),
        initial_nav=1.0,
        final_nav=float(nav_series[-1]),
        total_return=cum_ret,
        annual_return=annual_ret,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        calmar_ratio=calmar,
        daily_nav=daily_nav_records,
    )


def simulate_portfolio(
    price_df: pd.DataFrame,
    selection_map: dict[str, list[str]],
    config: PortfolioConfig,
    *,
    benchmark_ret: pd.Series | None = None,
) -> tuple[pd.DataFrame, list[RebalanceLogEntry], NavSummary]:
    """Simulate a daily long-only equal-weight portfolio.

    On each rebalance date the engine:
    1. applies the day's market returns to existing positions
    2. sets target equal-weight positions for the newly selected stocks
    3. computes per-stock position-weight deltas
    4. deducts costs = sum(|delta|) x one_way_cost from total portfolio value
    5. sets new positions; released capital moves to cash

    Between rebalances, stock weights drift with price changes.  Cash
    earns zero return.

    Args:
        price_df: HFQ-price DataFrame.  Required columns:
            ``ts_code``, ``trade_date``, ``daily_ret``.
        selection_map: ``{rebalance_date: [ts_code, ...]}``.
            Dates outside the rebalance calendar are ignored.
        config: Portfolio experiment configuration.
        benchmark_ret: Optional daily benchmark return series indexed
            by ``trade_date``.

    Returns:
        ``(nav_df, rebalance_log, nav_summary)``.

        *nav_df* columns: **trade_date**, **nav**, **daily_ret**,
        **cum_ret**, **cash_weight**, **benchmark_cum_ret** (if
        benchmark supplied).

        *rebalance_log*: one ``RebalanceLogEntry`` per rebalance event.

        *nav_summary*: ``NavSummary`` with start/end NAV, total return,
        annual return, Sharpe, max drawdown, Calmar.
    """
    cost_rate = config.one_way_cost
    all_dates = pd.DatetimeIndex(price_df["trade_date"].drop_duplicates()).sort_values()
    if len(all_dates) < 2:
        raise ValueError("price_df must contain at least 2 trading days")

    ret_lookup = _build_ret_lookup(price_df, all_dates)

    position_weights: dict[str, float] = {}
    cash_weight: float = 1.0
    nav: float = 1.0

    daily_ret_series: list[float] = []
    nav_series: list[float] = []
    cash_series: list[float] = []
    date_series: list[pd.Timestamp] = []
    rebalance_log: list[RebalanceLogEntry] = []

    for ts in all_dates:
        date_str = ts.strftime("%Y-%m-%d")

        day_rets = ret_lookup.get(ts, {})
        pos_ret = 0.0
        for code, w in list(position_weights.items()):
            r = day_rets.get(code, 0.0)
            pos_ret += w * r
            position_weights[code] = w * (1.0 + r)

        position_weights, cash_weight = _normalise_weights(position_weights, cash_weight)

        cost = 0.0
        turnover = 0.0
        selected_codes: list[str] = []
        alloc_weights: list[float] = []

        if date_str in selection_map:
            selected_codes = selection_map[date_str]
            n_selected = len(selected_codes)

            target_per_stock = 1.0 / n_selected if n_selected > 0 else 0.0

            new_weights: dict[str, float] = {}
            for code in selected_codes:
                new_weights[code] = target_per_stock

            cost, turnover = _compute_cost_and_turnover(
                position_weights, new_weights, cost_rate
            )

            position_weights = new_weights.copy()
            cash_weight = 1.0 - sum(position_weights.values())
            alloc_weights = [target_per_stock] * n_selected

        day_ret = pos_ret - cost
        nav *= (1.0 + day_ret)

        daily_ret_series.append(day_ret)
        nav_series.append(nav)
        cash_series.append(cash_weight)
        date_series.append(ts)

        if date_str in selection_map:
            rebalance_log.append(
                RebalanceLogEntry(
                    rebalance_date=date_str,
                    selected_codes=selected_codes,
                    weights=alloc_weights[:],
                    turnover=turnover,
                    cost=cost,
                    cash_pct=cash_weight,
                )
            )

    nav_df = pd.DataFrame(
        {
            "trade_date": date_series,
            "nav": nav_series,
            "daily_ret": daily_ret_series,
            "cum_ret": np.array(nav_series) - 1.0,
            "cash_weight": cash_series,
        }
    )

    if benchmark_ret is not None:
        bench_cum = (1.0 + benchmark_ret.loc[benchmark_ret.index.isin(date_series)]).cumprod()
        nav_df["benchmark_cum_ret"] = nav_df["trade_date"].map(
            lambda d: float(bench_cum.get(d, np.nan))
        )

    rets_series: pd.Series = pd.Series(nav_df["daily_ret"].dropna().values)
    nav_summary = _compute_nav_summary(rets_series, nav_series, date_series)

    return nav_df, rebalance_log, nav_summary