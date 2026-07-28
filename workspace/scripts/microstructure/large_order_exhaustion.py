"""
Large-order exhaustion condition — 大单/特大单主力资金衰竭预警.

Aggregates large-order (大单) + extra-large-order (特大单) net flow across all
A‑share stocks per trading day, and produces three signal variants that capture
different dimensions of large-money flow deterioration while the index stays
at elevated levels.

Supported data sources
----------------------
* **stk_moneyflow** (Tushare) — 2010 onwards, 5663 stocks
  Fields: ``buy_lg_amount``, ``sell_lg_amount``, ``buy_elg_amount``,
  ``sell_elg_amount``, ``net_mf_amount``.
  **Unit**: 万元 (ten-thousand CNY).  All *_amount columns are 万元.
  Large-order net = (buy_lg_amount + buy_elg_amount) −
                    (sell_lg_amount + sell_elg_amount).
* **stk_moneyflow_ths** (同花顺) — 2019‑07 onwards, ~5358 stocks
  Fields: ``net_amount``, ``buy_lg_amount``, ``net_d5_amount``.
  **Unit**: 万元 (ten-thousand CNY).  ``buy_lg_amount`` is already *net* of
  sell (大单净流入额).  ``net_amount`` is total net flow across all order
  sizes.

Signal variants
---------------
1. **net_flow_negative** (flow_deterioration): Aggregate large+extra-large
   net flow turns negative while the SSE index is within the top *N* % of its
   historical highs.
2. **flow_ratio_decline** (ratio_declining): Large-order net flow as a
   fraction of total market turnover is declining over a rolling window,
   signalling fading participation even while absolute levels may still be
   positive.
3. **rolling_sum_deterioration** (rolling_deterioration): The *N*‑day rolling
   sum of aggregate large-order net flow turns negative or crosses below a
   Z‑score threshold, capturing sustained multi‑day outflows.

Unit documentation
------------------
.. epigraph::

   **All *_amount fields in stk_moneyflow and stk_moneyflow_ths are in 万元 (ten-thousand CNY).**
   stk_factor_pro.amount is in **千元** (thousand CNY).
   Cross‑table ratio calculations must divide by 10 when converting to 万元:
   ``large_net_flow_wan / (total_amount_kcy / 10)``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import duckdb
import numpy as np
import pandas as pd

from .base import format_date, get_connection
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

DataSource = Literal["tushare", "ths"]
SignalVariant = Literal[
    "flow_deterioration",
    "ratio_declining",
    "rolling_deterioration",
]

LargeOrderSummary = dict[str, Any]


# ──────────────────────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────────────────────


def _validate_data_source(source: str) -> DataSource:
    """Validate and normalise the data source parameter."""
    if source not in ("tushare", "ths"):
        raise ValueError(
            f"Unsupported data_source: {source!r}. Must be 'tushare' or 'ths'."
        )
    return source  # type: ignore[return-value]


def _build_query_tushare(
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Build SQL query for Tushare stk_moneyflow large‑order aggregation.

    Computes per‑day aggregates of large+extra-large buy/sell amounts
    and joins with total market turnover (from stk_factor_pro) and SSE close
    (from idx_factor_pro).

    Unit handling
    -------------
    * moneyflow.*_amount → 万元
    * stk_factor_pro.amount → 千元 → / 10 to convert to 万元
    * net_flow_wan = SUM(buy_lg + buy_elg − sell_lg − sell_elg) in 万元
    * flow_ratio = net_flow_wan / (total_amount_kcy / 10)  → dimensionless
    """
    where_parts: list[str] = []
    if start_date is not None:
        where_parts.append(f"m.trade_date >= '{start_date}'")
    if end_date is not None:
        where_parts.append(f"m.trade_date <= '{end_date}'")
    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    return f"""
    WITH
        moneyflow_daily AS (
            SELECT
                trade_date,
                SUM(buy_lg_amount)  AS total_buy_lg_wan,
                SUM(sell_lg_amount) AS total_sell_lg_wan,
                SUM(buy_elg_amount) AS total_buy_elg_wan,
                SUM(sell_elg_amount) AS total_sell_elg_wan,
                SUM(buy_lg_amount + buy_elg_amount
                    - sell_lg_amount - sell_elg_amount) AS net_flow_wan
            FROM stk_moneyflow m
            {where_clause}
            GROUP BY trade_date
        ),
        turnover_daily AS (
            SELECT
                trade_date,
                SUM(amount) AS total_amount_kcy
            FROM stk_factor_pro
            WHERE amount IS NOT NULL AND amount > 0
            GROUP BY trade_date
        ),
        sse_daily AS (
            SELECT trade_date, close AS sse_close
            FROM idx_factor_pro
            WHERE ts_code = '{SSE_INDEX_CODE}'
        )
    SELECT
        mf.trade_date,
        mf.total_buy_lg_wan,
        mf.total_sell_lg_wan,
        mf.total_buy_elg_wan,
        mf.total_sell_elg_wan,
        mf.net_flow_wan,
        t.total_amount_kcy,
        s.sse_close,
        -- flow_ratio = net_flow_wan / (total_amount_kcy / 10)
        -- 万元 / 万元 → dimensionless
        mf.net_flow_wan / NULLIF(t.total_amount_kcy / 10.0, 0) AS flow_ratio
    FROM moneyflow_daily mf
    JOIN turnover_daily t ON mf.trade_date = t.trade_date
    JOIN sse_daily s     ON mf.trade_date = s.trade_date
    WHERE t.total_amount_kcy > 0
    ORDER BY mf.trade_date
    """


def _build_query_ths(
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Build SQL query for 同花顺 moneyflow large‑order aggregation.

    * buy_lg_amount → 大单净流入额(万元), already net
    * net_amount → 资金净流入(万元), all order sizes
    """
    where_parts: list[str] = []
    if start_date is not None:
        where_parts.append(f"m.trade_date >= '{start_date}'")
    if end_date is not None:
        where_parts.append(f"m.trade_date <= '{end_date}'")
    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    return f"""
    WITH
        moneyflow_daily AS (
            SELECT
                trade_date,
                SUM(buy_lg_amount) AS net_flow_wan,
                -- THS buy_lg_amount is already net (大单净流入额)
                SUM(net_amount)    AS total_net_wan,
                SUM(net_d5_amount) AS total_net_d5_wan
            FROM stk_moneyflow_ths m
            {where_clause}
            GROUP BY trade_date
        ),
        turnover_daily AS (
            SELECT
                trade_date,
                SUM(amount) AS total_amount_kcy
            FROM stk_factor_pro
            WHERE amount IS NOT NULL AND amount > 0
            GROUP BY trade_date
        ),
        sse_daily AS (
            SELECT trade_date, close AS sse_close
            FROM idx_factor_pro
            WHERE ts_code = '{SSE_INDEX_CODE}'
        )
    SELECT
        mf.trade_date,
        mf.net_flow_wan,
        mf.total_net_wan,
        mf.total_net_d5_wan,
        t.total_amount_kcy,
        s.sse_close,
        mf.net_flow_wan / NULLIF(t.total_amount_kcy / 10.0, 0) AS flow_ratio
    FROM moneyflow_daily mf
    JOIN turnover_daily t ON mf.trade_date = t.trade_date
    JOIN sse_daily s     ON mf.trade_date = s.trade_date
    WHERE t.total_amount_kcy > 0
    ORDER BY mf.trade_date
    """


def _sse_near_high_mask(
    sse_series: pd.Series,
    high_threshold_pct: float = 90.0,
) -> np.ndarray:
    """Return a boolean mask for days where SSE close is in the top
    ``high_threshold_pct`` percentile of its expanding historical window.

    Parameters
    ----------
    sse_series : pd.Series
        Chronologically ordered SSE close values.
    high_threshold_pct : float
        Percentile threshold, 0‑100.  Default 90 → top 10 % of highs.

    Returns
    -------
    np.ndarray (bool)
        True where SSE close ≥ the expanding historical percentile threshold.
    """
    values = sse_series.values
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    if n == 0:
        return mask

    for i, v in enumerate(values):
        hist = values[: i + 1]
        threshold = float(np.percentile(hist, high_threshold_pct))
        mask[i] = (v >= threshold) if i > 0 else True
    return mask


def _compute_signal_variants(
    df: pd.DataFrame,
    *,
    sse_high_pct: float = 90.0,
    ratio_ma_window: int = 20,
    rolling_sum_window: int = 5,
    rolling_z_threshold: float = -1.5,
) -> pd.DataFrame:
    """Compute the three signal-variant columns on the aggregated DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain: ``trade_date``, ``net_flow_wan``, ``flow_ratio``,
        ``sse_close``.
    sse_high_pct : float
        Percentile for "SSE near high" detection.
    ratio_ma_window : int
        Rolling window (days) for detecting flow‑ratio decline.
    rolling_sum_window : int
        Rolling window (days) for rolling‑sum signal.
    rolling_z_threshold : float
        Z‑score threshold below which the rolling sum triggers.

    Returns
    -------
    pd.DataFrame
        Input with additional columns:
        ``sse_near_high``, ``signal_flow_deterioration``,
        ``signal_ratio_declining``, ``rolling_sum_wan``,
        ``rolling_sum_z``, ``signal_rolling_deterioration``,
        ``signal_any``.
    """
    df = df.copy()

    # -- SSE near high --------------------------------------------------------
    df["sse_near_high"] = _sse_near_high_mask(df["sse_close"], sse_high_pct)

    # -- Variant A: net flow negative while SSE near high --------------------
    df["signal_flow_deterioration"] = (
        (df["net_flow_wan"] < 0) & df["sse_near_high"]
    )

    # -- Variant B: flow ratio declining (current < MA) -----------------------
    df["flow_ratio_ma"] = df["flow_ratio"].rolling(ratio_ma_window).mean()
    df["signal_ratio_declining"] = (
        df["flow_ratio"] < df["flow_ratio_ma"]
    ).fillna(False)

    # -- Variant C: N-day rolling sum deterioration ---------------------------
    df["rolling_sum_wan"] = df["net_flow_wan"].rolling(rolling_sum_window).sum()
    roll = df["rolling_sum_wan"].rolling(rolling_sum_window, min_periods=rolling_sum_window)
    mean = roll.mean()
    std = roll.std(ddof=0)
    df["rolling_sum_z"] = (df["rolling_sum_wan"] - mean) / std.replace({0.0: float("nan")})
    df["signal_rolling_deterioration"] = (
        df["rolling_sum_z"] < rolling_z_threshold
    ).fillna(False)

    # -- Any-signal composite -------------------------------------------------
    df["signal_any"] = (
        df["signal_flow_deterioration"]
        | df["signal_ratio_declining"]
        | df["signal_rolling_deterioration"]
    )

    return df


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def compute_large_order_exhaustion(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    data_source: DataSource = "tushare",
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    sse_high_pct: float = 90.0,
    ratio_ma_window: int = 20,
    rolling_sum_window: int = 5,
    rolling_z_threshold: float = -1.5,
) -> LargeOrderSummary:
    """Compute large-order exhaustion signals for A‑share market.

    Aggregates large+extra-large order net flow across all stocks, joins
    with total market turnover and SSE close, then computes three signal
    variants.

    Parameters
    ----------
    con_or_path : duckdb.DuckDBPyConnection or str
        Open DuckDB connection or path to ``.duckdb`` file.
        When a string is given, a read‑only connection is opened and
        automatically closed.
    data_source : str, default ``"tushare"``
        Moneyflow source: ``"tushare"`` (stk_moneyflow) or
        ``"ths"`` (stk_moneyflow_ths).
    start_date : str or date, optional
        Earliest trade date to include (YYYY-MM-DD).  Inclusive.
    end_date : str or date, optional
        Latest trade date to include (YYYY-MM-DD).  Inclusive.
    sse_high_pct : float, default 90.0
        Percentile threshold for "SSE near high" (0‑100).
    ratio_ma_window : int, default 20
        Rolling MA window for flow‑ratio decline detection.
    rolling_sum_window : int, default 5
        Rolling window for N‑day sum deterioration signal.
    rolling_z_threshold : float, default −1.5
        Z‑score threshold for the rolling‑sum signal.

    Returns
    -------
    LargeOrderSummary
        Structured dictionary with keys documented in the module docstring.
        Top‑level keys include ``latest_snapshot``, ``variants_summary``,
        ``historical_stats``, ``recent_10d``, ``daily_series``,
        ``parameters``, ``source_info``.
    """
    _validate_data_source(data_source)

    start_str = format_date(start_date) if start_date is not None else None
    end_str = format_date(end_date) if end_date is not None else None

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        if data_source == "tushare":
            query = _build_query_tushare(start_date=start_str, end_date=end_str)
            source_cols = ["total_buy_lg_wan", "total_sell_lg_wan",
                           "total_buy_elg_wan", "total_sell_elg_wan"]
            source_desc = "stk_moneyflow (Tushare, 万元)"
            source_unit = "万元 (ten-thousand CNY)"
        else:
            query = _build_query_ths(start_date=start_str, end_date=end_str)
            source_cols = ["total_net_wan", "total_net_d5_wan"]
            source_desc = "stk_moneyflow_ths (同花顺, 万元)"
            source_unit = "万元 (ten-thousand CNY)"

        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df.empty:
        raise ValueError(
            f"No data returned from {source_desc}. Check the date window "
            "and ensure the source table has data in the requested range."
        )

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)

    # Compute signal variants
    df = _compute_signal_variants(
        df,
        sse_high_pct=sse_high_pct,
        ratio_ma_window=ratio_ma_window,
        rolling_sum_window=rolling_sum_window,
        rolling_z_threshold=rolling_z_threshold,
    )

    # -- Latest snapshot -----------------------------------------------------
    latest = df.iloc[-1]
    latest_date = format_date(pd.Timestamp(latest["trade_date"]))  # type: ignore[arg-type]

    # -- Signal variant summary ----------------------------------------------
    variants_summary: dict[str, dict[str, Any]] = {}
    variant_map: list[tuple[str, str, str]] = [
        ("flow_deterioration", "A: Net flow negative while SSE near high",
         "signal_flow_deterioration"),
        ("ratio_declining", "B: Flow/turnover ratio declining",
         "signal_ratio_declining"),
        ("rolling_deterioration", "C: Rolling-sum deterioration",
         "signal_rolling_deterioration"),
    ]
    for key, desc, col in variant_map:
        signal_days = int(df[col].sum())
        variants_summary[key] = {
            "description": desc,
            "signal_days": signal_days,
            "signal_pct": float(signal_days / len(df) * 100),
            "latest_firing": bool(df[col].iloc[-1]),
            "parameters": {
                "sse_high_pct": sse_high_pct if key == "flow_deterioration" else None,
                "ratio_ma_window": ratio_ma_window if key == "ratio_declining" else None,
                "rolling_sum_window": rolling_sum_window if key == "rolling_deterioration" else None,
                "rolling_z_threshold": rolling_z_threshold if key == "rolling_deterioration" else None,
            },
        }

    # -- Historical stats ----------------------------------------------------
    hist = df[df["signal_any"]]
    n_signal_days = int(len(hist))
    signal_pct = float(n_signal_days / len(df) * 100) if len(df) > 0 else 0.0

    # SSE percentile of latest close (expanding window)
    sse_close_vals = df["sse_close"].values
    latest_sse = latest["sse_close"]
    sse_pct_latest = float(
        (sse_close_vals <= latest_sse).mean() * 100
    )

    # -- Recent 10-day table -------------------------------------------------
    recent = df.tail(10)
    recent_rows: list[dict[str, Any]] = []
    for _, row in recent.iterrows():
        recent_rows.append({
            "trade_date": format_date(pd.Timestamp(row["trade_date"])),  # type: ignore[arg-type]
            "net_flow_wan": float(row["net_flow_wan"]),
            "net_flow_billion_cny": float(row["net_flow_wan"] / 100_000),
            "flow_ratio_pct": float(row["flow_ratio"] * 100),
            "sse_close": float(row["sse_close"]),
            "sse_near_high": bool(row["sse_near_high"]),
            "signal_flow_deterioration": bool(row["signal_flow_deterioration"]),
            "signal_ratio_declining": bool(row["signal_ratio_declining"]),
            "signal_rolling_deterioration": bool(row["signal_rolling_deterioration"]),
            "signal_any": bool(row["signal_any"]),
        })

    # -- Daily series --------------------------------------------------------
    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        daily_series.append({
            "trade_date": format_date(pd.Timestamp(row["trade_date"])),  # type: ignore[arg-type]
            "net_flow_billion_cny": float(row["net_flow_wan"] / 100_000),
            "flow_ratio_pct": float(row["flow_ratio"] * 100),
            "sse_close": float(row["sse_close"]),
            "sse_near_high": bool(row["sse_near_high"]),
            "rolling_sum_wan": (
                float(row["rolling_sum_wan"])
                if pd.notna(row.get("rolling_sum_wan"))
                else None
            ),
            "rolling_sum_z": (
                float(row["rolling_sum_z"])
                if pd.notna(row.get("rolling_sum_z"))
                else None
            ),
            "signal_flow_deterioration": bool(row["signal_flow_deterioration"]),
            "signal_ratio_declining": bool(row["signal_ratio_declining"]),
            "signal_rolling_deterioration": bool(row["signal_rolling_deterioration"]),
            "signal_any": bool(row["signal_any"]),
        })

    # -- Assemble result -----------------------------------------------------
    return {
        "latest_snapshot": {
            "trade_date": latest_date,
            "net_flow_wan": float(latest["net_flow_wan"]),
            "net_flow_billion_cny": float(latest["net_flow_wan"] / 100_000),
            "flow_ratio": float(latest["flow_ratio"]),
            "flow_ratio_pct": float(latest["flow_ratio"] * 100),
            "sse_close": float(latest["sse_close"]),
            "sse_near_high": bool(latest["sse_near_high"]),
            "sse_percentile": sse_pct_latest,
            "rolling_sum_wan": (
                float(latest["rolling_sum_wan"])
                if pd.notna(latest.get("rolling_sum_wan"))
                else None
            ),
            "rolling_sum_z": (
                float(latest["rolling_sum_z"])
                if pd.notna(latest.get("rolling_sum_z"))
                else None
            ),
            "signal_flow_deterioration": bool(latest["signal_flow_deterioration"]),
            "signal_ratio_declining": bool(latest["signal_ratio_declining"]),
            "signal_rolling_deterioration": bool(latest["signal_rolling_deterioration"]),
            "signal_any": bool(latest["signal_any"]),
        },
        "variants_summary": variants_summary,
        "historical_stats": {
            "total_days": int(len(df)),
            "signal_days": n_signal_days,
            "signal_days_pct": signal_pct,
            "date_range_start": format_date(pd.Timestamp(df["trade_date"].iloc[0])),  # type: ignore[arg-type]
            "date_range_end": latest_date,
            "net_flow_max_billion_cny": float(df["net_flow_wan"].max() / 100_000),
            "net_flow_min_billion_cny": float(df["net_flow_wan"].min() / 100_000),
            "net_flow_mean_billion_cny": float(df["net_flow_wan"].mean() / 100_000),
            "flow_ratio_max_pct": float(df["flow_ratio"].max() * 100),
            "flow_ratio_min_pct": float(df["flow_ratio"].min() * 100),
        },
        "recent_10d": recent_rows,
        "daily_series": daily_series,
        "parameters": {
            "data_source": data_source,
            "sse_high_pct": sse_high_pct,
            "ratio_ma_window": ratio_ma_window,
            "rolling_sum_window": rolling_sum_window,
            "rolling_z_threshold": rolling_z_threshold,
        },
        "source_info": {
            "source_table": source_desc,
            "amount_unit": source_unit,
            "net_flow_formula": (
                "SUM(buy_lg_amount + buy_elg_amount − sell_lg_amount − sell_elg_amount)"
                if data_source == "tushare"
                else "SUM(buy_lg_amount)  -- already net (大单净流入额)"
            ),
            "turnover_unit": "千元 (thousand CNY), from stk_factor_pro.amount",
            "flow_ratio_formula": (
                "net_flow_wan / (total_amount_kcy / 10.0) — dimensionless"
            ),
            "sse_source": f"idx_factor_pro, ts_code={SSE_INDEX_CODE}",
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Pure signal time‑series builder (for tuning / evaluation)
# ──────────────────────────────────────────────────────────────────────────────


def compute_exhaustion_signal_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    data_source: DataSource = "tushare",
    sse_high_pct: float = 90.0,
    ratio_ma_window: int = 20,
    rolling_sum_window: int = 5,
    rolling_z_threshold: float = -1.5,
    df_agg: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build a daily large-order exhaustion signal time series.

    Similar to :func:`compute_signal_series` in ``tune_escape_top``,
    this returns a DataFrame suitable for merging with forward drawdowns
    for parameter tuning.

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB.  Ignored when ``df_agg`` is supplied.
    data_source : str
        ``"tushare"`` or ``"ths"``.
    sse_high_pct : float
    ratio_ma_window : int
    rolling_sum_window : int
    rolling_z_threshold : float
    df_agg : pd.DataFrame or None
        Pre‑aggregated DataFrame (from ``compute_large_order_exhaustion``
        daily_series or direct query).  Must contain ``trade_date``,
        ``net_flow_wan``, ``flow_ratio``, ``sse_close``.

    Returns
    -------
    pd.DataFrame
        ``trade_date``, ``sse_close``, ``sse_near_high``,
        ``signal_flow_deterioration``, ``signal_ratio_declining``,
        ``signal_rolling_deterioration``, ``signal_any``.
    """
    if df_agg is None:
        result = compute_large_order_exhaustion(
            duckdb_path,
            data_source=data_source,
            sse_high_pct=sse_high_pct,
            ratio_ma_window=ratio_ma_window,
            rolling_sum_window=rolling_sum_window,
            rolling_z_threshold=rolling_z_threshold,
        )
        df_agg = pd.DataFrame(result["daily_series"])
        df_agg["trade_date"] = pd.to_datetime(df_agg["trade_date"])
        df_agg = df_agg.sort_values("trade_date").reset_index(drop=True)

    # If the raw moneyflow columns are present (e.g. from synthetic test
    # fixture or direct query), compute signal variants on demand.
    raw_cols_present = all(
        c in df_agg.columns for c in ("net_flow_wan", "flow_ratio", "sse_close")
    )
    if raw_cols_present and "signal_any" not in df_agg.columns:
        df_agg = _compute_signal_variants(
            df_agg,
            sse_high_pct=sse_high_pct,
            ratio_ma_window=ratio_ma_window,
            rolling_sum_window=rolling_sum_window,
            rolling_z_threshold=rolling_z_threshold,
        )

    cols = [
        "trade_date",
        "sse_close",
        "sse_near_high",
        "signal_flow_deterioration",
        "signal_ratio_declining",
        "signal_rolling_deterioration",
        "signal_any",
    ]
    return pd.DataFrame(df_agg[[c for c in cols if c in df_agg.columns]])