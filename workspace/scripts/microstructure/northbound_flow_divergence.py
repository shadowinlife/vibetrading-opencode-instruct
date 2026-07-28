"""
Northbound flow divergence condition (candidate #10).

Pulls daily northbound (北向) capital flow data from Tushare's ``moneyflow_hsgt``
endpoint and computes divergence signals: whether weakening or negative
northbound flows while the SSE Composite index remains elevated predicts
forward drawdowns.

Data source
-----------
* Tushare ``moneyflow_hsgt`` — 2017-01 to present
  Fields: ``trade_date``, ``north_money`` (北向资金净买额, 亿元),
  ``south_money``, ``ggt_ss``, ``ggt_sz``, ``hgt``, ``sgt``.
  **Unit**: ``north_money`` is in 亿元 (100 million CNY).
  **Important**: ``north_money`` is cumulative (累计值). Daily flow is
  computed as ``north_money.diff()``.
  Requires ``TUSHARE_TOKEN`` environment variable.

Signal variants
---------------
1. **north_flow_outflow**: Northbound 5d rolling net flow Z‑score < −1.5
   — anomalous net selling while markets stay elevated.
2. **north_flow_weakening**: 5d rolling sum of northbound flow is declining
   (below its 20‑day MA) AND SSE close is near historical high.
3. **cumulative_divergence**: 20d cumulative northbound trend is decreasing
   while SSE 20d return is positive — market rising but foreign money
   retreating.
4. **signal**: Union of all three variants — fires when ANY divergence signal
   is active.

SSE data
--------
SSE Composite (000001.SH) close prices are loaded from local DuckDB
``idx_factor_pro`` table. This is the only DuckDB dependency — the
northbound flow data comes directly from Tushare.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .base import format_date, get_connection, rolling_zscore, write_json
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ── Constants ────────────────────────────────────────────────────────────────

TUSHARE_ENDPOINT = "moneyflow_hsgt"
"""Tushare API endpoint for northbound/southbound flow."""

NORTH_MONEY_UNIT = "亿元"
"""north_money is in 亿元 (100 million CNY, 亿 CNY)."""

DEFAULT_START_DATE = "20170101"
"""Default start date for northbound data pull."""

DEFAULT_NORTH_LOOKBACK = 252
"""Rolling window for northbound Z-score computation."""

DEFAULT_FLOW_WINDOW = 5
"""Window for rolling northbound flow sum."""

DEFAULT_Z_THRESHOLD = -1.5
"""Z-score threshold for outflow anomaly signal."""

DEFAULT_CUMULATIVE_WINDOW = 20
"""Window for cumulative northbound trend computation."""

DEFAULT_SSE_HIGH_PCT = 90.0
"""SSE close percentile threshold for 'near historical high'."""

DEFAULT_MA_WINDOW = 20
"""Moving average window for flow weakening detection."""

TUSHARE_SLEEP = 0.35
"""Seconds to sleep between Tushare API calls to respect rate limits."""

MAX_RETRIES = 3
"""Maximum number of retries for Tushare API calls."""

SIGNAL_VARIANTS = (
    "north_flow_outflow",
    "north_flow_weakening",
    "cumulative_divergence",
)
"""All signal variant names."""


# ── Public types ─────────────────────────────────────────────────────────────


NorthboundSummary = dict[str, Any]


# ── Tushare data fetching ────────────────────────────────────────────────────


def _load_token() -> str:
    """Load Tushare token from environment variable or .env file.

    Reads TUSHARE_TOKEN from os.environ. If not set, attempts to load
    from .env file in the current working directory via python-dotenv.
    """
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        return token

    try:
        from dotenv import load_dotenv

        load_dotenv()
        token = os.environ.get("TUSHARE_TOKEN")
        if token:
            return token
    except ImportError:
        pass

    raise RuntimeError(
        "TUSHARE_TOKEN not found in environment or .env file. "
        "Set TUSHARE_TOKEN env var or create a .env file."
    )


def _init_tushare():
    """Initialise and return a Tushare Pro API client."""
    token = _load_token()
    import tushare as ts

    ts.set_token(token)
    return ts.pro_api()


def fetch_northbound_data(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    sleep: float = TUSHARE_SLEEP,
    max_retries: int = MAX_RETRIES,
) -> pd.DataFrame:
    """Fetch northbound flow data from Tushare moneyflow_hsgt.

    Pulls data in chunks by date range to handle rate limits. Returns a
    DataFrame sorted by trade_date.

    Parameters
    ----------
    start_date : str or None
        Start date in YYYYMMDD format. Default is 20170101.
    end_date : str or None
        End date in YYYYMMDD format. Default is today.
    sleep : float
        Seconds to sleep between retries.
    max_retries : int
        Maximum retry attempts for each API call.

    Returns
    -------
    pd.DataFrame
        Columns: trade_date, north_money, south_money, hgt, sgt, ggt_ss, ggt_sz.
    """
    if start_date is None:
        start_date = DEFAULT_START_DATE
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    pro = _init_tushare()

    # Pull in annual chunks to avoid large response issues
    results: list[pd.DataFrame] = []
    chunk_start = int(start_date)
    chunk_end_year = int(str(end_date)[:4])

    for year in range(int(start_date[:4]), chunk_end_year + 1):
        y_start = str(year) + "0101"
        y_end = str(year) + "1231"

        # Clamp to requested date range
        actual_start = max(y_start, start_date)
        actual_end = min(y_end, end_date)

        if actual_start > actual_end:
            continue

        for attempt in range(max_retries):
            try:
                df = pro.query(
                    TUSHARE_ENDPOINT,
                    start_date=actual_start,
                    end_date=actual_end,
                )
                if df is not None and not df.empty:
                    results.append(df)
                break
            except Exception as exc:
                if attempt < max_retries - 1:
                    time.sleep(sleep * (attempt + 1))
                else:
                    print(
                        f"[warn] Failed to fetch {actual_start}-{actual_end} "
                        f"after {max_retries} attempts: {exc}"
                    )
        time.sleep(sleep)

    if not results:
        raise RuntimeError(
            f"No data returned from {TUSHARE_ENDPOINT} for "
            f"{start_date} to {end_date}"
        )

    df = pd.concat(results, ignore_index=True).drop_duplicates(
        subset=["trade_date"]
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")

    # Convert numeric fields from string to float
    numeric_cols = [
        "north_money",
        "south_money",
        "hgt",
        "sgt",
        "ggt_ss",
        "ggt_sz",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("trade_date").reset_index(drop=True)

    return df


# ── SSE data loading (DuckDB) ────────────────────────────────────────────────


def load_sse_data(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Load SSE Composite (000001.SH) close prices from DuckDB.

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB file.
    start_date, end_date : str or None
        Optional date filters in YYYY-MM-DD format.

    Returns
    -------
    pd.DataFrame
        Columns: trade_date, close.
    """
    con = get_connection(duckdb_path, read_only=True)
    try:
        where_parts = [f"ts_code = '{SSE_INDEX_CODE}'"]
        if start_date is not None:
            where_parts.append(f"trade_date >= '{start_date}'")
        if end_date is not None:
            where_parts.append(f"trade_date <= '{end_date}'")

        query = f"""
        SELECT trade_date, close
        FROM idx_factor_pro
        WHERE {' AND '.join(where_parts)}
        ORDER BY trade_date
        """
        df = con.execute(query).fetchdf()
    finally:
        con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


# ── Signal computation (pure functions) ──────────────────────────────────────


def _sse_near_high_mask(
    df_sse: pd.DataFrame,
    high_percentile: float = DEFAULT_SSE_HIGH_PCT,
) -> np.ndarray:
    """Compute boolean mask where SSE close is near historical high.

    Uses expanding-window percentile: for each day *t*, computes whether
    ``close[t] >= percentile(close[0..t], high_percentile)``.

    Parameters
    ----------
    df_sse : pd.DataFrame
        Must have trade_date and close columns, sorted chronologically.
    high_percentile : float
        Percentile threshold (0-100). Default 90.0.

    Returns
    -------
    np.ndarray
        Boolean array, same length as df_sse.
    """
    closes = df_sse["close"].values.astype(float)
    n = len(closes)

    # Input validation: short series fallback to naive mask
    if n < 1:
        return np.zeros(n, dtype=bool)

    mask = np.full(n, False)
    # min_periods: need at least 60 days for a meaningful percentile
    min_periods = 60
    current = closes.copy()

    for i in range(n):
        if i + 1 < min_periods:
            continue
        window = current[: i + 1]
        threshold = np.percentile(window, high_percentile)
        if closes[i] >= threshold:
            mask[i] = True

    return mask


def _compute_rolling_trend(
    series: np.ndarray,
    window: int,
) -> np.ndarray:
    """Compute rolling linear trend (slope) over *window* periods.

    Uses simple linear regression: slope = (Σ((x−x̄)(y−ȳ)))/(Σ(x−x̄)²).

    Parameters
    ----------
    series : np.ndarray
        1-D numeric array.
    window : int
        Rolling window size.

    Returns
    -------
    np.ndarray
        Slope array, same length. Leading (window-1) entries are NaN.
    """
    n = len(series)
    slopes = np.full(n, np.nan)
    if n < window:
        return slopes

    x = np.arange(window, dtype=float)
    x_mean = x.mean()
    x_demean = x - x_mean
    denom = np.sum(x_demean**2)

    if denom == 0:
        return slopes

    for i in range(window - 1, n):
        y = series[i - window + 1 : i + 1]
        y = y.astype(float)
        if np.any(np.isnan(y)):
            continue
        y_mean = y.mean()
        y_demean = y - y_mean
        numer = np.sum(x_demean * y_demean)
        slopes[i] = numer / denom

    return slopes


def compute_signals(
    df_north: pd.DataFrame,
    df_sse: pd.DataFrame,
    *,
    north_lookback: int = DEFAULT_NORTH_LOOKBACK,
    flow_window: int = DEFAULT_FLOW_WINDOW,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    cumulative_window: int = DEFAULT_CUMULATIVE_WINDOW,
    sse_high_pct: float = DEFAULT_SSE_HIGH_PCT,
    ma_window: int = DEFAULT_MA_WINDOW,
) -> pd.DataFrame:
    """Compute northbound flow divergence signals.

    Parameters
    ----------
    df_north : pd.DataFrame
        Northbound flow data from fetch_northbound_data(). Must have
        trade_date and north_money columns.
    df_sse : pd.DataFrame
        SSE close data from load_sse_data(). Must have trade_date and close.
    north_lookback : int
        Rolling window for Z-score computation (def. 252).
    flow_window : int
        Window for rolling northbound flow sum (def. 5).
    z_threshold : float
        Z-score threshold below which outflow anomaly signal fires (def. -1.5).
    cumulative_window : int
        Window for cumulative northbound trend (def. 20).
    sse_high_pct : float
        SSE percentile threshold for 'near high' (def. 90.0).
    ma_window : int
        MA window for flow weakening signal (def. 20).

    Returns
    -------
    pd.DataFrame
        Columns: trade_date, north_money, north_5d_sum, north_flow_z,
        north_5d_ma, sse_close, sse_near_high, north_flow_outflow,
        north_flow_weakening, cumulative_divergence, signal.
    """
    # Merge northbound and SSE data
    df = df_north[["trade_date", "north_money"]].merge(
        df_sse[["trade_date", "close"]],
        on="trade_date",
        how="inner",
    ).sort_values("trade_date").reset_index(drop=True)

    if df.empty:
        raise ValueError("No overlapping dates between northbound and SSE data")

    # ── Convert cumulative north_money to daily flow ──
    df["daily_north"] = df["north_money"].diff()
    dag = df["daily_north"].values.astype(float)
    dag[0] = df["north_money"].iloc[0]  # first day: use first cumulative value as daily

    nm = df["north_money"].values.astype(float)
    nd = df["daily_north"].values.astype(float)

    # 5-day rolling sum of DAILY northbound flow
    df["north_5d_sum"] = (
        df["daily_north"].rolling(flow_window, min_periods=1).sum()
    )

    # Z-score of 5d rolling sum over north_lookback
    df["north_flow_z"] = rolling_zscore(
        df["north_5d_sum"], north_lookback
    ).values

    # 5d rolling sum MA (for weakening detection)
    df["north_5d_ma"] = (
        df["north_5d_sum"].rolling(ma_window, min_periods=ma_window).mean()
    )

    # ── SSE near-high mask ──
    sse_mask = _sse_near_high_mask(df, high_percentile=sse_high_pct)
    df["sse_close"] = df["close"]
    df["sse_near_high"] = sse_mask

    # ── Signal 1: north_flow_outflow ─────────────────────────────────────
    z_arr = df["north_flow_z"].values.astype(float)
    outflow = (z_arr < z_threshold) & sse_mask
    outflow[np.isnan(z_arr)] = False
    df["north_flow_outflow"] = outflow

    # ── Signal 2: north_flow_weakening ────────────────────────────────────
    n5d = df["north_5d_sum"].values.astype(float)
    n5d_ma = df["north_5d_ma"].values.astype(float)
    weakening = (n5d < n5d_ma) & sse_mask
    weakening[np.isnan(n5d_ma)] = False
    df["north_flow_weakening"] = weakening

    # ── Signal 3: cumulative_divergence ───────────────────────────────────
    # 20d cumulative daily northbound trend decreasing while SSE 20d return > 0.
    n_trend = _compute_rolling_trend(
        nd, cumulative_window  # use daily north, not cumulative
    )
    sse_20d_ret = np.full(len(df), np.nan)
    close_arr = df["close"].values.astype(float)
    for i in range(cumulative_window, len(df)):
        if close_arr[i - cumulative_window] > 0:
            sse_20d_ret[i] = (
                close_arr[i] / close_arr[i - cumulative_window] - 1.0
            )

    div_mask = (
        (n_trend < 0)
        & (sse_20d_ret > 0)
        & sse_mask
    )
    div_mask[np.isnan(n_trend)] = False
    div_mask[np.isnan(sse_20d_ret)] = False
    df["cumulative_divergence"] = div_mask

    # ── Composite signal: ANY variant fires ───────────────────────────────
    df["signal"] = (
        df["north_flow_outflow"]
        | df["north_flow_weakening"]
        | df["cumulative_divergence"]
    )

    # Drop helper columns not needed in output
    df = df.drop(columns=["close"], errors="ignore")

    return df


# ── Summary builder (pure function) ──────────────────────────────────────────


def _build_summary(
    df_signals: pd.DataFrame,
    *,
    north_lookback: int,
    flow_window: int,
    z_threshold: float,
    cumulative_window: int,
    sse_high_pct: float,
    ma_window: int,
) -> dict[str, Any]:
    """Build structured summary from computed signals DataFrame.

    Pure function — no I/O, no DuckDB, no Tushare.
    """
    df = df_signals.copy()
    latest: Any = df.iloc[-1]
    latest_date: pd.Timestamp = pd.Timestamp(latest["trade_date"])

    # Latest values
    latest_north_money = float(latest["daily_north"])
    latest_north_5d_sum = float(latest["north_5d_sum"])
    latest_north_flow_z = (
        float(latest["north_flow_z"])
        if pd.notna(latest["north_flow_z"])
        else None
    )

    # SSE stats
    sse_close_vals = df["sse_close"].dropna()
    latest_sse_close = float(latest["sse_close"])

    # Global percentile of latest SSE close
    sse_pct = float(
        (sse_close_vals <= latest_sse_close).mean() * 100
    )

    # Signal counts
    n_total = len(df)
    n_outflow = int(df["north_flow_outflow"].sum())
    n_weakening = int(df["north_flow_weakening"].sum())
    n_cumul_div = int(df["cumulative_divergence"].sum())
    n_signal = int(df["signal"].sum())

    # Historical extremes
    north_money_vals = df["daily_north"].dropna()
    max_inflow = float(north_money_vals.max())
    max_inflow_date = format_date(
        df.loc[df["daily_north"].idxmax(), "trade_date"]
    )
    min_inflow = float(north_money_vals.min())
    min_inflow_date = format_date(
        df.loc[df["daily_north"].idxmin(), "trade_date"]
    )

    # Latest signal status
    latest_signal_variants: dict[str, bool] = {}
    for variant in SIGNAL_VARIANTS:
        if variant in df.columns:
            latest_signal_variants[variant] = bool(latest[variant])

    # 10-day recent history for overnight checks
    recent_n = min(10, len(df))
    recent = df.iloc[-recent_n:]
    recent_days: list[dict[str, Any]] = []
    for _, row in recent.iterrows():
        entry: dict[str, Any] = {
            "trade_date": format_date(pd.Timestamp(row["trade_date"])),
            "daily_north": (
                float(row["daily_north"])
                if pd.notna(row["daily_north"]) else None
            ),
        }
        if pd.notna(row["north_flow_z"]):
            entry["north_flow_z"] = round(float(row["north_flow_z"]), 4)
        for variant in SIGNAL_VARIANTS:
            if variant in row:
                entry[variant] = bool(row[variant])
        entry["signal"] = bool(row["signal"])
        entry["sse_close"] = float(row["sse_close"])
        recent_days.append(entry)

    # Daily series for full audit trail
    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        entry = {
            "trade_date": format_date(pd.Timestamp(row["trade_date"])),
            "daily_north": (
                float(row["daily_north"])
                if pd.notna(row["daily_north"]) else None
            ),
        }
        if pd.notna(row["north_flow_z"]):
            entry["north_flow_z"] = round(float(row["north_flow_z"]), 4)
        entry["sse_close"] = float(row["sse_close"])
        entry["sse_near_high"] = bool(row["sse_near_high"])
        for variant in SIGNAL_VARIANTS:
            if variant in row:
                entry[variant] = bool(row[variant])
        entry["signal"] = bool(row["signal"])
        daily_series.append(entry)

    return {
        "latest_trade_date": format_date(latest_date),
        "latest_north_money": latest_north_money,
        "latest_north_money_unit": NORTH_MONEY_UNIT,
        "latest_north_5d_sum": round(latest_north_5d_sum, 2),
        "latest_north_flow_z": latest_north_flow_z,
        "latest_sse_close": latest_sse_close,
        "sse_percentile": round(sse_pct, 2),
        "n_signal_days": n_signal,
        "n_total_days": n_total,
        "signal_pct": round(n_signal / n_total * 100, 2) if n_total > 0 else 0.0,
        "n_outflow_signal_days": n_outflow,
        "n_weakening_signal_days": n_weakening,
        "n_cumul_divergence_days": n_cumul_div,
        "max_single_day_inflow": round(max_inflow, 2),
        "max_inflow_date": max_inflow_date,
        "min_single_day_inflow": round(min_inflow, 2),
        "min_inflow_date": min_inflow_date,
        "latest_signals": latest_signal_variants,
        "signal_firing": bool(latest["signal"]),
        "params": {
            "north_lookback": north_lookback,
            "flow_window": flow_window,
            "z_threshold": z_threshold,
            "cumulative_window": cumulative_window,
            "sse_high_pct": sse_high_pct,
            "ma_window": ma_window,
        },
        "recent_10d": recent_days,
        "daily_series": daily_series,
    }


# ── Main entry point ─────────────────────────────────────────────────────────


def compute_northbound_divergence(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    north_lookback: int = DEFAULT_NORTH_LOOKBACK,
    flow_window: int = DEFAULT_FLOW_WINDOW,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    cumulative_window: int = DEFAULT_CUMULATIVE_WINDOW,
    sse_high_pct: float = DEFAULT_SSE_HIGH_PCT,
    ma_window: int = DEFAULT_MA_WINDOW,
) -> NorthboundSummary:
    """Compute northbound flow divergence signals.

    This is the primary entry point. It fetches northbound flow data from
    Tushare, loads SSE close data from DuckDB, computes three signal variants
    (outflow anomaly, flow weakening, cumulative divergence), and returns a
    structured summary.

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB file for SSE data.
    start_date : str or None
        Start date in YYYYMMDD format. Default is 20170101.
    end_date : str or None
        End date in YYYYMMDD format. Default is today.
    north_lookback : int
        Rolling window for Z-score (def. 252).
    flow_window : int
        Rolling window for flow sum (def. 5).
    z_threshold : float
        Z-score threshold for outflow signal (def. -1.5).
    cumulative_window : int
        Window for cumulative trend (def. 20).
    sse_high_pct : float
        SSE percentile for 'near high' (def. 90.0).
    ma_window : int
        MA window for flow weakening (def. 20).

    Returns
    -------
    dict
        Structured summary with keys: latest_trade_date, latest_north_money,
        latest_north_money_unit, latest_north_flow_z, latest_sse_close,
        sse_percentile, n_signal_days, n_total_days, signal_pct,
        n_outflow_signal_days, n_weakening_signal_days,
        n_cumul_divergence_days, max_single_day_inflow, max_inflow_date,
        min_single_day_inflow, min_inflow_date, latest_signals,
        signal_firing, params, recent_10d, daily_series.
    """
    # Fetch northbound flow from Tushare
    df_north = fetch_northbound_data(
        start_date=start_date,
        end_date=end_date,
    )

    # Load SSE close from DuckDB
    # Convert YYYYMMDD to YYYY-MM-DD for DuckDB query
    first_date = df_north["trade_date"].min()
    last_date = df_north["trade_date"].max()
    sse_start = first_date.strftime("%Y-%m-%d") if hasattr(first_date, "strftime") else str(first_date)[:10]
    sse_end = last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)[:10]

    df_sse = load_sse_data(
        duckdb_path,
        start_date=sse_start,
        end_date=sse_end,
    )

    if df_sse.empty:
        raise ValueError(
            f"No SSE data in DuckDB for date range {sse_start} to {sse_end}"
        )

    # Compute signals
    df_signals = compute_signals(
        df_north,
        df_sse,
        north_lookback=north_lookback,
        flow_window=flow_window,
        z_threshold=z_threshold,
        cumulative_window=cumulative_window,
        sse_high_pct=sse_high_pct,
        ma_window=ma_window,
    )

    summary = _build_summary(
        df_signals,
        north_lookback=north_lookback,
        flow_window=flow_window,
        z_threshold=z_threshold,
        cumulative_window=cumulative_window,
        sse_high_pct=sse_high_pct,
        ma_window=ma_window,
    )

    # Attach source metadata
    summary["source_metadata"] = {
        "northbound_source": "tushare:moneyflow_hsgt",
        "north_money_unit": NORTH_MONEY_UNIT,
        "sse_source": "duckdb:idx_factor_pro",
        "sse_index_code": SSE_INDEX_CODE,
        "data_fetch_time": datetime.now().isoformat(),
    }

    return summary


# ── Signal series (for validation / grid search) ─────────────────────────────


def compute_signal_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    north_lookback: int = DEFAULT_NORTH_LOOKBACK,
    flow_window: int = DEFAULT_FLOW_WINDOW,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    cumulative_window: int = DEFAULT_CUMULATIVE_WINDOW,
    sse_high_pct: float = DEFAULT_SSE_HIGH_PCT,
    ma_window: int = DEFAULT_MA_WINDOW,
    df_north_inject: pd.DataFrame | None = None,
    df_sse_inject: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return a minimal signal-annotated DataFrame for validation pipelines.

    Supports data injection for offline testing:
    - df_north_inject → bypasses Tushare fetch
    - df_sse_inject → bypasses DuckDB load

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB file. Ignored if df_sse_inject is provided.
    start_date, end_date : str or None
        Date range for the data pull. Ignored if injection provided.
    north_lookback, flow_window, etc. : int/float
        Signal parameters passed to compute_signals().
    df_north_inject : pd.DataFrame or None
        Synthetic northbound DataFrame for offline testing.
        Must have trade_date and north_money columns.
    df_sse_inject : pd.DataFrame or None
        Synthetic SSE DataFrame for offline testing.
        Must have trade_date and close columns.

    Returns
    -------
    pd.DataFrame
        Columns: trade_date, north_money, north_flow_z, sse_close,
        sse_near_high, north_flow_outflow, north_flow_weakening,
        cumulative_divergence, signal.
    """
    if df_north_inject is not None:
        df_north = df_north_inject.copy()
    else:
        df_north = fetch_northbound_data(
            start_date=start_date,
            end_date=end_date,
        )

    if df_sse_inject is not None:
        df_sse = df_sse_inject.copy()
    else:
        first_date = df_north["trade_date"].min()
        last_date = df_north["trade_date"].max()
        sse_start = (
            first_date.strftime("%Y-%m-%d")
            if hasattr(first_date, "strftime")
            else str(first_date)[:10]
        )
        sse_end = (
            last_date.strftime("%Y-%m-%d")
            if hasattr(last_date, "strftime")
            else str(last_date)[:10]
        )
        df_sse = load_sse_data(
            duckdb_path,
            start_date=sse_start,
            end_date=sse_end,
        )

    df_signals = compute_signals(
        df_north,
        df_sse,
        north_lookback=north_lookback,
        flow_window=flow_window,
        z_threshold=z_threshold,
        cumulative_window=cumulative_window,
        sse_high_pct=sse_high_pct,
        ma_window=ma_window,
    )

    # Return only the essential columns for validation
    essential = [
        "trade_date",
        "north_money",
        "north_flow_z",
        "sse_close",
        "sse_near_high",
    ] + list(SIGNAL_VARIANTS) + ["signal"]

    return df_signals[[c for c in essential if c in df_signals.columns]].copy()