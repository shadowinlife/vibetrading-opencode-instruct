"""
Breadth divergence condition: market breadth weakening while index stays elevated.

Computes a daily breadth ratio from ``stk_factor_pro.pct_chg`` across all A-shares
and compares it against index price levels from ``idx_factor_pro`` (SSE 000001.SH).

Signal definition
-----------------
A signal fires when **both** conditions hold on the same day:

1. **Breadth weakness**: ``breadth_ratio`` is below its rolling ``N``-day percentile
   (i.e. breadth is unusually weak relative to the recent window).
2. **Index elevation**: ``sse_close`` is near its rolling ``N``-day high
   (above a percentile threshold, e.g. 75th).

The intuition: an index that keeps rising (or staying elevated) while fewer stocks
participate is a classic topping pattern — the rally is narrowing.

Validation
----------
Signals are validated against forward max drawdowns of the SSE index
at horizons 20, 60, and 120 trading days, using the same methodology as
``tune_escape_top.compute_forward_drawdowns``.

Data source note
----------------
``idx_quote_dc`` (the daily quote table) does **not** contain data for
``000001.SH`` — it only covers ``BK****.DC`` concept boards from 2024-12-20.
This module therefore derives breadth directly from ``stk_factor_pro.pct_chg``,
which gives full coverage from 2010 onward.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .base import get_connection, pct_rank
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_HORIZONS: tuple[int, ...] = (20, 60, 120)
"""Default rolling window sizes in trading days for breadth percentile computation."""

DEFAULT_BREADTH_PCT_THRESHOLD: float = 25.0
"""Default percentile threshold: breadth_ratio must be below this percentile to
qualify as 'unusually weak' (lower = more selective)."""

DEFAULT_INDEX_PCT_THRESHOLD: float = 75.0
"""Default percentile threshold: sse_close must be above this percentile
relative to the rolling window to qualify as 'index elevated'."""


# ── Data loading ───────────────────────────────────────────────────────────────


def _load_breadth_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
) -> pd.DataFrame:
    """Compute daily breadth ratio from all A-shares in ``stk_factor_pro``.

    Returns a DataFrame with columns ``trade_date``, ``n_stocks``, ``up_num``,
    ``down_num``, ``flat_num``, ``breadth_ratio``, sorted by trade_date.
    """
    con = get_connection(duckdb_path, read_only=True)
    df = con.execute("""
        SELECT trade_date,
               COUNT(*)                                                   AS n_stocks,
               SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END)              AS up_num,
               SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END)              AS down_num,
               SUM(CASE WHEN pct_chg = 0 THEN 1 ELSE 0 END)              AS flat_num
        FROM stk_factor_pro
        WHERE pct_chg IS NOT NULL
        GROUP BY trade_date
        ORDER BY trade_date
    """).fetchdf()
    con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # Breadth ratio: up / (up + down), ignoring flat
    denom = df["up_num"].astype(float) + df["down_num"].astype(float)
    df["breadth_ratio"] = np.where(denom > 0, df["up_num"].astype(float) / denom, np.nan)
    return df.reset_index(drop=True)


def _load_sse_close(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
) -> pd.DataFrame:
    """Load SSE close prices from ``idx_factor_pro``.

    Returns columns ``trade_date``, ``sse_close``.
    """
    con = get_connection(duckdb_path, read_only=True)
    df = con.execute(
        "SELECT trade_date, close AS sse_close FROM idx_factor_pro "
        "WHERE ts_code = ? ORDER BY trade_date",
        [SSE_INDEX_CODE],
    ).fetchdf()
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.reset_index(drop=True)


# ── Breadth divergence detection ───────────────────────────────────────────────


def compute_breadth_signals(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    breadth_pct_threshold: float = DEFAULT_BREADTH_PCT_THRESHOLD,
    index_pct_threshold: float = DEFAULT_INDEX_PCT_THRESHOLD,
) -> pd.DataFrame:
    """Compute breadth divergence signals.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    horizons : tuple[int, ...]
        Rolling window sizes in trading days.  Default ``(20, 60, 120)``.
    breadth_pct_threshold : float
        Breadth ratio must be **below** this percentile to signal weakness.
        Range [0, 100]; lower = more selective.  Default 25.0.
    index_pct_threshold : float
        SSE close must be **above** this percentile to signal elevation.
        Range [0, 100]; higher = more selective.  Default 75.0.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``breadth_ratio``, ``sse_close``,
        ``breadth_pct_Hd``, ``close_pct_Hd`` (for each horizon H),
        ``signal_Hd`` (for each horizon H).
    """
    df_b = _load_breadth_series(duckdb_path)
    df_s = _load_sse_close(duckdb_path)
    df = df_b.merge(df_s, on="trade_date", how="inner").sort_values("trade_date").reset_index(drop=True)

    for h in horizons:
        # Rolling percentile rank (0-100) within each window
        df[f"breadth_pct_{h}d"] = (
            df["breadth_ratio"]
            .rolling(h, min_periods=max(h // 2, 20))
            .apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100, raw=False)
        )
        df[f"close_pct_{h}d"] = (
            df["sse_close"]
            .rolling(h, min_periods=max(h // 2, 20))
            .apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100, raw=False)
        )

        # Signal: breadth weak AND index elevated
        breadth_weak = df[f"breadth_pct_{h}d"] <= breadth_pct_threshold
        index_high = df[f"close_pct_{h}d"] >= index_pct_threshold
        df[f"signal_{h}d"] = breadth_weak & index_high

    return df


# ── Validation: forward drawdowns ──────────────────────────────────────────────


def _compute_forward_drawdowns_from_df(
    df: pd.DataFrame,
    close_col: str = "sse_close",
    date_col: str = "trade_date",
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """Compute forward max drawdowns for the SSE close series in *df*.

    Pure function — no DuckDB required.  Uses the same algorithm as
    ``tune_escape_top.compute_forward_drawdowns``.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain ``trade_date`` (or *date_col*) and close column, sorted.
    close_col : str
        Column name for the close price series.
    date_col : str
        Column name for the date.
    horizons : tuple[int, ...]
        Forward windows in trading days.

    Returns
    -------
    pd.DataFrame
        Copy of *df* with additional ``fwd_dd_Hd`` and ``label_Hd`` columns.
    """
    result = df.sort_values(date_col).reset_index(drop=True).copy()
    close = result[close_col].values
    n = len(result)

    for h in horizons:
        dd = np.full(n, np.nan)
        for i in range(n):
            if i + 1 < n:
                future = close[i + 1 : min(i + h + 1, n)]
                if len(future) > 0:
                    dd[i] = float(np.min(future) / close[i] - 1.0)
        result[f"fwd_dd_{h}d"] = dd

    return result


# ── Aggregate signal evaluation ────────────────────────────────────────────────


@dataclass
class BreadthSignalSummary:
    """Summary statistics for breadth divergence signals at one horizon."""

    horizon_days: int
    """Rolling window size."""
    total_days: int
    """Total trading days with valid signal column."""
    signal_days: int
    """Number of days where the signal fired."""
    signal_pct: float
    """Signal days as percentage of total."""
    mean_fwd_dd_signal: float
    """Mean forward drawdown on signal days."""
    mean_fwd_dd_nonsignal: float
    """Mean forward drawdown on non-signal days."""
    mean_fwd_dd_all: float
    """Mean forward drawdown across all days."""
    direction_ok: bool
    """``True`` if signal-day DD is more negative than non-signal-day DD."""


def evaluate_signals(
    df: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    """Evaluate breadth divergence signals against forward drawdowns.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`compute_breadth_signals`.
    horizons : tuple[int, ...]
        Horizons matching those in *df*.

    Returns
    -------
    dict
        Keys: ``coverage``, ``signal_counts`` (list of per-horizon dicts),
        ``overall_classification``.
    """
    # Compute forward DD
    df = _compute_forward_drawdowns_from_df(df, horizons=horizons)

    coverage_start = str(df["trade_date"].min().date())
    coverage_end = str(df["trade_date"].max().date())
    coverage_years = (df["trade_date"].max() - df["trade_date"].min()).days / 365.25

    summaries: list[BreadthSignalSummary] = []
    for h in horizons:
        sig_col = f"signal_{h}d"
        dd_col = f"fwd_dd_{h}d"

        valid = df[dd_col].notna()
        sig = valid & df[sig_col].fillna(False)
        nonsig = valid & ~df[sig_col].fillna(False)

        total = valid.sum()
        n_sig = sig.sum()
        pct = n_sig / total * 100 if total > 0 else 0.0

        dd_sig = df.loc[sig, dd_col].mean() if n_sig > 0 else float("nan")
        dd_nonsig = df.loc[nonsig, dd_col].mean() if nonsig.sum() > 0 else float("nan")
        dd_all = df.loc[valid, dd_col].mean()

        direction_ok = (
            not np.isnan(dd_sig)
            and not np.isnan(dd_nonsig)
            and dd_sig < dd_nonsig
        )

        summaries.append(BreadthSignalSummary(
            horizon_days=h,
            total_days=int(total),
            signal_days=int(n_sig),
            signal_pct=round(pct, 2),
            mean_fwd_dd_signal=round(float(dd_sig), 4) if not np.isnan(dd_sig) else float("nan"),
            mean_fwd_dd_nonsignal=round(float(dd_nonsig), 4) if not np.isnan(dd_nonsig) else float("nan"),
            mean_fwd_dd_all=round(float(dd_all), 4),
            direction_ok=direction_ok,
        ))

    # Overall classification
    direction_oks = [s.direction_ok for s in summaries]
    all_ok = all(direction_oks)
    any_ok = any(direction_oks)

    if coverage_years < 5.0:
        classification = "research_only"
        reason = f"Coverage {coverage_years:.1f} years < 5 year minimum"
    elif all_ok:
        # Check selectivity: signal_pct should be in reasonable range (0.5% - 25%)
        sig_pcts_ok = all(0.5 <= s.signal_pct <= 25.0 for s in summaries)
        if sig_pcts_ok:
            classification = "validated"
            reason = "Direction correct across all horizons with acceptable selectivity"
        else:
            classification = "research_only"
            reason = "Direction OK but signal selectivity outside optimal range"
    elif any_ok:
        classification = "research_only"
        reason = "Direction correct for some but not all horizons"
    else:
        classification = "rejected"
        reason = "Direction wrong across all horizons"

    return {
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "coverage_years": round(coverage_years, 2),
        "classification": classification,
        "classification_reason": reason,
        "signal_counts": [
            {
                "horizon_days": s.horizon_days,
                "total_days": s.total_days,
                "signal_days": s.signal_days,
                "signal_pct": s.signal_pct,
                "mean_fwd_dd_signal": s.mean_fwd_dd_signal,
                "mean_fwd_dd_nonsignal": s.mean_fwd_dd_nonsignal,
                "mean_fwd_dd_all": s.mean_fwd_dd_all,
                "direction_ok": s.direction_ok,
            }
            for s in summaries
        ],
    }


# ── Grid search ────────────────────────────────────────────────────────────────


def grid_search_breadth_signals(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    breadth_thresholds: tuple[float, ...] = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 50.0),
    index_thresholds: tuple[float, ...] = (60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 90.0),
) -> pd.DataFrame:
    """Grid-search over breadth/index percentile thresholds.

    Returns a DataFrame with one row per parameter combination, including
    signal counts and mean forward drawdowns for each horizon.
    """
    df = compute_breadth_signals(
        duckdb_path,
        horizons=horizons,
        breadth_pct_threshold=50.0,  # placeholder — overridden per grid cell
        index_pct_threshold=50.0,
    )

    # Pre-compute forward DD once
    df = _compute_forward_drawdowns_from_df(df, horizons=horizons)

    rows: list[dict[str, Any]] = []
    for b in breadth_thresholds:
        for i in index_thresholds:
            row: dict[str, Any] = {"breadth_threshold": b, "index_threshold": i}
            for h in horizons:
                sig_col = f"signal_{h}d"
                dd_col = f"fwd_dd_{h}d"
                # Recompute signal for this threshold pair
                b_weak = df[f"breadth_pct_{h}d"] <= b
                i_high = df[f"close_pct_{h}d"] >= i
                signal = b_weak & i_high

                valid = df[dd_col].notna()
                n_sig = (valid & signal).sum()
                dd_sig = df.loc[valid & signal, dd_col].mean() if n_sig > 0 else np.nan
                dd_non = df.loc[valid & ~signal, dd_col].mean()
                dd_all = df.loc[valid, dd_col].mean()

                row[f"{h}d_signals"] = int(n_sig)
                row[f"{h}d_dd_signal"] = round(float(dd_sig), 4) if not np.isnan(dd_sig) else None
                row[f"{h}d_dd_nonsignal"] = round(float(dd_non), 4)
                row[f"{h}d_dd_all"] = round(float(dd_all), 4)
                row[f"{h}d_direction_ok"] = bool(not np.isnan(dd_sig) and dd_sig < dd_non)

            # Composite DD: mean across horizons
            comp_dds = [
                row[f"{h}d_dd_signal"] for h in horizons
                if row.get(f"{h}d_dd_signal") is not None
            ]
            row["composite_dd"] = round(float(np.mean(comp_dds)), 4) if comp_dds else None
            row["min_signals"] = min(
                row[f"{h}d_signals"] for h in horizons if row.get(f"{h}d_signals") is not None
            )
            row["direction_ok_all"] = all(
                row.get(f"{h}d_direction_ok", False) for h in horizons
            )
            rows.append(row)

    return (
        pd.DataFrame(rows)
        .sort_values("composite_dd", ascending=True, na_position="last")
        .reset_index(drop=True)
    )