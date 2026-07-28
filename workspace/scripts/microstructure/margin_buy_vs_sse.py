"""
Margin-buy ratio vs SSE close divergence indicator — pure computation.

Core function ``compute_margin_buy_vs_sse(duckdb_path)`` aggregates
``stk_margin``, ``stk_factor_pro``, and ``idx_factor_pro`` (SSE close)
into a structured JSON-serialisable dict capturing:

- latest margin-buy ratio, margin balance, SSE close
- 5/20-day rolling means and 20-day changes
- divergence detection: margin balance rising while margin-buy ratio falling
- historical percentile / rank
- recent 10 trading-day snapshot

Formula (preserved from reference):
    margin_buy_ratio = SUM(rzmre) / (SUM(amount) * 1000)
where ``amount`` is in thousands of yuan (千元) from ``stk_factor_pro``.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd


def fmt_date(value: object) -> str:
    """Convert a pandas Timestamp-like value to ``YYYY-MM-DD`` string."""
    return str(pd.Timestamp(str(value)).date())


# ------------------------------------------------------------------ #
# core computation
# ------------------------------------------------------------------ #
def compute_margin_buy_vs_sse(
    duckdb_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
    divergence_lookback_days: int = 20,
) -> dict[str, Any]:
    """Aggregate margin-buy / turnover ratio and SSE divergence.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database (e.g. ``./duckdb/ashare.duckdb``).
    start_date : str or None
        Optional lower-bound filter on ``trade_date`` (YYYY-MM-DD).
    end_date : str or None
        Optional upper-bound filter on ``trade_date`` (YYYY-MM-DD).
    divergence_lookback_days : int
        Trading-day lookback for divergence detection (ratio / rzye / SSE
        change).  Default 20 (preserves pre-existing behaviour).

    Returns
    -------
    dict
        Structured summary with keys documented in the module docstring.
    """
    date_filter = ""
    params: list[str] = []
    if start_date is not None:
        date_filter += "  AND m.trade_date >= ?\n"
        params.append(start_date)
    if end_date is not None:
        date_filter += "  AND m.trade_date <= ?\n"
        params.append(end_date)

    # ------------------------------------------------------------------ #
    # SQL: join stk_margin (aggregated), stk_factor_pro (aggregated),
    #      idx_factor_pro (SSE close only)
    # ------------------------------------------------------------------ #
    query = rf"""
    WITH turnover AS (
        SELECT trade_date, SUM(amount) AS total_amount_kcy
        FROM stk_factor_pro
        WHERE amount IS NOT NULL
          AND amount > 0
        GROUP BY trade_date
    ), margin AS (
        SELECT trade_date, SUM(rzmre) AS total_rzmre_yuan, SUM(rzye) AS total_rzye_yuan
        FROM stk_margin
        GROUP BY trade_date
    ), sse AS (
        SELECT trade_date, close AS sse_close
        FROM idx_factor_pro
        WHERE ts_code = '000001.SH'
    )
    SELECT
        m.trade_date,
        m.total_rzmre_yuan,
        m.total_rzye_yuan,
        t.total_amount_kcy,
        s.sse_close,
        m.total_rzmre_yuan / (t.total_amount_kcy * 1000.0) AS margin_buy_ratio
    FROM margin m
    JOIN turnover t ON m.trade_date = t.trade_date
    JOIN sse s     ON m.trade_date = s.trade_date
    WHERE t.total_amount_kcy > 0
         {date_filter}
    ORDER BY m.trade_date
    """

    con = duckdb.connect(duckdb_path, read_only=True)
    df = con.execute(query, params).fetchdf()
    con.close()

    if df.empty:
        raise ValueError(
            "No data returned. Verify the DuckDB path and that "
            "stk_margin / stk_factor_pro / idx_factor_pro tables have data "
            "in the requested date range."
        )

    # ------------------------------------------------------------------ #
    # rolling computations (pandas)
    # ------------------------------------------------------------------ #
    lb = divergence_lookback_days
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["margin_buy_ratio_ma5"] = df["margin_buy_ratio"].rolling(5).mean()
    df["margin_buy_ratio_ma20"] = df["margin_buy_ratio"].rolling(20).mean()
    df["rzye_ma20"] = df["total_rzye_yuan"].rolling(20).mean()
    df["sse_ma20"] = df["sse_close"].rolling(20).mean()
    df[f"ratio_{lb}d_change"] = df["margin_buy_ratio"] / df["margin_buy_ratio"].shift(lb) - 1.0
    df[f"rzye_{lb}d_change"] = df["total_rzye_yuan"] / df["total_rzye_yuan"].shift(lb) - 1.0
    df[f"sse_{lb}d_change"] = df["sse_close"] / df["sse_close"].shift(lb) - 1.0

    # ------------------------------------------------------------------ #
    # latest snapshot
    # ------------------------------------------------------------------ #
    latest = df.iloc[-1]
    latest_trade_date = fmt_date(latest["trade_date"])

    # ------------------------------------------------------------------ #
    # divergence detection
    # ------------------------------------------------------------------ #
    trailing = df.dropna(
        subset=[f"ratio_{lb}d_change", f"rzye_{lb}d_change", f"sse_{lb}d_change"]
    ).copy()
    divergence = trailing[
        (trailing[f"rzye_{lb}d_change"] > 0) & (trailing[f"ratio_{lb}d_change"] < 0)
    ]
    latest_is_divergence = (
        bool(
            (latest[f"rzye_{lb}d_change"] > 0) and (latest[f"ratio_{lb}d_change"] < 0)
        )
        if pd.notna(latest[f"rzye_{lb}d_change"]) and pd.notna(latest[f"ratio_{lb}d_change"])
        else False
    )

    # ------------------------------------------------------------------ #
    # historical stats
    # ------------------------------------------------------------------ #
    ratio_rank = int((df["margin_buy_ratio"] > latest["margin_buy_ratio"]).sum()) + 1
    ratio_percentile = float((df["margin_buy_ratio"] <= latest["margin_buy_ratio"]).mean())

    # ------------------------------------------------------------------ #
    # recent 10d table
    # ------------------------------------------------------------------ #
    recent = df.tail(10)
    recent_rows: list[dict[str, Any]] = []
    for _, row in recent.iterrows():
        recent_rows.append({
            "trade_date": fmt_date(row["trade_date"]),
            "margin_buy_ratio_pct": float(row["margin_buy_ratio"] * 100),
            "rzye_trillion": float(row["total_rzye_yuan"] / 1e12),
            "sse_close": float(row["sse_close"]),
        })

    # ------------------------------------------------------------------ #
    # assemble result
    # ------------------------------------------------------------------ #
    result: dict[str, Any] = {
        "latest_trade_date": latest_trade_date,
        "latest_margin_buy_ratio": float(latest["margin_buy_ratio"]),
        "latest_margin_buy_ratio_pct": float(latest["margin_buy_ratio"] * 100),
        "latest_total_rzmre_billion": float(latest["total_rzmre_yuan"] / 1e8),
        "latest_total_rzye_trillion": float(latest["total_rzye_yuan"] / 1e12),
        "latest_sse_close": float(latest["sse_close"]),
        "latest_ratio_vs_ma20_pct_points": float(
            (latest["margin_buy_ratio"] - latest["margin_buy_ratio_ma20"]) * 100
        ),
        f"latest_rzye_vs_{lb}d": float(latest[f"rzye_{lb}d_change"]),
        f"latest_ratio_vs_{lb}d": float(latest[f"ratio_{lb}d_change"]),
        f"latest_sse_vs_{lb}d": float(latest[f"sse_{lb}d_change"]),
        "latest_is_divergence": latest_is_divergence,
        "divergence_days_total": int(len(divergence)),
        "ratio_historical_rank": ratio_rank,
        "ratio_historical_percentile": ratio_percentile,
        "historical_ratio_max_pct": float(df["margin_buy_ratio"].max() * 100),
        "historical_ratio_min_pct": float(df["margin_buy_ratio"].min() * 100),
        "recent_10d": recent_rows,
    }

    return result