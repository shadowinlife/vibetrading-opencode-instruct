"""
Liquidity tightening signal module — candidate #13.

Pulls Shibor (daily interbank rates) and LPR (loan prime rate) from Tushare,
then computes three signal variants that detect monetary policy tightening:

**Signal variants**:

A) **Short-term rate squeeze**: ON or 1W Shibor breaks above their rolling
   N-day percentile (default 80th), indicating liquidity stress in the
   interbank market.

B) **ON-1W spread inversion**: The overnight rate exceeds the 1-week rate,
   a classic short-end yield-curve stress signal.  Normally ON < 1W; when
   this inverts, it signals near-term funding pressure.

C) **LPR policy pivot**: The 1Y LPR (loan prime rate) reverses upward after
   a period of stability or decline.  Monthly-frequency data, forward-filled
   for daily signal computation.

**Data sources** (both Tushare FREE tier, T+0 same-day release ~11:00–11:30 CST):

- ``pro.shibor(date='YYYYMMDD')`` → daily: ``on, 1w, 2w, 1m, 3m, 6m, 9m, 1y``
- ``pro.shibor_lpr(date='YYYYMMDD')`` → monthly: ``1y, 5y``

**Effective date**: Shibor published 11:00 daily, LPR published 11:30 daily on
the 20th of each month.  Both are effectively same-day — no lag adjustment needed.

**Coverage**: 2018-04-18 to present (Shibor), 2018-01-02 to present (LPR).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .base import format_date, get_connection, pct_rank, write_json
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default rolling window (trading days) for Shibor percentile computation.
# ~500 trading days ≈ 2 years.
DEFAULT_SHIBOR_ROLLING_DAYS: int = 500

# Default percentile threshold for Shibor rate squeeze signal (0–100).
DEFAULT_SHIBOR_PERCENTILE: float = 80.0

# LPR lookback (months) for detecting a pivot — rate must have been stable
# or declining for at least this many months before turning up.
DEFAULT_LPR_STABILITY_MONTHS: int = 6

# Column names in Tushare shibor response.
SHIBOR_FIELDS: list[str] = [
    "date", "on", "1w", "2w", "1m", "3m", "6m", "9m", "1y",
]

# Column names in Tushare shibor_lpr response.
LPR_FIELDS: list[str] = ["date", "1y", "5y"]

# ── Public result type ──────────────────────────────────────────────────

LiquidityTighteningSummary = dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════
# Data fetching (Tushare)
# ═══════════════════════════════════════════════════════════════════════════


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


def _fetch_shibor(
    start_date: str = "20180101",
    end_date: str | None = None,
) -> pd.DataFrame | None:
    """Fetch daily Shibor rates from Tushare.

    Returns DataFrame with columns: date, on, 1w, 2w, 1m, 3m, 6m, 9m, 1y.
    Returns None if Tushare is unavailable or returns empty data.
    """
    token = _load_tushare_token()
    if token is None:
        return None

    try:
        import tushare as ts
        pro = ts.pro_api(token)

        if end_date is None:
            end_date = date.today().strftime("%Y%m%d")

        df: pd.DataFrame = pro.query(  # type: ignore[no-untyped-call]
            "shibor",
            start_date=start_date,
            end_date=end_date,
            limit=10000,
        )
    except Exception:
        return None

    if df is None or df.empty:
        return None

    required = {"date", "on", "1w"}
    missing = required - set(df.columns)
    if missing:
        return None

    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.rename(columns={"date": "trade_date"})
    return df


def _fetch_lpr(
    start_date: str = "20180101",
    end_date: str | None = None,
) -> pd.DataFrame | None:
    """Fetch monthly LPR from Tushare.

    Returns DataFrame with columns: trade_date, lpr_1y, lpr_5y.
    Returns None if Tushare is unavailable or returns empty data.
    """
    token = _load_tushare_token()
    if token is None:
        return None

    try:
        import tushare as ts
        pro = ts.pro_api(token)

        if end_date is None:
            end_date = date.today().strftime("%Y%m%d")

        df: pd.DataFrame = pro.query(  # type: ignore[no-untyped-call]
            "shibor_lpr",
            start_date=start_date,
            end_date=end_date,
            limit=5000,
        )
    except Exception:
        return None

    if df is None or df.empty:
        return None

    required = {"date", "1y"}
    missing = required - set(df.columns)
    if missing:
        return None

    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    df = df.rename(columns={"date": "trade_date", "1y": "lpr_1y", "5y": "lpr_5y"})
    return df


# ═══════════════════════════════════════════════════════════════════════════
# Signal computation (pure functions — testable offline)
# ═══════════════════════════════════════════════════════════════════════════


def _compute_rolling_percentile(
    series: pd.Series,
    window: int,
) -> pd.Series:
    """Compute rolling percentile rank (0–100) for each element.

    Uses strict ``<`` comparison → equal values get lower rank.
    """
    result = pd.Series(np.nan, index=series.index, dtype=float)
    values = series.values.astype(float)
    n = len(values)

    for i in range(n):
        if i < window:
            continue
        window_vals = values[i - window : i]
        # Count how many are strictly less than current value
        rank = np.sum(window_vals < values[i])
        result.iloc[i] = (rank / window) * 100.0

    return result


def compute_shibor_rate_squeeze_signal(
    df_shibor: pd.DataFrame,
    *,
    rolling_days: int = DEFAULT_SHIBOR_ROLLING_DAYS,
    percentile_threshold: float = DEFAULT_SHIBOR_PERCENTILE,
) -> pd.DataFrame:
    """Compute signal variant A: short-term rate squeeze.

    Signal fires when ON or 1W Shibor exceeds its rolling percentile threshold.

    Parameters
    ----------
    df_shibor : pd.DataFrame
        Must have columns: trade_date, on, 1w.
    rolling_days : int
        Rolling window for percentile computation.
    percentile_threshold : float
        Percentile threshold (0–100).  Signal when rate ≥ this percentile.

    Returns
    -------
    pd.DataFrame
        Same index with additional columns:
        ``on_pct``, ``w1_pct``, ``rate_squeeze``.
    """
    df = df_shibor.copy().sort_values("trade_date").reset_index(drop=True)

    df["on_pct"] = _compute_rolling_percentile(df["on"], rolling_days)
    df["w1_pct"] = _compute_rolling_percentile(df["1w"], rolling_days)

    df["rate_squeeze"] = (
        (df["on_pct"] >= percentile_threshold) |
        (df["w1_pct"] >= percentile_threshold)
    )

    return df


def compute_spread_inversion_signal(
    df_shibor: pd.DataFrame,
) -> pd.DataFrame:
    """Compute signal variant B: ON-1W spread inversion.

    Signal fires when the overnight rate exceeds the 1-week rate (ON > 1W).
    Normally ON < 1W in a healthy term-structure; inversion indicates
    short-end funding stress.

    Parameters
    ----------
    df_shibor : pd.DataFrame
        Must have columns: trade_date, on, 1w.

    Returns
    -------
    pd.DataFrame
        Same index with additional columns:
        ``on_1w_spread``, ``spread_inversion``.
    """
    df = df_shibor.copy().sort_values("trade_date").reset_index(drop=True)

    df["on_1w_spread"] = df["on"] - df["1w"]
    df["spread_inversion"] = df["on_1w_spread"] > 0

    return df


def compute_lpr_pivot_signal(
    df_lpr: pd.DataFrame,
    *,
    stability_months: int = DEFAULT_LPR_STABILITY_MONTHS,
) -> pd.DataFrame:
    """Compute signal variant C: LPR 1Y policy pivot.

    Signal fires when the 1Y LPR increases after being stable or declining
    for at least ``stability_months`` consecutive months.  This is a
    monthly-frequency signal converted to daily via forward-fill.

    Parameters
    ----------
    df_lpr : pd.DataFrame
        Must have columns: trade_date, lpr_1y.
    stability_months : int
        Minimum number of months the LPR must have been stable or declining
        before an increase is considered a pivot.

    Returns
    -------
    pd.DataFrame
        Same index with additional columns:
        ``lpr_1y_diff``, ``lpr_months_stable``, ``lpr_pivot``.
    """
    df = df_lpr.copy().sort_values("trade_date").reset_index(drop=True)

    # Compute month-over-month difference
    df["lpr_1y_diff"] = df["lpr_1y"].diff()

    # Count consecutive months of stability (≤ 0 change) before current row
    df["lpr_months_stable"] = 0
    stable_count = 0
    for i in range(len(df)):
        if i == 0:
            continue
        months_before_this_row = stable_count
        if pd.notna(df["lpr_1y_diff"].iloc[i]) and df["lpr_1y_diff"].iloc[i] <= 0:
            stable_count += 1
        elif pd.notna(df["lpr_1y_diff"].iloc[i]) and df["lpr_1y_diff"].iloc[i] > 0:
            stable_count = 0
        df.loc[df.index[i], "lpr_months_stable"] = months_before_this_row

    # Signal: rate increased AND was stable for ≥ stability_months
    df["lpr_pivot"] = (
        (df["lpr_1y_diff"] > 0) &
        (df["lpr_months_stable"] >= stability_months)
    )

    return df


def _forward_fill_lpr_to_daily(
    df_lpr: pd.DataFrame,
    df_shibor: pd.DataFrame,
) -> pd.DataFrame:
    """Forward-fill monthly LPR signals to daily frequency.

    Matches LPR data to the nearest Shibor trade date (forward-fill from
    the most recent LPR announcement).
    """
    lpr = df_lpr.copy().sort_values("trade_date")

    # Get all unique trade dates from Shibor
    dates = df_shibor["trade_date"].dropna().unique()
    dates = sorted(dates)

    lpr_dates = lpr["trade_date"].values

    rows = []
    for d in dates:
        d_ts = pd.Timestamp(d)
        # Find the most recent LPR date ≤ d
        mask = lpr_dates <= d_ts
        if not mask.any():
            continue
        latest_idx = np.where(mask)[0][-1]
        row = lpr.iloc[latest_idx].to_dict()
        row["trade_date"] = d_ts
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=["trade_date", "lpr_1y", "lpr_5y", "lpr_1y_diff",
                                       "lpr_months_stable", "lpr_pivot"])

    result = pd.DataFrame(rows)
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.sort_values("trade_date").reset_index(drop=True)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Main entry points
# ═══════════════════════════════════════════════════════════════════════════


def compute_liquidity_tightening(
    *,
    rolling_days: int = DEFAULT_SHIBOR_ROLLING_DAYS,
    percentile_threshold: float = DEFAULT_SHIBOR_PERCENTILE,
    stability_months: int = DEFAULT_LPR_STABILITY_MONTHS,
    df_shibor_inject: pd.DataFrame | None = None,
    df_lpr_inject: pd.DataFrame | None = None,
) -> LiquidityTighteningSummary:
    """Compute all three liquidity tightening signal variants.

    Parameters
    ----------
    rolling_days : int
        Rolling window for Shibor percentile computation.
    percentile_threshold : float
        Percentile threshold for rate squeeze signal.
    stability_months : int
        Months of stability required before LPR pivot signal.
    df_shibor_inject : pd.DataFrame | None
        Pre-fetched Shibor DataFrame (for offline testing).  Must have
        columns: trade_date, on, 1w, 2w, 1m, 3m, 6m, 9m, 1y.
    df_lpr_inject : pd.DataFrame | None
        Pre-fetched LPR DataFrame (for offline testing).  Must have
        columns: trade_date, lpr_1y, lpr_5y.

    Returns
    -------
    dict
        Structured summary with keys:
        ``latest_trade_date``, ``source_used``, ``n_days``,
        ``shibor_rate_squeeze``, ``spread_inversion``, ``lpr_pivot``,
        ``signal_any``, ``daily_series``.
    """
    # Fetch or use injected data
    if df_shibor_inject is not None:
        df_shibor = df_shibor_inject.copy()
        source_used = "injected"
    else:
        df_shibor = _fetch_shibor()
        source_used = "tushare"
        if df_shibor is None:
            return {
                "source_used": "unavailable",
                "error": "Tushare shibor API unavailable",
                "n_days": 0,
            }

    if df_lpr_inject is not None:
        df_lpr = df_lpr_inject.copy()
    else:
        df_lpr = _fetch_lpr()
        if df_lpr is None:
            return {
                "source_used": "unavailable",
                "error": "Tushare shibor_lpr API unavailable",
                "n_days": 0,
            }

    # Compute signal variants
    df_squeeze = compute_shibor_rate_squeeze_signal(
        df_shibor,
        rolling_days=rolling_days,
        percentile_threshold=percentile_threshold,
    )
    df_inversion = compute_spread_inversion_signal(df_shibor)
    df_lpr_pivot = compute_lpr_pivot_signal(
        df_lpr,
        stability_months=stability_months,
    )

    # Forward-fill LPR to daily
    df_lpr_daily = _forward_fill_lpr_to_daily(df_lpr_pivot, df_shibor)

    # Merge all signals
    merged = df_squeeze[["trade_date", "on", "1w", "on_pct", "w1_pct", "rate_squeeze"]].merge(
        df_inversion[["trade_date", "on_1w_spread", "spread_inversion"]],
        on="trade_date",
        how="left",
    )
    merged = merged.merge(
        df_lpr_daily[["trade_date", "lpr_1y", "lpr_pivot"]],
        on="trade_date",
        how="left",
    )

    merged["signal_any"] = (
        merged["rate_squeeze"].fillna(False).astype(bool) |
        merged["spread_inversion"].fillna(False).astype(bool) |
        merged["lpr_pivot"].fillna(False).astype(bool)
    )

    n_days = len(merged)
    latest_trade_date = (
        str(merged["trade_date"].max().date()) if n_days > 0 else "N/A"
    )

    # Latest values
    latest = merged.iloc[-1] if n_days > 0 else {}

    # Build daily series
    daily_series = []
    for _, row in merged.iterrows():
        daily_series.append({
            "trade_date": str(row["trade_date"].date()),
            "on": float(row["on"]) if pd.notna(row["on"]) else None,
            "1w": float(row["1w"]) if pd.notna(row["1w"]) else None,
            "on_pct": round(float(row["on_pct"]), 2) if pd.notna(row["on_pct"]) else None,
            "w1_pct": round(float(row["w1_pct"]), 2) if pd.notna(row["w1_pct"]) else None,
            "on_1w_spread": round(float(row["on_1w_spread"]), 4) if pd.notna(row["on_1w_spread"]) else None,
            "lpr_1y": float(row["lpr_1y"]) if pd.notna(row["lpr_1y"]) else None,
            "rate_squeeze": bool(row["rate_squeeze"]),
            "spread_inversion": bool(row["spread_inversion"]),
            "lpr_pivot": bool(row["lpr_pivot"]),
            "signal_any": bool(row["signal_any"]),
        })

    # Compute signal statistics
    n_rate_squeeze = int(merged["rate_squeeze"].sum())
    n_spread_inversion = int(merged["spread_inversion"].sum())
    n_lpr_pivot = int(merged["lpr_pivot"].sum())
    n_signal_any = int(merged["signal_any"].sum())

    return {
        # Metadata
        "latest_trade_date": latest_trade_date,
        "source_used": source_used,
        "n_days": n_days,

        # Latest observation
        "latest_on": float(latest.get("on", np.nan)),
        "latest_1w": float(latest.get("1w", np.nan)),
        "latest_on_pct": float(latest.get("on_pct", np.nan)),
        "latest_w1_pct": float(latest.get("w1_pct", np.nan)),
        "latest_on_1w_spread": float(latest.get("on_1w_spread", np.nan)),
        "latest_lpr_1y": float(latest.get("lpr_1y", np.nan)),

        # Signal counts
        "n_rate_squeeze": n_rate_squeeze,
        "n_spread_inversion": n_spread_inversion,
        "n_lpr_pivot": n_lpr_pivot,
        "n_signal_any": n_signal_any,
        "rate_squeeze_pct": round(n_rate_squeeze / n_days * 100, 2) if n_days > 0 else 0.0,
        "spread_inversion_pct": round(n_spread_inversion / n_days * 100, 2) if n_days > 0 else 0.0,
        "lpr_pivot_pct": round(n_lpr_pivot / n_days * 100, 2) if n_days > 0 else 0.0,
        "signal_any_pct": round(n_signal_any / n_days * 100, 2) if n_days > 0 else 0.0,

        # Parameters
        "rolling_days": rolling_days,
        "percentile_threshold": percentile_threshold,
        "stability_months": stability_months,

        # Daily series
        "daily_series": daily_series,
    }


def compute_signal_series(
    *,
    rolling_days: int = DEFAULT_SHIBOR_ROLLING_DAYS,
    percentile_threshold: float = DEFAULT_SHIBOR_PERCENTILE,
    stability_months: int = DEFAULT_LPR_STABILITY_MONTHS,
    signal_variant: str = "signal_any",
    df_shibor_inject: pd.DataFrame | None = None,
    df_lpr_inject: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute a boolean signal series for generic validator consumption.

    Parameters
    ----------
    rolling_days : int
        Rolling window for Shibor percentile.
    percentile_threshold : float
        Percentile threshold for squeeze signal.
    stability_months : int
        Months of stability before LPR pivot.
    signal_variant : str
        One of: ``"rate_squeeze"``, ``"spread_inversion"``, ``"lpr_pivot"``,
        ``"signal_any"``.
    df_shibor_inject : pd.DataFrame | None
        Pre-fetched Shibor DataFrame for offline testing.
    df_lpr_inject : pd.DataFrame | None
        Pre-fetched LPR DataFrame for offline testing.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``signal`` (bool).
    """
    summary = compute_liquidity_tightening(
        rolling_days=rolling_days,
        percentile_threshold=percentile_threshold,
        stability_months=stability_months,
        df_shibor_inject=df_shibor_inject,
        df_lpr_inject=df_lpr_inject,
    )

    if "daily_series" not in summary:
        return pd.DataFrame(columns=["trade_date", "signal"])

    rows = []
    for entry in summary["daily_series"]:
        signal_val = entry.get(signal_variant, False)
        rows.append({
            "trade_date": pd.Timestamp(entry["trade_date"]),
            "signal": bool(signal_val),
        })

    return pd.DataFrame(rows).sort_values("trade_date").reset_index(drop=True)