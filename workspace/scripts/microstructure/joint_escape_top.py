"""
Joint escape-top warning module with ensemble-based multi-condition resolution.

Loads validated condition signal series from DuckDB, resolves a unified
warning level via :mod:`ensemble`, and returns structured output with
per-condition contributions, excluded conditions, and configuration metadata.

**Design principles** inherited from the escape-top validation framework:

* Only ``VALIDATED`` conditions (from ``condition_manifest.json``) are active
  contributors by default.
* ``REJECTED``, ``RESEARCH_ONLY``, ``BLOCKED_BY_DATA``, and
  ``BLOCKED_BY_PERMISSION`` conditions appear in ``excluded_conditions``.
* Signal loading respects each condition's validated parameters.
* No ML / logistic-regression — all weights are human-configured.

Current validated conditions
----------------------------
* ``margin_divergence`` — 40d lookback (from #2)
* ``volatility_atr_expansion`` — joint ATR + realised vol (from #5)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .base import get_connection
from .ensemble import ConditionResult, EnsembleConfig, resolve_ensemble
from .metadata import DEFAULT_DUCKDB_PATH

# ── Public constants ───────────────────────────────────────────────────────────

_MANIFEST_FALLBACK = (
    Path(__file__).resolve().parents[2]
    / "tmp/microstructure/validation/condition_manifest.json"
)

# Validated condition defaults (per the generic-validator grid-search best params).
_CONDITION_DEFAULTS: dict[str, dict[str, Any]] = {
    "margin_divergence": {
        "divergence_lookback_days": 40,
        "source": "tune_escape_top._load_margin_divergence_series",
    },
    "volatility_atr_expansion": {
        "atr_rolling_days": 500,
        "atr_percentile": 80.0,
        "vol_rolling_days": 250,
        "vol_percentile": 80.0,
        "signal_column": "joint_vol_signal",
        "source": "volatility_atr_expansion.compute_volatility_signals",
    },
    "concentration": {
        "concentration_threshold": 0.50,
        "signal_column": "signal",
        "source": "tune_escape_top._load_concentration_series",
    },
    "large_order_exhaustion": {
        "data_source": "tushare",
        "signal_column": "signal_any",
        "source": "large_order_exhaustion.compute_exhaustion_signal_series",
    },
}

# Classification strings treated as "excluded" (not active contributors).
_EXCLUDED_CLASSIFICATIONS = frozenset(
    {"rejected", "research_only", "blocked_by_data", "blocked_by_permission"}
)


# ── Public API ────────────────────────────────────────────────────────────────


def compute_joint_warning(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    config: EnsembleConfig | None = None,
    manifest_path: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_rejected: bool = False,
) -> dict[str, Any]:
    """Compute the joint ensemble escape-top warning.

    Loads validated condition signal series from the local DuckDB, resolves
    the ensemble warning level for the latest trading day, and returns a
    structured dictionary.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    config : EnsembleConfig or None
        Ensemble resolution configuration.  Defaults to
        ``EnsembleConfig(mode="VOTE_K_OF_M")`` which, with 2 validated
        conditions, gives k_yellow=1 / k_red=2 (matches AND semantics).
    manifest_path : str or Path or None
        Path to ``condition_manifest.json``.  When ``None``, uses the
        default location under ``tmp/microstructure/validation/``.
    start_date : str or None
        Optional lower-bound date filter (YYYY-MM-DD).
    end_date : str or None
        Optional upper-bound date filter (YYYY-MM-DD).
    include_rejected : bool
        If ``True``, also include rejected/research-only conditions as
        active contributors (subject to ``config`` resolution).  Blocked
        conditions (no signal module) are always excluded.

    Returns
    -------
    dict
        Keys:

        * ``report_date`` — latest trading date evaluated
        * ``joint_warning`` — ``"RED"`` / ``"YELLOW"`` / ``"GREEN"``
        * ``ensemble_mode`` — the resolved mode string
        * ``parameters`` — effective ensemble configuration snapshot
        * ``active_conditions`` — list of per-condition ``ConditionResult``
          entries with extra metadata (hit, signal column, thresholds)
        * ``excluded_conditions`` — list of conditions excluded (with
          classification, reason)
        * ``manifest_path`` — resolved path to the manifest used
        * ``total_conditions`` — counts by classification
        * ``daily_series`` — optional full history (if ``start_date`` /
          ``end_date`` is wide enough, returns daily ensemble output)
        * ``board_concentration`` — (RED only) per-board penetration
          breakdown and recommended broad-based index anchor for
          post-RED drawdown tracking.  See
          :mod:`board_concentration` for details.

    Raises
    ------
    ValueError
        If no validated conditions are available or if signal loading fails.
    FileNotFoundError
        If the condition manifest cannot be located.
    """
    # 1. Load condition manifest
    mf_path = _resolve_manifest_path(manifest_path)
    manifest = _load_manifest(mf_path)

    # 2. Determine active vs excluded condition ids
    active_ids, excluded_entries = _classify_conditions(
        manifest, include_rejected=include_rejected
    )

    if not active_ids:
        raise ValueError(
            "No active conditions available for ensemble resolution. "
            "Check the condition manifest for VALIDATED entries, "
            "or set include_rejected=True."
        )

    # 3. Resolve ensemble config
    if config is None:
        config = EnsembleConfig(mode="VOTE_K_OF_M")

    # 4. Load signal series for each active condition
    signal_dfs: dict[str, pd.DataFrame] = {}
    for cid in active_ids:
        signal_dfs[cid] = _load_signal_series(cid, duckdb_path, start_date, end_date)

    # 5. Align all signal series on trade_date and extract latest-day signals
    aligned = _align_signal_series(signal_dfs)
    latest_date = aligned["trade_date"].iloc[-1] if len(aligned) > 0 else None
    latest_day = aligned.iloc[-1] if len(aligned) > 0 else None

    # 6. Build ConditionResult objects for the latest day
    condition_results, active_metadata = _build_condition_results(
        latest_day, active_ids, config
    )

    # 7. Resolve ensemble warning
    warning = resolve_ensemble(condition_results, config)

    # 8. Build output
    report_date_str = (
        str(latest_date)[:10]
        if latest_date is not None and not pd.isna(latest_date)
        else None
    )

    output: dict[str, Any] = {
        "report_date": report_date_str,
        "joint_warning": warning,
        "ensemble_mode": config.mode,
        "parameters": _config_to_params(config),
        "active_conditions": active_metadata,
        "excluded_conditions": excluded_entries,
        "manifest_path": str(mf_path),
        "total_conditions": {
            "total": manifest.get("summary", {}).get("total", 0),
            "validated": manifest.get("summary", {}).get("validated", 0),
            "rejected": manifest.get("summary", {}).get("rejected", 0),
            "research_only": manifest.get("summary", {}).get("research_only", 0),
            "blocked_by_data": manifest.get("summary", {}).get("blocked_by_data", 0),
            "blocked_by_permission": manifest.get("summary", {}).get("blocked_by_permission", 0),
        },
    }

    # 9. On RED signal, detect which broad-based index has the richest
    #    concentration of top-turnover stocks (board_concentration probe).
    if warning == "RED" and report_date_str is not None:
        try:
            from .board_concentration import compute_board_concentration

            board_result = compute_board_concentration(
                duckdb_path, trade_date=report_date_str,
            )
            output["board_concentration"] = board_result
        except Exception as exc:  # noqa: BLE001
            output["board_concentration"] = {
                "error": str(exc),
                "trade_date": report_date_str,
            }

    return output


def compute_joint_signal_series(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    config: EnsembleConfig | None = None,
    manifest_path: str | Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_rejected: bool = False,
) -> pd.DataFrame:
    """Compute the daily joint ensemble warning as a time series.

    Returns a DataFrame with ``trade_date``, per-condition hit columns,
    and ``joint_warning`` for every trading day.

    Parameters match :func:`compute_joint_warning`.

    Returns
    -------
    pd.DataFrame
        Columns: ``trade_date``, ``<condition_id>_hit`` for each active
        condition, ``joint_warning``.
    """
    mf_path = _resolve_manifest_path(manifest_path)
    manifest = _load_manifest(mf_path)

    active_ids, _ = _classify_conditions(manifest, include_rejected=include_rejected)

    if not active_ids:
        raise ValueError("No active conditions available.")

    if config is None:
        config = EnsembleConfig(mode="VOTE_K_OF_M")

    # Load all signal series
    signal_dfs = {}
    for cid in active_ids:
        signal_dfs[cid] = _load_signal_series(cid, duckdb_path, start_date, end_date)

    aligned = _align_signal_series(signal_dfs)

    # Resolve warning for each day
    warnings: list[str] = []
    for _, row in aligned.iterrows():
        results, _ = _build_condition_results(row, active_ids, config)
        warnings.append(resolve_ensemble(results, config))

    out = aligned[["trade_date"] + [f"{cid}_hit" for cid in active_ids]].copy()
    out["joint_warning"] = warnings
    return pd.DataFrame(out)


# ── Private helpers ───────────────────────────────────────────────────────────


def _resolve_manifest_path(manifest_path: str | Path | None) -> Path:
    if manifest_path is None:
        return _MANIFEST_FALLBACK
    p = Path(manifest_path)
    if not p.is_file():
        raise FileNotFoundError(f"Condition manifest not found: {p}")
    return p


def _load_manifest(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _classify_conditions(
    manifest: dict[str, Any],
    *,
    include_rejected: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Split manifest conditions into active ids and excluded entries."""
    active_ids: list[str] = []
    excluded: list[dict[str, Any]] = []

    for c in manifest.get("conditions", []):
        cid = c.get("condition_id", "unknown")
        classification = c.get("classification", "unknown")

        if classification == "validated":
            active_ids.append(cid)
            continue

        if include_rejected and classification in ("rejected", "research_only"):
            # Only include if the condition has a known signal loader.
            if cid in _CONDITION_DEFAULTS:
                active_ids.append(cid)
                continue

        excluded.append({
            "condition_id": cid,
            "condition_name": c.get("condition_name", cid),
            "classification": classification,
            "reason": _exclusion_reason(c),
        })

    return active_ids, excluded


def _exclusion_reason(condition: dict[str, Any]) -> str:
    classification = condition.get("classification", "unknown")
    notes = condition.get("notes", "")
    if classification == "rejected":
        return "Failed validation gates (direction, separation, or selectivity)"
    if classification == "research_only":
        return "Research-grade signal; not promoted to production defaults"
    if classification == "blocked_by_data":
        return f"Signal module not yet implemented: {notes}"
    if classification == "blocked_by_permission":
        return f"No accessible API: {notes}"
    return f"Classification: {classification}"


def _load_signal_series(
    condition_id: str,
    duckdb_path: str,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    """Load the daily boolean-hit series for a validated condition."""
    if condition_id == "margin_divergence":
        from .tune_escape_top import _load_margin_divergence_series  # noqa: PLC0415

        lb = _CONDITION_DEFAULTS["margin_divergence"]["divergence_lookback_days"]
        df: pd.DataFrame = _load_margin_divergence_series(
            duckdb_path, divergence_lookback_days=lb
        )
        df["_signal"] = df["divergence_hit"].astype(bool)
        return pd.DataFrame(df[["trade_date", "_signal"]])

    if condition_id == "volatility_atr_expansion":
        from .volatility_atr_expansion import (  # noqa: PLC0415
            compute_volatility_signals,
        )

        d = _CONDITION_DEFAULTS["volatility_atr_expansion"]
        df: pd.DataFrame = compute_volatility_signals(
            duckdb_path,
            atr_rolling_days=d["atr_rolling_days"],
            atr_percentile=d["atr_percentile"],
            vol_rolling_days=d["vol_rolling_days"],
            vol_percentile=d["vol_percentile"],
        )
        col = d["signal_column"]
        if col not in df.columns:
            raise ValueError(
                f"Expected signal column {col!r} not found in volatility signal"
                f" output. Available: {list(df.columns)}"
            )
        df["_signal"] = df[col].astype(bool)
        return pd.DataFrame(df[["trade_date", "_signal"]])

    if condition_id == "concentration":
        from .tune_escape_top import _load_concentration_series  # noqa: PLC0415

        d = _CONDITION_DEFAULTS["concentration"]
        threshold = d.get("concentration_threshold", 0.50)
        df: pd.DataFrame = _load_concentration_series(duckdb_path)
        df["_signal"] = (df["top5_share"] >= threshold).astype(bool)
        return pd.DataFrame(df[["trade_date", "_signal"]])

    if condition_id == "large_order_exhaustion":
        from .large_order_exhaustion import (  # noqa: PLC0415
            compute_exhaustion_signal_series,
        )

        d = _CONDITION_DEFAULTS["large_order_exhaustion"]
        source = d.get("data_source", "tushare")
        col = d.get("signal_column", "signal_any")
        df: pd.DataFrame = compute_exhaustion_signal_series(
            duckdb_path, data_source=source,
        )
        if col not in df.columns:
            raise ValueError(
                f"Expected signal column {col!r} not found in"
                f" exhaustion signal output. Available: {list(df.columns)}"
            )
        df["_signal"] = df[col].astype(bool)
        return pd.DataFrame(df[["trade_date", "_signal"]])

    raise ValueError(f"Unknown condition: {condition_id!r}")


def _align_signal_series(
    signal_dfs: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Merge multiple per-condition signal DataFrames on ``trade_date``."""
    dfs = []
    for cid, df in signal_dfs.items():
        df = df.copy()
        df[cid + "_hit"] = df["_signal"]
        dfs.append(df[["trade_date", cid + "_hit"]])
    out = dfs[0]
    for df in dfs[1:]:
        out = out.merge(df, on="trade_date", how="outer")
    out = out.sort_values("trade_date").reset_index(drop=True)
    for cid in signal_dfs:
        col = cid + "_hit"
        if col in out.columns:
            out[col] = out[col].fillna(False).infer_objects(copy=False)
    return out


def _build_condition_results(
    row: pd.Series | None,
    active_ids: list[str],
    config: EnsembleConfig,
) -> tuple[list[ConditionResult], list[dict[str, Any]]]:
    """Build ConditionResult list and metadata from a row."""
    results: list[ConditionResult] = []
    metadata: list[dict[str, Any]] = []

    if row is None:
        return results, metadata

    for cid in active_ids:
        col = cid + "_hit"
        hit = bool(row.get(col, False)) if col in row.index else False

        # Determine score for WEIGHTED_SCORE mode
        score: float | None = None
        if config.mode == "WEIGHTED_SCORE":
            # For WEIGHTED_SCORE, use 1.0/0.0 from hit by default
            # (ConditionResult.effective_score handles None → hit-based)
            pass  # score stays None — ConditionResult derives from hit

        default = _CONDITION_DEFAULTS.get(cid, {})
        results.append(ConditionResult(cid, hit=hit, score=score))
        metadata.append({
            "condition_id": cid,
            "hit": hit,
            "score": score,
            **{k: v for k, v in default.items() if k != "source"},
        })

    return results, metadata


def _config_to_params(config: EnsembleConfig) -> dict[str, Any]:
    """Serialize EnsembleConfig to a JSON-safe dict."""
    params: dict[str, Any] = {"mode": config.mode}
    if config.k_yellow is not None:
        params["k_yellow"] = config.k_yellow
    if config.k_red is not None:
        params["k_red"] = config.k_red
    if config.weights is not None:
        params["weights"] = config.weights
    if config.red_threshold is not None:
        params["red_threshold"] = config.red_threshold
    if config.yellow_threshold is not None:
        params["yellow_threshold"] = config.yellow_threshold
    return params