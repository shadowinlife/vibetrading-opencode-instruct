"""
Hyperparameter tuning engine for escape-top warning indicators.

Optimises ``concentration_threshold`` and ``divergence_lookback_days``
against forward drawdowns of the Shanghai Composite Index (000001.SH).

Workflow
--------
1. Compute forward drawdowns of SSE close over configurable horizons.
2. Generate daily binary labels ("large drawdown ahead").
3. For each parameter combination, compute the joint escape-top signal
   (concentration hit AND margin/SSE divergence) as a daily time series.
4. Evaluate signals against forward drawdowns and rank by warning quality.

Objective
---------
Parameter sets are ranked primarily by mean forward drawdown after signal
fires (more negative = earlier / better warning before large drawdowns).
"""

from __future__ import annotations

from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .base import format_date, get_connection
from .metadata import CONCENTRATION_TOP_PCT, DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE

# ── Forward drawdowns ────────────────────────────────────────────────────────


def compute_forward_drawdowns(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    horizons: list[int] | None = None,
) -> pd.DataFrame:
    """Load SSE close and compute forward max drawdowns.

    For each trading day *t*, the forward drawdown over horizon *H* is::

        min(close[t+1 .. t+H]) / close[t] − 1

    A value of −0.10 means the SSE declined at least 10 % from *close[t]*
    within the next *H* trading days.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    horizons : list[int] or None
        Forward windows in trading days.  Default ``[20, 60, 120]``.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``close``, ``fwd_dd_20d``, ``fwd_dd_60d``,
        ``fwd_dd_120d``.
    """
    if horizons is None:
        horizons = [20, 60, 120]

    con = get_connection(duckdb_path, read_only=True)
    df = con.execute(
        "SELECT trade_date, close FROM idx_factor_pro "
        "WHERE ts_code = ? ORDER BY trade_date",
        [SSE_INDEX_CODE],
    ).fetchdf()
    con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)

    close = df["close"].values
    n = len(df)

    for h in horizons:
        dd = np.full(n, np.nan)
        for i in range(n):
            if i + 1 < n:
                future = close[i + 1 : min(i + h + 1, n)]
                if len(future) > 0:
                    dd[i] = float(np.min(future) / close[i] - 1.0)  # type: ignore[arg-type]
        df[f"fwd_dd_{h}d"] = dd

    return df


def generate_labels(
    df_sse: pd.DataFrame,
    *,
    horizons: list[int] | None = None,
    thresholds: dict[int, float] | None = None,
) -> pd.DataFrame:
    """Generate binary drawdown labels from forward drawdown data.

    Parameters
    ----------
    df_sse : pd.DataFrame
        Output of :func:`compute_forward_drawdowns`.
    horizons : list[int] or None
        Forward horizons.  Default ``[20, 60, 120]``.
    thresholds : dict[int, float] or None
        Map *horizon* → *drawdown threshold* (negative float).  A label is
        ``True`` when the forward drawdown ≤ threshold.
        Default: ``{20: −0.03, 60: −0.05, 120: −0.08}``.

    Returns
    -------
    pd.DataFrame
        Copy of *df_sse* with additional ``label_Hd`` boolean columns.
    """
    if horizons is None:
        horizons = [20, 60, 120]
    if thresholds is None:
        thresholds = {20: -0.03, 60: -0.05, 120: -0.08}

    df = df_sse.copy()
    for h in horizons:
        col = f"fwd_dd_{h}d"
        if col not in df.columns:
            continue
        df[f"label_{h}d"] = df[col] <= thresholds.get(h, -0.05)
    return df


# ── Concentration time series (internal) ─────────────────────────────────────


def _load_concentration_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    top_pct: float = CONCENTRATION_TOP_PCT,
) -> pd.DataFrame:
    """Return daily top-N% turnover concentration share as a DataFrame.

    Reuses the existing ``compute_concentration`` function and extracts its
    ``daily_series`` list.
    """

    # local import avoids circularity at package-init time
    from .concentration import compute_concentration  # noqa: PLC0415

    result = compute_concentration(duckdb_path, top_pct=top_pct)
    daily = pd.DataFrame(result["daily_series"])
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    return pd.DataFrame(
        daily[["trade_date", "top5_share"]]
        .sort_values("trade_date")  # type: ignore[call-overload]
        .reset_index(drop=True)
    )


# ── Margin-buy / SSE divergence time series (internal) ───────────────────────


def _load_margin_divergence_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    divergence_lookback_days: int = 20,
) -> pd.DataFrame:
    """Return daily margin-buy ratio and divergence-hit time series.

    Queries ``stk_margin``, ``stk_factor_pro`` and ``idx_factor_pro``
    directly (mirrors the logic in ``compute_margin_buy_vs_sse``) so the
    lookback window can be parameterised independently.
    """
    con = get_connection(duckdb_path, read_only=True)
    query = """
    WITH
        turnover AS (
            SELECT trade_date, SUM(amount) AS total_amount_kcy
            FROM stk_factor_pro
            WHERE amount IS NOT NULL AND amount > 0
            GROUP BY trade_date
        ),
        margin AS (
            SELECT trade_date,
                   SUM(rzmre) AS total_rzmre_yuan,
                   SUM(rzye)   AS total_rzye_yuan
            FROM stk_margin GROUP BY trade_date
        ),
        sse AS (
            SELECT trade_date, close AS sse_close
            FROM idx_factor_pro WHERE ts_code = '000001.SH'
        )
    SELECT
        m.trade_date,
        m.total_rzye_yuan,
        t.total_amount_kcy,
        s.sse_close,
        m.total_rzmre_yuan / (t.total_amount_kcy * 1000.0) AS margin_buy_ratio
    FROM margin m
    JOIN turnover t ON m.trade_date = t.trade_date
    JOIN sse s     ON m.trade_date = s.trade_date
    WHERE t.total_amount_kcy > 0
    ORDER BY m.trade_date
    """
    df = con.execute(query).fetchdf()
    con.close()

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    lb = divergence_lookback_days

    df[f"ratio_{lb}d_change"] = (
        df["margin_buy_ratio"] / df["margin_buy_ratio"].shift(lb) - 1.0
    )
    df[f"rzye_{lb}d_change"] = (
        df["total_rzye_yuan"] / df["total_rzye_yuan"].shift(lb) - 1.0
    )

    df["divergence_hit"] = (
        (df[f"rzye_{lb}d_change"] > 0) & (df[f"ratio_{lb}d_change"] < 0)
    )

    return pd.DataFrame(
        df[
            [
                "trade_date",
                "margin_buy_ratio",
                "total_rzye_yuan",
                "sse_close",
                "divergence_hit",
            ]
        ]
    )


# ── Joint signal builder ─────────────────────────────────────────────────────


def compute_signal_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    concentration_threshold: float = 0.45,
    divergence_lookback_days: int = 20,
    concentration_top_pct: float = CONCENTRATION_TOP_PCT,
    df_conc: pd.DataFrame | None = None,
    df_div: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build a daily joint escape-top signal time series.

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB.  Ignored when *df_conc* / *df_div* are supplied.
    concentration_threshold : float
        ``top5_share`` above which the concentration leg fires.
    divergence_lookback_days : int
        Trading-day lookback for margin/SSE divergence.
    concentration_top_pct : float
        Top-% of stocks for concentration computation.
    df_conc : pd.DataFrame or None
        Precomputed concentration series (avoids re-querying DB).
        Must have ``trade_date`` and ``top5_share`` columns.
    df_div : pd.DataFrame or None
        Precomputed divergence series.  Must have ``trade_date``,
        ``margin_buy_ratio`` and ``divergence_hit`` columns.

    Returns
    -------
    pd.DataFrame
        ``trade_date`, ``top5_share``, ``concentration_hit``,
        ``divergence_hit``, ``joint_signal``, ``margin_buy_ratio``.
    """
    if df_conc is None:
        df_conc = _load_concentration_series(duckdb_path, top_pct=concentration_top_pct)
    if df_div is None:
        df_div = _load_margin_divergence_series(
            duckdb_path, divergence_lookback_days=divergence_lookback_days
        )

    df = df_conc.merge(
        df_div[["trade_date", "margin_buy_ratio", "divergence_hit"]],
        on="trade_date",
        how="inner",
    )

    df["concentration_hit"] = df["top5_share"] >= concentration_threshold
    df["joint_signal"] = df["concentration_hit"] & df["divergence_hit"]

    cols = [
        "trade_date",
        "top5_share",
        "concentration_hit",
        "divergence_hit",
        "joint_signal",
        "margin_buy_ratio",
    ]
    return pd.DataFrame(df[[c for c in cols if c in df.columns]])


# ── Single-parameter evaluation ──────────────────────────────────────────────


def _eval_params(
    df_merged: pd.DataFrame,
    horizons: list[int],
    dd_thresholds: dict[int, float],
) -> dict[str, Any]:
    """Evaluate a parameter combination's signal against forward drawdowns.

    Parameters
    ----------
    df_merged : pd.DataFrame
        Must contain ``joint_signal`` and ``fwd_dd_{H}d`` columns.
    horizons : list[int]
        Forward drawdown horizons.
    dd_thresholds : dict[int, float]
        Drawdown thresholds for binary label generation.

    Returns
    -------
    dict
        Per-horizon metrics plus ``_composite_dd`` for ranking.
    """
    df = generate_labels(df_merged, horizons=horizons, thresholds=dd_thresholds)
    results: dict[str, Any] = {}

    for h in horizons:
        dd_col = f"fwd_dd_{h}d"
        label_col = f"label_{h}d"
        if dd_col not in df.columns:
            continue

        signal_mask = df["joint_signal"].fillna(False)
        signal_rows = df[signal_mask]
        non_signal_rows = df[~signal_mask]
        valid = df[dd_col].notna()

        n_signal = int(signal_mask.sum())
        n_valid = int(valid.sum())

        if n_signal == 0:
            results[f"horizon_{h}d"] = {
                "n_signal": 0,
                "n_valid": n_valid,
                "mean_fwd_dd_signal": None,
                "mean_fwd_dd_no_signal": float(non_signal_rows[dd_col].mean())
                if len(non_signal_rows) > 0
                else None,
                "signal_pct": 0.0,
                "precision": None,
                "recall": None,
                "f1": None,
            }
            continue

        mean_dd_signal = float(signal_rows[dd_col].mean())
        mean_dd_no = (
            float(non_signal_rows[dd_col].mean())
            if len(non_signal_rows) > 0
            else None
        )

        # Binary classification metrics (using future-drawdown labels)
        valid_labels = df[label_col].notna()
        if valid_labels.sum() > 0:
            tp = int((signal_mask & df[label_col] & valid_labels).sum())
            fp = int((signal_mask & ~df[label_col] & valid_labels).sum())
            fn = int((~signal_mask & df[label_col] & valid_labels).sum())
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = (
                (2 * precision * recall / (precision + recall))
                if (precision + recall) > 0
                else 0.0
            )
        else:
            precision = recall = f1 = None

        results[f"horizon_{h}d"] = {
            "n_signal": n_signal,
            "n_valid": n_valid,
            "mean_fwd_dd_signal": mean_dd_signal,
            "mean_fwd_dd_no_signal": mean_dd_no,
            "signal_pct": (
                float(n_signal / n_valid * 100) if n_valid > 0 else 0.0
            ),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    # Composite: average forward drawdown after signal across horizons.
    # More negative → better warning before larger drawdowns.
    dd_vals = [
        results[f"horizon_{h}d"]["mean_fwd_dd_signal"]
        for h in horizons
        if f"horizon_{h}d" in results
        and results[f"horizon_{h}d"]["mean_fwd_dd_signal"] is not None
    ]
    results["_composite_dd"] = float(np.mean(dd_vals)) if dd_vals else None

    return results


# ── Grid search ──────────────────────────────────────────────────────────────


def grid_search(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    concentration_thresholds: list[float] | None = None,
    divergence_lookbacks: list[int] | None = None,
    horizons: list[int] | None = None,
    dd_thresholds: dict[int, float] | None = None,
    concentration_top_pct: float = CONCENTRATION_TOP_PCT,
    min_signals: int = 0,
) -> dict[str, Any]:
    """Grid-search escape-top parameters against forward SSE drawdowns.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    concentration_thresholds : list[float] or None
        Thresholds for the concentration leg.
        Default: ``[0.40, 0.43, 0.45, 0.48, 0.50]``.
    divergence_lookbacks : list[int] or None
        Lookback windows (trading days) for margin/SSE divergence.
        Default: ``[10, 15, 20, 30, 40]``.
    horizons : list[int] or None
        Forward drawdown horizons.  Default: ``[20, 60, 120]``.
    dd_thresholds : dict[int, float] or None
        Drawdown thresholds for labels.
        Default: ``{20: −0.03, 60: −0.05, 120: −0.08}``.
    concentration_top_pct : float
        Top-% of stocks for concentration (default 5.0).
    min_signals : int
        Minimum number of joint-signal days required for a parameter
        combination to be eligible for the robust ranking. Default 0.

    Returns
    -------
    dict
        ``sse_summary``, ``grid_results``, ``best_params``, ``top_ranked``.
    """
    if concentration_thresholds is None:
        concentration_thresholds = [0.40, 0.43, 0.45, 0.48, 0.50]
    if divergence_lookbacks is None:
        divergence_lookbacks = [10, 15, 20, 30, 40]
    if horizons is None:
        horizons = [20, 60, 120]
    if dd_thresholds is None:
        dd_thresholds = {20: -0.03, 60: -0.05, 120: -0.08}

    # 1. Load shared data once
    print(f"[tune] Loading SSE close with forward drawdowns …", flush=True)
    df_sse = compute_forward_drawdowns(duckdb_path, horizons=horizons)

    print(f"[tune] Loading concentration series …", flush=True)
    df_conc = _load_concentration_series(duckdb_path, top_pct=concentration_top_pct)

    # Cache divergence series per unique lookback
    print(f"[tune] Loading margin divergence series for {len(set(divergence_lookbacks))} lookbacks …", flush=True)
    _div_cache: dict[int, pd.DataFrame] = {}
    for lb in sorted(set(divergence_lookbacks)):
        _div_cache[lb] = _load_margin_divergence_series(
            duckdb_path, divergence_lookback_days=lb
        )

    # 2. Grid evaluation
    grid: list[dict[str, Any]] = []
    total = len(concentration_thresholds) * len(divergence_lookbacks)
    count = 0

    for conc_thresh in concentration_thresholds:
        for div_lb in divergence_lookbacks:
            count += 1
            print(
                f"  [{count:2d}/{total}] conc_thresh={conc_thresh:.2f} "
                f"div_lb={div_lb:2d}",
                flush=True,
            )

            df_div = _div_cache[div_lb]
            df_sig = compute_signal_series(
                duckdb_path,
                concentration_threshold=conc_thresh,
                concentration_top_pct=concentration_top_pct,
                df_conc=df_conc,
                df_div=df_div,
            )

            # Merge with SSE forward drawdowns
            dd_cols = ["trade_date"] + [f"fwd_dd_{h}d" for h in horizons]
            df_merged = df_sig.merge(df_sse[dd_cols], on="trade_date", how="inner")

            metrics = _eval_params(df_merged, horizons, dd_thresholds)
            grid.append({
                "concentration_threshold": conc_thresh,
                "divergence_lookback_days": div_lb,
                "n_signals": int(df_merged["joint_signal"].sum()),
                "metrics": metrics,
            })

    # 3. Rank by composite drawdown after signal (more negative = better)
    grid.sort(key=lambda r: r["metrics"].get("_composite_dd") or 0.0)

    robust_grid = [r for r in grid if r["n_signals"] >= min_signals]
    robust_grid.sort(key=lambda r: r["metrics"].get("_composite_dd") or 0.0)

    ranked_grid = robust_grid if robust_grid else grid

    return {
        "sse_summary": {
            "start_date": format_date(pd.Timestamp(df_sse["trade_date"].min())),  # type: ignore[arg-type]
            "end_date": format_date(pd.Timestamp(df_sse["trade_date"].max())),  # type: ignore[arg-type]
            "n_days": len(df_sse),
            "horizons": horizons,
            "dd_thresholds": {str(k): v for k, v in dd_thresholds.items()},
            "ssi_close_start": float(df_sse["close"].iloc[0]),
            "ssi_close_latest": float(df_sse["close"].iloc[-1]),
        },
        "concentration_thresholds": concentration_thresholds,
        "divergence_lookbacks": divergence_lookbacks,
        "min_signals": min_signals,
        "grid_results": grid,
        "best_params": {
            "concentration_threshold": ranked_grid[0]["concentration_threshold"] if ranked_grid else None,
            "divergence_lookback_days": ranked_grid[0]["divergence_lookback_days"] if ranked_grid else None,
            "composite_dd_after_signal": ranked_grid[0]["metrics"].get("_composite_dd") if ranked_grid else None,
            "n_signals": ranked_grid[0]["n_signals"] if ranked_grid else None,
            "metrics": ranked_grid[0]["metrics"] if ranked_grid else {},
            "used_robust_filter": bool(min_signals > 0 and len(robust_grid) > 0),
        },
        "robust_grid_results": robust_grid,
        "top_ranked": [
            {
                "rank": i + 1,
                "concentration_threshold": r["concentration_threshold"],
                "divergence_lookback_days": r["divergence_lookback_days"],
                "composite_dd_after_signal": r["metrics"].get("_composite_dd"),
                "n_signals": r["n_signals"],
            }
            for i, r in enumerate(ranked_grid[:5])
        ],
    }
