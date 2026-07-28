"""
Macro credit impulse signal module for escape-top microstructure framework.

Uses Tushare ``cn_m`` (M2 money supply) and ``sf_month`` (social financing)
to detect tightening macro liquidity conditions that historically preceded
market drawdowns.

**Critical date handling**: Monthly macro data is released ~10-15 days after
month-end (PBOC schedule).  This module:

1. Computes ``effective_date = month_end + 15 calendar days`` for each release.
2. Forward-fills the latest known value from ``effective_date`` to the next
   ``effective_date``, preventing look-ahead bias.
3. Signals are only emitted on or after the effective_date of the data they use.

**Signal**: M2 YoY growth decelerating for ≥2 consecutive months, combined
with an elevated market (SSE close above 250-day MA), triggers a tightening
credit impulse warning.

Usage::

    from scripts.microstructure.macro_credit_impulse import (
        compute_macro_credit_impulse_signal,
    )
    df_cn_m, df_sf, df_signal = compute_macro_credit_impulse_signal(
        start_month="201501",
        end_month="202605",
    )

Based on the effective-date audit pattern from
:mod:`scripts.microstructure.effective_date_audit`.
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import tushare as ts

# ── Constants ─────────────────────────────────────────────────────────────────

RELEASE_LAG_CALENDAR_DAYS: int = 15
"""PBOC releases M2/social-financing data ~10-15 days after month-end.
We use 15 as a conservative upper bound."""

SSE_MA_WINDOW: int = 250
"""Trading-day window for market-elevation (close > 250d MA) condition."""

M2_DECLINE_CONSECUTIVE: int = 2
"""Number of consecutive months M2 YoY must decline to trigger tightening."""

DEFAULT_DB_PATH: str = "./duckdb/ashare.duckdb"

# ── Token loading ─────────────────────────────────────────────────────────────


def _load_tushare_token() -> str:
    """Load Tushare token from environment or local .env file.

    Precedence: TUSHARE_TOKEN env var > ./.env file.
    """
    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        return token
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().strip().split("\n"):
            if line.startswith("TUSHARE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not token:
        raise RuntimeError(
            "TUSHARE_TOKEN not found. Set env var TUSHARE_TOKEN or "
            "add it to ./.env file."
        )
    return token


# ── Effective date helpers ────────────────────────────────────────────────────


def month_end_date(month_str: str) -> date:
    """Return the last calendar day of a month given as ``YYYYMM``.

    >>> month_end_date("202503")
    datetime.date(2025, 3, 31)
    """
    y = int(month_str[:4])
    m = int(month_str[4:6])
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, last_day)


def effective_date_from_month(month_str: str, lag_days: int = RELEASE_LAG_CALENDAR_DAYS) -> date:
    """Compute the effective date for a monthly macro release.

    effective_date = month_end(month_str) + lag_days calendar days.

    >>> effective_date_from_month("202503")
    datetime.date(2025, 4, 15)
    """
    return month_end_date(month_str) + timedelta(days=lag_days)


def compute_effective_date_series(months: list[str]) -> list[date]:
    """Compute effective_dates for a sequence of monthly releases.

    Sorted in chronological order; each entry is month_end + 15d.
    """
    return [effective_date_from_month(m) for m in months]


# ── Tushare data fetching ─────────────────────────────────────────────────────


def fetch_cn_m(
    start_month: str = "201501",
    end_month: str | None = None,
    token: str | None = None,
) -> pd.DataFrame:
    """Fetch M2 money supply data from Tushare ``cn_m`` endpoint.

    Parameters
    ----------
    start_month : str
        Start month in ``YYYYMM`` format (default 201501).
    end_month : str or None
        End month in ``YYYYMM`` format.  Defaults to current month.
    token : str or None
        Tushare token.  Loaded from env if not provided.

    Returns
    -------
    pd.DataFrame
        Columns: month, m0, m0_yoy, m0_mom, m1, m1_yoy, m1_mom, m2, m2_yoy, m2_mom,
        period_date, month_end, effective_date.
    """
    if token is None:
        token = _load_tushare_token()
    pro = ts.pro_api(token)

    # cn_m accepts m=YYYYMM for single month — we need to iterate months
    if end_month is None:
        end_month = date.today().strftime("%Y%m")

    months: list[str] = []
    y, m = int(start_month[:4]), int(start_month[4:6])
    ey, em = int(end_month[:4]), int(end_month[4:6])
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    frames: list[pd.DataFrame] = []
    for m_str in months:
        try:
            row = pro.cn_m(m=m_str)
            if not row.empty:
                frames.append(row)
        except Exception:
            # Some months may not be available yet
            pass

    if not frames:
        raise ValueError(f"No cn_m data fetched for range {start_month}–{end_month}")

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("month").reset_index(drop=True)

    # Compute effective dates
    df["period_date"] = pd.to_datetime(df["month"], format="%Y%m")
    df["month_end"] = df["month"].apply(lambda x: month_end_date(str(x)))
    df["effective_date"] = df["month"].apply(
        lambda x: effective_date_from_month(str(x))
    )
    # Convert effective_date to pd.Timestamp for merge alignment
    df["effective_date"] = pd.to_datetime(df["effective_date"])

    return df


def fetch_sf_month(
    start_month: str = "201501",
    end_month: str | None = None,
    token: str | None = None,
) -> pd.DataFrame | None:
    """Fetch social financing (社融) monthly data from Tushare ``sf_month``.

    Returns None when the API rate limit is exceeded or data is unavailable.
    """
    if token is None:
        token = _load_tushare_token()
    pro = ts.pro_api(token)

    if end_month is None:
        end_month = date.today().strftime("%Y%m")

    months: list[str] = []
    y, m = int(start_month[:4]), int(start_month[4:6])
    ey, em = int(end_month[:4]), int(end_month[4:6])
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1

    frames: list[pd.DataFrame] = []
    rate_limited = False
    for m_str in months:
        try:
            row = pro.query("sf_month", m=m_str)
            if not row.empty:
                frames.append(row)
        except Exception:
            rate_limited = True

    if not frames:
        if rate_limited:
            return None
        return None

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("month").reset_index(drop=True)

    df["period_date"] = pd.to_datetime(df["month"], format="%Y%m")
    df["month_end"] = df["month"].apply(lambda x: month_end_date(str(x)))
    df["effective_date"] = df["month"].apply(
        lambda x: effective_date_from_month(str(x))
    )
    df["effective_date"] = pd.to_datetime(df["effective_date"])

    return df


# ── Forward-fill to daily calendar ────────────────────────────────────────────


def forward_fill_daily(
    df_monthly: pd.DataFrame,
    value_cols: list[str],
    start_date: str = "2015-01-01",
    end_date: str | None = None,
) -> pd.DataFrame:
    """Forward-fill monthly data across all calendar days.

    For each monthly release at effective_date, the values are forward-filled
    across all subsequent calendar days until the next release's effective_date.

    Parameters
    ----------
    df_monthly : pd.DataFrame
        Must have ``effective_date`` and ``value_cols``.
    value_cols : list[str]
        Columns to forward-fill.
    start_date : str
        Earliest date in output (YYYY-MM-DD).
    end_date : str or None
        Latest date in output.  Defaults to today.

    Returns
    -------
    pd.DataFrame
        Columns: calendar_date, plus all *value_cols*, plus effective_date.
    """
    if end_date is None:
        end_date = date.today().isoformat()

    # Build daily calendar from start_date to end_date
    daily = pd.date_range(start=start_date, end=end_date, freq="D")
    df_daily = pd.DataFrame({"calendar_date": daily})

    # Sort monthly by effective_date for asof merge
    df_sorted = df_monthly.sort_values("effective_date").reset_index(drop=True)

    # Build a DataFrame of effective_dates + values
    release_df = df_sorted[["effective_date"] + value_cols].copy()

    # Forward-fill: for each calendar day, find the latest release whose
    # effective_date <= calendar_date
    # Use merge_asof with direction="backward" (use last known value)
    df_daily_sorted = df_daily.sort_values("calendar_date")
    release_df_sorted = release_df.sort_values("effective_date")

    result = pd.merge_asof(
        df_daily_sorted,
        release_df_sorted,
        left_on="calendar_date",
        right_on="effective_date",
        direction="backward",  # use last known release on or before calendar_date
    )

    # Drop rows where no release data is available yet (before first release)
    result = result.dropna(subset=value_cols + ["effective_date"])

    return result.reset_index(drop=True)


# ── Signal computation ────────────────────────────────────────────────────────


def compute_macro_credit_impulse_signal(
    start_month: str = "201501",
    end_month: str | None = None,
    token: str | None = None,
    duckdb_path: str = DEFAULT_DB_PATH,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compute macro credit impulse signal from cn_m and sf_month data.

    This is the main entry point.  Fetches Tushare data, computes effective
    dates, forward-fills to daily granularity, computes signal.

    The signal fires when:
    1. M2 YoY growth has declined for ≥2 consecutive months (vs prior month),
       AND
    2. The SSE Composite close is above its 250-day moving average (market
       is "elevated")

    Returns
    -------
    (df_cn_m, df_sf, df_signal) : tuple of DataFrames
        - df_cn_m: cleaned cn_m monthly data with effective_date
        - df_sf: cleaned sf_month monthly data with effective_date (may be None)
        - df_signal: daily signal DataFrame with columns:
          calendar_date, m2_yoy, m2_yoy_mom_change, m2_yoy_declining_streak,
          sse_close, sse_ma250, market_elevated, signal
    """
    if end_month is None:
        end_month = date.today().strftime("%Y%m")

    if token is None:
        token = _load_tushare_token()

    # ── Step 1: Fetch monthly macro data ──────────────────────────────────
    try:
        df_cn_m = fetch_cn_m(start_month=start_month, end_month=end_month, token=token)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch cn_m data: {e}") from e

    df_sf = fetch_sf_month(start_month=start_month, end_month=end_month, token=token)

    # ── Step 2: Compute M2 YoY momentum on monthly data ───────────────────
    df_cn_m = df_cn_m.sort_values("month").reset_index(drop=True)
    # M2 YoY month-over-month change (2nd derivative)
    df_cn_m["m2_yoy_mom_change"] = df_cn_m["m2_yoy"].diff()
    # Declining streak: count consecutive months where m2_yoy_mom_change < 0
    streak = 0
    streak_vals: list[int] = []
    for v in df_cn_m["m2_yoy_mom_change"]:
        if pd.notna(v) and v < 0:
            streak += 1
        else:
            streak = 0
        streak_vals.append(streak)
    df_cn_m["m2_yoy_declining_streak"] = streak_vals

    # ── Step 3: Compute social financing YoY momentum ─────────────────────
    if df_sf is not None:
        df_sf = df_sf.sort_values("month").reset_index(drop=True)
        df_sf["inc_month_yoy"] = df_sf["inc_month"].pct_change(periods=12)

    # ── Step 4: Forward-fill to daily calendar ────────────────────────────
    start_cal = f"{start_month[:4]}-{start_month[4:6]}-01"
    cn_m_value_cols = [
        "m2_yoy", "m2_yoy_mom_change", "m2_yoy_declining_streak",
        "m2", "m1_yoy", "m0_yoy",
    ]
    df_cn_m_daily = forward_fill_daily(
        df_cn_m, cn_m_value_cols, start_date=start_cal,
    )

    # ── Step 5: Merge (cn_m); sf_month optional ───────────────────────────
    df_merged = df_cn_m_daily.copy()

    sf_value_cols = ["inc_month", "inc_month_yoy", "inc_cumval", "stk_endval"]
    if df_sf is not None:
        df_sf_daily = forward_fill_daily(
            df_sf, sf_value_cols, start_date=start_cal,
        )
        df_merged = df_merged.merge(
            df_sf_daily[["calendar_date"] + sf_value_cols],
            on="calendar_date",
            how="left",
        )
    else:
        for col in sf_value_cols:
            df_merged[col] = np.nan

    df_merged = df_merged.sort_values("calendar_date").reset_index(drop=True)

    # ── Step 6: Compute credit impulse ────────────────────────────────────
    # credit_impulse = ΔM2_yoy / M2_yoy_lag_12m (acceleration relative to level)
    df_merged["m2_yoy_lag12"] = df_merged["m2_yoy"].shift(12)
    df_merged["credit_impulse"] = np.where(
        df_merged["m2_yoy_lag12"].abs() > 1e-12,
        df_merged["m2_yoy"].diff(12) / df_merged["m2_yoy_lag12"].abs(),
        np.nan,
    )

# ── Step 7: Fetch SSE close for market-elevation check ────────────────
    try:
        df_sse = _fetch_sse_close(duckdb_path)
        if not df_sse.empty:
            df_sse["trade_date_dt"] = pd.to_datetime(df_sse["trade_date"]).astype("datetime64[ns]")
            df_sse = df_sse.sort_values("trade_date_dt")
            df_sse["sse_ma250"] = (
                df_sse["sse_close"].rolling(window=SSE_MA_WINDOW, min_periods=SSE_MA_WINDOW).mean()
            )
            df_sse["market_elevated"] = df_sse["sse_close"] > df_sse["sse_ma250"]

            sse_keep = df_sse[["trade_date_dt", "sse_close", "sse_ma250", "market_elevated"]].copy()
            sse_keep = sse_keep.rename(columns={"trade_date_dt": "calendar_date"})

            df_merged = pd.merge_asof(
                df_merged.sort_values("calendar_date"),
                sse_keep.sort_values("calendar_date"),
                on="calendar_date",
                direction="backward",
            )
            df_merged["market_elevated"] = df_merged["market_elevated"].fillna(False).astype(bool)
    except Exception:
        df_merged["sse_close"] = np.nan
        df_merged["sse_ma250"] = np.nan
        df_merged["market_elevated"] = False

    # ── Step 8: Compute signal ────────────────────────────────────────────
    # Signal fires when:
    #   1. M2 YoY declining streak >= 2 consecutive months
    #   2. Market is elevated (close > 250d MA)
    # The signal is a tightening credit impulse warning
    df_merged["signal"] = (
        (df_merged["m2_yoy_declining_streak"].fillna(0) >= M2_DECLINE_CONSECUTIVE)
        & df_merged["market_elevated"].fillna(False)
    )

    # Ensure types
    df_merged["signal"] = df_merged["signal"].astype(bool)

    return df_cn_m, df_sf, df_merged


def _fetch_sse_close(duckdb_path: str) -> pd.DataFrame:
    """Fetch SSE Composite close prices from local DuckDB.

    Returns empty DataFrame on failure.
    """
    try:
        import duckdb
        con = duckdb.connect(duckdb_path, read_only=True)
        df = con.execute("""
            SELECT trade_date, close AS sse_close
            FROM idx_factor_pro
            WHERE ts_code = '000001.SH'
            ORDER BY trade_date
        """).fetchdf()
        con.close()
        return df
    except Exception:
        return pd.DataFrame()


# ── Convenience: signal-only output ────────────────────────────────────────────


def compute_signal_dataframe(
    start_month: str = "201501",
    end_month: str | None = None,
    token: str | None = None,
    duckdb_path: str = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Compute macro credit impulse signal and return only the signal DataFrame.

    Convenience wrapper around :func:`compute_macro_credit_impulse_signal`.
    """
    _, _, df_signal = compute_macro_credit_impulse_signal(
        start_month=start_month,
        end_month=end_month,
        token=token,
        duckdb_path=duckdb_path,
    )
    return df_signal