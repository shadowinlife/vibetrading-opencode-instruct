"""
Broad market valuation percentile condition — candidate #6.

Computes PE and PB percentile ranks of a broad-market index (default SSE
Composite 000001.SH) relative to a historical lookback window.  Signals fire
when both PE percentile > X% AND PB percentile > Y%, indicating the market
is near historically expensive levels.

**Source hierarchy** (primary → fallback):

1. Tushare ``index_dailybasic`` — index-level PE/PB/PE_TTM from Tushare.
   Requires ``TUSHARE_TOKEN`` env var.  Returns exactly what the index
   provider reports (e.g., SSE Composite weighted PE).
2. Local ``stk_factor_pro`` aggregation — market-cap-weighted PE/PB computed
   from all A-share stocks on each trade date.  Uses ``pe * total_mv /
   SUM(total_mv)`` for PE (positive PE only) and ``pb * total_mv /
   SUM(total_mv)`` for PB.

The ``source_used`` field in the output records which source was active and
whether fallback was applied.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .base import format_date, get_connection, pct_rank, write_json
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default lookback in trading days for percentile computation.
DEFAULT_PE_PERCENTILE_LOOKBACK: int = 1000
DEFAULT_PB_PERCENTILE_LOOKBACK: int = 1000

# Default thresholds for signal firing (percentile, 0-100).
DEFAULT_PE_THRESHOLD: float = 80.0
DEFAULT_PB_THRESHOLD: float = 80.0

# Columns returned by Tushare index_dailybasic relevant to this module.
TUSHARE_VALUATION_FIELDS: list[str] = [
    "ts_code",
    "trade_date",
    "pe",
    "pe_ttm",
    "pb",
    "total_mv",
]

# ── Public result type ──────────────────────────────────────────────────

ValuationPercentileSummary = dict[str, Any]


# ── Tushare source ──────────────────────────────────────────────────────


def _load_tushare_token() -> str | None:
    """Load Tushare token from env or .env, returning None if unavailable."""
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return os.getenv("TUSHARE_TOKEN")
    except ImportError:
        return None


def _fetch_tushare_index_dailybasic(
    index_code: str = SSE_INDEX_CODE,
    trade_date: str | None = None,
) -> pd.DataFrame | None:
    """Fetch PE/PB from Tushare ``index_dailybasic`` for *index_code*.

    Returns a DataFrame with columns ``trade_date, pe, pe_ttm, pb, total_mv``,
    or ``None`` if the Tushare API is unavailable or returns empty data.
    """
    token = _load_tushare_token()
    if token is None:
        return None

    try:
        import tushare as ts
        pro = ts.pro_api(token)

        kwargs: dict[str, Any] = {"ts_code": index_code}
        if trade_date is not None:
            kwargs["trade_date"] = trade_date

        df: pd.DataFrame = pro.query("index_dailybasic", **kwargs)  # type: ignore[no-untyped-call]
    except Exception:
        return None

    if df is None or df.empty:
        return None

    required = {"trade_date", "pe", "pb"}
    missing = required - set(df.columns)
    if missing:
        return None

    out = df[["trade_date", "pe", "pe_ttm", "pb", "total_mv"]].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out = out.sort_values("trade_date").reset_index(drop=True)
    return out


# ── Local fallback ──────────────────────────────────────────────────────


def _compute_local_valuation(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Compute market-cap-weighted PE/PB from ``stk_factor_pro``.

    Returns a DataFrame with columns ``trade_date, pe_w, pb_w, total_mv_sum``
    where ``pe_w`` and ``pb_w`` are market-cap-weighted averages.
    """
    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    where_clauses: list[str] = ["amount > 0", "total_mv > 0"]
    if start_date is not None:
        where_clauses.append(f"trade_date >= '{start_date}'")
    if end_date is not None:
        where_clauses.append(f"trade_date <= '{end_date}'")
    where_line = " AND ".join(where_clauses)

    pe_query = f"""
    WITH pe_eligible AS (
        SELECT trade_date, pe, total_mv
        FROM stk_factor_pro
        WHERE {where_line} AND pe > 0
    ), pe_daily_agg AS (
        SELECT
            trade_date,
            SUM(pe * total_mv) / NULLIF(SUM(total_mv), 0) AS pe_w
        FROM pe_eligible
        GROUP BY trade_date
    )
    SELECT * FROM pe_daily_agg ORDER BY trade_date
    """

    pb_query = f"""
    WITH pb_eligible AS (
        SELECT trade_date, pb, total_mv
        FROM stk_factor_pro
        WHERE {where_line} AND pb > 0
    ), pb_daily_agg AS (
        SELECT
            trade_date,
            SUM(pb * total_mv) / NULLIF(SUM(total_mv), 0) AS pb_w
        FROM pb_eligible
        GROUP BY trade_date
    )
    SELECT * FROM pb_daily_agg ORDER BY trade_date
    """

    total_mv_query = f"""
    SELECT trade_date, SUM(total_mv) AS total_mv_sum
    FROM stk_factor_pro
    WHERE {where_line}
    GROUP BY trade_date
    ORDER BY trade_date
    """

    try:
        df_pe = con.execute(pe_query).fetchdf()
        df_pb = con.execute(pb_query).fetchdf()
        df_mv = con.execute(total_mv_query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df_pe.empty or df_pb.empty:
        raise ValueError(
            "No valuation data in stk_factor_pro for the given date range. "
            "Check that pe/pb columns are populated."
        )

    df_pe["trade_date"] = pd.to_datetime(df_pe["trade_date"])
    df_pb["trade_date"] = pd.to_datetime(df_pb["trade_date"])
    df_mv["trade_date"] = pd.to_datetime(df_mv["trade_date"])

    out = df_pe.merge(df_pb, on="trade_date", how="outer").merge(
        df_mv, on="trade_date", how="outer"
    )
    out = out.sort_values("trade_date").reset_index(drop=True)
    return out


# ── Percentile computation ──────────────────────────────────────────────


def _compute_pe_pb_percentile(
    df: pd.DataFrame,
    *,
    pe_col: str = "pe",
    pb_col: str = "pb",
    pe_lookback: int = DEFAULT_PE_PERCENTILE_LOOKBACK,
    pb_lookback: int = DEFAULT_PB_PERCENTILE_LOOKBACK,
) -> tuple[float, float]:
    """Compute PE and PB percentile ranks of the latest row in *df*.

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns matching *pe_col*, *pb_col*, sorted ascending on date.
    pe_lookback : int
        Trading-day lookback for PE percentile.
    pb_lookback : int
        Trading-day lookback for PB percentile.

    Returns
    -------
    tuple[float, float]
        PE percentile (0–100), PB percentile (0–100).
    """
    if df.empty:
        return float("nan"), float("nan")

    assert pe_col in df.columns, f"Column {pe_col!r} not in DataFrame"
    assert pb_col in df.columns, f"Column {pb_col!r} not in DataFrame"

    pe_series = df[pe_col].dropna().tail(pe_lookback)
    pb_series = df[pb_col].dropna().tail(pb_lookback)

    if pe_series.empty or pb_series.empty:
        return float("nan"), float("nan")

    pe_pct = float(pct_rank(pe_series).iloc[-1])
    pb_pct = float(pct_rank(pb_series).iloc[-1])
    return pe_pct, pb_pct


def _describe_source(
    df: pd.DataFrame,
    use_tushare: bool,
    tushare_failed: bool,
    tushare_reason: str | None,
) -> dict[str, Any]:
    """Build source metadata for output."""
    if use_tushare:
        return {
            "source_used": "tushare:index_dailybasic",
            "source_type": "external",
            "fallback_applied": False,
            "fallback_reason": None,
        }
    else:
        return {
            "source_used": "local:stk_factor_pro_aggregation",
            "source_type": "local",
            "fallback_applied": tushare_failed,
            "fallback_reason": tushare_reason if tushare_failed else None,
        }


# ── Public API ──────────────────────────────────────────────────────────


def compute_valuation_percentile(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    index_code: str = SSE_INDEX_CODE,
    pe_lookback: int = DEFAULT_PE_PERCENTILE_LOOKBACK,
    pb_lookback: int = DEFAULT_PB_PERCENTILE_LOOKBACK,
    pe_threshold: float = DEFAULT_PE_THRESHOLD,
    pb_threshold: float = DEFAULT_PB_THRESHOLD,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    force_local: bool = False,
) -> ValuationPercentileSummary:
    """Compute broad-market PE/PB percentile ranks with source fallback.

    Source priority:
    1. Tushare ``index_dailybasic`` (if token available and not ``force_local``)
    2. Local ``stk_factor_pro`` market-cap-weighted aggregation

    Parameters
    ----------
    con_or_path : duckdb.DuckDBPyConnection or str
        An open DuckDB connection or path.  Only used for the local fallback;
        Tushare does not need a DB connection.
    index_code : str
        Index code for Tushare query (e.g. ``"000001.SH"``).  Ignored when
        the local fallback is used.
    pe_lookback : int
        Trading-day lookback for PE percentile (default 1000).
    pb_lookback : int
        Trading-day lookback for PB percentile (default 1000).
    pe_threshold : float
        Percentile threshold for PE signal (default 80).
    pb_threshold : float
        Percentile threshold for PB signal (default 80).
    start_date : str or date, optional
        Earliest date to consider.
    end_date : str or date, optional
        Latest date to consider.
    force_local : bool
        If ``True``, skip Tushare and use the local fallback directly.

    Returns
    -------
    ValuationPercentileSummary
        Dictionary with keys:
        * ``report_date`` (str)
        * ``pe_percentile`` (float, 0–100)
        * ``pb_percentile`` (float, 0–100)
        * ``pe_value`` (float) — latest PE (index or market-cap-weighted)
        * ``pb_value`` (float) — latest PB
        * ``total_mv`` (float or None)
        * ``pe_signal`` (bool) — PE percentile >= pe_threshold
        * ``pb_signal`` (bool) — PB percentile >= pb_threshold
        * ``joint_signal`` (bool) — both PE and PB above thresholds
        * ``source`` (dict) — provenance metadata
        * ``pe_threshold``, ``pb_threshold``, ``pe_lookback``, ``pb_lookback``
        * ``historical_pe_max``, ``historical_pe_min``
        * ``historical_pb_max``, ``historical_pb_min``
        * ``daily_series`` (list[dict]) — full daily time series
    """
    start_str = format_date(start_date) if start_date is not None else None
    end_str = format_date(end_date) if end_date is not None else None

    use_tushare = False
    tushare_failed = False
    tushare_reason: str | None = None
    df: pd.DataFrame | None = None

    if not force_local:
        try:
            df = _fetch_tushare_index_dailybasic(
                index_code=index_code,
                trade_date=end_str,
            )
        except Exception:
            df = None

        if df is not None and not df.empty:
            use_tushare = True
        else:
            tushare_failed = True
            tushare_reason = (
                "Tushare index_dailybasic unavailable or returned empty data"
            )

    if df is None or df.empty:
        df = _compute_local_valuation(
            con_or_path,
            start_date=start_str,
            end_date=end_str,
        )
        if "pe_w" in df.columns:
            df = df.rename(columns={"pe_w": "pe", "pb_w": "pb"})
        if "total_mv_sum" in df.columns:
            df = df.rename(columns={"total_mv_sum": "total_mv"})

    if df.empty:
        raise ValueError(
            "No valuation data available from either Tushare or local "
            "stk_factor_pro. Check data sources and date range."
        )

    pe_pct, pb_pct = _compute_pe_pb_percentile(
        df,
        pe_lookback=pe_lookback,
        pb_lookback=pb_lookback,
    )

    latest = df.iloc[-1]
    latest_date = pd.Timestamp(latest["trade_date"])
    pe_val = float(latest["pe"]) if pd.notna(latest.get("pe")) else float("nan")
    pb_val = float(latest["pb"]) if pd.notna(latest.get("pb")) else float("nan")
    mv_val = (
        float(latest["total_mv"])
        if "total_mv" in df.columns and pd.notna(latest.get("total_mv"))
        else None
    )

    pe_signal = bool(pe_pct >= pe_threshold) if not np.isnan(pe_pct) else False
    pb_signal = bool(pb_pct >= pb_threshold) if not np.isnan(pb_pct) else False
    joint_signal = pe_signal and pb_signal

    pe_series_clean = df["pe"].dropna()
    pb_series_clean = df["pb"].dropna()
    pe_max = float(pe_series_clean.max()) if not pe_series_clean.empty else float("nan")
    pe_min = float(pe_series_clean.min()) if not pe_series_clean.empty else float("nan")
    pb_max = float(pb_series_clean.max()) if not pb_series_clean.empty else float("nan")
    pb_min = float(pb_series_clean.min()) if not pb_series_clean.empty else float("nan")

    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        row_date = pd.Timestamp(row["trade_date"])
        entry: dict[str, Any] = {
            "trade_date": format_date(row_date),
            "pe": float(row["pe"]) if pd.notna(row.get("pe")) else None,
            "pb": float(row["pb"]) if pd.notna(row.get("pb")) else None,
        }
        if "total_mv" in df.columns:
            entry["total_mv"] = (
                float(row["total_mv"])
                if pd.notna(row.get("total_mv"))
                else None
            )
        daily_series.append(entry)

    source = _describe_source(df, use_tushare, tushare_failed, tushare_reason)

    return {
        "report_date": format_date(latest_date),
        "pe_percentile": float(pe_pct),
        "pb_percentile": float(pb_pct),
        "pe_value": pe_val,
        "pb_value": pb_val,
        "total_mv": mv_val,
        "pe_signal": pe_signal,
        "pb_signal": pb_signal,
        "joint_signal": joint_signal,
        "source": source,
        "thresholds": {
            "pe_threshold": pe_threshold,
            "pb_threshold": pb_threshold,
            "pe_lookback": pe_lookback,
            "pb_lookback": pb_lookback,
        },
        "historical_extremes": {
            "pe_max": pe_max,
            "pe_min": pe_min,
            "pb_max": pb_max,
            "pb_min": pb_min,
        },
        "daily_series": daily_series,
    }


# ── Bulk summary helper (for validation) ──────────────────────────────


def compute_valuation_series(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    pe_lookback: int = DEFAULT_PE_PERCENTILE_LOOKBACK,
    pb_lookback: int = DEFAULT_PB_PERCENTILE_LOOKBACK,
    force_local: bool = False,
) -> pd.DataFrame:
    """Compute daily PE/PB percentile series for validation / tuning.

    Returns a DataFrame with columns ``trade_date, pe, pb, pe_percentile,
    pb_percentile`` suitable for plotting or statistical analysis.
    """
    if force_local:
        df = _compute_local_valuation(con_or_path)
        df = df.rename(columns={"pe_w": "pe", "pb_w": "pb"})
        if "total_mv_sum" in df.columns:
            df = df.rename(columns={"total_mv_sum": "total_mv"})
    else:
        df = _fetch_tushare_index_dailybasic()
        if df is None or df.empty:
            df = _compute_local_valuation(con_or_path)
            df = df.rename(columns={"pe_w": "pe", "pb_w": "pb"})
            if "total_mv_sum" in df.columns:
                df = df.rename(columns={"total_mv_sum": "total_mv"})

    if df.empty:
        raise ValueError("No valuation data available from any source.")

    pe_pcts: list[float] = []
    pb_pcts: list[float] = []
    pe_vals = df["pe"].values
    pb_vals = df["pb"].values
    n = len(df)

    for i in range(n):
        pe_start = max(0, i - pe_lookback + 1)
        pb_start = max(0, i - pb_lookback + 1)
        pe_win = pd.Series(pe_vals[pe_start : i + 1]).dropna()
        pb_win = pd.Series(pb_vals[pb_start : i + 1]).dropna()

        pe_pcts.append(float(pct_rank(pe_win).iloc[-1]) if not pe_win.empty else float("nan"))
        pb_pcts.append(float(pct_rank(pb_win).iloc[-1]) if not pb_win.empty else float("nan"))

    df["pe_percentile"] = pe_pcts
    df["pb_percentile"] = pb_pcts
    return df