"""
Turnover-to-market-cap heat condition (candidate #4).

Computes the daily ratio of total market turnover to total market capitalisation
and generates overheat signals based on absolute thresholds and rolling
percentile rank.

Two computation paths
---------------------
Path A (primary) : ``SUM(stk_factor_pro.amount) / SUM(stk_factor_pro.total_mv)``
    Amount is **千元** (thousands of CNY), total_mv is **万元** (ten-thousands CNY).
    The ratio is computed as::

        SUM(amount * 1000) / SUM(total_mv * 10000) = SUM(amount) / (SUM(total_mv) * 10)

Path B (supplemental) : ``idx_quote_dc``
    Coverage starts 2024-12-20 only.  Used as a cross-check when aligned dates
    are available.

Unit metadata
-------------
- ``amount``   : 千元 (thousands CNY)
- ``total_mv`` : 万元 (ten-thousands CNY)
- ``ratio``    : dimensionless (turnover / market cap)
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .base import format_date, get_connection, pct_rank, rolling_zscore, write_json
from .metadata import DEFAULT_DUCKDB_PATH

# ── Private helpers ──────────────────────────────────────────────────────────


def _validate_date_window(
    start_date: str | date | None,
    end_date: str | date | None,
) -> tuple[str | None, str | None]:
    """Normalise and validate an optional date window."""
    if start_date is None and end_date is not None:
        raise ValueError("--start-date is required when --end-date is specified")
    if end_date is not None and start_date is not None and start_date > end_date:  # type: ignore[operator]
        raise ValueError(f"start_date ({start_date}) must be <= end_date ({end_date})")
    start_str = format_date(start_date) if start_date is not None else None
    end_str = format_date(end_date) if end_date is not None else None
    return start_str, end_str


def _build_where_clause(start_date: str | None, end_date: str | None) -> str:
    """Build a SQL WHERE clause from optional date bounds."""
    clauses: list[str] = []
    if start_date is not None:
        clauses.append(f"trade_date >= '{start_date}'")
    if end_date is not None:
        clauses.append(f"trade_date <= '{end_date}'")
    return f"AND {' AND '.join(clauses)}" if clauses else ""


# ── Path A: stock-level aggregation ─────────────────────────────────────────


def compute_ratio_from_stocks(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    """Compute daily turnover/market-cap ratio by aggregating all A-share stocks.

    Units
    -----
    ``stk_factor_pro.amount``  → 千元 (thousands CNY)
    ``stk_factor_pro.total_mv`` → 万元 (ten-thousands CNY)

    Ratio = SUM(amount * 1000) / SUM(total_mv * 10000)
          = SUM(amount) / (SUM(total_mv) * 10)

    Only stocks with ``amount > 0 AND total_mv > 0`` are included.
    Exchange filter: ``.SH``, ``.SZ``, ``.BJ``.

    Parameters
    ----------
    con_or_path : DuckDB connection or path string
    start_date, end_date : str or date, optional
        Trade-date window (inclusive).

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``total_amount_kyuan``, ``total_mv_wyuan``,
        ``ratio``, ``stock_count``.
    """
    start_str, end_str = _validate_date_window(start_date, end_date)
    where_clause = _build_where_clause(start_str, end_str)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = f"""
        SELECT
            trade_date,
            SUM(amount)   AS total_amount_kyuan,
            SUM(total_mv) AS total_mv_wyuan,
            SUM(amount) / (NULLIF(SUM(total_mv), 0) * 10.0) AS ratio,
            COUNT(DISTINCT ts_code) AS stock_count
        FROM stk_factor_pro
        WHERE amount IS NOT NULL
          AND amount > 0
          AND total_mv IS NOT NULL
          AND total_mv > 0
          AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ' OR ts_code LIKE '%.BJ')
          {where_clause}
        GROUP BY trade_date
        ORDER BY trade_date
        """
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


# ── Path B: index-level cross-check ─────────────────────────────────────────


def compute_ratio_from_index(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    index_code: str = "000001.SH",
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    """Compute daily turnover/market-cap ratio using ``idx_quote_dc``.

    **Coverage caveat**: ``idx_quote_dc`` starts 2024-12-20 only.  This path
    is suitable as a supplemental cross-check, not as a primary data source.

    Uses ``total_mv`` (万元) and ``turnover_rate`` (already a percentage).
    The raw ratio is ``turnover_rate / 100`` (dimensionless).

    Parameters
    ----------
    con_or_path : DuckDB connection or path string
    index_code : str
        Index code in Tushare format (default ``"000001.SH"`` = SSE Composite).
    start_date, end_date : str or date, optional

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``total_mv_wyuan``, ``turnover_rate_pct``,
        ``ratio``, ``index_code``.
    """
    start_str, end_str = _validate_date_window(start_date, end_date)
    where_clause = _build_where_clause(start_str, end_str)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = f"""
        SELECT
            trade_date,
            total_mv       AS total_mv_wyuan,
            turnover_rate  AS turnover_rate_pct,
            turnover_rate / 100.0 AS ratio,
            ts_code        AS index_code
        FROM idx_quote_dc
        WHERE ts_code = '{index_code}'
          AND turnover_rate IS NOT NULL
          AND total_mv IS NOT NULL
          {where_clause}
        ORDER BY trade_date
        """
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df.empty:
        raise ValueError(
            f"No idx_quote_dc data for index_code='{index_code}'. "
            "Note: idx_quote_dc coverage starts 2024-12-20 only."
        )

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


# ── Heat signal computation ─────────────────────────────────────────────────


def compute_heat_signal(
    df_ratio: pd.DataFrame,
    *,
    absolute_threshold: float | None = None,
    percentile_threshold: float = 80.0,
    rolling_window: int = 252,
) -> pd.DataFrame:
    """Generate overheat signals from a daily ratio series.

    Two hit conditions are evaluated independently:

    1. **Absolute hit**: ``ratio >= absolute_threshold``
    2. **Rolling percentile hit**: ``rolling_percentile >= percentile_threshold``
       where the rolling window is typically 252 trading days (≈1 year).

    Parameters
    ----------
    df_ratio : pd.DataFrame
        Must have ``trade_date`` and ``ratio`` columns (output of
        ``compute_ratio_from_stocks`` or ``compute_ratio_from_index``).
    absolute_threshold : float or None
        Absolute ratio threshold.  ``None`` disables this condition.
        Reasonable default for A-shares: 0.04 (4 %).
    percentile_threshold : float
        Rolling percentile threshold in [0, 100].  Default 80.0.
    rolling_window : int
        Lookback window in trading days for rolling percentile. Default 252.

    Returns
    -------
    pd.DataFrame
        Copy of ``df_ratio`` with additional columns:
        ``roll_pct``, ``abs_hit``, ``pct_hit``, ``joint_hit``.
    """
    if "ratio" not in df_ratio.columns or "trade_date" not in df_ratio.columns:
        raise ValueError("Input must have 'trade_date' and 'ratio' columns")

    df = df_ratio.sort_values("trade_date").reset_index(drop=True).copy()

    # Rolling percentile rank (0–100) over trailing window.
    df["roll_pct"] = (
        df["ratio"]
        .rolling(rolling_window, min_periods=min(rolling_window, 60))
        .apply(lambda x: (x.rank(pct=True).iloc[-1] * 100.0), raw=False)
    )

    # Absolute hit
    if absolute_threshold is not None:
        df["abs_hit"] = df["ratio"] >= absolute_threshold
    else:
        df["abs_hit"] = False

    # Percentile hit
    df["pct_hit"] = df["roll_pct"] >= percentile_threshold

    # Joint hit: both conditions fire (OR logic when abs_threshold is None)
    df["joint_hit"] = df["abs_hit"].fillna(False) & df["pct_hit"].fillna(False)

    return df


# ── Pure summary builder (testable) ─────────────────────────────────────────


def _build_turnover_heat_summary(
    df: pd.DataFrame,
    *,
    absolute_threshold: float | None,
    percentile_threshold: float,
    rolling_window: int,
    source: str,
) -> dict[str, Any]:
    """Build a structured summary from the daily ratio + signal DataFrame.

    This is a pure function — no I/O, no DuckDB.  Separated for testability.
    """
    latest: Any = df.iloc[-1]
    latest_trade_date: pd.Timestamp = pd.Timestamp(latest["trade_date"])

    # Historical percentiles of the ratio
    ratio_vals = df["ratio"].dropna()
    latest_ratio = float(latest["ratio"])

    # Ratio percentile rank (global, not rolling)
    global_pct = float((ratio_vals <= latest_ratio).mean() * 100)

    max_idx = int(df["ratio"].idxmax())
    max_ratio = float(df.loc[max_idx, "ratio"])
    max_date = format_date(pd.Timestamp(df.loc[max_idx, "trade_date"]))

    # Signal counts
    n_signal = int(df["joint_hit"].sum())
    n_days = len(df)

    # Latest signal values
    latest_roll_pct = (
        float(latest["roll_pct"]) if pd.notna(latest["roll_pct"]) else None
    )

    # Daily series for downstream
    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        row_date = pd.Timestamp(row["trade_date"])
        entry: dict[str, Any] = {
            "trade_date": format_date(row_date),
            "ratio": float(row["ratio"]),
            "ratio_pct": float(row["ratio"] * 100),
        }
        if "total_amount_kyuan" in row:
            entry["total_amount_kyuan"] = float(row["total_amount_kyuan"])
            entry["total_mv_wyuan"] = float(row["total_mv_wyuan"])
            entry["stock_count"] = int(row["stock_count"])
        if "roll_pct" in row and pd.notna(row["roll_pct"]):
            entry["roll_pct"] = float(row["roll_pct"])
        if "abs_hit" in row:
            entry["abs_hit"] = bool(row["abs_hit"])
        if "pct_hit" in row:
            entry["pct_hit"] = bool(row["pct_hit"])
        if "joint_hit" in row:
            entry["joint_hit"] = bool(row["joint_hit"])
        daily_series.append(entry)

    # Unit metadata (critical for auditability)
    unit_metadata = {
        "amount_unit": "千元 (thousands CNY)",
        "total_mv_unit": "万元 (ten-thousands CNY)",
        "ratio_unit": "dimensionless",
        "ratio_formula": "SUM(amount) / (SUM(total_mv) * 10)",
        "source": source,
    }

    return {
        "latest_trade_date": format_date(latest_trade_date),
        "latest_ratio": latest_ratio,
        "latest_ratio_pct": latest_ratio * 100,
        "latest_roll_pct": latest_roll_pct,
        "global_percentile_of_latest": global_pct,
        "historical_max_ratio": max_ratio,
        "historical_max_ratio_pct": max_ratio * 100,
        "historical_max_date": max_date,
        "n_signal_days": n_signal,
        "n_total_days": n_days,
        "signal_pct": float(n_signal / n_days * 100) if n_days > 0 else 0.0,
        "absolute_threshold": absolute_threshold,
        "percentile_threshold": percentile_threshold,
        "rolling_window": rolling_window,
        "unit_metadata": unit_metadata,
        "daily_series": daily_series,
    }


# ── Public API ──────────────────────────────────────────────────────────────


def compute_turnover_mcap_heat(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    absolute_threshold: float | None = 0.04,
    percentile_threshold: float = 80.0,
    rolling_window: int = 252,
    source: str = "stk_factor_pro",
) -> dict[str, Any]:
    """Compute the turnover-to-market-cap heat signal.

    This is the primary entry point.  It computes the daily turnover/market-cap
    ratio from stock-level aggregates, applies absolute and rolling-percentile
    thresholds, and returns a structured summary.

    Parameters
    ----------
    con_or_path : DuckDB connection or path string
    start_date, end_date : str or date, optional
    absolute_threshold : float or None
        Absolute ratio threshold for heat signal.  ``None`` disables.
        Default 0.04 (4 %).
    percentile_threshold : float
        Rolling percentile threshold (0–100).  Default 80.0.
    rolling_window : int
        Rolling window in trading days for percentile.  Default 252.
    source : str
        Data source identifier: ``"stk_factor_pro"`` (default) or
        ``"idx_quote_dc"``.

    Returns
    -------
    dict
        Structured summary with keys: ``latest_trade_date``, ``latest_ratio``,
        ``latest_ratio_pct``, ``latest_roll_pct``, ``global_percentile_of_latest``,
        ``historical_max_ratio``, ``historical_max_ratio_pct``,
        ``historical_max_date``, ``n_signal_days``, ``n_total_days``,
        ``signal_pct``, ``absolute_threshold``, ``percentile_threshold``,
        ``rolling_window``, ``unit_metadata``, ``daily_series``.
    """
    start_str, end_str = _validate_date_window(start_date, end_date)

    if source == "idx_quote_dc":
        df = compute_ratio_from_index(
            con_or_path, start_date=start_str, end_date=end_str
        )
    else:
        df = compute_ratio_from_stocks(
            con_or_path, start_date=start_str, end_date=end_str
        )

    if df.empty:
        raise ValueError(
            "No data returned. Check the date window and data source availability."
        )

    df_signal = compute_heat_signal(
        df,
        absolute_threshold=absolute_threshold,
        percentile_threshold=percentile_threshold,
        rolling_window=rolling_window,
    )

    return _build_turnover_heat_summary(
        df_signal,
        absolute_threshold=absolute_threshold,
        percentile_threshold=percentile_threshold,
        rolling_window=rolling_window,
        source=source,
    )


# ── Signal series (for tuning / grid search) ────────────────────────────────


def load_ratio_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    source: str = "stk_factor_pro",
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> pd.DataFrame:
    """Return the daily turnover/market-cap ratio as a minimal DataFrame.

    Used by tuning / validation pipelines that need a clean ``trade_date``,
    ``ratio`` series for grid search.

    Parameters
    ----------
    duckdb_path : str
    source : str
        ``"stk_factor_pro"`` or ``"idx_quote_dc"``.
    start_date, end_date : str or date, optional

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``ratio``.
    """
    if source == "idx_quote_dc":
        df = compute_ratio_from_index(
            duckdb_path, start_date=start_date, end_date=end_date
        )
    else:
        df = compute_ratio_from_stocks(
            duckdb_path, start_date=start_date, end_date=end_date
        )
    return pd.DataFrame(
        df[["trade_date", "ratio"]]
        .sort_values("trade_date")
        .reset_index(drop=True)
    )


def compute_heat_signal_series(
    df_ratio: pd.DataFrame,
    *,
    absolute_threshold: float | None = None,
    percentile_threshold: float = 80.0,
    rolling_window: int = 252,
    use_or_logic: bool = False,
) -> pd.DataFrame:
    """Generate a daily binary heat-signal column for grid search.

    Parameters
    ----------
    df_ratio : pd.DataFrame
        Must have ``trade_date`` and ``ratio`` columns.
    absolute_threshold : float or None
    percentile_threshold : float
    rolling_window : int
    use_or_logic : bool
        If ``True``, ``joint_hit = abs_hit OR pct_hit``.
        If ``False`` (default), ``joint_hit = abs_hit AND pct_hit``.

    Returns
    -------
    pd.DataFrame
        ``trade_date``, ``ratio``, ``roll_pct``, ``abs_hit``, ``pct_hit``,
        ``heat_signal``.
    """
    df = compute_heat_signal(
        df_ratio,
        absolute_threshold=absolute_threshold,
        percentile_threshold=percentile_threshold,
        rolling_window=rolling_window,
    )

    if use_or_logic:
        df["heat_signal"] = df["abs_hit"].fillna(False) | df["pct_hit"].fillna(False)
    else:
        df["heat_signal"] = df["abs_hit"].fillna(False) & df["pct_hit"].fillna(False)

    return pd.DataFrame(
        df[
            [
                "trade_date",
                "ratio",
                "roll_pct",
                "abs_hit",
                "pct_hit",
                "heat_signal",
            ]
        ].sort_values("trade_date").reset_index(drop=True)
    )