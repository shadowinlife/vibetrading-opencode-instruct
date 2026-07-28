"""
Winner-rate / cost-pressure indicator.

Uses ``stk_cyq_perf`` (winner_rate, cost percentile fields) to
compute market-wide chip-profit pressure.  High winner_rate across the
market — a large share of stocks where most holders are sitting on
profits — historically precedes corrections (profit-taking).

**Data source**: ``stk_cyq_perf`` only.  ``stk_cyq_chips`` is
**never** used (removed from sync per AGENTS.md).

Aggregates computed daily:
  * ``avg_winner_rate``   — cross-sectional mean of winner_rate
  * ``med_winner_rate``   — cross-sectional median of winner_rate
  * ``pct_gt_90``         — % of stocks with winner_rate > 90 %
  * ``pct_gt_95``         — % of stocks with winner_rate > 95 %
  * ``cost_50pct_ratio``  — median cost_50pct / close price for high-winner stocks
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pandas as pd

from .base import format_date, get_connection
from .metadata import DEFAULT_DUCKDB_PATH

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

WinnerRatePressureSummary = dict[str, Any]


# ── Default thresholds ───────────────────────────────────────────────────────

DEFAULT_WR_AVG_THRESHOLD: float = 60.0   # avg winner_rate above 60 % → concern
DEFAULT_WR_MED_THRESHOLD: float = 55.0   # median winner_rate above 55 % → concern
DEFAULT_WR_GT90_THRESHOLD: float = 0.15  # > 15 % of stocks with wr > 90 → concern
DEFAULT_WR_GT95_THRESHOLD: float = 0.05  # > 5 % of stocks with wr > 95 → concern
DEFAULT_COST_RATIO_THRESHOLD: float = 0.95  # cost_50pct/close > 0.95 → tight margin


# ── Private helpers ──────────────────────────────────────────────────────────


def _validate_date_window(
    start_date: str | date | None,
    end_date: str | date | None,
) -> tuple[str | None, str | None]:
    """Normalise and validate an optional date window."""
    if start_date is None and end_date is not None:
        raise ValueError("--start-date is required when --end-date is specified")
    if end_date is not None and start_date > end_date:  # type: ignore[operator]
        raise ValueError(f"start_date ({start_date}) must be <= end_date ({end_date})")

    start_str = format_date(start_date) if start_date is not None else None
    end_str = format_date(end_date) if end_date is not None else None
    return start_str, end_str


def _build_query(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Build the DuckDB query for daily winner-rate aggregates.

    Returns one row per trade_date with market-wide chip-profit stats.
    """
    where_clauses: list[str] = []
    if start_date is not None:
        where_clauses.append(f"p.trade_date >= '{start_date}'")
    if end_date is not None:
        where_clauses.append(f"p.trade_date <= '{end_date}'")
    where_line = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    return f"""
    WITH wr_agg AS (
        SELECT
            p.trade_date,
            COUNT(*)         AS stock_count,
            AVG(p.winner_rate)          AS avg_winner_rate,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY p.winner_rate) AS med_winner_rate,
            SUM(CASE WHEN p.winner_rate > 90 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS pct_gt_90,
            SUM(CASE WHEN p.winner_rate > 95 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(COUNT(*), 0) AS pct_gt_95,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY p.winner_rate)
                FILTER (WHERE p.winner_rate > 90) AS med_hi_wr
        FROM stk_cyq_perf p
        {where_line}
        GROUP BY p.trade_date
    )
    SELECT *
    FROM wr_agg
    ORDER BY trade_date
    """


# ── Public API ───────────────────────────────────────────────────────────────


def compute_winner_rate_pressure(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> WinnerRatePressureSummary:
    """Compute market-wide chip-profit / winner-rate pressure.

    Parameters
    ----------
    con_or_path : duckdb.DuckDBPyConnection or str
        An open DuckDB connection *or* a path to a ``.duckdb`` file.
        When a path is given a **read-only** connection is opened and
        automatically closed after the query.
    start_date : str or date, optional
        Earliest trade date to include (inclusive).
    end_date : str or date, optional
        Latest trade date to include (inclusive).

    Returns
    -------
    WinnerRatePressureSummary
        Dictionary with keys:

        * ``latest_trade_date`` (str)
        * ``latest_stock_count`` (int)
        * ``latest_avg_winner_rate`` (float, 0–100)
        * ``latest_med_winner_rate`` (float, 0–100)
        * ``latest_pct_gt_90`` (float, 0–1)
        * ``latest_pct_gt_95`` (float, 0–1)
        * ``latest_med_hi_wr`` (float, median wr for wr>90 stocks)
        * ``historical_max_avg_wr`` (float)
        * ``historical_max_avg_wr_date`` (str)
        * ``historical_max_pct_gt90`` (float)
        * ``historical_max_pct_gt90_date`` (str)
        * ``historical_percentile_of_avg_wr`` (float, 0–1)
        * ``historical_percentile_of_pct_gt90`` (float, 0–1)
        * ``cost_pressure`` (dict)
        * ``threshold_stats`` (dict)
        * ``hit`` (bool) — whether the latest day exceeds any default threshold
        * ``daily_series`` (list of dict)
        * ``coverage_note`` (str) — data availability note
        * ``data_range`` (dict) — min/max dates in the result
    """
    start_str, end_str = _validate_date_window(start_date, end_date)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = _build_query(start_date=start_str, end_date=end_str)
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df.empty:
        raise ValueError(
            "No data returned from stk_cyq_perf. Check the date window."
        )

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return _build_summary(df)


def _build_summary(df: pd.DataFrame) -> WinnerRatePressureSummary:
    """Construct the summary dict from the daily-aggregated DataFrame."""
    latest: Any = df.iloc[-1]
    latest_trade_date: pd.Timestamp = pd.Timestamp(latest["trade_date"])

    # Historical extremes
    max_avg_idx: int = int(df["avg_winner_rate"].idxmax())
    max_avg_row: Any = df.loc[max_avg_idx]
    max_avg_date: pd.Timestamp = pd.Timestamp(max_avg_row["trade_date"])

    max_pct90_idx: int = int(df["pct_gt_90"].idxmax())
    max_pct90_row: Any = df.loc[max_pct90_idx]
    max_pct90_date: pd.Timestamp = pd.Timestamp(max_pct90_row["trade_date"])

    # Percentile ranks
    avg_wr_percentile: float = float(
        (df["avg_winner_rate"] <= latest["avg_winner_rate"]).mean()
    )
    pct90_percentile: float = float(
        (df["pct_gt_90"] <= latest["pct_gt_90"]).mean()
    )

    # Threshold-check
    wr_avg_hit = bool(latest["avg_winner_rate"] >= DEFAULT_WR_AVG_THRESHOLD)
    wr_med_hit = bool(latest["med_winner_rate"] >= DEFAULT_WR_MED_THRESHOLD)
    wr_gt90_hit = bool(latest["pct_gt_90"] >= DEFAULT_WR_GT90_THRESHOLD)
    wr_gt95_hit = bool(latest["pct_gt_95"] >= DEFAULT_WR_GT95_THRESHOLD)
    any_hit = wr_avg_hit or wr_med_hit or wr_gt90_hit or wr_gt95_hit

    # Threshold statistics: how many days hit each threshold
    thresholds = {
        f"avg_wr >= {DEFAULT_WR_AVG_THRESHOLD}": wr_avg_hit,
        f"med_wr >= {DEFAULT_WR_MED_THRESHOLD}": wr_med_hit,
        f"pct_gt_90 >= {DEFAULT_WR_GT90_THRESHOLD}": wr_gt90_hit,
        f"pct_gt_95 >= {DEFAULT_WR_GT95_THRESHOLD}": wr_gt95_hit,
    }
    threshold_stats: dict[str, dict[str, Any]] = {}
    for label, is_hit in thresholds.items():
        field = label.split(" ")[0]
        if field.startswith("avg"):
            series = df["avg_winner_rate"]
            thresh = DEFAULT_WR_AVG_THRESHOLD
        elif field.startswith("med"):
            series = df["med_winner_rate"]
            thresh = DEFAULT_WR_MED_THRESHOLD
        elif field == "pct_gt_90":
            series = df["pct_gt_90"]
            thresh = DEFAULT_WR_GT90_THRESHOLD
        else:
            series = df["pct_gt_95"]
            thresh = DEFAULT_WR_GT95_THRESHOLD

        hits = df[series >= thresh]
        threshold_stats[label] = {
            "hit_count": int(len(hits)),
            "hit_rate": float(len(hits) / max(len(df), 1)),
            "latest_hit": bool(is_hit),
            "sample_dates": [
                format_date(pd.Timestamp(r["trade_date"]))
                for _, r in hits.head(10).iterrows()
            ],
        }

    # Top-10 extreme days
    df_sorted_avg = df.sort_values("avg_winner_rate", ascending=False).reset_index(drop=True)
    top10_dates: list[dict[str, Any]] = []
    for _, row in df_sorted_avg.head(10).iterrows():
        top10_dates.append({
            "trade_date": format_date(pd.Timestamp(row["trade_date"])),
            "avg_winner_rate": float(row["avg_winner_rate"]),
            "med_winner_rate": float(row["med_winner_rate"]),
            "pct_gt_90": float(row["pct_gt_90"]),
            "pct_gt_95": float(row["pct_gt_95"]),
        })

    # Daily series
    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        daily_series.append({
            "trade_date": format_date(pd.Timestamp(row["trade_date"])),
            "stock_count": int(row["stock_count"]),
            "avg_winner_rate": float(row["avg_winner_rate"]),
            "med_winner_rate": float(row["med_winner_rate"]),
            "pct_gt_90": float(row["pct_gt_90"]),
            "pct_gt_95": float(row["pct_gt_95"]),
        })

    # Cost pressure for the latest day (determined by a separate query)
    cost_pressure = _compute_cost_pressure(latest_trade_date)

    # Coverage note
    data_start = format_date(pd.Timestamp(df.iloc[0]["trade_date"]))
    data_end = format_date(pd.Timestamp(df.iloc[-1]["trade_date"]))
    coverage_note = (
        f"stk_cyq_perf data from {data_start} to {data_end}. "
        "stk_cyq_chips is NOT used (removed from sync). "
        "No live cost/price ratio query if no close-price join available."
    )

    return {
        "latest_trade_date": format_date(latest_trade_date),
        "latest_stock_count": int(latest["stock_count"]),
        "latest_avg_winner_rate": float(latest["avg_winner_rate"]),
        "latest_med_winner_rate": float(latest["med_winner_rate"]),
        "latest_pct_gt_90": float(latest["pct_gt_90"]),
        "latest_pct_gt_95": float(latest["pct_gt_95"]),
        "latest_med_hi_wr": (
            float(latest["med_hi_wr"]) if pd.notna(latest["med_hi_wr"]) else None
        ),
        "historical_max_avg_wr": float(max_avg_row["avg_winner_rate"]),
        "historical_max_avg_wr_date": format_date(max_avg_date),
        "historical_max_pct_gt90": float(max_pct90_row["pct_gt_90"]),
        "historical_max_pct_gt90_date": format_date(max_pct90_date),
        "historical_percentile_of_avg_wr": float(avg_wr_percentile),
        "historical_percentile_of_pct_gt90": float(pct90_percentile),
        "cost_pressure": cost_pressure,
        "threshold_stats": threshold_stats,
        "hit": any_hit,
        "top10_dates": top10_dates,
        "daily_series": daily_series,
        "coverage_note": coverage_note,
        "data_range": {
            "min_date": data_start,
            "max_date": data_end,
        },
    }


def _compute_cost_pressure(
    latest_date: pd.Timestamp,
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
) -> dict[str, Any]:
    """Compute cost/price ratio metrics for the latest date.

    Joins stk_cyq_perf with stk_factor_pro to get close prices.
    Only considers stocks with winner_rate > 90 (high-profit cohort).
    """
    date_str = format_date(latest_date)
    con = get_connection(duckdb_path, read_only=True)
    try:
        df = con.execute(f"""
            SELECT
                p.winner_rate,
                p.cost_5pct,
                p.cost_50pct,
                p.cost_95pct,
                p.weight_avg,
                f.close,
                p.cost_50pct / NULLIF(f.close, 0) AS cost50_close_ratio,
                p.weight_avg / NULLIF(f.close, 0) AS wavg_close_ratio,
                (f.close - p.cost_50pct) / NULLIF(f.close, 0) AS profit_margin
            FROM stk_cyq_perf p
            JOIN stk_factor_pro f ON p.ts_code = f.ts_code AND p.trade_date = f.trade_date
            WHERE p.trade_date = '{date_str}'
              AND p.winner_rate > 90
              AND f.close > 0
        """).fetchdf()
    finally:
        con.close()

    if df.empty:
        return {
            "latest_date": date_str,
            "n_high_wr_stocks": 0,
            "note": "No stocks with winner_rate > 90 found on this date.",
        }

    median_cost50_close = float(df["cost50_close_ratio"].median())
    median_wavg_close = float(df["wavg_close_ratio"].median())
    median_profit_margin = float(df["profit_margin"].median())
    n_stocks = len(df)

    return {
        "latest_date": date_str,
        "n_high_wr_stocks": int(n_stocks),
        "median_cost50_close_ratio": round(median_cost50_close, 4),
        "median_wavg_close_ratio": round(median_wavg_close, 4),
        "median_profit_margin": round(median_profit_margin, 4),
        "cost_ratio_above_095": bool(median_cost50_close >= 0.95),
        "note": (
            "High-winner-rate stocks (wr>90) have cost_50pct/close "
            f"median of {median_cost50_close:.4f}. "
            f"Profit margin median: {median_profit_margin:.4f}. "
            f"{n_stocks} stocks in high-profit cohort."
        ),
    }


def compute_signal_series(
    df: pd.DataFrame | None = None,
    *,
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Return a daily signal series (0/1) for winner-rate pressure.

    Parameters
    ----------
    df : pd.DataFrame or None
        If provided, use this DataFrame as the daily-agg result
        (from ``_build_query``).  Otherwise, query DuckDB.
    duckdb_path : str
        Path to DuckDB file (used only if ``df`` is None).
    start_date, end_date : str or None
        Date window (used only if ``df`` is None).

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``avg_winner_rate``, ``pct_gt_90``,
        ``signal`` (0 or 1).
    """
    if df is None:
        con = get_connection(duckdb_path, read_only=True)
        try:
            query = _build_query(start_date=start_date, end_date=end_date)
            df = con.execute(query).fetchdf()
        finally:
            con.close()
        if df.empty:
            return pd.DataFrame(columns=["trade_date", "avg_winner_rate", "pct_gt_90", "signal"])
        df["trade_date"] = pd.to_datetime(df["trade_date"])

    df = df.copy()
    df["signal"] = (
        (df["avg_winner_rate"] >= DEFAULT_WR_AVG_THRESHOLD)
        | (df["pct_gt_90"] >= DEFAULT_WR_GT90_THRESHOLD)
    ).astype(int)
    return df[["trade_date", "avg_winner_rate", "pct_gt_90", "signal"]]