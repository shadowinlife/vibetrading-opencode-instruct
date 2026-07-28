"""
National Team capital flow signal module (condition #17).

Tracks quarterly holdings of known national team entities from
``fin_top10_holders`` and computes aggregate holding changes as a
market regime signal.

Data source
-----------
``fin_top10_holders`` (quarterly, top-10 holders only):
  ts_code, end_date, holder_name, hold_amount, hold_ratio

Known national team entities
----------------------------
- 中央汇金 (Central Huijin) — direct investment + asset management
- 中证金融资产管理计划 (CSF AM plans) — via fund management companies
- 全国社保基金组合 (National Social Security Fund portfolios)
- 国新投资有限公司 (Guoxin Investment)
- 梧桐树/凤山/坤藤 (SAFE platforms)

Signal definition
-----------------
A signal fires when **all** of the following hold:

1. **Holdings reduction**: Aggregate national team ``hold_amount`` has decreased
   for ``consecutive_quarters`` consecutive **full-data** quarters (Q2 06-30,
   Q4 12-31). Q1 (03-31) and Q3 (09-30) are excluded from signal computation
   because they have very sparse coverage (~20 entries vs ~1000+ for Q2/Q4).

2. **Market elevated**: SSE index close is above its rolling ``N``-day percentile
   (default 75th percentile), indicating the market is not at a trough.

The intuition: when national team entities — the "smart money" with policy
orientation — are systematically reducing their visible holdings while the
market remains elevated, it may signal a top formation.

Known limitations
-----------------
- **Partial visibility**: ``fin_top10_holders`` only tracks the TOP 10 holders.
  National team holdings below the top 10 are invisible. This is a proxy, not
  a comprehensive tracker.
- **Quarterly frequency**: Data is quarterly (end_date: 03-31, 06-30, 09-30,
  12-31) with ~30-day reporting lag. Signals are sparse (~4 per year).
- **Sparse Q1/Q3**: Q1 and Q3 reports have very few entries because most
  companies only report top 10 holders in semi-annual (06-30) and annual
  (12-31) reports. These quarters are excluded from signal computation.
- **hold_amount not inflation-adjusted**: Nominal values only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .base import get_connection, format_date
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ── Constants ──────────────────────────────────────────────────────────────────

NATIONAL_TEAM_WHERE_CLAUSE: str = """
    holder_name LIKE '%汇金%'
    OR holder_name LIKE '%中证金融%'
    OR holder_name LIKE '%社保基金%'
    OR holder_name LIKE '%国新投资%'
    OR holder_name LIKE '%梧桐树%'
    OR holder_name LIKE '%凤山%'
    OR holder_name LIKE '%坤藤%'
"""

# Full-data quarters: 06-30 (semi-annual) and 12-31 (annual).
# Q1 (03-31) and Q3 (09-30) have very sparse coverage and are excluded.
FULL_DATA_MONTHS: tuple[int, int] = (6, 12)

# Reporting lag: quarterly reports are released within 30 calendar days
# of quarter end.  The effective_date is end_date + 30 days.
REPORTING_LAG_DAYS: int = 30

# Default signal parameters.
DEFAULT_CONSECUTIVE_QUARTERS: int = 2
"""Number of consecutive full-data quarters with reducing holdings to trigger."""

DEFAULT_INDEX_PCT_THRESHOLD: float = 75.0
"""SSE close must be above this rolling percentile to signal 'market elevated'."""

DEFAULT_INDEX_WINDOW: int = 120
"""Rolling window in trading days for index elevation check."""

DEFAULT_HORIZONS: tuple[int, ...] = (20, 60, 120)
"""Forward drawdown horizons for validation."""


# ── Data loading ───────────────────────────────────────────────────────────────


def _load_national_team_quarterly(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
) -> pd.DataFrame:
    """Load quarterly aggregated national team holdings from ``fin_top10_holders``.

    Returns a DataFrame with columns:
        end_date, stocks, entries, total_hold_amount, total_hold_amount_yi,
        is_full_quarter

    ``is_full_quarter`` is True for 06-30 and 12-31 (semi-annual and annual
    reports with full coverage), False for 03-31 and 09-30 (sparse quarterly
    reports with very limited coverage).
    """
    con = get_connection(duckdb_path, read_only=True)
    df = con.execute(f"""
        SELECT
            end_date,
            COUNT(DISTINCT ts_code)                         AS stocks,
            COUNT(*)                                        AS entries,
            SUM(hold_amount)                                AS total_hold_amount,
            SUM(hold_amount) * 1e-8                         AS total_hold_amount_yi
        FROM fin_top10_holders
        WHERE {NATIONAL_TEAM_WHERE_CLAUSE}
        GROUP BY end_date
        ORDER BY end_date
    """).fetchdf()
    con.close()

    df["end_date"] = pd.to_datetime(df["end_date"])
    df["is_full_quarter"] = df["end_date"].dt.month.isin(FULL_DATA_MONTHS)
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


# ── Effective date mapping ─────────────────────────────────────────────────────


def _compute_effective_date(end_date: pd.Timestamp | date) -> date:
    """Compute effective date: end_date + REPORTING_LAG_DAYS calendar days.

    The effective date is the earliest date when the quarterly holdings
    data can be assumed to be publicly available.
    """
    if isinstance(end_date, pd.Timestamp):
        d = end_date.date()
    else:
        d = end_date
    return d + timedelta(days=REPORTING_LAG_DAYS)


# ── Signal computation ─────────────────────────────────────────────────────────


def _compute_quarterly_changes(df_nt: pd.DataFrame) -> pd.DataFrame:
    """Compute quarter-over-quarter changes in national team holdings.

    Only full-data quarters (06-30, 12-31) are used for change computation.
    Adds columns:
        hold_change (raw amount change)
        hold_change_pct (percentage change)
        stock_change (change in distinct stocks held)

    Returns DataFrame with the same columns plus change columns, sorted by end_date.
    """
    df = df_nt.copy().sort_values("end_date").reset_index(drop=True)

    # Compute changes between consecutive quarters (all quarters)
    df["hold_change"] = df["total_hold_amount"].diff()
    df["hold_change_pct"] = df["total_hold_amount"].pct_change() * 100.0
    df["stock_change"] = df["stocks"].diff()

    # Flag: is this a reduction quarter?
    df["is_reduction"] = df["hold_change"] < 0

    return df


def _detect_consecutive_reductions(
    df: pd.DataFrame,
    consecutive_quarters: int = DEFAULT_CONSECUTIVE_QUARTERS,
) -> pd.DataFrame:
    """Flag full-data quarters where holdings have decreased for
    *consecutive_quarters* consecutive full-data periods.

    Reductions are computed between consecutive full-data quarters only
    (skipping sparse Q1/Q3), because sparse quarters have very different
    coverage and would distort the comparison.

    Adds column ``consecutive_reduction``: True when the current full quarter
    and the previous ``consecutive_quarters - 1`` full-data quarters all
    show ``total_hold_amount`` declining relative to the previous full quarter.
    """
    df = df.copy()
    full_mask = df["is_full_quarter"]
    df["consecutive_reduction"] = False

    # Collect full-quarter hold amounts for direct comparison
    full_indices = [i for i in range(len(df)) if full_mask.iloc[i]]
    full_hold = [df["total_hold_amount"].iloc[i] for i in full_indices]

    for idx_pos, i in enumerate(full_indices):
        if idx_pos < consecutive_quarters - 1:
            # Not enough full quarters yet
            continue

        # Check that this full quarter and preceding (consecutive_quarters - 1)
        # full quarters each declined vs their respective prior full quarter.
        all_reducing = True
        for k in range(consecutive_quarters):
            curr_pos = idx_pos - k
            if curr_pos <= 0:
                all_reducing = False
                break
            if full_hold[curr_pos] >= full_hold[curr_pos - 1]:
                all_reducing = False
                break
        if all_reducing:
            df.loc[df.index[i], "consecutive_reduction"] = True

    return df


def compute_national_team_signals(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    consecutive_quarters: int = DEFAULT_CONSECUTIVE_QUARTERS,
    index_pct_threshold: float = DEFAULT_INDEX_PCT_THRESHOLD,
    index_window: int = DEFAULT_INDEX_WINDOW,
) -> pd.DataFrame:
    """Compute national team capital flow signals.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    consecutive_quarters : int
        Number of consecutive full-data quarters with reducing holdings required
        to trigger the signal.  Default 2.
    index_pct_threshold : float
        SSE close must be above this rolling percentile to signal 'market elevated'.
        Range [0, 100]; higher = more selective.  Default 75.0.
    index_window : int
        Rolling window in trading days for index elevation percentile.
        Default 120.

    Returns
    -------
    pd.DataFrame
        Columns: ``end_date``, ``effective_date``, ``stocks``, ``entries``,
        ``total_hold_amount_yi``, ``hold_change_pct``, ``is_full_quarter``,
        ``is_reduction``, ``consecutive_reduction``, ``sse_close``,
        ``close_pct``, ``signal``.
    """
    # Load data
    df_nt = _load_national_team_quarterly(duckdb_path)
    df_sse = _load_sse_close(duckdb_path)

    # Compute changes
    df_nt = _compute_quarterly_changes(df_nt)
    df_nt = _detect_consecutive_reductions(df_nt, consecutive_quarters)

    # Compute effective dates
    df_nt["effective_date"] = df_nt["end_date"].apply(_compute_effective_date)
    df_nt["effective_date"] = pd.to_datetime(df_nt["effective_date"])

    # Map SSE close to effective_date (nearest trading day >= effective_date)
    # For each effective_date, find the SSE close on or after that date
    sse_dates = df_sse["trade_date"].values
    sse_closes = df_sse["sse_close"].values

    mapped_closes = []
    for eff_date in df_nt["effective_date"].values:
        # Find the first trading day >= effective_date
        mask = sse_dates >= eff_date
        if mask.any():
            idx = np.argmax(mask)
            mapped_closes.append(sse_closes[idx])
        else:
            mapped_closes.append(np.nan)

    df_nt["sse_close"] = mapped_closes

    # Rolling percentile of SSE close (computed on the full SSE series, then
    # mapped to effective dates)
    df_sse_work = df_sse.copy()
    df_sse_work["close_pct"] = (
        df_sse_work["sse_close"]
        .rolling(index_window, min_periods=max(index_window // 2, 20))
        .apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100, raw=False)
    )

    # Map percentile to effective dates
    sse_pct_map = dict(zip(df_sse_work["trade_date"], df_sse_work["close_pct"]))
    mapped_pcts = []
    for eff_date in df_nt["effective_date"].values:
        mask = sse_dates >= eff_date
        if mask.any():
            idx = np.argmax(mask)
            td = sse_dates[idx]
            mapped_pcts.append(sse_pct_map.get(td, np.nan))
        else:
            mapped_pcts.append(np.nan)

    df_nt["close_pct"] = mapped_pcts

    # Signal: consecutive reduction AND market elevated
    df_nt["signal"] = (
        df_nt["consecutive_reduction"].fillna(False)
        & df_nt["close_pct"].notna()
        & (df_nt["close_pct"] >= index_pct_threshold)
    )

    return df_nt


# ── Validation: forward drawdowns ──────────────────────────────────────────────


def _compute_forward_drawdowns_from_sse(
    df_nt: pd.DataFrame,
    df_sse: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """Compute forward max drawdowns from the SSE close at each effective_date.

    For each row in *df_nt*, computes the forward max drawdown of the SSE
    index over the next *h* trading days starting from the effective_date.

    Parameters
    ----------
    df_nt : pd.DataFrame
        National team quarterly data with ``effective_date`` column.
    df_sse : pd.DataFrame
        SSE daily data with ``trade_date`` and ``sse_close`` columns.
    horizons : tuple[int, ...]
        Forward windows in trading days.

    Returns
    -------
    pd.DataFrame
        Copy of *df_nt* with additional ``fwd_dd_Hd`` columns.
    """
    result = df_nt.copy()
    close_vals = df_sse["sse_close"].values
    dates_vals = df_sse["trade_date"].values

    for h in horizons:
        dd = np.full(len(result), np.nan, dtype=float)
        for i in range(len(result)):
            eff_date = result["effective_date"].iloc[i]
            # Find the index in SSE data for this effective_date
            mask = dates_vals >= eff_date
            if not mask.any():
                continue
            sse_idx = int(np.argmax(mask))
            if sse_idx + 1 >= len(close_vals):
                continue
            future = close_vals[sse_idx + 1 : min(sse_idx + h + 1, len(close_vals))]
            if len(future) > 0 and close_vals[sse_idx] > 0:
                dd[i] = float(np.min(future) / close_vals[sse_idx] - 1.0)
        result[f"fwd_dd_{h}d"] = dd

    return result


# ── Aggregate evaluation ───────────────────────────────────────────────────────


@dataclass
class NationalTeamSignalSummary:
    """Summary statistics for national team flow signals at one horizon."""

    horizon_days: int
    """Forward drawdown horizon in trading days."""
    total_quarters: int
    """Total full-data quarters with valid forward DD."""
    signal_quarters: int
    """Number of full-data quarters where the signal fired."""
    signal_pct: float
    """Signal quarters as percentage of total."""
    mean_fwd_dd_signal: float
    """Mean forward drawdown on signal quarters."""
    mean_fwd_dd_nonsignal: float
    """Mean forward drawdown on non-signal quarters."""
    mean_fwd_dd_all: float
    """Mean forward drawdown across all valid quarters."""
    direction_ok: bool
    """True if signal-quarter DD is more negative than non-signal-quarter DD."""


def evaluate_signals(
    df_nt: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
) -> dict[str, Any]:
    """Evaluate national team flow signals against forward drawdowns.

    Parameters
    ----------
    df_nt : pd.DataFrame
        Output of :func:`compute_national_team_signals`.
    horizons : tuple[int, ...]
        Forward drawdown horizons.
    duckdb_path : str
        Path to the DuckDB database (for loading SSE close).

    Returns
    -------
    dict
        Keys: ``coverage``, ``signal_counts`` (list of per-horizon dicts),
        ``overall_classification``.
    """
    df_sse = _load_sse_close(duckdb_path)
    df = _compute_forward_drawdowns_from_sse(df_nt, df_sse, horizons=horizons)

    coverage_start = str(df["end_date"].min().date())
    coverage_end = str(df["end_date"].max().date())
    coverage_years = (df["end_date"].max() - df["end_date"].min()).days / 365.25

    # Only evaluate on full-data quarters
    full_mask = df["is_full_quarter"]
    df_full = df[full_mask].copy()

    summaries: list[NationalTeamSignalSummary] = []
    for h in horizons:
        dd_col = f"fwd_dd_{h}d"

        valid = df_full[dd_col].notna()
        sig = valid & df_full["signal"].fillna(False)
        nonsig = valid & ~df_full["signal"].fillna(False)

        total = int(valid.sum())
        n_sig = int(sig.sum())
        pct = n_sig / total * 100 if total > 0 else 0.0

        dd_sig = df_full.loc[sig, dd_col].mean() if n_sig > 0 else float("nan")
        dd_nonsig = (
            df_full.loc[nonsig, dd_col].mean() if nonsig.sum() > 0 else float("nan")
        )
        dd_all = float(df_full.loc[valid, dd_col].mean())

        direction_ok = (
            not np.isnan(dd_sig)
            and not np.isnan(dd_nonsig)
            and dd_sig < dd_nonsig
        )

        summaries.append(
            NationalTeamSignalSummary(
                horizon_days=h,
                total_quarters=total,
                signal_quarters=n_sig,
                signal_pct=round(pct, 2),
                mean_fwd_dd_signal=round(float(dd_sig), 4)
                if not np.isnan(dd_sig)
                else float("nan"),
                mean_fwd_dd_nonsignal=round(float(dd_nonsig), 4)
                if not np.isnan(dd_nonsig)
                else float("nan"),
                mean_fwd_dd_all=round(dd_all, 4),
                direction_ok=direction_ok,
            )
        )

    # Overall classification
    direction_oks = [s.direction_ok for s in summaries]
    all_ok = all(direction_oks)
    any_ok = any(direction_oks)

    if coverage_years < 5.0:
        classification = "research_only"
        reason = f"Coverage {coverage_years:.1f} years < 5 year minimum"
    elif not any_ok:
        classification = "rejected"
        reason = "Direction wrong across all horizons"
    elif not all_ok:
        classification = "research_only"
        reason = "Direction correct for some but not all horizons"
    else:
        # Check selectivity: signal_pct should be in reasonable range
        sig_pcts_ok = all(0.5 <= s.signal_pct <= 25.0 for s in summaries)
        if sig_pcts_ok:
            classification = "validated"
            reason = "Direction correct across all horizons with acceptable selectivity"
        else:
            classification = "research_only"
            reason = "Direction OK but signal selectivity outside optimal range"

    # Collect signal quarters
    signal_quarters = df_full.loc[df_full["signal"].fillna(False), "end_date"]
    signal_dates = [format_date(d) for d in signal_quarters.dt.date.tolist()]

    return {
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "coverage_years": round(coverage_years, 2),
        "total_quarters": int(len(df)),
        "full_data_quarters": int(full_mask.sum()),
        "sparse_quarters": int((~full_mask).sum()),
        "classification": classification,
        "classification_reason": reason,
        "signal_quarters": signal_dates,
        "signal_counts": [
            {
                "horizon_days": s.horizon_days,
                "total_quarters": s.total_quarters,
                "signal_quarters": s.signal_quarters,
                "signal_pct": s.signal_pct,
                "mean_fwd_dd_signal": s.mean_fwd_dd_signal,
                "mean_fwd_dd_nonsignal": s.mean_fwd_dd_nonsignal,
                "mean_fwd_dd_all": s.mean_fwd_dd_all,
                "direction_ok": s.direction_ok,
            }
            for s in summaries
        ],
        "limitations": [
            "fin_top10_holders only tracks TOP 10 holders — national team holdings below top 10 are invisible",
            "Quarterly data only (end_date: 03-31, 06-30, 09-30, 12-31) with ~30-day reporting lag",
            "Q1 (03-31) and Q3 (09-30) have very sparse coverage (~20 entries vs 1000+ for Q2/Q4)",
            "hold_amount is nominal, not inflation-adjusted",
            "This is a proxy, not a comprehensive national team holdings tracker",
        ],
    }


# ── Grid search ────────────────────────────────────────────────────────────────


def grid_search_national_team_signals(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    consecutive_quarters_values: tuple[int, ...] = (1, 2, 3),
    index_thresholds: tuple[float, ...] = (50.0, 60.0, 70.0, 75.0, 80.0, 85.0, 90.0),
    index_windows: tuple[int, ...] = (60, 120, 250),
) -> pd.DataFrame:
    """Grid-search over consecutive quarters, index thresholds, and index windows.

    Returns a DataFrame with one row per parameter combination, including
    signal counts and mean forward drawdowns for each horizon.
    """
    df_sse = _load_sse_close(duckdb_path)
    rows: list[dict[str, Any]] = []

    for cq in consecutive_quarters_values:
        for iw in index_windows:
            df_nt = compute_national_team_signals(
                duckdb_path,
                consecutive_quarters=cq,
                index_pct_threshold=50.0,  # placeholder, overridden per cell
                index_window=iw,
            )

            for it in index_thresholds:
                row: dict[str, Any] = {
                    "consecutive_quarters": cq,
                    "index_threshold": it,
                    "index_window": iw,
                }

                # Recompute signal with this threshold
                df_nt["signal"] = (
                    df_nt["consecutive_reduction"].fillna(False)
                    & df_nt["close_pct"].notna()
                    & (df_nt["close_pct"] >= it)
                )

                df = _compute_forward_drawdowns_from_sse(
                    df_nt, df_sse, horizons=horizons
                )
                full_mask = df["is_full_quarter"]
                df_full = df[full_mask].copy()

                for h in horizons:
                    dd_col = f"fwd_dd_{h}d"
                    sig_col = "signal"

                    valid = df_full[dd_col].notna()
                    sig = valid & df_full[sig_col].fillna(False)
                    nonsig = valid & ~df_full[sig_col].fillna(False)

                    n_sig = int(sig.sum())
                    dd_sig = (
                        df_full.loc[sig, dd_col].mean() if n_sig > 0 else np.nan
                    )
                    dd_non = (
                        df_full.loc[nonsig, dd_col].mean()
                        if nonsig.sum() > 0
                        else np.nan
                    )
                    dd_all = float(df_full.loc[valid, dd_col].mean())

                    row[f"{h}d_signals"] = n_sig
                    row[f"{h}d_dd_signal"] = (
                        round(float(dd_sig), 4) if not np.isnan(dd_sig) else None
                    )
                    row[f"{h}d_dd_nonsignal"] = (
                        round(float(dd_non), 4)
                        if not np.isnan(dd_non)
                        else None
                    )
                    row[f"{h}d_dd_all"] = round(dd_all, 4)
                    row[f"{h}d_direction_ok"] = bool(
                        not np.isnan(dd_sig)
                        and not np.isnan(dd_non)
                        and dd_sig < dd_non
                    )

                # Composite DD: mean across horizons
                comp_dds = [
                    row[f"{h}d_dd_signal"]
                    for h in horizons
                    if row.get(f"{h}d_dd_signal") is not None
                ]
                row["composite_dd"] = (
                    round(float(np.mean(comp_dds)), 4) if comp_dds else None
                )
                row["total_signals"] = sum(
                    row[f"{h}d_signals"]
                    for h in horizons
                    if row.get(f"{h}d_signals") is not None
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