"""Batch data loaders for multi-stock portfolio backtesting.

This module provides two SEPARATE loader functions operating on a resolved
universe from ``scripts.backtest.universe``.  Each loader enforces a specific
data contract adhering to the dual-dataframe architecture:

.. code-block:: text

    Factor loader  →  stk_alpha158  →  raw price context (close + 158 factor cols)
    Trading loader →  stk_factor_pro →  HFQ price context (close_hfq → close,
                                          daily_ret, realized_vol_20d_ann)

**Dual-Dataframe Separation (CRITICAL)**:

- ``load_alpha158_batch()`` returns a DataFrame where the ``close`` column is
  **RAW (不复权)** — this aligns with the qlib Alpha158 standard and must be
  used only for factor/signal computation.
- ``load_prices_batch()`` returns a DataFrame where the ``close`` column is
  **HFQ (后复权)** — this is the trading/PnL price and must be used only for
  return calculation, portfolio valuation, and backtest simulation.

These two DataFrames MUST NOT be merged into a single ambiguous contract.
The ``check_alignment()`` helper validates that both loaders cover the same
``(ts_code, trade_date)`` key space and surfaces any divergence.

**Diagnostics-first**: Every missing ts_code (zero rows in window) is
recorded explicitly in the ``missing_codes`` dict — never silently dropped.

Usage::

    from scripts.backtest.data import (
        load_alpha158_batch,
        load_prices_batch,
        check_alignment,
    )

    factor_df, f_diag = load_alpha158_batch(
        ["000001.SZ", "601777.SH"], "2024-01-01", "2025-12-31",
    )
    price_df, p_diag = load_prices_batch(
        ["000001.SZ", "601777.SH"], "2024-01-01", "2025-12-31",
    )
    align = check_alignment(factor_df, price_df)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 158 factor column names in exact qlib source-code order
from scripts.alpha158.metadata import ALPHA158_FACTOR_NAMES  # noqa: E402

_FACTOR_COL_NAMES: list[str] = list(ALPHA158_FACTOR_NAMES)

# Default DuckDB path relative to project root
_DEFAULT_DB_PATH = "./duckdb/ashare.duckdb"

# Annualisation factor for daily volatility → annualised
_TRADING_DAYS_PER_YEAR = 252  # standard for A-share market

# Realised vol rolling window size (20 trading days ≈ 1 month)
_VOL_WINDOW = 20


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BatchLoadResult:
    """Result of a batch data load with explicit diagnostics.

    Attributes:
        df: Loaded DataFrame with ``ts_code`` + ``trade_date`` + data columns.
            Empty DataFrame with correct schema if no data was loaded.
        loaded_codes: ts_codes that had at least one row in the window.
        missing_codes: Map of ts_code → reason for codes with zero rows.
            Never silently dropped — every requested code appears either
            in ``loaded_codes`` or ``missing_codes``.
        n_rows_total: Total number of rows in ``df``.
        diagnostics: Extended per-code metadata (row counts, date ranges).
    """

    df: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    loaded_codes: list[str] = field(default_factory=list)
    missing_codes: dict[str, str] = field(default_factory=dict)
    n_rows_total: int = 0
    diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Factor loader: stk_alpha158 → raw price context
# ---------------------------------------------------------------------------


def load_alpha158_batch(
    ts_codes: list[str],
    start_date: str,
    end_date: str,
    db_path: str = _DEFAULT_DB_PATH,
) -> BatchLoadResult:
    """Load Alpha158 factor data for multiple stocks from ``stk_alpha158``.

    Queries ``stk_alpha158`` for all requested ts_codes within the date window.
    Returns a DataFrame with ``ts_code``, ``trade_date``, ``close`` (raw, 不复权),
    and all 158 Alpha158 factor columns.

    The ``close`` column represents **RAW (不复权)** closing price — this is
    the factor-side price used for Alpha158 computation.  It must NOT be
    used for trading/PnL purposes.

    Args:
        ts_codes: List of Tushare-format stock codes (e.g. ``["000001.SZ"]``).
        start_date: Window start, inclusive (YYYY-MM-DD).
        end_date: Window end, inclusive (YYYY-MM-DD).
        db_path: Path to the DuckDB file.  Default ``./duckdb/ashare.duckdb``.

    Returns:
        ``BatchLoadResult`` where ``df`` contains the joined factor data.
        ``df.columns`` includes ``ts_code``, ``trade_date``, ``close``,
        and all 158 Alpha158 factor columns.  Codes with zero rows in the
        window are recorded in ``result.missing_codes``.
    """
    if not ts_codes:
        return BatchLoadResult()

    con = duckdb.connect(db_path, read_only=True)  # read_only — safe for validation
    try:
        # Build parameterized IN clause — DuckDB supports list params natively
        placeholders = ",".join(["?"] * len(ts_codes))

        # stk_alpha158 stores only factor columns (no close).  JOIN with
        # stk_factor_pro to pull in the raw close (not close_hfq!) for
        # downstream signal builders that need pricing context.
        factor_col_names = [f"a.{col}" for col in _FACTOR_COL_NAMES]
        factor_col_list = ",\n               ".join(factor_col_names)

        df = con.execute(
            f"""
            SELECT a.ts_code,
                   a.trade_date,
                   f.close AS close,
                   {factor_col_list}
            FROM stk_alpha158 a
            JOIN stk_factor_pro f
              ON a.ts_code = f.ts_code
             AND a.trade_date = f.trade_date
            WHERE a.ts_code IN ({placeholders})
              AND a.trade_date >= ?
              AND a.trade_date <= ?
            ORDER BY a.ts_code, a.trade_date
            """,
            [*ts_codes, start_date, end_date],
        ).fetchdf()
    finally:
        con.close()  # Always close, even on exception

    # Build per-code diagnostics: row count per ts_code
    per_code_rows: dict[str, int] = {}
    if not df.empty:
        per_code_rows = df.groupby("ts_code").size().to_dict()

    # Surface missing codes explicitly (never silently drop)
    loaded_codes: list[str] = []
    missing_codes: dict[str, str] = {}
    diagnostics: dict[str, dict[str, Any]] = {}

    for code in ts_codes:
        n = per_code_rows.get(code, 0)
        if n == 0:
            # No Alpha158 data in window — record as missing
            missing_codes[code] = "no stk_alpha158 rows in window"
            diagnostics[code] = {"rows": 0, "loaded": False}
        else:
            loaded_codes.append(code)
            diagnostics[code] = {"rows": n, "loaded": True}

    # Convert trade_date to datetime for consistent downstream handling
    if not df.empty and "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"])

    return BatchLoadResult(
        df=df,
        loaded_codes=loaded_codes,
        missing_codes=missing_codes,
        n_rows_total=len(df),
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Trading loader: stk_factor_pro → HFQ price context
# ---------------------------------------------------------------------------


def load_prices_batch(
    ts_codes: list[str],
    start_date: str,
    end_date: str,
    db_path: str = _DEFAULT_DB_PATH,
) -> BatchLoadResult:
    """Load HFQ trading data for multiple stocks from ``stk_factor_pro``.

    Queries ``stk_factor_pro`` for all requested ts_codes within the date
    window.  Returns a DataFrame with ``ts_code``, ``trade_date``, ``close``
    (aliased from ``close_hfq``), ``open``, ``high``, ``low``, ``daily_ret``,
    and ``realized_vol_20d_ann``.

    The ``close`` column here is **HFQ (后复权)** — this is the trading/PnL
    price, computed as ``close_hfq AS close``.  It must NOT be confused with
    the factor-side raw ``close`` from ``load_alpha158_batch()``.

    Per-stock derivations (via ``groupby('ts_code')``):

    - ``daily_ret``: ``close.pct_change().fillna(0.0)`` — computed within
      each stock's own time series (no cross-stock leakage).
    - ``realized_vol_20d_ann``: ``daily_ret.rolling(20).std() * sqrt(252)``
      — annualised 20-day realised volatility, per stock.

    Rows with NULL ``close_hfq`` are excluded from the query.

    Args:
        ts_codes: List of Tushare-format stock codes (e.g. ``["000001.SZ"]``).
        start_date: Window start, inclusive (YYYY-MM-DD).
        end_date: Window end, inclusive (YYYY-MM-DD).
        db_path: Path to the DuckDB file.  Default ``./duckdb/ashare.duckdb``.

    Returns:
        ``BatchLoadResult`` where ``df`` contains HFQ trading data with
        per-stock ``daily_ret`` and ``realized_vol_20d_ann``.
    """
    if not ts_codes:
        return BatchLoadResult()

    con = duckdb.connect(db_path, read_only=True)
    try:
        placeholders = ",".join(["?"] * len(ts_codes))

        # Load HFQ price fields from stk_factor_pro.
        # close_hfq → aliased AS close for PnL engine compatibility.
        # open_hfq/high_hfq/low_hfq loaded for potential future use (e.g. volatility).
        # close_hfq IS NOT NULL: exclude rows without valid HFQ price.
        df = con.execute(
            f"""
            SELECT ts_code, trade_date,
                   close_hfq AS close,
                   open_hfq AS open,
                   high_hfq AS high,
                   low_hfq AS low
            FROM stk_factor_pro
            WHERE ts_code IN ({placeholders})
              AND trade_date >= ?
              AND trade_date <= ?
              AND close_hfq IS NOT NULL
            ORDER BY ts_code, trade_date
            """,
            [*ts_codes, start_date, end_date],
        ).fetchdf()
    finally:
        con.close()

    # Compute per-stock daily_ret (pct_change within each stock's timeseries).
    # groupby ensures no cross-stock leakage — each stock's returns are
    # computed independently from its own close_hfq series.
    if not df.empty:
        df["daily_ret"] = (
            df.groupby("ts_code", group_keys=False)["close"]
            .pct_change()
            .fillna(0.0)
        )

        # Compute per-stock 20-day annualised realised volatility.
        # rolling(20).std() within groupby → each stock's own volatility.
        # * sqrt(252) → annualise from daily frequency.
        df["realized_vol_20d_ann"] = (
            df.groupby("ts_code", group_keys=False)["daily_ret"]
            .transform(lambda x: x.rolling(_VOL_WINDOW, min_periods=1).std()
                       * np.sqrt(_TRADING_DAYS_PER_YEAR))
        )

    # Build per-code diagnostics
    per_code_rows: dict[str, int] = {}
    if not df.empty:
        per_code_rows = df.groupby("ts_code").size().to_dict()

    loaded_codes: list[str] = []
    missing_codes: dict[str, str] = {}
    diagnostics: dict[str, dict[str, Any]] = {}

    for code in ts_codes:
        n = per_code_rows.get(code, 0)
        if n == 0:
            # No HFQ price data in window — record as missing
            missing_codes[code] = "no stk_factor_pro (HFQ) rows in window"
            diagnostics[code] = {"rows": 0, "loaded": False}
        else:
            loaded_codes.append(code)
            diagnostics[code] = {"rows": n, "loaded": True}

    # Convert trade_date to datetime for consistent downstream handling
    if not df.empty and "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"])

    return BatchLoadResult(
        df=df,
        loaded_codes=loaded_codes,
        missing_codes=missing_codes,
        n_rows_total=len(df),
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Alignment diagnostics
# ---------------------------------------------------------------------------


@dataclass
class AlignmentResult:
    """Diagnostics comparing factor and price (ts_code, trade_date) key sets.

    Attributes:
        common_keys: Set of ``(ts_code, trade_date)`` keys present in both
            factor and price DataFrames.
        factor_only_keys: Keys present only in the factor DataFrame (missing
            from price).
        price_only_keys: Keys present only in the price DataFrame (missing
            from factor).
        n_factor_total: Total unique keys in the factor DataFrame.
        n_price_total: Total unique keys in the price DataFrame.
        n_common: Number of shared keys.
        all_aligned: True if ``price_only_keys`` and ``factor_only_keys``
            are both empty.
    """

    common_keys: set[tuple[str, pd.Timestamp]] = field(default_factory=set)
    factor_only_keys: set[tuple[str, pd.Timestamp]] = field(default_factory=set)
    price_only_keys: set[tuple[str, pd.Timestamp]] = field(default_factory=set)
    n_factor_total: int = 0
    n_price_total: int = 0
    n_common: int = 0
    all_aligned: bool = True


def check_alignment(
    factor_df: pd.DataFrame,
    price_df: pd.DataFrame,
) -> AlignmentResult:
    """Compare ``(ts_code, trade_date)`` key sets between factor and price DataFrames.

    This is a diagnostic tool — it does NOT modify either DataFrame.
    It reports which keys exist only in one side, and which are common.

    Typical usage is to validate that ``load_alpha158_batch`` and
    ``load_prices_batch`` cover the same date range and stock set before
    merging for portfolio simulation.

    Args:
        factor_df: Factor DataFrame from ``load_alpha158_batch()``.
            Must contain ``ts_code`` and ``trade_date`` columns.
        price_df: Trading DataFrame from ``load_prices_batch()``.
            Must contain ``ts_code`` and ``trade_date`` columns.

    Returns:
        ``AlignmentResult`` with set-level comparison and counts.
        ``all_aligned`` is True ONLY when both factor_only and price_only
        are empty (perfect overlap).
    """
    # Extract (ts_code, trade_date) key sets from both DataFrames.
    # Using set of tuples for O(1) membership checks.
    factor_keys: set[tuple[str, pd.Timestamp]] = set()
    if not factor_df.empty:
        factor_keys = set(
            zip(factor_df["ts_code"], factor_df["trade_date"])
        )

    price_keys: set[tuple[str, pd.Timestamp]] = set()
    if not price_df.empty:
        price_keys = set(
            zip(price_df["ts_code"], price_df["trade_date"])
        )

    # Compute overlaps and divergences
    common = factor_keys & price_keys
    factor_only = factor_keys - price_keys  # in factor but NOT in price
    price_only = price_keys - factor_keys    # in price but NOT in factor

    return AlignmentResult(
        common_keys=common,
        factor_only_keys=factor_only,
        price_only_keys=price_only,
        n_factor_total=len(factor_keys),
        n_price_total=len(price_keys),
        n_common=len(common),
        all_aligned=(len(factor_only) == 0 and len(price_only) == 0),
    )