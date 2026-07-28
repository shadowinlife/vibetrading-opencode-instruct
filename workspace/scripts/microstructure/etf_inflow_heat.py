"""
ETF inflow heat signal module.

Computes daily aggregate ETF turnover normalised by total A-share market
turnover and generates retail-inflow-heat signals via rolling-percentile and
absolute-threshold rules.

The **contrarian hypothesis**:
  When ETF aggregate turnover spikes to historically extreme levels (rolling
  percentile) while the market is already elevated, it signals **retail
  euphoria / late-cycle chasing** — historically a bearish drawdown signal.

Two signal conditions are computed independently and combined:
  1. **Percentile hit**: ETF-to-market turnover ratio ≥ ``percentile_threshold``
     (rolling window percentile, default 95.0 over 252 trading days).
  2. **Concentration hit**: top-5 ETF turnover share ≥ ``concentration_threshold``
     (default 50.0 % — flows are highly concentrated in a few popular ETFs).

Data coverage
-------------
- ``fund_daily`` ETDF data: 2020-02-10 to present, 2,074 codes.
  **Broad coverage starts 2023-07-31** (1,200+ ETFs); pre-2023-07 only
  1 ETF with sparse data.  Use ``broad_coverage_only=True`` (default) to
  exclude the pre-2023-07 thin-coverage period.
- ``stk_factor_pro`` market turnover: 2010-01-04 to present.

Primary ETF codes tracked (for concentration analysis)
-------------------------------------------------------
510050.SH  上证50ETF          510300.SH  沪深300ETF
510500.SH  中证500ETF         510310.SH  沪深300ETF易方达
159919.SZ  沪深300ETF         159915.SZ  创业板ETF
510880.SH  红利ETF            512100.SH  中证1000ETF
512880.SH  证券ETF            588000.SH  科创50ETF
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb
import pandas as pd

from .base import format_date, get_connection, write_json
from .metadata import DEFAULT_DUCKDB_PATH

# ── Known major ETF codes (for concentration analysis) ─────────────────────
MAJOR_ETF_CODES: tuple[str, ...] = (
    "510050.SH",  # 上证50ETF
    "510300.SH",  # 沪深300ETF
    "510500.SH",  # 中证500ETF
    "510310.SH",  # 沪深300ETF易方达
    "159919.SZ",  # 沪深300ETF
    "159915.SZ",  # 创业板ETF
    "510880.SH",  # 红利ETF
    "512100.SH",  # 中证1000ETF
    "512880.SH",  # 证券ETF
    "588000.SH",  # 科创50ETF
)

# ── Coverage cut point ────────────────────────────────────────────────────
BROAD_COVERAGE_START_VARCHAR: str = "20230731"
BROAD_COVERAGE_START_DATE: str = "2023-07-31"
"""Date from which fund_daily has 1,200+ ETFs (reliable aggregation)."""


# ── Private helpers ───────────────────────────────────────────────────────


def _to_date_format(date_str: str | None) -> str | None:
    """Convert YYYYMMDD to YYYY-MM-DD for DATE columns."""
    if date_str is None:
        return None
    if len(date_str) == 10 and date_str[4] == "-":
        return date_str  # already YYYY-MM-DD
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def _build_where_clause(
    start_date: str | None,
    end_date: str | None,
    date_col: str = "trade_date",
) -> str:
    """Build SQL WHERE clause from optional date bounds.

    Parameters
    ----------
    start_date, end_date : str or None
        Date strings (``YYYYMMDD`` for VARCHAR columns, ``YYYY-MM-DD`` for DATE).
    date_col : str
        Column expression to use in comparisons (default ``trade_date``).
        Use ``strptime(trade_date, '%Y%m%d')`` when the source column is VARCHAR(YYYYMMDD).
        but comparison values are YYYYMMDD strings.
    """
    clauses: list[str] = []
    if start_date is not None:
        clauses.append(f"{date_col} >= '{start_date}'")
    if end_date is not None:
        clauses.append(f"{date_col} <= '{end_date}'")
    return f"AND {' AND '.join(clauses)}" if clauses else ""


# ── Data loading ─────────────────────────────────────────────────────────


def load_etf_turnover_ratio(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    broad_coverage_only: bool = True,
) -> pd.DataFrame:
    """Load daily ETF-to-market turnover ratio from local DuckDB.

    Computes two aggregations per trading day:

    * ``etf_amount`` — SUM(fund_daily.amount) for all ETF codes
    * ``market_amount`` — SUM(stk_factor_pro.amount) for all A-share stocks
      (``.SH`` / ``.SZ`` / ``.BJ``) where ``amount > 0``

    Both ``amount`` columns are in **千元** (thousands CNY), so the ratio
    is dimensionless and no unit conversion is needed.

    Parameters
    ----------
    con_or_path : DuckDB connection or path string
    start_date : str or None
        Inclusive start date (``YYYYMMDD`` string).
    end_date : str or None
        Inclusive end date (``YYYYMMDD`` string).
    broad_coverage_only : bool
        If ``True`` (default), restrict to dates >= ``20230731`` where
        ``fund_daily`` has 1,200+ ETFs.  Pre‑2023-07 data only has 1 ETF.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``etf_amount_kyuan``, ``market_amount_kyuan``,
        ``etf_code_count``, ``etf_turnover_ratio``.
    """
    effective_start_varchar = (
        f"{BROAD_COVERAGE_START_VARCHAR}" if broad_coverage_only and start_date is None
        else start_date
    )
    if broad_coverage_only and start_date is not None:
        effective_start_varchar = max(start_date, BROAD_COVERAGE_START_VARCHAR)
    effective_start_date = (
        f"{BROAD_COVERAGE_START_DATE}" if broad_coverage_only and start_date is None
        else _to_date_format(start_date) if start_date is not None
        else None
    )
    if broad_coverage_only and start_date is not None and effective_start_date is not None:
        effective_start_date = max(effective_start_date, BROAD_COVERAGE_START_DATE)
    effective_end_date = _to_date_format(end_date) if end_date is not None else None

    where_etf = _build_where_clause(effective_start_varchar, end_date)
    where_mkt = _build_where_clause(effective_start_date, effective_end_date)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = f"""
        WITH etf_agg AS (
            SELECT
                strptime(trade_date, '%Y%m%d') AS trade_date,
                SUM(amount)                  AS etf_amount_kyuan,
                COUNT(DISTINCT ts_code)      AS etf_code_count
            FROM fund_daily
            WHERE amount IS NOT NULL AND amount > 0
                  {where_etf}
            GROUP BY trade_date
        ),
        mkt_agg AS (
            SELECT
                trade_date,
                SUM(amount)                  AS market_amount_kyuan
            FROM stk_factor_pro
            WHERE amount IS NOT NULL
              AND amount > 0
              AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ' OR ts_code LIKE '%.BJ')
                  {where_mkt}
            GROUP BY trade_date
        )
        SELECT
            e.trade_date,
            e.etf_amount_kyuan,
            m.market_amount_kyuan,
            e.etf_code_count,
            e.etf_amount_kyuan / NULLIF(m.market_amount_kyuan, 0)
                AS etf_turnover_ratio
        FROM etf_agg e
        INNER JOIN mkt_agg m ON e.trade_date = m.trade_date
        ORDER BY e.trade_date
        """
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def load_etf_concentration(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    broad_coverage_only: bool = True,
    top_n: int = 5,
) -> pd.DataFrame:
    """Compute top-N ETF turnover concentration per trading day.

    For each trading day, computes the share of total ETF turnover
    accounted for by the top ``top_n`` ETFs (by amount).

    Parameters
    ----------
    con_or_path : DuckDB connection or path string
    start_date, end_date : str or None
    broad_coverage_only : bool
    top_n : int
        Number of top ETFs for concentration share (default 5).

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``etf_code_count``,
        ``top_n_amount_kyuan``, ``total_etf_amount_kyuan``,
        ``top_n_concentration`` (0–1).
    """
    effective_start = (
        f"{BROAD_COVERAGE_START_VARCHAR}" if broad_coverage_only and start_date is None
        else start_date
    )
    if broad_coverage_only and start_date is not None:
        effective_start = max(start_date, BROAD_COVERAGE_START_VARCHAR)

    where_clause = _build_where_clause(effective_start, end_date)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = f"""
        WITH daily_all AS (
            SELECT
                strptime(trade_date, '%Y%m%d') AS trade_date,
                COUNT(DISTINCT ts_code)              AS etf_code_count,
                SUM(amount)                          AS total_etf_amount_kyuan
            FROM fund_daily
            WHERE amount IS NOT NULL AND amount > 0
                  {where_clause}
            GROUP BY trade_date
        ),
        daily_top AS (
            SELECT
                strptime(trade_date, '%Y%m%d') AS trade_date,
                SUM(amount)                          AS top_n_amount_kyuan
            FROM (
                SELECT
                    trade_date,
                    ts_code,
                    amount,
                    ROW_NUMBER() OVER (
                        PARTITION BY trade_date ORDER BY amount DESC
                    ) AS rn
                FROM fund_daily
                WHERE amount IS NOT NULL AND amount > 0
                      {where_clause}
            ) ranked
            WHERE rn <= {top_n}
            GROUP BY trade_date
        )
        SELECT
            a.trade_date,
            a.etf_code_count,
            t.top_n_amount_kyuan,
            a.total_etf_amount_kyuan,
            t.top_n_amount_kyuan / NULLIF(a.total_etf_amount_kyuan, 0)
                AS top_n_concentration
        FROM daily_all a
        INNER JOIN daily_top t ON a.trade_date = t.trade_date
        ORDER BY a.trade_date
        """
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    df["trade_date"] = pd.to_datetime(
        df["trade_date"], format="%Y%m%d"
    )
    return df


# ── Signal computation ────────────────────────────────────────────────────


def compute_etf_heat_signal(
    df_ratio: pd.DataFrame,
    df_conc: pd.DataFrame,
    *,
    percentile_threshold: float = 95.0,
    rolling_window: int = 252,
    concentration_threshold: float = 0.50,
) -> pd.DataFrame:
    """Generate ETF-inflow-heat signals from ratio and concentration series.

    Two independent hit conditions are evaluated and combined:

    1. **Percentile hit**: ``rolling_pct >= percentile_threshold``
       where ``rolling_pct`` is the percentile rank of the current day's
       ``etf_turnover_ratio`` within the trailing rolling window.

    2. **Concentration hit**: ``top_n_concentration >= concentration_threshold``
       — flows are heavily concentrated in a few popular ETFs (default 50%).

    3. **Composite signal**: both percentile AND concentration hits fire
       simultaneously.

    Parameters
    ----------
    df_ratio : pd.DataFrame
        Must have ``trade_date`` and ``etf_turnover_ratio`` columns
        (output of ``load_etf_turnover_ratio``).
    df_conc : pd.DataFrame
        Must have ``trade_date`` and ``top_n_concentration`` columns
        (output of ``load_etf_concentration``).
    percentile_threshold : float
        Rolling percentile threshold in [0, 100].  Default 95.0.
    rolling_window : int
        Lookback window in trading days for rolling percentile.  Default 252.
    concentration_threshold : float
        Top-5 concentration threshold (0–1).  Default 0.50.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``etf_turnover_ratio``, ``roll_pct``,
        ``top_n_concentration``, ``pct_hit``, ``conc_hit``, ``signal``.
    """
    for col in ("trade_date", "etf_turnover_ratio"):
        if col not in df_ratio.columns:
            raise ValueError(f"df_ratio must have '{col}' column")
    for col in ("trade_date", "top_n_concentration"):
        if col not in df_conc.columns:
            raise ValueError(f"df_conc must have '{col}' column")

    # Merge on trade_date
    merged = (
        df_ratio[["trade_date", "etf_turnover_ratio"]]
        .merge(
            df_conc[["trade_date", "top_n_concentration"]],
            on="trade_date",
            how="inner",
        )
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    # Rolling percentile rank (0–100) of ETF turnover ratio
    merged["roll_pct"] = (
        merged["etf_turnover_ratio"]
        .rolling(rolling_window, min_periods=min(rolling_window, 60))
        .apply(
            lambda x: (x.rank(pct=True).iloc[-1] * 100.0),
            raw=False,
        )
    )

    # Percentile hit
    merged["pct_hit"] = merged["roll_pct"] >= percentile_threshold

    # Concentration hit
    merged["conc_hit"] = merged["top_n_concentration"] >= concentration_threshold

    # Composite signal: both conditions fire
    merged["signal"] = (
        merged["pct_hit"].fillna(False) & merged["conc_hit"].fillna(False)
    )

    return merged


# ── Summary builder (pure, testable) ──────────────────────────────────────


def _build_etf_heat_summary(
    df_signal: pd.DataFrame,
    *,
    percentile_threshold: float,
    rolling_window: int,
    concentration_threshold: float,
    broad_coverage_only: bool,
) -> dict[str, Any]:
    """Build structured summary from the signal DataFrame.

    Pure function — no I/O, no DuckDB.  Testable offline.
    """
    latest = df_signal.iloc[-1]
    latest_trade_date: pd.Timestamp = pd.Timestamp(latest["trade_date"])

    ratio_vals = df_signal["etf_turnover_ratio"].dropna()
    latest_ratio = float(latest["etf_turnover_ratio"])

    # Global percentile of latest ratio
    global_pct = float((ratio_vals <= latest_ratio).mean() * 100)

    # Historical max
    max_idx = int(df_signal["etf_turnover_ratio"].idxmax())
    max_ratio = float(df_signal.loc[max_idx, "etf_turnover_ratio"])
    max_date = format_date(pd.Timestamp(df_signal.loc[max_idx, "trade_date"]))

    # Signal stats
    n_signal = int(df_signal["signal"].sum())
    n_days = len(df_signal)
    n_pct_hit = int(df_signal["pct_hit"].fillna(False).sum())
    n_conc_hit = int(df_signal["conc_hit"].fillna(False).sum())

    latest_roll_pct = (
        float(latest["roll_pct"]) if pd.notna(latest["roll_pct"]) else None
    )
    latest_conc = (
        float(latest["top_n_concentration"])
        if pd.notna(latest["top_n_concentration"])
        else None
    )

    # Daily series
    daily_series: list[dict[str, Any]] = []
    for _, row in df_signal.iterrows():
        row_date = pd.Timestamp(row["trade_date"])
        entry: dict[str, Any] = {
            "trade_date": format_date(row_date),
            "etf_turnover_ratio": float(row["etf_turnover_ratio"]),
            "etf_turnover_ratio_pct": float(row["etf_turnover_ratio"] * 100),
        }
        if "roll_pct" in row and pd.notna(row["roll_pct"]):
            entry["roll_pct"] = float(row["roll_pct"])
        if "top_n_concentration" in row and pd.notna(row["top_n_concentration"]):
            entry["top_n_concentration"] = float(row["top_n_concentration"])
            entry["top_n_concentration_pct"] = float(row["top_n_concentration"] * 100)
        if "pct_hit" in row:
            entry["pct_hit"] = bool(row["pct_hit"])
        if "conc_hit" in row:
            entry["conc_hit"] = bool(row["conc_hit"])
        if "signal" in row:
            entry["signal"] = bool(row["signal"])
        daily_series.append(entry)

    # Unit metadata
    unit_metadata = {
        "etf_amount_unit": "千元 (thousands CNY)",
        "market_amount_unit": "千元 (thousands CNY)",
        "ratio_unit": "dimensionless",
        "ratio_formula": "SUM(fund_daily.amount) / SUM(stk_factor_pro.amount)",
        "concentration_note": "top-5 ETF amount share of total ETF amount",
    }

    # Data coverage note
    if broad_coverage_only:
        coverage_note = (
            "Broad ETF coverage only (>= 2023-07-31, ~1200+ ETFs). "
            "Pre-2023-07 data excluded (only 1 ETF with sparse data)."
        )
    else:
        coverage_note = (
            "Full date range.  NOTE: pre-2023-07-31 only has 1 ETF; "
            "aggregation meaningful only from 2023-07-31 onward."
        )

    return {
        "condition_id": "etf_inflow_heat",
        "latest_trade_date": format_date(latest_trade_date),
        "latest_etf_turnover_ratio": latest_ratio,
        "latest_etf_turnover_ratio_pct": latest_ratio * 100,
        "latest_roll_pct": latest_roll_pct,
        "latest_top_n_concentration": latest_conc,
        "latest_top_n_concentration_pct": (latest_conc * 100) if latest_conc is not None else None,
        "global_percentile_of_latest": global_pct,
        "historical_max_ratio": max_ratio,
        "historical_max_ratio_pct": max_ratio * 100,
        "historical_max_date": max_date,
        "n_signal_days": n_signal,
        "n_total_days": n_days,
        "signal_pct": float(n_signal / n_days * 100) if n_days > 0 else 0.0,
        "n_pct_hit_days": n_pct_hit,
        "n_conc_hit_days": n_conc_hit,
        "percentile_threshold": percentile_threshold,
        "rolling_window": rolling_window,
        "concentration_threshold": concentration_threshold,
        "broad_coverage_only": broad_coverage_only,
        "coverage_note": coverage_note,
        "major_etf_codes": list(MAJOR_ETF_CODES),
        "unit_metadata": unit_metadata,
        "daily_series": daily_series,
    }


# ── Public API ─────────────────────────────────────────────────────────────


def compute_etf_inflow_heat(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    percentile_threshold: float = 95.0,
    rolling_window: int = 252,
    concentration_threshold: float = 0.50,
    broad_coverage_only: bool = True,
) -> dict[str, Any]:
    """Compute the ETF inflow heat signal.

    This is the primary entry point.  It loads daily ETF-to-market turnover
    ratio and top-5 concentration from local DuckDB, applies rolling-percentile
    and concentration thresholds, and returns a structured summary.

    Parameters
    ----------
    con_or_path : DuckDB connection or path string
    start_date, end_date : str or None
        Date bounds in ``YYYYMMDD`` format.
    percentile_threshold : float
        Rolling percentile threshold (0–100).  Default 95.0.
    rolling_window : int
        Rolling window in trading days.  Default 252.
    concentration_threshold : float
        Top-5 concentration threshold (0–1).  Default 0.50.
    broad_coverage_only : bool
        If ``True`` (default), restrict to >= 2023-07-31 where fund_daily
        has 1,200+ ETFs.  Pre‑2023-07 data only has 1 ETF.

    Returns
    -------
    dict
        Structured summary.  Key fields: ``condition_id``,
        ``latest_etf_turnover_ratio``, ``latest_roll_pct``,
        ``latest_top_n_concentration``, ``n_signal_days``, ``signal_pct``,
        ``daily_series``, ``unit_metadata``, ``major_etf_codes``.
    """
    df_ratio = load_etf_turnover_ratio(
        con_or_path,
        start_date=start_date,
        end_date=end_date,
        broad_coverage_only=broad_coverage_only,
    )
    if df_ratio.empty:
        raise ValueError("No ETF turnover ratio data returned.")

    df_conc = load_etf_concentration(
        con_or_path,
        start_date=start_date,
        end_date=end_date,
        broad_coverage_only=broad_coverage_only,
    )
    if df_conc.empty:
        raise ValueError("No ETF concentration data returned.")

    df_signal = compute_etf_heat_signal(
        df_ratio,
        df_conc,
        percentile_threshold=percentile_threshold,
        rolling_window=rolling_window,
        concentration_threshold=concentration_threshold,
    )

    return _build_etf_heat_summary(
        df_signal,
        percentile_threshold=percentile_threshold,
        rolling_window=rolling_window,
        concentration_threshold=concentration_threshold,
        broad_coverage_only=broad_coverage_only,
    )


# ── Signal series (for validation / grid search) ───────────────────────────


def load_etf_signal_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    percentile_threshold: float = 95.0,
    rolling_window: int = 252,
    concentration_threshold: float = 0.50,
    broad_coverage_only: bool = True,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Return daily binary signal for validation pipelines.

    Parameters
    ----------
    duckdb_path : str
    percentile_threshold : float
    rolling_window : int
    concentration_threshold : float
    broad_coverage_only : bool
    start_date, end_date : str or None

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``etf_turnover_ratio``, ``roll_pct``,
        ``top_n_concentration``, ``pct_hit``, ``conc_hit``, ``signal``.
    """
    with get_connection(duckdb_path, read_only=True) as con:
        df_ratio = load_etf_turnover_ratio(
            con,
            start_date=start_date,
            end_date=end_date,
            broad_coverage_only=broad_coverage_only,
        )
        df_conc = load_etf_concentration(
            con,
            start_date=start_date,
            end_date=end_date,
            broad_coverage_only=broad_coverage_only,
        )

    df = compute_etf_heat_signal(
        df_ratio,
        df_conc,
        percentile_threshold=percentile_threshold,
        rolling_window=rolling_window,
        concentration_threshold=concentration_threshold,
    )

    return (
        df[
            [
                "trade_date",
                "etf_turnover_ratio",
                "roll_pct",
                "top_n_concentration",
                "pct_hit",
                "conc_hit",
                "signal",
            ]
        ]
        .sort_values("trade_date")
        .reset_index(drop=True)
    )