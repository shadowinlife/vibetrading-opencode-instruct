"""Fixed-interval rebalance scheduling and top-N stock selection.

Provides two reusable primitives for v1 portfolio backtesting:

1. ``generate_rebalance_dates`` — deterministic rebalance calendar from a
   start date, end date, and fixed interval (calendar days).
2. ``select_top_n`` — rank eligible stocks by a signal column on a given
   rebalance date, exclude NaN-signal names, and return up to
   ``max_positions``.

Both follow the v1 design decision: newly selected stocks enter immediately
on rebalance day.  No sector-neutral filtering or signal-triggered
rebalancing in v1.

Usage::

    from scripts.backtest.selection import generate_rebalance_dates, select_top_n

    rebalance_dates = generate_rebalance_dates("2024-01-01", "2025-12-31", 20)
    selected, excluded = select_top_n(df, rebalance_dates[0], "signal", 10)

This module is additive — it does not modify any existing backtest modules.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Rebalance calendar
# ---------------------------------------------------------------------------


def generate_rebalance_dates(
    start_date: str,
    end_date: str,
    freq_days: int,
) -> list[str]:
    """Generate a deterministic list of rebalance dates at a fixed calendar-day
    interval within the inclusive date range.

    Dates are returned as ISO-format ``YYYY-MM-DD`` strings. The first date
    is always ``start_date``; subsequent dates are ``start_date + k * freq_days``
    for ``k = 1, 2, ...``, stopping when the next date would exceed ``end_date``.

    Args:
        start_date: Inclusive start date (YYYY-MM-DD).
        end_date: Inclusive end date (YYYY-MM-DD).
        freq_days: Rebalance interval in calendar days.  Must be > 0.

    Returns:
        Sorted list of ISO date strings, starting with ``start_date``.

    Raises:
        ValueError: if ``freq_days <= 0`` or dates are invalid.

    Examples:
        >>> generate_rebalance_dates("2024-01-01", "2024-01-31", 10)
        ['2024-01-01', '2024-01-11', '2024-01-21', '2024-01-31']
    """
    if freq_days <= 0:
        raise ValueError(f"freq_days must be > 0, got {freq_days}")

    # Use pd.date_range for robust calendar arithmetic.
    # Closed="left" when freq > calendar frequency would exclude the last
    # day — so we generate one interval past end_date and filter.
    dates = pd.date_range(start=start_date, end=end_date, freq=f"{freq_days}D")
    return [d.strftime("%Y-%m-%d") for d in dates]


# ---------------------------------------------------------------------------
# Top-N selection
# ---------------------------------------------------------------------------


def select_top_n(
    df: pd.DataFrame,
    date: str,
    signal_col: str,
    max_positions: int,
    higher_better: bool = True,
) -> tuple[list[str], dict[str, str]]:
    """Select up to ``max_positions`` stocks on a single rebalance date.

    Eligible stocks are those with a **non-NaN** signal value on ``date``.
    Stocks with NaN or missing signals are excluded and recorded with a
    ``"NaN signal"`` reason.

    Selection logic:
        - Filter to the given ``date``.
        - Exclude any stock whose ``signal_col`` is NaN (or missing row).
        - Rank remaining stocks by ``signal_col``:
            - ascending if ``higher_better=False``.
            - descending if ``higher_better=True`` (default).
        - Keep the top ``max_positions`` (or fewer, if fewer are eligible).

    Args:
        df: Factor DataFrame with columns ``ts_code``, ``trade_date``,
            and the signal column.  ``trade_date`` may be any type
            comparable to ``date`` (string, Timestamp, etc.).
        date: Rebalance date as ``YYYY-MM-DD`` string.
        signal_col: Name of the signal column to rank by.
        max_positions: Maximum number of stocks to return.  Must be >= 0.
        higher_better: If True (default), higher signal values are preferred.

    Returns:
        ``(selected_codes, excluded_map)`` where:
        - ``selected_codes`` is a ``list[str]`` of selected ``ts_code`` values,
          in the ranked order.
        - ``excluded_map`` is ``{ts_code: reason_string}`` for every stock
          present on ``date`` that was excluded.

    Raises:
        ValueError: if ``max_positions < 0`` or ``signal_col`` not in columns.
    """
    if max_positions < 0:
        raise ValueError(f"max_positions must be >= 0, got {max_positions}")
    if signal_col not in df.columns:
        raise ValueError(
            f"signal column '{signal_col}' not found in DataFrame columns: "
            f"{list(df.columns)}"
        )

    # Filter to the target date
    day_df = df[df["trade_date"] == date].copy()

    excluded: dict[str, str] = {}

    if day_df.empty:
        return ([], excluded)

    # Identify NaN-signal stocks (or missing signal column values).
    # Use .loc[:, signal_col] to disambiguate Series return type for pyright.
    signal_series = day_df.loc[:, signal_col]
    nan_mask = signal_series.isna()
    if bool(nan_mask.any()):
        nan_codes = day_df.loc[nan_mask, "ts_code"].tolist()
        for code in nan_codes:
            excluded[code] = "NaN signal"

    # Eligible = non-NaN signal
    eligible = day_df.loc[~nan_mask]

    if eligible.empty:
        return ([], excluded)

    # Rank: higher_better → descending, else → ascending
    # Sort by signal: higher_better → descending, else → ascending
    ranked: pd.DataFrame = eligible.sort_values(
        signal_col, ascending=not higher_better
    )

    # Take top max_positions. head(0) returns empty, head(N) caps.
    capped = ranked.head(max_positions)

    # Any eligible stock NOT in the top max_positions is "ranked out"
    selected_set = set(capped["ts_code"])
    all_eligible = set(eligible["ts_code"])
    ranked_out = all_eligible - selected_set
    for code in sorted(ranked_out):
        excluded[code] = "ranked below top-N cutoff"

    return (capped["ts_code"].tolist(), excluded)