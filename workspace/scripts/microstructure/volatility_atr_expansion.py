"""
Volatility / ATR expansion condition for the escape-top microstructure framework.

Candidate #5: Tests whether elevated ATR (Average True Range) and/or elevated
realized volatility (annualised std of daily returns) act as an early-warning
signal for significant forward drawdowns of the Shanghai Composite Index.

Two signal variants are provided:

1. **Standalone ATR percentile**: Signal when ATR exceeds its rolling N-th
   percentile (contemporaneous/past data only).
2. **ATR after elevated turnover/concentration**: Signal when ATR is elevated
   AND concentration was high in the preceding K-day window (the
   ``atr_after_turnover`` variant).

Additionally, realised volatility is compared against ATR-field volatility
to assess which measure provides a stronger directional signal.

Data sources
------------
- ``idx_factor_pro.atr_bfq``: N=20 day ATR for SSE 000001.SH
- ``idx_factor_pro.close``: Daily close for realised vol computation
- Concentration series: from ``tune_escape_top._load_concentration_series``

All signals use **only contemporaneous/past data**.  Forward-drawdown
labels (20d/60d/120d) are computed separately and used only for
evaluation, never as signal inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .base import get_connection, pct_rank, top_pct_mask
from .metadata import DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ATR_ROLLING_DAYS: int = 500
"""Default rolling window (trading days) for ATR percentile computation.

500 trading days ≈ 2 calendar years — long enough to establish a stable
percentile baseline but short enough to adapt to regime changes."""

DEFAULT_VOL_ROLLING_DAYS: int = 250
"""Default rolling window (trading days) for realised-vol percentile
computation."""

DEFAULT_ATR_PERCENTILE: float = 80.0
"""Default percentile threshold for ATR elevation signal (0–100)."""

DEFAULT_VOL_PERCENTILE: float = 80.0
"""Default percentile threshold for realised vol elevation signal (0–100)."""

DEFAULT_TURNOVER_LOOKBACK_DAYS: int = 20
"""Default lookback window (trading days) for the 'after high turnover'
gate: how far back to check whether concentration was elevated."""

DEFAULT_CONCENTRATION_THRESHOLD: float = 0.45
"""Default top5_share threshold for the concentration/is-elevated gate."""

FORWARD_HORIZONS: list[int] = [20, 60, 120]
"""Forward drawdown horizons in trading days."""

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolatilitySignal:
    """One day's volatility-expansion signal outputs."""

    trade_date: pd.Timestamp
    atr: float
    atr_percentile: float  # 0–100
    atr_signal: bool  # ATR > percentile threshold
    realized_vol: float  # annualised
    vol_percentile: float  # 0–100
    vol_signal: bool  # realised vol > percentile threshold
    joint_vol_signal: bool  # ATR + realised vol both elevated
    concentration_elevated: bool  # concentration was high in preceding window
    atr_after_turnover_signal: bool  # ATR elevated AND concentration was high recently


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_sse_atr_and_close(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
) -> pd.DataFrame:
    """Load ATR and close for SSE Composite Index.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``atr``, ``close``.
        Sorted by ``trade_date`` ascending.
    """
    con = get_connection(duckdb_path, read_only=True)
    df = con.execute(
        "SELECT trade_date, atr_bfq AS atr, close "
        "FROM idx_factor_pro "
        "WHERE ts_code = ? "
        "ORDER BY trade_date",
        [SSE_INDEX_CODE],
    ).fetchdf()
    con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values("trade_date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Realised volatility computation (pure function)
# ---------------------------------------------------------------------------


def compute_realized_vol(
    df: pd.DataFrame,
    *,
    vol_window: int = DEFAULT_VOL_ROLLING_DAYS,
    trading_days_per_year: int = 250,
) -> pd.Series:
    """Compute annualised realised volatility from daily close returns.

    Uses the standard formula: ``std(daily_returns, rolling_window) × √trading_days_per_year``.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain ``close`` column sorted chronologically.
    vol_window : int
        Rolling window for volatility computation in trading days.
    trading_days_per_year : int
        Annualisation factor (default 250).

    Returns
    -------
    pd.Series
        Annualised realised volatility.  First ``vol_window-1`` values are NaN
        (insufficient data for a full window).
    """
    rets = df["close"].pct_change(fill_method=None)
    vol = rets.rolling(vol_window, min_periods=vol_window).std() * np.sqrt(trading_days_per_year)
    return vol


# ---------------------------------------------------------------------------
# Percentile signal helpers (pure functions)
# ---------------------------------------------------------------------------


def _compute_rolling_percentile(
    series: pd.Series,
    window: int,
) -> pd.Series:
    """Compute rolling percentile rank (0-100) of each value within its window.

    Parameters
    ----------
    series : pd.Series
        Numeric series sorted chronologically.
    window : int
        Rolling window in periods. Each value's percentile is computed against
        the preceding ``window`` values (including itself).

    Returns
    -------
    pd.Series
        Percentile rank 0-100.  First ``window-1`` values are NaN.
    """
    return series.rolling(window, min_periods=window).apply(
        lambda x: (x < x.iloc[-1]).mean() * 100.0,  # type: ignore[union-attr]
        raw=False,
    )


def _rolling_percentile_signal(
    series: pd.Series,
    window: int,
    threshold: float,
) -> pd.Series:
    """Return boolean signal when series exceeds its rolling ``threshold``-th percentile.

    Parameters
    ----------
    series : pd.Series
        Numeric series (e.g. ATR, realised vol).
    window : int
        Rolling window for percentile computation.
    threshold : float
        Percentile threshold 0-100. Signal=True when value crosses above this percentile.

    Returns
    -------
    pd.Series
        Boolean signal.  NaN where insufficient data.
    """
    pct = _compute_rolling_percentile(series, window)
    return pct >= threshold


# ---------------------------------------------------------------------------
# ATR-after-turnover signal (pure function)
# ---------------------------------------------------------------------------


def _recent_concentration_elevated(
    concentration_series: pd.Series,
    lookback_days: int,
    threshold: float,
) -> pd.Series:
    """Check whether concentration was elevated at any point in the preceding window.

    For each day *t*, returns ``True`` if any of ``concentration[t-lookback_days : t]``
    exceeds *threshold*.  This uses **only past/contemporaneous data** (no future leak).

    Parameters
    ----------
    concentration_series : pd.Series
        Index-aligned with the main DataFrame; contains top5_share values.
    lookback_days : int
        Number of preceding trading days to check.
    threshold : float
        Concentration threshold (e.g. 0.45).

    Returns
    -------
    pd.Series
        Boolean series.  First ``lookback_days-1`` rows are False by definition
        (insufficient history).
    """
    n = len(concentration_series)
    result = pd.Series(False, index=concentration_series.index, dtype=bool)

    values = concentration_series.values
    for i in range(n):
        start = max(0, i - lookback_days)
        if np.nanmax(values[start : i + 1]) >= threshold:
            result.iloc[i] = True

    return result


# ---------------------------------------------------------------------------
# Main signal computation
# ---------------------------------------------------------------------------


def compute_volatility_signals(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    atr_rolling_days: int = DEFAULT_ATR_ROLLING_DAYS,
    atr_percentile: float = DEFAULT_ATR_PERCENTILE,
    vol_rolling_days: int = DEFAULT_VOL_ROLLING_DAYS,
    vol_percentile: float = DEFAULT_VOL_PERCENTILE,
    turnover_lookback_days: int = DEFAULT_TURNOVER_LOOKBACK_DAYS,
    concentration_threshold: float = DEFAULT_CONCENTRATION_THRESHOLD,
) -> pd.DataFrame:
    """Compute all volatility / ATR expansion signals for SSE Composite Index.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    atr_rolling_days : int
        Rolling window for ATR percentile computation.
    atr_percentile : float
        Percentile threshold for ATR signal (0–100).
    vol_rolling_days : int
        Rolling window for realised-vol percentile computation.
    vol_percentile : float
        Percentile threshold for realised-vol signal (0–100).
    turnover_lookback_days : int
        How many days back to check for elevated concentration.
    concentration_threshold : float
        Concentration (top5_share) threshold for the turnover gate.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``atr``, ``atr_percentile``, ``atr_signal``,
        ``realized_vol``, ``vol_percentile``, ``vol_signal``,
        ``joint_vol_signal``, ``concentration_elevated``,
        ``atr_after_turnover_signal``.
    """
    # 1. Load SSE ATR + close data
    df = _load_sse_atr_and_close(duckdb_path)

    # 2. Compute realised volatility (annualised)
    df["realized_vol"] = compute_realized_vol(df, vol_window=vol_rolling_days)

    # 3. Compute ATR rolling percentile and signal
    df["atr_percentile"] = _compute_rolling_percentile(df["atr"], atr_rolling_days)
    df["atr_signal"] = df["atr_percentile"] >= atr_percentile

    # 4. Compute realised-vol rolling percentile and signal
    df["vol_percentile"] = _compute_rolling_percentile(
        df["realized_vol"], vol_rolling_days
    )
    df["vol_signal"] = df["vol_percentile"] >= vol_percentile

    # 5. Joint signal: both ATR and realised vol elevated
    df["joint_vol_signal"] = df["atr_signal"] & df["vol_signal"]

    # 6. Load concentration series and compute turnover gate
    from .tune_escape_top import _load_concentration_series  # noqa: PLC0415

    df_conc = _load_concentration_series(duckdb_path)
    df = df.merge(
        df_conc[["trade_date", "top5_share"]],
        on="trade_date",
        how="left",
    )

    df["concentration_elevated"] = _recent_concentration_elevated(
        df["top5_share"],
        lookback_days=turnover_lookback_days,
        threshold=concentration_threshold,
    )

    # 7. ATR after turnover: ATR elevated AND concentration was high recently
    df["atr_after_turnover_signal"] = (
        df["atr_signal"] & df["concentration_elevated"]
    )

    return df


# ---------------------------------------------------------------------------
# Forward-drawdown evaluation
# ---------------------------------------------------------------------------


def compute_forward_drawdowns_from_df(
    df: pd.DataFrame,
    *,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    """Compute forward drawdowns from close prices already in the DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain ``trade_date`` and ``close`` columns, sorted ascending.
    horizons : list[int] or None
        Forward windows in trading days. Default ``[20, 60, 120]``.

    Returns
    -------
    pd.DataFrame
        Same columns plus ``fwd_dd_{H}d`` for each horizon.
        Last *H* rows have NaN drawdowns (no future data available).
    """
    if horizons is None:
        horizons = FORWARD_HORIZONS

    out = df.copy()
    close = out["close"].values
    n = len(out)

    for h in horizons:
        dd = np.full(n, np.nan)
        for i in range(n):
            if i + 1 < n:
                future = close[i + 1 : min(i + h + 1, n)]
                if len(future) > 0:
                    dd[i] = float(np.min(future) / close[i] - 1.0)  # type: ignore[arg-type]
        out[f"fwd_dd_{h}d"] = dd

    return out


@dataclass(frozen=True)
class SignalEvalResult:
    """Evaluation result for one signal variant across all horizons."""

    signal_name: str
    n_signal_days: int
    n_total_valid: int
    horizon_results: list[HorizonEval] = field(default_factory=list)


@dataclass(frozen=True)
class HorizonEval:
    """Evaluation result for one signal at one horizon."""

    horizon_days: int
    n_signal: int
    mean_fwd_dd_signal: float | None
    mean_fwd_dd_no_signal: float | None
    direction_ok: bool
    welch_t_stat: float | None
    welch_p_value: float | None


def _welch_ttest(
    a: np.ndarray,
    b: np.ndarray,
) -> tuple[float, float]:
    """Welch's t-test (unequal variance). Returns (t_stat, p_value)."""
    from scipy import stats  # noqa: PLC0415

    t_stat, p_value = stats.ttest_ind(a, b, equal_var=False, nan_policy="omit")
    return float(t_stat), float(p_value)


def evaluate_signal(
    signal: pd.Series,
    df_fwd: pd.DataFrame,
    *,
    signal_name: str = "volatility_signal",
    horizons: list[int] | None = None,
) -> SignalEvalResult:
    """Evaluate a binary signal series against forward drawdowns.

    Parameters
    ----------
    signal : pd.Series
        Boolean signal series, index-aligned with *df_fwd*.
    df_fwd : pd.DataFrame
        Must contain ``fwd_dd_{H}d`` columns for each horizon.
    signal_name : str
        Label for the result.
    horizons : list[int] or None
        Forward horizons. Default ``[20, 60, 120]``.

    Returns
    -------
    SignalEvalResult
        Per-horizon metrics.
    """
    if horizons is None:
        horizons = FORWARD_HORIZONS

    mask_signal = signal.fillna(False).values
    n_signal_days = int(mask_signal.sum())

    # Valid rows: signal is not NaN and forward DD is not NaN
    valid_mask = pd.Series(True, index=df_fwd.index)
    for h in horizons:
        col = f"fwd_dd_{h}d"
        if col in df_fwd.columns:
            valid_mask = valid_mask & df_fwd[col].notna()

    n_total_valid = int(valid_mask.sum())

    horizon_results: list[HorizonEval] = []
    for h in horizons:
        col = f"fwd_dd_{h}d"
        if col not in df_fwd.columns:
            continue

        valid = df_fwd[col].notna() & signal.notna()
        sig_valid = signal[valid].fillna(False)
        dd = df_fwd.loc[valid, col]

        dd_signal = dd[sig_valid.values]
        dd_no_signal = dd[~sig_valid.values]

        n_sig = len(dd_signal)
        mean_sig = float(dd_signal.mean()) if n_sig > 0 else None
        mean_no = float(dd_no_signal.mean()) if len(dd_no_signal) > 0 else None
        direction_ok = (
            mean_sig is not None
            and mean_no is not None
            and mean_sig < mean_no
        )

        t_stat = None
        p_value = None
        if n_sig >= 5 and len(dd_no_signal) >= 5:
            try:
                t_stat, p_value = _welch_ttest(
                    dd_signal.dropna().values,
                    dd_no_signal.dropna().values,
                )
            except Exception:
                pass

        horizon_results.append(
            HorizonEval(
                horizon_days=h,
                n_signal=n_sig,
                mean_fwd_dd_signal=mean_sig,
                mean_fwd_dd_no_signal=mean_no,
                direction_ok=direction_ok,
                welch_t_stat=t_stat,
                welch_p_value=p_value,
            )
        )

    return SignalEvalResult(
        signal_name=signal_name,
        n_signal_days=n_signal_days,
        n_total_valid=n_total_valid,
        horizon_results=horizon_results,
    )


# ---------------------------------------------------------------------------
# Full validation pipeline
# ---------------------------------------------------------------------------


def run_full_validation(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    atr_rolling_days: int = DEFAULT_ATR_ROLLING_DAYS,
    atr_percentile: float = DEFAULT_ATR_PERCENTILE,
    vol_rolling_days: int = DEFAULT_VOL_ROLLING_DAYS,
    vol_percentile: float = DEFAULT_VOL_PERCENTILE,
    turnover_lookback_days: int = DEFAULT_TURNOVER_LOOKBACK_DAYS,
    concentration_threshold: float = DEFAULT_CONCENTRATION_THRESHOLD,
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """Run the full validation pipeline: compute signals, evaluate against
    forward drawdowns, and return a structured result.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    atr_rolling_days : int
        Rolling window for ATR percentile.
    atr_percentile : float
        Percentile threshold for ATR signal.
    vol_rolling_days : int
        Rolling window for realised-vol percentile.
    vol_percentile : float
        Percentile threshold for realised-vol signal.
    turnover_lookback_days : int
        Days to look back for concentration gate.
    concentration_threshold : float
        Concentration threshold for turnover gate.
    horizons : list[int] or None
        Forward horizons for evaluation.

    Returns
    -------
    dict
        ``parameters``, ``data_summary``, ``signal_comparison``,
        ``atr_vs_realized_vol_comparison``, ``forward_drawdown_eval``.
    """
    if horizons is None:
        horizons = FORWARD_HORIZONS

    # 1. Compute signals
    df = compute_volatility_signals(
        duckdb_path,
        atr_rolling_days=atr_rolling_days,
        atr_percentile=atr_percentile,
        vol_rolling_days=vol_rolling_days,
        vol_percentile=vol_percentile,
        turnover_lookback_days=turnover_lookback_days,
        concentration_threshold=concentration_threshold,
    )

    # 2. Compute forward drawdowns
    df_fwd = compute_forward_drawdowns_from_df(df, horizons=horizons)

    # 3. Evaluate each signal variant
    atr_eval = evaluate_signal(
        df_fwd["atr_signal"], df_fwd, signal_name="atr_standalone", horizons=horizons
    )
    vol_eval = evaluate_signal(
        df_fwd["vol_signal"], df_fwd, signal_name="realized_vol_standalone", horizons=horizons
    )
    joint_eval = evaluate_signal(
        df_fwd["joint_vol_signal"], df_fwd, signal_name="atr_and_vol_joint", horizons=horizons
    )
    atr_turnover_eval = evaluate_signal(
        df_fwd["atr_after_turnover_signal"],
        df_fwd,
        signal_name="atr_after_turnover",
        horizons=horizons,
    )

    # 4. Build result
    def _eval_to_dict(ev: SignalEvalResult) -> dict[str, Any]:
        return {
            "signal_name": ev.signal_name,
            "n_signal_days": ev.n_signal_days,
            "n_total_valid": ev.n_total_valid,
            "signal_pct": (
                round(ev.n_signal_days / ev.n_total_valid * 100, 2)
                if ev.n_total_valid > 0
                else 0.0
            ),
            "horizons": {
                f"{h.horizon_days}d": {
                    "n_signal": h.n_signal,
                    "mean_fwd_dd_signal": (
                        round(h.mean_fwd_dd_signal, 6) if h.mean_fwd_dd_signal is not None else None
                    ),
                    "mean_fwd_dd_no_signal": (
                        round(h.mean_fwd_dd_no_signal, 6) if h.mean_fwd_dd_no_signal is not None else None
                    ),
                    "direction_ok": h.direction_ok,
                    "welch_t_stat": round(h.welch_t_stat, 4) if h.welch_t_stat is not None else None,
                    "welch_p_value": round(h.welch_p_value, 4) if h.welch_p_value is not None else None,
                }
                for h in ev.horizon_results
            },
        }

    return {
        "parameters": {
            "atr_rolling_days": atr_rolling_days,
            "atr_percentile": atr_percentile,
            "vol_rolling_days": vol_rolling_days,
            "vol_percentile": vol_percentile,
            "turnover_lookback_days": turnover_lookback_days,
            "concentration_threshold": concentration_threshold,
            "forward_horizons": horizons,
        },
        "data_summary": {
            "start_date": str(df["trade_date"].min().date()),
            "end_date": str(df["trade_date"].max().date()),
            "n_trading_days": len(df),
            "atr_mean": round(float(df["atr"].mean()), 4),
            "atr_latest": round(float(df["atr"].iloc[-1]), 4),
            "realized_vol_mean": round(float(df["realized_vol"].dropna().mean()), 4),
            "realized_vol_latest": (
                round(float(df["realized_vol"].dropna().iloc[-1]), 4)
                if df["realized_vol"].notna().any()
                else None
            ),
        },
        "signal_comparison": {
            "atr_standalone": _eval_to_dict(atr_eval),
            "realized_vol_standalone": _eval_to_dict(vol_eval),
            "atr_and_vol_joint": _eval_to_dict(joint_eval),
            "atr_after_turnover": _eval_to_dict(atr_turnover_eval),
        },
        "atr_vs_realized_vol_comparison": _compare_atr_vs_vol(
            df_fwd, horizons, atr_percentile, vol_percentile
        ),
    }


def _compare_atr_vs_vol(
    df_fwd: pd.DataFrame,
    horizons: list[int],
    atr_percentile: float,
    vol_percentile: float,
) -> dict[str, Any]:
    """Compare ATR-field vs computed-realised-vol in directional strength."""
    comparison: dict[str, Any] = {
        "method": "Compare mean forward DD after ATR signal vs realised-vol signal across horizons",
        "atr_percentile": atr_percentile,
        "vol_percentile": vol_percentile,
        "horizons": {},
    }

    atr_sig = df_fwd["atr_signal"].fillna(False)
    vol_sig = df_fwd["vol_signal"].fillna(False)

    for h in horizons:
        col = f"fwd_dd_{h}d"
        if col not in df_fwd.columns:
            continue

        valid = df_fwd[col].notna() & atr_sig.notna() & vol_sig.notna()
        dd = df_fwd.loc[valid, col]

        atr_dd = dd[atr_sig[valid].values]
        vol_dd = dd[vol_sig[valid].values]
        joint_dd = dd[(atr_sig[valid] & vol_sig[valid]).values]
        atr_only_dd = dd[(atr_sig[valid] & ~vol_sig[valid]).values]
        vol_only_dd = dd[(~atr_sig[valid] & vol_sig[valid]).values]

        comparison["horizons"][f"{h}d"] = {
            "atr_signal": {
                "n": len(atr_dd),
                "mean_fwd_dd": round(float(atr_dd.mean()), 6) if len(atr_dd) > 0 else None,
            },
            "vol_signal": {
                "n": len(vol_dd),
                "mean_fwd_dd": round(float(vol_dd.mean()), 6) if len(vol_dd) > 0 else None,
            },
            "joint_both_elevated": {
                "n": len(joint_dd),
                "mean_fwd_dd": round(float(joint_dd.mean()), 6) if len(joint_dd) > 0 else None,
            },
            "atr_only": {
                "n": len(atr_only_dd),
                "mean_fwd_dd": round(float(atr_only_dd.mean()), 6) if len(atr_only_dd) > 0 else None,
            },
            "vol_only": {
                "n": len(vol_only_dd),
                "mean_fwd_dd": round(float(vol_only_dd.mean()), 6) if len(vol_only_dd) > 0 else None,
            },
        }
    return comparison