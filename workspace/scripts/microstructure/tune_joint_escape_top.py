"""
Hyperparameter tuning engine for ensemble escape-top warning resolution.

Optimises ensemble parameters (VOTE_K_OF_M thresholds, WEIGHTED_SCORE weights
and score thresholds) against forward drawdowns of the Shanghai Composite
Index (000001.SH) using a walk-forward train/test split.

Workflow
--------
1. Load daily signal series for the 2 VALIDATED conditions:
   - margin_divergence (condition #2)
   - volatility_atr_expansion (condition #5)
2. Compute forward drawdowns of SSE close over [20, 60, 120] trading days.
3. Build an ensemble parameter grid:
   - VOTE_K_OF_M: k_red ∈ [1, 2] (k_yellow = 1)
   - WEIGHTED_SCORE: weight ratios × red/yellow thresholds
   - AND mode (baseline reference)
4. Walk-forward: rank by train 2010-2018, evaluate OOS on test 2019-2026.
5. Sensitivity: ±10% threshold perturbation for best config.
6. Compare composite DD, signal count, precision against baseline.

Baseline
--------
``strong`` preset: AND mode, concentration=0.50 + margin_divergence 40d
  - composite DD = -0.0591, 11 signals
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from .base import format_date, get_connection
from .ensemble import ConditionResult, EnsembleConfig, resolve_ensemble
from .metadata import CONCENTRATION_TOP_PCT, DEFAULT_DUCKDB_PATH, SSE_INDEX_CODE
from .tune_escape_top import (
    _load_concentration_series,
    _load_margin_divergence_series,
    compute_forward_drawdowns,
    generate_labels,
)

# ── Constants ────────────────────────────────────────────────────────────────

_TRAIN_START = "2010-01-04"
_TRAIN_END = "2018-12-31"
_TEST_START = "2019-01-01"
_TEST_END = "2026-05-27"

_DEFAULT_HORIZONS = [20, 60, 120]
_DEFAULT_DD_THRESHOLDS: dict[int, float] = {20: -0.03, 60: -0.05, 120: -0.08}

# Minimum signals required for robustness.
_MIN_SIGNALS = 10

# Sensitivity perturbation fraction.
_SENS_PERTURB = 0.10


# ── Signal loaders ───────────────────────────────────────────────────────────


def load_validated_condition_signals(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    divergence_lookback_days: int = 40,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the daily signal series for the 2 validated escape-top conditions.

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB.
    divergence_lookback_days : int
        Lookback window for margin/SSE divergence (default 40).

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame]
        (margin_divergence_df, volatility_df).  Each contains at least
        ``trade_date`` and ``signal`` columns.
    """
    # ── Condition #2: margin divergence ─────────────────────────────────
    df_margin = _load_margin_divergence_series(
        duckdb_path, divergence_lookback_days=divergence_lookback_days
    )
    df_margin["signal"] = df_margin["divergence_hit"]

    # ── Condition #5: volatility / ATR expansion ────────────────────────
    from .volatility_atr_expansion import compute_volatility_signals  # noqa: PLC0415

    df_vol = compute_volatility_signals(duckdb_path)
    df_vol["signal"] = df_vol["joint_vol_signal"]

    return (
        df_margin[["trade_date", "divergence_hit", "signal"]].copy(),
        df_vol[["trade_date", "joint_vol_signal", "signal"]].copy(),
    )


def _align_signals(
    df_margin: pd.DataFrame,
    df_vol: pd.DataFrame,
) -> pd.DataFrame:
    """Inner-join the two condition DataFrames on ``trade_date``.

    Returns a DataFrame with columns:
      ``trade_date``, ``margin_signal``, ``vol_signal``.
    """
    df = df_margin[["trade_date", "signal"]].rename(
        columns={"signal": "margin_signal"}
    ).merge(
        df_vol[["trade_date", "signal"]].rename(
            columns={"signal": "vol_signal"}
        ),
        on="trade_date",
        how="inner",
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values("trade_date").reset_index(drop=True)


# ── Ensemble signal builder ──────────────────────────────────────────────────


def compute_ensemble_signal_series(
    df_aligned: pd.DataFrame,
    config: EnsembleConfig,
) -> pd.Series:
    """Compute the ensemble warning level per trading day.

    Returns
    -------
    pd.Series
        Same index as *df_aligned*, values: ``"GREEN"``, ``"YELLOW"``, ``"RED"``.
    """
    levels: list[str] = []
    for _, row in df_aligned.iterrows():
        results = [
            ConditionResult("margin_divergence", hit=bool(row["margin_signal"])),
            ConditionResult("volatility_atr_expansion", hit=bool(row["vol_signal"])),
        ]
        levels.append(resolve_ensemble(results, config))
    return pd.Series(levels, index=df_aligned.index)


# ── Single-config evaluation ─────────────────────────────────────────────────


@dataclass
class ConfigEvalResult:
    """Evaluation result for a single ensemble configuration."""

    mode: str
    params: dict[str, Any]
    n_signals: int
    n_valid: int
    composite_dd: float | None
    horizon_metrics: dict[str, Any] = field(default_factory=dict)
    signal_pct: float = 0.0
    precision_60d: float | None = None


def _evaluate_config(
    df_merged: pd.DataFrame,
    config: EnsembleConfig,
    *,
    horizons: list[int],
    dd_thresholds: dict[int, float],
    mode_label: str,
    params: dict[str, Any],
) -> ConfigEvalResult:
    """Evaluate a single ensemble config against forward DD.

    Parameters
    ----------
    df_merged : pd.DataFrame
        Must contain ``margin_signal``, ``vol_signal`` + ``fwd_dd_{H}d`` columns
        (already aligned with forward drawdowns).
    config : EnsembleConfig
        The ensemble config to evaluate.
    horizons : list[int]
        Forward DD horizons.
    dd_thresholds : dict[int, float]
        Drawdown thresholds for binary label generation.
    mode_label : str
        Human-readable mode name for reporting.
    params : dict
        Config parameters serialised for reporting.

    Returns
    -------
    ConfigEvalResult
    """
    df = generate_labels(df_merged, horizons=horizons, thresholds=dd_thresholds)

    levels = compute_ensemble_signal_series(df, config)
    is_signal = levels.isin({"YELLOW", "RED"})
    n_signal = int(is_signal.sum())
    n_valid = len(df)

    if n_signal == 0:
        return ConfigEvalResult(
            mode=mode_label,
            params=params,
            n_signals=0,
            n_valid=n_valid,
            composite_dd=None,
        )

    # Per-horizon metrics.
    dd_vals: list[float] = []
    horizon_metrics: dict[str, Any] = {}

    for h in horizons:
        dd_col = f"fwd_dd_{h}d"
        label_col = f"label_{h}d"
        if dd_col not in df.columns:
            continue

        signal_mask = is_signal
        signal_rows = df[signal_mask][dd_col]
        non_signal_rows = df[~signal_mask][dd_col]

        mean_dd_signal = float(signal_rows.mean())
        mean_dd_no = float(non_signal_rows.mean()) if len(non_signal_rows) > 0 else None
        dd_vals.append(mean_dd_signal)

        # Binary classification metrics.
        valid = df[label_col].notna() & df[dd_col].notna()
        tp = int((signal_mask & df[label_col] & valid).sum())
        fp = int((signal_mask & ~df[label_col] & valid).sum())
        fn = int((~signal_mask & df[label_col] & valid).sum())
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            (2 * precision * recall / (precision + recall))
            if (precision + recall) > 0
            else 0.0
        )

        horizon_metrics[f"horizon_{h}d"] = {
            "mean_fwd_dd_signal": mean_dd_signal,
            "mean_fwd_dd_no_signal": mean_dd_no,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    composite_dd = float(np.mean(dd_vals)) if dd_vals else None
    signal_pct = float(n_signal / n_valid * 100) if n_valid > 0 else 0.0

    # Precision at 60d as a summary stat.
    prec_60 = horizon_metrics.get("horizon_60d", {}).get("precision")

    return ConfigEvalResult(
        mode=mode_label,
        params=params,
        n_signals=n_signal,
        n_valid=n_valid,
        composite_dd=composite_dd,
        horizon_metrics=horizon_metrics,
        signal_pct=signal_pct,
        precision_60d=prec_60,
    )


# ── Baseline computation ─────────────────────────────────────────────────────


def compute_baseline(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    horizons: list[int] | None = None,
    dd_thresholds: dict[int, float] | None = None,
    conc_threshold: float = 0.50,
    div_lookback: int = 40,
) -> dict[str, Any]:
    """Compute the baseline (AND mode: concentration + margin divergence).

    This matches the ``strong`` preset from ``tune_escape_top.grid_search``.

    Returns
    -------
    dict
        ``composite_dd``, ``n_signals``, ``horizon_metrics``.
    """
    if horizons is None:
        horizons = _DEFAULT_HORIZONS
    if dd_thresholds is None:
        dd_thresholds = _DEFAULT_DD_THRESHOLDS

    df_sse = compute_forward_drawdowns(duckdb_path, horizons=horizons)
    df_conc = _load_concentration_series(duckdb_path)
    df_div = _load_margin_divergence_series(
        duckdb_path, divergence_lookback_days=div_lookback
    )

    from .tune_escape_top import compute_signal_series  # noqa: PLC0415

    df_sig = compute_signal_series(
        duckdb_path,
        concentration_threshold=conc_threshold,
        concentration_top_pct=CONCENTRATION_TOP_PCT,
        df_conc=df_conc,
        df_div=df_div,
    )

    dd_cols = ["trade_date"] + [f"fwd_dd_{h}d" for h in horizons]
    df_merged = df_sig.merge(df_sse[dd_cols], on="trade_date", how="inner")
    df_labels = generate_labels(df_merged, horizons=horizons, thresholds=dd_thresholds)

    signal_mask = df_labels["joint_signal"].fillna(False)
    n_signals = int(signal_mask.sum())
    n_valid = len(df_labels)

    dd_vals: list[float] = []
    horizon_metrics: dict[str, Any] = {}
    for h in horizons:
        dd_col = f"fwd_dd_{h}d"
        label_col = f"label_{h}d"
        if dd_col not in df_labels.columns:
            continue
        mean_dd = float(df_labels.loc[signal_mask, dd_col].mean())
        dd_vals.append(mean_dd)

        valid = df_labels[label_col].notna()
        tp = int((signal_mask & df_labels[label_col] & valid).sum())
        fp = int((signal_mask & ~df_labels[label_col] & valid).sum())
        fn = int((~signal_mask & df_labels[label_col] & valid).sum())
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            (2 * precision * recall / (precision + recall))
            if (precision + recall) > 0
            else 0.0
        )
        horizon_metrics[f"horizon_{h}d"] = {
            "mean_fwd_dd_signal": mean_dd,
            "mean_fwd_dd_no_signal": float(df_labels.loc[~signal_mask, dd_col].mean()),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    composite_dd = float(np.mean(dd_vals)) if dd_vals else None

    return {
        "mode": "AND (baseline: conc + margin)",
        "params": {
            "concentration_threshold": conc_threshold,
            "divergence_lookback_days": div_lookback,
        },
        "n_signals": n_signals,
        "n_valid": n_valid,
        "composite_dd": composite_dd,
        "horizon_metrics": horizon_metrics,
        "signal_pct": float(n_signals / n_valid * 100) if n_valid > 0 else 0.0,
    }


# ── Parameter grid builders ──────────────────────────────────────────────────


def _build_vote_grid() -> list[tuple[EnsembleConfig, str, dict[str, Any]]]:
    """Build VOTE_K_OF_M parameter grid.

    M=2 (two validated conditions).  k_yellow=1 is the only meaningful
    value (ceil(2/3)=1, and k_yellow=2 would collapse YELLOW == RED).
    k_red in [1, 2]: OR-mode vs AND-mode RED.
    """
    configs: list[tuple[EnsembleConfig, str, dict[str, Any]]] = []
    for k_red in [1, 2]:
        config = EnsembleConfig(
            mode="VOTE_K_OF_M",
            k_yellow=1,
            k_red=k_red,
        )
        label = f"VOTE_K_OF_M(k_y=1,k_r={k_red})"
        params = {"k_yellow": 1, "k_red": k_red}
        configs.append((config, label, params))
    return configs


def _build_weighted_grid(coarse: bool = True) -> list[tuple[EnsembleConfig, str, dict[str, Any]]]:
    """Build WEIGHTED_SCORE parameter grid.

    Parameters
    ----------
    coarse : bool
        If True, use a coarse grid.  If False, use a fine grid around
        the most promising regions.

    Returns
    -------
    list[tuple[EnsembleConfig, str, dict]]
    """
    configs: list[tuple[EnsembleConfig, str, dict[str, Any]]] = []

    # Weight combinations: (margin_weight, volatility_weight).
    # Normalised internally by the ensemble; these absolute values
    # are ordinal only (the ratio matters).
    if coarse:
        weight_pairs: list[tuple[float, float]] = [
            (0.3, 0.7),   # vol dominates
            (0.4, 0.6),   # vol leans
            (0.5, 0.5),   # equal
            (0.6, 0.4),   # margin leans
            (0.7, 0.3),   # margin dominates
        ]
    else:
        weight_pairs = [
            (0.45, 0.55),
            (0.50, 0.50),
            (0.55, 0.45),
        ]

    if coarse:
        red_thresholds = [0.5, 0.6, 0.7, 0.8]
        yellow_thresholds = [0.2, 0.3, 0.4]
    else:
        red_thresholds = [0.55, 0.60, 0.65, 0.70]
        yellow_thresholds = [0.25, 0.30, 0.35]

    for w_m, w_v in weight_pairs:
        for red_t in red_thresholds:
            for yellow_t in yellow_thresholds:
                if yellow_t >= red_t:
                    continue  # enforce yellow < red
                config = EnsembleConfig(
                    mode="WEIGHTED_SCORE",
                    weights={
                        "margin_divergence": w_m,
                        "volatility_atr_expansion": w_v,
                    },
                    red_threshold=red_t,
                    yellow_threshold=yellow_t,
                )
                label = (
                    f"WEIGHTED(w_m={w_m:.1f},w_v={w_v:.1f},"
                    f" red={red_t:.1f},yel={yellow_t:.1f})"
                )
                params = {
                    "weights": {"margin_divergence": w_m, "volatility_atr_expansion": w_v},
                    "red_threshold": red_t,
                    "yellow_threshold": yellow_t,
                }
                configs.append((config, label, params))
    return configs


# ── Walk-forward evaluation ──────────────────────────────────────────────────


def _split_train_test(
    df_aligned: pd.DataFrame,
    df_fwd: pd.DataFrame,
    *,
    train_start: str = _TRAIN_START,
    train_end: str = _TRAIN_END,
    test_start: str = _TEST_START,
    test_end: str = _TEST_END,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split aligned signals + forward DD into train and test DataFrames.

    Returns
    -------
    tuple
        (train_with_dd, test_with_dd, df_aligned_full).
    """
    start = pd.Timestamp(train_start)
    split = pd.Timestamp(test_start)
    end = pd.Timestamp(test_end)

    train_mask = (df_aligned["trade_date"] >= start) & (df_aligned["trade_date"] < split)
    test_mask = (df_aligned["trade_date"] >= split) & (df_aligned["trade_date"] <= end)

    train_aligned = df_aligned[train_mask].copy()
    test_aligned = df_aligned[test_mask].copy()

    dd_cols = ["trade_date"] + [
        f"fwd_dd_{h}d" for h in _DEFAULT_HORIZONS
    ]
    df_fwd_sub = df_fwd[dd_cols].copy()

    train_merged = train_aligned.merge(df_fwd_sub, on="trade_date", how="inner")
    test_merged = test_aligned.merge(df_fwd_sub, on="trade_date", how="inner")

    return train_merged, test_merged, df_aligned


def _eval_configs_on_split(
    df_merged: pd.DataFrame,
    configs: list[tuple[EnsembleConfig, str, dict[str, Any]]],
    *,
    horizons: list[int],
    dd_thresholds: dict[int, float],
    min_signals: int = _MIN_SIGNALS,
) -> list[ConfigEvalResult]:
    """Evaluate all configs on a single data split."""
    results: list[ConfigEvalResult] = []
    for config, label, params in configs:
        result = _evaluate_config(
            df_merged, config,
            horizons=horizons,
            dd_thresholds=dd_thresholds,
            mode_label=label,
            params=params,
        )
        if result.n_signals >= min_signals and result.composite_dd is not None:
            results.append(result)
    return results


# ── Sensitivity analysis ─────────────────────────────────────────────────────


def _sensitivity_perturb(
    config: EnsembleConfig,
    label: str,
    params: dict[str, Any],
    df_merged: pd.DataFrame,
    *,
    horizons: list[int],
    dd_thresholds: dict[int, float],
    perturb_pct: float = _SENS_PERTURB,
) -> dict[str, Any]:
    """Evaluate ±perturb_pct threshold perturbation around *config*.

    For WEIGHTED_SCORE: perturb red_threshold and yellow_threshold.
    For VOTE_K_OF_M: K thresholds are integer, so skip perturbation.

    Returns
    -------
    dict
        Keys: ``base``, ``up``, ``down``, ``degradation_up``,
        ``degradation_down``, ``max_degradation``.
    """
    try:
        base = _evaluate_config(df_merged, config, horizons=horizons, dd_thresholds=dd_thresholds, mode_label=label, params=params)
    except Exception:
        return {"error": "base evaluation failed"}

    if config.mode == "VOTE_K_OF_M":
        # Integer K can't be perturbed continuously — treat as no degradation.
        return {
            "base": {
                "composite_dd": base.composite_dd,
                "n_signals": base.n_signals,
            },
            "up": None,
            "down": None,
            "degradation_up": 0.0,
            "degradation_down": 0.0,
            "max_degradation": 0.0,
            "note": "VOTE_K_OF_M with integer K — perturbation not applicable",
        }

    if config.mode != "WEIGHTED_SCORE":
        return {"error": f"unsupported mode for sensitivity: {config.mode}"}

    red_t = config.red_threshold or 0.7
    yellow_t = config.yellow_threshold or 0.3

    # Up perturbation: increase thresholds (stricter)
    red_up = min(red_t * (1 + perturb_pct), 0.99)
    yellow_up = min(yellow_t * (1 + perturb_pct), red_up - 0.01)
    config_up = EnsembleConfig(
        mode="WEIGHTED_SCORE",
        weights=config.weights,
        red_threshold=red_up,
        yellow_threshold=yellow_up,
    )

    # Down perturbation: decrease thresholds (looser)
    red_down = max(red_t * (1 - perturb_pct), 0.11)
    yellow_down = max(yellow_t * (1 - perturb_pct), 0.01)
    config_down = EnsembleConfig(
        mode="WEIGHTED_SCORE",
        weights=config.weights,
        red_threshold=red_down,
        yellow_threshold=yellow_down,
    )

    try:
        result_up = _evaluate_config(
            df_merged, config_up, horizons=horizons,
            dd_thresholds=dd_thresholds,
            mode_label=label + "_up",
            params=params,
        )
    except Exception:
        result_up = None
    try:
        result_down = _evaluate_config(
            df_merged, config_down, horizons=horizons,
            dd_thresholds=dd_thresholds,
            mode_label=label + "_down",
            params=params,
        )
    except Exception:
        result_down = None

    base_dd = base.composite_dd or 0.0
    up_dd = result_up.composite_dd if result_up and result_up.composite_dd is not None else None
    down_dd = result_down.composite_dd if result_down and result_down.composite_dd is not None else None

    # Degradation = abs change in composite DD (more positive = worse).
    degradation_up = abs((up_dd or base_dd) - base_dd) if up_dd is not None else 0.0
    degradation_down = abs((down_dd or base_dd) - base_dd) if down_dd is not None else 0.0

    return {
        "base": {
            "composite_dd": base.composite_dd,
            "n_signals": base.n_signals,
        },
        "up": {
            "composite_dd": up_dd,
            "n_signals": result_up.n_signals if result_up else None,
            "red_threshold": red_up,
            "yellow_threshold": yellow_up,
        },
        "down": {
            "composite_dd": down_dd,
            "n_signals": result_down.n_signals if result_down else None,
            "red_threshold": red_down,
            "yellow_threshold": yellow_down,
        },
        "degradation_up": round(degradation_up, 6),
        "degradation_down": round(degradation_down, 6),
        "max_degradation": round(max(degradation_up, degradation_down), 6),
    }


# ── Main tuning entry point ──────────────────────────────────────────────────


def tune_joint_ensemble(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    horizons: list[int] | None = None,
    dd_thresholds: dict[int, float] | None = None,
    min_signals: int = _MIN_SIGNALS,
    train_start: str = _TRAIN_START,
    train_end: str = _TRAIN_END,
    test_start: str = _TEST_START,
    test_end: str = _TEST_END,
    coarse_grid: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """Tune ensemble escape-top parameters with walk-forward OOS evaluation.

    Parameters
    ----------
    duckdb_path : str
        Path to DuckDB.
    horizons : list[int] or None
        Forward DD horizons (default [20, 60, 120]).
    dd_thresholds : dict[int, float] or None
        DD thresholds for label generation.
    min_signals : int
        Minimum signal count for robustness.
    train_start, train_end, test_start, test_end : str
        Date boundaries for train/test split.
    coarse_grid : bool
        If True, use coarse WEIGHTED_SCORE grid; else fine grid.
    verbose : bool
        Print progress.

    Returns
    -------
    dict
        Keys: ``oos_results``, ``best_oos``, ``baseline``,
        ``sensitivity``, ``comparison``, ``recommendation``.
    """
    if horizons is None:
        horizons = _DEFAULT_HORIZONS
    if dd_thresholds is None:
        dd_thresholds = _DEFAULT_DD_THRESHOLDS

    # ── 1. Load data ──────────────────────────────────────────────────────
    if verbose:
        print("[tune_joint] Loading validated condition signals …", flush=True)
    df_margin, df_vol = load_validated_condition_signals(duckdb_path)
    df_aligned = _align_signals(df_margin, df_vol)

    if verbose:
        print("[tune_joint] Computing forward drawdowns …", flush=True)
    df_fwd = compute_forward_drawdowns(duckdb_path, horizons=horizons)

    # ── 2. Train/test split ───────────────────────────────────────────────
    if verbose:
        print(
            f"[tune_joint] Split: train {train_start}–{train_end}, "
            f"test {test_start}–{test_end}",
            flush=True,
        )
    train_merged, test_merged, _ = _split_train_test(
        df_aligned, df_fwd,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
    )

    # ── 3. Build config grids ─────────────────────────────────────────────
    vote_configs = _build_vote_grid()
    weighted_configs = _build_weighted_grid(coarse=coarse_grid)
    all_configs = vote_configs + weighted_configs

    # Also evaluate AND mode as reference.
    and_config = EnsembleConfig(mode="AND")
    and_label = "AND(margin+vol)"
    and_params: dict[str, Any] = {"mode": "AND"}
    all_configs.append((and_config, and_label, and_params))

    if verbose:
        print(
            f"[tune_joint] Evaluating {len(all_configs)} configs "
            f"({len(vote_configs)} VOTE + {len(weighted_configs)} WEIGHTED + 1 AND) …",
            flush=True,
        )

    # ── 4. Train ranking ──────────────────────────────────────────────────
    train_results = _eval_configs_on_split(
        train_merged, all_configs,
        horizons=horizons, dd_thresholds=dd_thresholds,
        min_signals=min_signals,
    )
    # Rank by composite DD (more negative = better).
    train_results.sort(key=lambda r: r.composite_dd or 0.0)

    if verbose:
        print(
            f"[tune_joint] Train: {len(train_results)} configs pass "
            f"min_signals={min_signals}",
            flush=True,
        )

    # ── 5. OOS evaluation (all configs) ───────────────────────────────────
    oos_results = _eval_configs_on_split(
        test_merged, all_configs,
        horizons=horizons, dd_thresholds=dd_thresholds,
        min_signals=min_signals,
    )
    oos_results.sort(key=lambda r: r.composite_dd or 0.0)

    if verbose:
        print(
            f"[tune_joint] OOS: {len(oos_results)} configs pass "
            f"min_signals={min_signals}",
            flush=True,
        )

    # ── 6. Best OOS ───────────────────────────────────────────────────────
    best_oos = oos_results[0] if oos_results else None

    if verbose and best_oos:
        print(
            f"[tune_joint] Best OOS: {best_oos.mode} "
            f"composite_dd={best_oos.composite_dd:.4f}, "
            f"n_signals={best_oos.n_signals}",
            flush=True,
        )

    # ── 7. Baseline ───────────────────────────────────────────────────────
    if verbose:
        print("[tune_joint] Computing baseline …", flush=True)
    baseline = compute_baseline(
        duckdb_path,
        horizons=horizons,
        dd_thresholds=dd_thresholds,
    )

    if verbose:
        print(
            f"[tune_joint] Baseline: composite_dd={baseline['composite_dd']:.4f}, "
            f"n_signals={baseline['n_signals']}",
            flush=True,
        )

    # ── 8. Sensitivity analysis ───────────────────────────────────────────
    sensitivity: dict[str, Any] | None = None
    if best_oos:
        if verbose:
            print("[tune_joint] Running sensitivity analysis …", flush=True)
        config = _reconstruct_config(best_oos.mode, best_oos.params)
        sensitivity = _sensitivity_perturb(
            config, best_oos.mode, best_oos.params,
            test_merged,
            horizons=horizons,
            dd_thresholds=dd_thresholds,
        )

    # ── 9. Comparison vs baseline ─────────────────────────────────────────
    comparison = _compare_vs_baseline(best_oos, baseline)

    if verbose:
        print(
            f"[tune_joint] {comparison['recommendation']}",
            flush=True,
        )

    # ── 10. Build output ──────────────────────────────────────────────────
    return {
        "data_window": {
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "horizons": horizons,
            "dd_thresholds": dd_thresholds,
            "min_signals": min_signals,
        },
        "sse_trading_days": {
            "train": len(train_merged),
            "test": len(test_merged),
            "total": len(df_fwd),
        },
        "config_count": {
            "vote_k_of_m": len(vote_configs),
            "weighted_score": len(weighted_configs),
            "and_reference": 1,
            "total": len(all_configs),
        },
        "train_top5": _serialise_results(train_results[:5]),
        "oos_results": _serialise_results(oos_results),
        "best_oos": _serialise_result(best_oos) if best_oos else None,
        "baseline": baseline,
        "sensitivity": sensitivity,
        "comparison": comparison,
        "recommendation": comparison["recommendation"],
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _reconstruct_config(
    mode: str,
    params: dict[str, Any],
) -> EnsembleConfig:
    """Reconstruct an EnsembleConfig from its serialised params."""
    mode_upper = mode.split("(")[0] if "(" in mode else mode
    if "VOTE" in mode_upper:
        return EnsembleConfig(
            mode="VOTE_K_OF_M",
            k_yellow=params.get("k_yellow"),
            k_red=params.get("k_red"),
        )
    if "WEIGHTED" in mode_upper:
        return EnsembleConfig(
            mode="WEIGHTED_SCORE",
            weights=params.get("weights"),
            red_threshold=params.get("red_threshold"),
            yellow_threshold=params.get("yellow_threshold"),
        )
    if "AND" in mode_upper:
        return EnsembleConfig(mode="AND")
    raise ValueError(f"Cannot reconstruct config for mode: {mode!r}")


def _serialise_result(r: ConfigEvalResult | None) -> dict[str, Any] | None:
    """Serialise a ConfigEvalResult to a plain dict."""
    if r is None:
        return None
    return {
        "mode": r.mode,
        "params": r.params,
        "n_signals": r.n_signals,
        "n_valid": r.n_valid,
        "composite_dd": r.composite_dd,
        "signal_pct": round(r.signal_pct, 2),
        "precision_60d": (
            round(r.precision_60d, 4) if r.precision_60d is not None else None
        ),
        "horizon_metrics": r.horizon_metrics,
    }


def _serialise_results(
    results: list[ConfigEvalResult],
) -> list[dict[str, Any]]:
    return [_serialise_result(r) for r in results if r is not None]  # type: ignore[misc]


def _compare_vs_baseline(
    best_oos: ConfigEvalResult | None,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Compare best ensemble OOS against baseline and produce recommendation.

    Criterion: composite DD must be ≥10% more negative (deeper drawdown) than
    baseline.  Signal count is supplementary — not a substitute for DD
    improvement.
    """
    if best_oos is None:
        return {
            "recommendation": "fallback_to_baseline",
            "reason": "no ensemble config passed OOS min_signals filter",
            "delta_dd": None,
            "delta_pct": None,
            "signal_ratio": None,
        }

    base_dd = baseline.get("composite_dd")
    best_dd = best_oos.composite_dd

    if base_dd is None or best_dd is None:
        return {
            "recommendation": "fallback_to_baseline",
            "reason": "cannot compare (missing composite DD)",
            "delta_dd": None,
            "delta_pct": None,
            "signal_ratio": None,
        }

    # DD is negative; more negative = better.
    # delta_pct = (best_dd - base_dd) / |base_dd| * 100
    # e.g. best_dd=-0.10, base_dd=-0.05 → (-0.10+0.05)/0.05*100 = -100%
    # So improvement means delta_pct ≤ -10%.
    delta_dd = best_dd - base_dd
    delta_pct = (delta_dd / abs(base_dd)) * 100.0 if base_dd != 0 else 0.0

    base_dd_signal = _count_best_signals(best_oos, baseline)
    signal_ratio = (
        best_oos.n_signals / base_dd_signal
        if base_dd_signal and base_dd_signal > 0
        else None
    )

    dd_improved = delta_pct <= -10.0

    if dd_improved:
        recommendation = "use_ensemble"
        reason = f"ensemble improves composite DD by {abs(delta_pct):.1f}%"
    else:
        recommendation = "fallback_to_baseline"
        dd_note = f"DD delta={delta_pct:+.1f}% (needs ≤−10%)"
        sig_note = f"signal ratio={signal_ratio:.1f}x" if signal_ratio else ""
        reason = f"{dd_note}; {sig_note}"

    return {
        "recommendation": recommendation,
        "reason": reason,
        "delta_dd": round(delta_dd, 6),
        "delta_pct": round(delta_pct, 2),
        "signal_ratio": round(signal_ratio, 2) if signal_ratio is not None else None,
        "baseline_composite_dd": base_dd,
        "ensemble_composite_dd": best_dd,
        "baseline_n_signals": base_dd_signal,
        "ensemble_n_signals": best_oos.n_signals,
    }


def _count_best_signals(
    best_oos: ConfigEvalResult,
    baseline: dict[str, Any],
) -> int:
    return baseline.get("n_signals", 0)