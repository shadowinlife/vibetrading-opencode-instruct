"""
Top-5% turnover concentration indicator.

On each trade date, ranks all stocks by daily turnover (``amount``),
takes the top ``ceil(stock_count * TOP_PCT)`` stocks, and computes the share
of their combined turnover in total market turnover.

The core function ``compute_concentration`` is a pure-ish API: it accepts a
DuckDB connection (or path) and an optional date window, and returns a
structured dictionary with the latest value, historical extremes, percentile
rank, threshold-hit statistics, and the raw daily series.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pandas as pd

from .base import format_date, get_connection
from .metadata import CONCENTRATION_TOP_PCT, DEFAULT_DUCKDB_PATH

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

ConcentrationSummary = dict[str, Any]


# ── Private helpers ──────────────────────────────────────────────────────────

def _validate_date_window(
    start_date: str | date | None,
    end_date: str | date | None,
) -> tuple[str | None, str | None]:
    """Normalise and validate an optional date window.

    Returns ``(start_str, end_str)`` strings or ``(None, None)``.
    """
    if start_date is None and end_date is not None:
        raise ValueError("--start-date is required when --end-date is specified")
    if end_date is not None and start_date > end_date:  # type: ignore[operator]
        raise ValueError(f"start_date ({start_date}) must be <= end_date ({end_date})")

    start_str = format_date(start_date) if start_date is not None else None
    end_str = format_date(end_date) if end_date is not None else None
    return start_str, end_str


def _build_query(
    *,
    top_pct: float = CONCENTRATION_TOP_PCT,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Build the parameterised DuckDB query for daily concentration.

    The query is parameterised locally because ``duckdb.connect`` with
    ``read_only=True`` does not support ``execute(sql, params)`` with
    expression parameters.  Dates and the percentage threshold are
    compiled into the SQL string with input validation.
    """
    if not (0 < top_pct <= 100):
        raise ValueError(f"top_pct must be in (0, 100], got {top_pct}")

    pct = top_pct / 100.0  # SQL-safe float literal

    where_clauses: list[str] = []
    if start_date is not None:
        where_clauses.append(f"trade_date >= '{start_date}'")
    if end_date is not None:
        where_clauses.append(f"trade_date <= '{end_date}'")
    where_line = f"AND {' AND '.join(where_clauses)}" if where_clauses else ""

    return f"""
    WITH base AS (
        SELECT
            trade_date,
            ts_code,
            amount,
            ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY amount DESC, ts_code) AS rn,
            COUNT(*) OVER (PARTITION BY trade_date) AS stock_count,
            SUM(amount) OVER (PARTITION BY trade_date) AS total_amount
        FROM stk_factor_pro
        WHERE amount IS NOT NULL
          AND amount > 0
          AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ' OR ts_code LIKE '%.BJ')
          {where_line}
    ), daily AS (
        SELECT
            trade_date,
            MAX(stock_count) AS stock_count,
            CAST(CEIL(MAX(stock_count) * {pct}) AS BIGINT) AS top_n,
            SUM(CASE WHEN rn <= CEIL(stock_count * {pct}) THEN amount ELSE 0 END)
                / NULLIF(MAX(total_amount), 0) AS top5_share,
            MAX(total_amount) AS total_amount,
            SUM(CASE WHEN rn <= CEIL(stock_count * {pct}) THEN amount ELSE 0 END) AS top5_amount
        FROM base
        GROUP BY trade_date
    )
    SELECT *
    FROM daily
    ORDER BY trade_date
    """


# ── Public API ───────────────────────────────────────────────────────────────


def compute_concentration(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    top_pct: float = CONCENTRATION_TOP_PCT,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> ConcentrationSummary:
    """Compute top-N% turnover concentration summary.

    Parameters
    ----------
    con_or_path : duckdb.DuckDBPyConnection or str
        An open DuckDB connection *or* a path to a ``.duckdb`` file.
        When a path is given a **read-only** connection is opened and
        automatically closed after the query.
    top_pct : float
        Percentage of stocks to include in the top group (default 5.0).
    start_date : str or date, optional
        Earliest trade date to include (inclusive).
    end_date : str or date, optional
        Latest trade date to include (inclusive).

    Returns
    -------
    ConcentrationSummary
        Dictionary with keys:

        * ``latest_trade_date`` (str)
        * ``latest_top5_share`` (float, 0–1)
        * ``latest_top5_share_pct`` (float, 0–100)
        * ``latest_total_amount_billion_cny`` (float)
        * ``latest_top5_amount_billion_cny`` (float)
        * ``latest_stock_count`` (int)
        * ``latest_top_n`` (int)
        * ``historical_max_trade_date`` (str)
        * ``historical_max_top5_share`` (float)
        * ``historical_max_top5_share_pct`` (float)
        * ``historical_rank_of_latest`` (int)
        * ``historical_percentile_of_latest`` (float, 0–1)
        * ``top10_dates`` (list of dict)
        * ``threshold_stats`` (dict)
        * ``daily_series`` (list of dict — the full daily time series)
    """
    start_str, end_str = _validate_date_window(start_date, end_date)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = _build_query(
            top_pct=top_pct,
            start_date=start_str,
            end_date=end_str,
        )
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df.empty:
        raise ValueError(
            "No data returned. Check the date window and ensure "
            "stk_factor_pro contains valid turnover data."
        )

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return _build_summary(df)


def _build_summary(df: pd.DataFrame) -> ConcentrationSummary:
    """Construct the summary dict from the daily-aggregated DataFrame."""
    latest: Any = df.iloc[-1]
    latest_trade_date: pd.Timestamp = pd.Timestamp(latest["trade_date"])  # type: ignore[arg-type, assignment]

    max_idx: int = int(df["top5_share"].idxmax())  # type: ignore[arg-type]
    max_row: Any = df.loc[max_idx]
    max_trade_date: pd.Timestamp = pd.Timestamp(max_row["trade_date"])  # type: ignore[arg-type, assignment]

    # Sort descending so we can compute rank and top-10 cleanly.
    df_sorted = df.sort_values(
        ["top5_share", "trade_date"], ascending=[False, True]
    ).reset_index(drop=True)

    # 1-based rank of the latest date.
    rank_mask = df_sorted["trade_date"] == latest_trade_date
    latest_rank: int = int(rank_mask.idxmax()) + 1  # type: ignore[arg-type]
    latest_percentile: float = float(
        (df["top5_share"] <= latest["top5_share"]).mean()
    )

    # Threshold-hit statistics.
    thresholds = [0.45, 0.48, 0.49, 0.50]
    threshold_stats: dict[str, dict[str, Any]] = {}
    for t in thresholds:
        hits = df[df["top5_share"] >= t]
        threshold_stats[str(t)] = {
            "count": int(len(hits)),
            "dates": [
                format_date(d)
                for d in pd.to_datetime(hits["trade_date"]).tolist()[:20]
            ],
        }

    # Daily series for downstream consumers.
    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        row_date = pd.Timestamp(row["trade_date"])  # type: ignore[arg-type]
        daily_series.append({
            "trade_date": format_date(row_date),
            "stock_count": int(row["stock_count"]),
            "top_n": int(row["top_n"]),
            "top5_share": float(row["top5_share"]),
            "top5_share_pct": float(row["top5_share"] * 100),
            "total_amount_billion_cny": float(row["total_amount"] / 1_000_000),
            "top5_amount_billion_cny": float(row["top5_amount"] / 1_000_000),
        })

    return {
        "latest_trade_date": format_date(latest_trade_date),  # type: ignore[arg-type]
        "latest_top5_share": float(latest["top5_share"]),
        "latest_top5_share_pct": float(latest["top5_share"] * 100),
        "latest_total_amount_billion_cny": float(
            latest["total_amount"] / 1_000_000
        ),
        "latest_top5_amount_billion_cny": float(
            latest["top5_amount"] / 1_000_000
        ),
        "latest_stock_count": int(latest["stock_count"]),
        "latest_top_n": int(latest["top_n"]),
        "historical_max_trade_date": format_date(max_trade_date),  # type: ignore[arg-type]
        "historical_max_top5_share": float(max_row["top5_share"]),
        "historical_max_top5_share_pct": float(max_row["top5_share"] * 100),
        "historical_rank_of_latest": latest_rank,
        "historical_percentile_of_latest": float(latest_percentile),
        "top10_dates": [
            {
                "trade_date": format_date(
                    pd.Timestamp(row["trade_date"])  # type: ignore[arg-type]
                ),
                "top5_share_pct": float(row["top5_share"] * 100),
            }
            for _, row in df_sorted.head(10).iterrows()
        ],
        "threshold_stats": threshold_stats,
        "daily_series": daily_series,
    }
