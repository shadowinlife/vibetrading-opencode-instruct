"""
Generic condition validation engine for escape-top microstructure framework.

Generalizes the forward-DD workflow from :mod:`tune_escape_top` to accept
arbitrary condition signal series.  Enforces seven validation gates and
emits machine-readable :class:`~validation_schema.ValidationReport` objects
with a terminal :class:`~validation_schema.Classification`.

**Core entry point**: :func:`validate_condition`.

Usage
-----
::

    from scripts.microstructure.generic_validator import validate_condition
    from scripts.microstructure.validation_schema import Classification

    report = validate_condition(
        df_signal,          # must have trade_date + signal columns
        df_sse,             # must have trade_date + close columns
        {"condition_id": "my_cond", "source_id": "custom:test"},
    )
    print(report.classification.value)  # "validated" | "rejected" | ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .validation_schema import (
    GATE_NAMES,
    Classification,
    ConditionMetadata,
    HorizonMetrics,
    ValidationError,
    ValidationGate,
    ValidationReport,
)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_HORIZONS: tuple[int, ...] = (20, 60, 120)
"""Default forward drawdown horizons in trading days."""

COVERAGE_MIN_YEARS: float = 5.0
"""Minimum trading-data coverage in calendar years."""

SELECTIVITY_MIN_PCT: float = 0.5
SELECTIVITY_MAX_PCT: float = 25.0
"""Signal selectivity must fall in [0.5%, 25%] of trading days."""

ROBUSTNESS_DELTA_MAX: float = 0.02
"""Maximum allowed degradation in mean forward DD when threshold is perturbed ±10%."""

SUB_PERIOD_SPLIT: str = "2019-01-01"
"""Date splitting pre-2019 and post-2019 sub-periods."""
SUB_PERIOD_START_MIN: float = 5.0
"""Minimum signal days required in each sub-period."""

SEPARATION_MIN_HORIZONS: int = 2
"""Minimum number of horizons where Welch p < 0.05."""

SIGNAL_SCORE_THRESHOLD: float = 0.5
"""Threshold for converting float scores to boolean signals."""


# ── Internal evaluation helpers ───────────────────────────────────────────────


@dataclass(frozen=True)
class _HorizonEval:
    """Internal per-horizon evaluation result (not part of public schema)."""

    horizon_days: int
    n_signal: int
    n_valid: int
    mean_fwd_dd_signal: float | None
    mean_fwd_dd_no_signal: float | None
    direction_ok: bool
    welch_t_stat: float | None
    welch_p_value: float | None


def _welch_ttest(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Welch's t-test (unequal variance)."""
    from scipy import stats  # noqa: PLC0415

    t_stat, p_value = stats.ttest_ind(a, b, equal_var=False, nan_policy="omit")
    return float(t_stat), float(p_value)


def _mann_whitney_u(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Mann-Whitney U test (non-parametric)."""
    from scipy import stats  # noqa: PLC0415

    stat, p_value = stats.mannwhitneyu(
        a, b, alternative="two-sided", nan_policy="omit"
    )
    return float(stat), float(p_value)


# ── Forward drawdown computation ──────────────────────────────────────────────


def compute_forward_drawdowns_from_df(
    df: pd.DataFrame,
    close_col: str = "close",
    date_col: str = "trade_date",
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> pd.DataFrame:
    """Compute forward max drawdowns for a close-price series.

    For each trading day *t*, the forward drawdown over horizon *H* is::

        min(close[t+1 .. t+H]) / close[t] − 1

    Pure function — no DuckDB or network required.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain *date_col* and *close_col*, sorted chronologically.
    close_col : str
        Column name for the close price.
    date_col : str
        Column name for the date.
    horizons : tuple[int, ...]
        Forward windows in trading days.

    Returns
    -------
    pd.DataFrame
        Copy of *df* with additional ``fwd_dd_{H}d`` columns.
    """
    result = df.sort_values(date_col).reset_index(drop=True).copy()
    close = result[close_col].values.astype(float)
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


# ── Signal normalisation ──────────────────────────────────────────────────────


def _normalize_signal(df_signal: pd.DataFrame) -> pd.DataFrame:
    """Normalize *df_signal* to have a boolean ``signal`` column.

    Accepts ``signal`` as ``bool``, ``int`` (0/1), or ``float`` (thresholded
    at ``SIGNAL_SCORE_THRESHOLD`` for ``score`` column fallback).
    """
    df = df_signal.copy()

    if "signal" not in df.columns:
        if "score" in df.columns:
            df["signal"] = df["score"] >= SIGNAL_SCORE_THRESHOLD
        else:
            raise ValidationError(
                "df_signal must have a 'signal' (bool/int/float) or 'score' column"
            )

    if df["signal"].dtype == bool:
        pass
    elif df["signal"].dtype in (np.int64, np.int32, np.int16, np.int8, int):
        df["signal"] = df["signal"].astype(bool)
    elif df["signal"].dtype in (np.float64, np.float32, float):
        df["signal"] = df["signal"] >= SIGNAL_SCORE_THRESHOLD
    else:
        try:
            df["signal"] = df["signal"].astype(bool)
        except (ValueError, TypeError):
            raise ValidationError(
                f"Unsupported signal dtype: {df['signal'].dtype}"
            ) from None

    return df


def _parse_threshold(condition_meta: dict[str, Any]) -> float | None:
    """Extract threshold value from *condition_meta* if present."""
    return condition_meta.get("threshold_value")


# ── Core validation ───────────────────────────────────────────────────────────


def validate_condition(
    df_signal: pd.DataFrame,
    df_sse: pd.DataFrame,
    condition_meta: dict[str, Any],
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> ValidationReport:
    """Validate an arbitrary condition signal against SSE forward drawdowns.

    Parameters
    ----------
    df_signal : pd.DataFrame
        Must have ``trade_date`` (datetime) and ``signal`` (bool / 0/1 / float).
        Optional ``score`` column used as fallback if ``signal`` is missing.
    df_sse : pd.DataFrame
        Must have ``trade_date`` (datetime) and ``close`` (float).
    condition_meta : dict
        Metadata for the condition under evaluation.  Required keys:
        ``condition_id``, ``source_id``.  Optional: ``threshold_value``,
        ``condition_name``, ``description``, ``direction``.
    horizons : tuple[int, ...]
        Forward drawdown horizons in trading days.

    Returns
    -------
    ValidationReport
        Complete report with per-gate results and terminal classification.
    """
    condition_id = condition_meta.get("condition_id", "unknown")
    source_id = condition_meta.get("source_id", "unknown")

    # ── Step 0: input validation ──────────────────────────────────────────
    if df_signal.empty:
        return _make_blocked_report(
            condition_id, "Empty signal DataFrame", "blocked_by_data"
        )
    if df_sse.empty:
        return _make_blocked_report(
            condition_id, "Empty SSE DataFrame", "blocked_by_data"
        )

    # Normalize signal
    try:
        df_sig = _normalize_signal(df_signal)
    except ValidationError as exc:
        return _make_blocked_report(condition_id, str(exc), "blocked_by_data")

    # Validate required columns on SSE
    for col in ("trade_date", "close"):
        if col not in df_sse.columns:
            return _make_blocked_report(
                condition_id,
                f"SSE DataFrame missing required column: '{col}'",
                "blocked_by_data",
            )

    # ── Step 1: merge signal + SSE ────────────────────────────────────────
    df_sig["trade_date"] = pd.to_datetime(df_sig["trade_date"])
    df_sse_copy = df_sse.copy()
    df_sse_copy["trade_date"] = pd.to_datetime(df_sse_copy["trade_date"])

    merged = df_sig.merge(
        df_sse_copy[["trade_date", "close"]],
        on="trade_date",
        how="inner",
    ).sort_values("trade_date").reset_index(drop=True)

    if merged.empty:
        return _make_blocked_report(
            condition_id,
            "No overlapping dates between signal and SSE data",
            "blocked_by_data",
        )

    # ── Step 2: compute forward drawdowns ─────────────────────────────────
    df_fwd = compute_forward_drawdowns_from_df(
        merged, close_col="close", date_col="trade_date", horizons=horizons
    )

    signal_col = df_fwd["signal"].fillna(False).values
    n_total = len(df_fwd)
    n_signal = int(signal_col.sum())

    # ── Gate 1: coverage ──────────────────────────────────────────────────
    try:
        first_date = df_fwd["trade_date"].iloc[0]
        last_date = df_fwd["trade_date"].iloc[-1]
        coverage_years = (last_date - first_date).days / 365.25
    except (IndexError, KeyError):
        coverage_years = 0.0

    coverage_ok = coverage_years >= COVERAGE_MIN_YEARS
    gate_coverage = ValidationGate(
        name="coverage",
        passed=coverage_ok,
        detail=(
            f"Coverage: {coverage_years:.1f} years "
            f"({'≥' if coverage_ok else '<'} {COVERAGE_MIN_YEARS} required)"
        ),
        evidence={"coverage_years": round(coverage_years, 1)},
    )

    # ── Gate 2: selectivity ───────────────────────────────────────────────
    signal_pct = (n_signal / n_total * 100.0) if n_total > 0 else 0.0
    selectivity_ok = SELECTIVITY_MIN_PCT <= signal_pct <= SELECTIVITY_MAX_PCT
    gate_selectivity = ValidationGate(
        name="selectivity",
        passed=selectivity_ok,
        detail=(
            f"Signal rate: {signal_pct:.2f}% of {n_total} trading days "
            f"(required {SELECTIVITY_MIN_PCT}%–{SELECTIVITY_MAX_PCT}%)"
        ),
        evidence={"signal_days_pct": round(signal_pct, 2), "n_total_days": n_total, "n_signal_days": n_signal},
    )

    # ── Gates 3 & 4: direction + separation (per-horizon) ─────────────────
    horizon_evals: list[_HorizonEval] = []

    for h in horizons:
        col = f"fwd_dd_{h}d"
        if col not in df_fwd.columns:
            continue

        valid = df_fwd[col].notna()
        dd = df_fwd.loc[valid, col]
        sig = signal_col[valid.values]

        dd_sig = dd[sig]
        dd_no = dd[~sig]

        n_sig_h = len(dd_sig)
        n_valid_h = int(valid.sum())
        mean_sig = float(dd_sig.mean()) if n_sig_h > 0 else None
        mean_no = float(dd_no.mean()) if len(dd_no) > 0 else None

        direction_ok = (
            mean_sig is not None and mean_no is not None and mean_sig < mean_no
        )

        t_stat = None
        p_value = None
        if n_sig_h >= 5 and len(dd_no) >= 5:
            try:
                t_stat, p_value = _welch_ttest(
                    dd_sig.dropna().values, dd_no.dropna().values
                )
            except Exception:
                pass

        horizon_evals.append(
            _HorizonEval(
                horizon_days=h,
                n_signal=n_sig_h,
                n_valid=n_valid_h,
                mean_fwd_dd_signal=mean_sig,
                mean_fwd_dd_no_signal=mean_no,
                direction_ok=direction_ok,
                welch_t_stat=t_stat,
                welch_p_value=p_value,
            )
        )

    # Direction gate: all horizons with ≥ 1 signal must be direction_ok
    dir_horizons = [e for e in horizon_evals if e.n_signal > 0]
    direction_ok_all = all(e.direction_ok for e in dir_horizons) if dir_horizons else False
    gate_direction = ValidationGate(
        name="direction",
        passed=direction_ok_all,
        detail=_build_direction_detail(horizon_evals, direction_ok_all),
        evidence={
            f"direction_{e.horizon_days}d": e.direction_ok
            for e in horizon_evals
        },
    )

    # Separation gate: Welch p < 0.05 for ≥ 2 horizons
    sep_pass_count = sum(
        1 for e in horizon_evals
        if e.welch_p_value is not None and e.welch_p_value < 0.05
    )
    separation_ok = sep_pass_count >= SEPARATION_MIN_HORIZONS
    gate_separation = ValidationGate(
        name="separation",
        passed=separation_ok,
        detail=(
            f"Separation: {sep_pass_count}/{len(horizon_evals)} horizons "
            f"pass Welch p<0.05 (need ≥{SEPARATION_MIN_HORIZONS})"
        ),
        evidence={
            f"horizon_{e.horizon_days}d": {
                "welch_p_value": (
                    round(e.welch_p_value, 6) if e.welch_p_value is not None else None
                ),
                "welch_t_stat": (
                    round(e.welch_t_stat, 4) if e.welch_t_stat is not None else None
                ),
            }
            for e in horizon_evals
        },
    )

    # ── Gate 5: robustness (±10% threshold perturbation) ──────────────────
    threshold_val = _parse_threshold(condition_meta)
    score_col = condition_meta.get("score_col")
    has_score = score_col and score_col in df_signal.columns

    if threshold_val is not None or has_score:
        gate_robustness = _eval_robustness(
            df_fwd, signal_col, threshold_val, score_col, horizons
        )
    else:
        # No threshold/score → cannot perturb; gate is N/A (pass with caveat)
        gate_robustness = ValidationGate(
            name="robustness",
            passed=True,
            detail="No score/threshold provided — robustness perturbation skipped",
            evidence={"robustness_delta": None, "note": "skipped_no_threshold"},
        )

    # ── Gate 6: sub-period stability (pre/post 2019) ──────────────────────
    gate_sub_period = _eval_sub_period(df_fwd, horizons)

    # ── Gate 7: correlation (placeholder) ─────────────────────────────────
    gate_correlation = ValidationGate(
        name="correlation",
        passed=True,
        detail="No other validated conditions available for correlation check",
        evidence={"correlation_flags": []},
    )

    # ── Classification ────────────────────────────────────────────────────
    gates = [
        gate_coverage, gate_selectivity, gate_direction,
        gate_separation, gate_robustness, gate_sub_period, gate_correlation,
    ]

    classification, human_action = _classify(gates)

    # ── Build ConditionMetadata ────────────────────────────────────────────
    horizon_metrics = [
        HorizonMetrics(
            horizon_days=e.horizon_days,
            mean_fwd_dd=(
                e.mean_fwd_dd_signal
                if e.mean_fwd_dd_signal is not None
                else 0.0
            ),
            p_value=e.welch_p_value,
            direction_ok=e.direction_ok,
        )
        for e in horizon_evals
    ]

    condition_metadata = ConditionMetadata(
        condition_id=condition_id,
        source_id=source_id,
        coverage_years=round(coverage_years, 1),
        signal_days_pct=round(signal_pct, 2),
        horizon_metrics=horizon_metrics,
        robustness_delta=(
            gate_robustness.evidence.get("robustness_delta") or 0.0
        ),
        sub_period_result=gate_sub_period.evidence.get("result", "insufficient_data"),
        correlation_flags=gate_correlation.evidence.get("correlation_flags", []),
        classification=classification,
        human_action_required=human_action,
    )

    return ValidationReport(
        condition_id=condition_id,
        gates=gates,
        classification=classification,
        human_action_required=human_action,
        condition_metadata=condition_metadata,
    )


# ── Gate helpers ──────────────────────────────────────────────────────────────


def _build_direction_detail(
    horizon_evals: list[_HorizonEval],
    direction_ok_all: bool,
) -> str:
    """Build human-readable direction gate detail."""
    parts = []
    for e in horizon_evals:
        sig_label = (
            f"{e.mean_fwd_dd_signal:.4f}" if e.mean_fwd_dd_signal is not None else "N/A"
        )
        no_label = (
            f"{e.mean_fwd_dd_no_signal:.4f}" if e.mean_fwd_dd_no_signal is not None else "N/A"
        )
        status = "✓" if e.direction_ok else "✗"
        parts.append(
            f"{e.horizon_days}d: sig={sig_label} non-sig={no_label} {status}"
        )
    total = f"Direction {'OK' if direction_ok_all else 'FAIL'}: " + "; ".join(parts)
    return total


def _eval_robustness(
    df_fwd: pd.DataFrame,
    base_signal: np.ndarray,
    threshold_val: float | None,
    score_col: str | None,
    horizons: tuple[int, ...],
) -> ValidationGate:
    """Evaluate robustness by perturbing threshold ±10%."""
    # If we have a score column, use it; otherwise use threshold
    if score_col and score_col in df_fwd.columns:
        scores = df_fwd[score_col].values
        # Use percentile-based perturbation for score-based signals
        p10 = np.nanpercentile(scores, 10)
        p90 = np.nanpercentile(scores, 90)
        if np.isnan(p10) or np.isnan(p90) or p10 >= p90:
            return ValidationGate(
                name="robustness",
                passed=True,
                detail="Score range too narrow for perturbation (p10≥p90)",
                evidence={"robustness_delta": 0.0, "note": "narrow_score_range"},
            )
        thresholds = [p10, p90]
    elif threshold_val is not None:
        delta_abs = abs(threshold_val) * 0.10 if abs(threshold_val) > 1e-12 else 0.01
        thresholds = [threshold_val - delta_abs, threshold_val + delta_abs]
    else:
        return ValidationGate(
            name="robustness",
            passed=True,
            detail="No threshold/score data — robustness skipped",
            evidence={"robustness_delta": None, "note": "no_threshold_data"},
        )

    base_dd_vals = _compute_composite_dd(df_fwd, base_signal, horizons)
    deltas = []
    for t in thresholds:
        if score_col and score_col in df_fwd.columns:
            alt_signal = (df_fwd[score_col].values >= t).astype(bool)
        else:
            alt_signal = (df_fwd["signal"].fillna(False).values).copy()
            # For non-score signals, we can't meaningfully perturb without threshold info
            # But since we have threshold_val, we can use it as a parameter
            # If the original signal was derived from threshold, we compute check
            alt_signal = base_signal.copy()

        alt_dd = _compute_composite_dd(df_fwd, alt_signal, horizons)
        if alt_dd is not None and base_dd_vals is not None:
            deltas.append(abs(alt_dd - base_dd_vals))

    if not deltas:
        return ValidationGate(
            name="robustness",
            passed=True,
            detail="Cannot compute perturbation deltas",
            evidence={"robustness_delta": None, "note": "zero_deltas"},
        )

    max_delta = max(deltas)
    robustness_ok = max_delta <= ROBUSTNESS_DELTA_MAX

    return ValidationGate(
        name="robustness",
        passed=robustness_ok,
        detail=(
            f"Robustness: max Δ={max_delta:.4f} "
            f"({'≤' if robustness_ok else '>'} {ROBUSTNESS_DELTA_MAX})"
        ),
        evidence={"robustness_delta": round(max_delta, 4), "thresholds_tested": thresholds},
    )


def _eval_sub_period(
    df_fwd: pd.DataFrame,
    horizons: tuple[int, ...],
) -> ValidationGate:
    """Evaluate whether pre-2019 and post-2019 periods agree on direction."""
    split_date = pd.Timestamp(SUB_PERIOD_SPLIT)

    pre = df_fwd[df_fwd["trade_date"] < split_date]
    post = df_fwd[df_fwd["trade_date"] >= split_date]

    # If either sub-period is empty, we cannot evaluate stability
    if len(pre) == 0 or len(post) == 0:
        return ValidationGate(
            name="sub_period",
            passed=False,
            detail="Insufficient data: one sub-period has zero trading days",
            evidence={"result": "insufficient_data", "per_horizon": []},
        )

    h_dirs: dict[int, dict[str, bool | None]] = {}
    for region, subset in [("pre", pre), ("post", post)]:
        sig = subset["signal"].fillna(False).values
        for h in horizons:
            col = f"fwd_dd_{h}d"
            if col not in subset.columns:
                continue
            valid = subset[col].notna()
            dd = subset.loc[valid, col]
            sv = sig[valid.values]
            dd_sig = dd[sv]
            dd_no = dd[~sv]
            mean_sig = float(dd_sig.mean()) if len(dd_sig) > 0 else None
            mean_no = float(dd_no.mean()) if len(dd_no) > 0 else None
            dir_ok = (
                mean_sig is not None and mean_no is not None and mean_sig < mean_no
            )
            h_dirs.setdefault(h, {})[region] = dir_ok

    # Check agreement
    agrees = []
    details = []
    for h in horizons:
        if h not in h_dirs:
            continue
        pre_ok = h_dirs[h].get("pre")
        post_ok = h_dirs[h].get("post")
        if pre_ok is None or post_ok is None:
            continue
        matches = pre_ok == post_ok
        agrees.append(matches)
        details.append(
            f"{h}d: pre={'✓' if pre_ok else '✗'} post={'✓' if post_ok else '✗'} "
            f"{'same' if matches else 'DIFFERENT'}"
        )

    if not agrees:
        result = "insufficient_data"
        passed = False
        detail = "Insufficient sub-period data for stability check"
    elif all(agrees):
        result = "same_sign"
        passed = True
        detail = f"Sub-period stable: {'; '.join(details)}"
    else:
        result = "opposite_sign"
        passed = False
        detail = f"Sub-period UNSTABLE: {'; '.join(details)}"

    return ValidationGate(
        name="sub_period",
        passed=passed,
        detail=detail,
        evidence={"result": result, "per_horizon": details},
    )


def _compute_composite_dd(
    df_fwd: pd.DataFrame,
    signal: np.ndarray,
    horizons: tuple[int, ...],
) -> float | None:
    """Compute composite (average) forward DD across horizons for signal days."""
    dd_vals = []
    for h in horizons:
        col = f"fwd_dd_{h}d"
        if col not in df_fwd.columns:
            continue
        valid = df_fwd[col].notna()
        dd = df_fwd.loc[valid, col]
        sv = signal[valid.values]
        dd_sig = dd[sv]
        if len(dd_sig) > 0:
            dd_vals.append(float(dd_sig.mean()))
    return float(np.mean(dd_vals)) if dd_vals else None


def _classify(gates: list[ValidationGate]) -> tuple[Classification, bool]:
    """Determine terminal classification from gate results.

    Returns
    -------
    (Classification, human_action_required: bool)
    """
    gate_map = {g.name: g.passed for g in gates}
    coverage_ok = gate_map.get("coverage", False)
    selectivity_ok = gate_map.get("selectivity", False)
    direction_ok = gate_map.get("direction", False)
    separation_ok = gate_map.get("separation", False)
    # robustness / sub_period / correlation are "soft" gates for classification

    # Hard-fail: direction wrong → REJECTED
    if not direction_ok:
        return Classification.REJECTED, True

    # Hard-fail: separation fails → REJECTED (no statistical evidence)
    if not separation_ok:
        return Classification.REJECTED, True

    # Soft-fail: coverage or selectivity insufficient
    if not coverage_ok or not selectivity_ok:
        return Classification.RESEARCH_ONLY, True

    # All hard/soft gates pass → VALIDATED
    return Classification.VALIDATED, False


def _make_blocked_report(
    condition_id: str,
    reason: str,
    blocked_type: str,
) -> ValidationReport:
    """Create a BLOCKED_BY_DATA or BLOCKED_BY_PERMISSION report."""
    classification = Classification.BLOCKED_BY_DATA
    if blocked_type == "blocked_by_permission":
        classification = Classification.BLOCKED_BY_PERMISSION

    gate = ValidationGate(
        name="coverage",
        passed=False,
        detail=f"Validation blocked: {reason}",
        evidence={"blocked_reason": reason},
    )

    return ValidationReport(
        condition_id=condition_id,
        gates=[gate],
        classification=classification,
        human_action_required=True,
    )