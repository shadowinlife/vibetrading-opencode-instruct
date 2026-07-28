"""
Pairwise correlation audit for escape-top microstructure candidate signals.

Computes Pearson and Spearman correlations among all non-blocked condition
signal series, flags redundant pairs (|correlation| > 0.75), and generates
a correlation matrix, JSON audit, and Markdown summary report.

Usage
-----
::

    python -m scripts.microstructure.correlation_audit

Or import programmatically::

    from scripts.microstructure.correlation_audit import (
        run_correlation_audit,
        compute_pairwise_correlations,
        flag_redundant_pairs,
    )
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(REPO_ROOT, "duckdb", "ashare.duckdb")
OUTPUT_DIR = os.path.join(REPO_ROOT, "tmp", "microstructure", "validation")

# ─────────────────────────────────────────────────────────────────────────────
# Condition metadata
# ─────────────────────────────────────────────────────────────────────────────

CONDITION_INFO: Dict[str, dict] = {
    "concentration": {
        "name": "Top-5% Turnover Concentration",
        "tier": "baseline",
        "group": 1,
        "classification": "rejected",
    },
    "margin_divergence": {
        "name": "Margin Buy / SSE Divergence",
        "tier": "baseline",
        "group": 2,
        "classification": "validated",
    },
    "breadth_divergence": {
        "name": "Breadth Divergence (Broad Market Weakness)",
        "tier": "P0",
        "group": 3,
        "classification": "rejected",
    },
    "turnover_mcap_heat": {
        "name": "Turnover-to-Market-Cap Heat",
        "tier": "P0",
        "group": 4,
        "classification": "rejected",
    },
    "volatility_atr_expansion": {
        "name": "Volatility / ATR Expansion",
        "tier": "P0",
        "group": 5,
        "classification": "validated",
    },
    "valuation_percentile": {
        "name": "Valuation Percentile (PE+PB)",
        "tier": "P0",
        "group": 6,
        "classification": "rejected",
    },
    "sector_turnover_crowding": {
        "name": "Sector Turnover Crowding (HHI)",
        "tier": "P0",
        "group": 7,
        "classification": "rejected",
    },
    "large_order_exhaustion": {
        "name": "Large-Order Exhaustion (Moneyflow)",
        "tier": "P0",
        "group": 8,
        "classification": "research_only",
    },
    "winner_rate_pressure": {
        "name": "Winner Rate / Cost Pressure",
        "tier": "P0",
        "group": 9,
        "classification": "rejected",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Signal extraction (DuckDB-dependent — not tested offline)
# ─────────────────────────────────────────────────────────────────────────────


def _extract_concentration_signal() -> pd.DataFrame:
    """Extract concentration signal (top5_share >= 0.50)."""
    from scripts.microstructure.tune_escape_top import _load_concentration_series

    df = _load_concentration_series(DB_PATH)
    df["signal"] = (df["top5_share"] >= 0.50).astype(int)
    df.rename(columns={"top5_share": "score_concentration"}, inplace=True)
    return df[["trade_date", "score_concentration", "signal"]].copy()


def _extract_margin_divergence_signal(lookback: int = 40) -> pd.DataFrame:
    """Extract margin buy/SSE divergence signal."""
    from scripts.microstructure.tune_escape_top import _load_margin_divergence_series

    df = _load_margin_divergence_series(DB_PATH, divergence_lookback_days=lookback)
    df["signal"] = df["divergence_hit"].astype(int)
    df.rename(columns={"margin_buy_ratio": "score_margin_divergence"}, inplace=True)
    return df[["trade_date", "score_margin_divergence", "signal"]].copy()


def _extract_breadth_signal() -> pd.DataFrame:
    """Extract breadth divergence signal (signal_120d)."""
    from scripts.microstructure.breadth_divergence import compute_breadth_signals

    df = compute_breadth_signals(DB_PATH)
    df["signal"] = df["signal_120d"].astype(int)
    df.rename(columns={"breadth_ratio": "score_breadth_divergence"}, inplace=True)
    return df[["trade_date", "score_breadth_divergence", "signal"]].copy()


def _extract_turnover_mcap_signal(threshold: float = 0.025) -> pd.DataFrame:
    """Extract turnover/mcap heat signal."""
    from scripts.microstructure.turnover_mcap_heat import (
        compute_heat_signal_series,
        load_ratio_series,
    )

    df_ratio = load_ratio_series(DB_PATH)
    df_sig = compute_heat_signal_series(
        df_ratio,
        absolute_threshold=threshold,
        percentile_threshold=60.0,
        rolling_window=126,
    )
    df_sig["signal"] = df_sig["heat_signal"].astype(int)
    df_sig.rename(columns={"ratio": "score_turnover_mcap_heat"}, inplace=True)
    return df_sig[["trade_date", "score_turnover_mcap_heat", "signal"]].copy()


def _extract_volatility_signal() -> pd.DataFrame:
    """Extract joint volatility signal (ATR + realised vol both elevated)."""
    from scripts.microstructure.volatility_atr_expansion import compute_volatility_signals

    df = compute_volatility_signals(DB_PATH)
    df["signal"] = df["joint_vol_signal"].astype(int)
    df.rename(columns={"atr": "score_atr", "realized_vol": "score_volatility_atr_expansion"}, inplace=True)
    return df[["trade_date", "score_volatility_atr_expansion", "signal"]].copy()


def _extract_valuation_signal() -> pd.DataFrame:
    """Extract valuation percentile signal (PE + PB >= 80th percentile)."""
    from scripts.microstructure.valuation_percentile import compute_valuation_series

    df = compute_valuation_series(DB_PATH, force_local=True)
    df["signal"] = ((df["pe_percentile"] >= 80.0) & (df["pb_percentile"] >= 80.0)).astype(int)
    df["score_valuation_percentile"] = (df["pe_percentile"] + df["pb_percentile"]) / 2.0
    return df[["trade_date", "score_valuation_percentile", "signal"]].copy()


def _extract_sector_crowding_signal() -> pd.DataFrame:
    """Extract sector turnover crowding signal (HHI >= P80)."""
    from scripts.microstructure.sector_turnover_crowding import compute_sector_crowding

    result = compute_sector_crowding(DB_PATH, sector_level="L1")
    daily = pd.DataFrame(result["daily_series"])
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    daily["hhi_percentile"] = daily["hhi"].rank(pct=True) * 100
    daily["signal"] = (daily["hhi_percentile"] >= 80.0).astype(int)
    daily.rename(columns={"hhi": "score_sector_turnover_crowding"}, inplace=True)
    return daily[["trade_date", "score_sector_turnover_crowding", "signal"]].copy()


def _extract_large_order_signal() -> pd.DataFrame:
    """Extract large-order exhaustion signal (signal_any, Tushare source)."""
    from scripts.microstructure.large_order_exhaustion import compute_exhaustion_signal_series

    df = compute_exhaustion_signal_series(DB_PATH, data_source="tushare")
    df["signal"] = df["signal_any"].astype(int)
    df.rename(columns={"sse_close": "score_large_order_exhaustion"}, inplace=True)
    return df[["trade_date", "score_large_order_exhaustion", "signal"]].copy()


def _extract_winner_rate_signal() -> pd.DataFrame:
    """Extract winner rate pressure signal."""
    from scripts.microstructure.winner_rate_pressure import compute_signal_series

    df = compute_signal_series(duckdb_path=DB_PATH)
    df["signal"] = df["signal"].astype(int)
    df.rename(columns={"avg_winner_rate": "score_winner_rate_pressure"}, inplace=True)
    return df[["trade_date", "score_winner_rate_pressure", "signal"]].copy()


# Registry: condition_id → extractor function
SIGNAL_EXTRACTORS: Dict[str, callable] = {
    "concentration": _extract_concentration_signal,
    "margin_divergence": _extract_margin_divergence_signal,
    "breadth_divergence": _extract_breadth_signal,
    "turnover_mcap_heat": _extract_turnover_mcap_signal,
    "volatility_atr_expansion": _extract_volatility_signal,
    "valuation_percentile": _extract_valuation_signal,
    "sector_turnover_crowding": _extract_sector_crowding_signal,
    "large_order_exhaustion": _extract_large_order_signal,
    "winner_rate_pressure": _extract_winner_rate_signal,
}


# ─────────────────────────────────────────────────────────────────────────────
# Pure functions (testable offline)
# ─────────────────────────────────────────────────────────────────────────────


def align_signals(
    signal_dfs: Dict[str, pd.DataFrame],
    date_col: str = "trade_date",
    signal_col: str = "signal",
) -> Tuple[pd.DataFrame, List[str]]:
    """
    Align all signal DataFrames on a common date index.

    Returns:
        (aligned_df, condition_ids): DataFrame with trade_date as index and
        one column per condition (0/1 signal), plus sorted list of condition IDs.
    """
    if not signal_dfs:
        return pd.DataFrame(), []

    # Build a dict of trade_date → signal series for each condition
    aligned: Dict[str, pd.Series] = {}
    for cond_id, df in signal_dfs.items():
        if df is None or df.empty or signal_col not in df.columns:
            continue
        s = df.set_index(date_col)[signal_col].astype(float)
        s = s[~s.index.duplicated(keep="first")]
        aligned[cond_id] = s

    if not aligned:
        return pd.DataFrame(), []

    result = pd.DataFrame(aligned)
    cond_ids = sorted(result.columns.tolist())
    result = result[cond_ids]  # consistent column order
    result.index.name = date_col
    return result, cond_ids


def compute_pairwise_correlations(
    aligned: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Compute Pearson and Spearman pairwise correlation matrices.

    Both matrices are symmetric DataFrames with condition IDs as row/column labels.
    Uses pairwise complete observations (skipna).

    Returns:
        (pearson_df, spearman_df): square symmetric DataFrames.
    """
    if aligned.empty:
        return pd.DataFrame(), pd.DataFrame()

    cond_ids = list(aligned.columns)

    pearson = pd.DataFrame(np.eye(len(cond_ids)), index=cond_ids, columns=cond_ids)
    spearman = pd.DataFrame(np.eye(len(cond_ids)), index=cond_ids, columns=cond_ids)

    for i, ci in enumerate(cond_ids):
        for j, cj in enumerate(cond_ids):
            if i >= j:
                continue
            mask = aligned[ci].notna() & aligned[cj].notna()
            if mask.sum() < 3:
                pearson.loc[ci, cj] = pearson.loc[cj, ci] = np.nan
                spearman.loc[ci, cj] = spearman.loc[cj, ci] = np.nan
                continue
            x, y = aligned.loc[mask, ci].values, aligned.loc[mask, cj].values

            if np.std(x) == 0 or np.std(y) == 0:
                pearson.loc[ci, cj] = pearson.loc[cj, ci] = np.nan
                spearman.loc[ci, cj] = spearman.loc[cj, ci] = np.nan
                continue

            pearson.loc[ci, cj] = pearson.loc[cj, ci] = pearsonr(x, y)[0]
            spearman.loc[ci, cj] = spearman.loc[cj, ci] = spearmanr(x, y)[0]

    return pearson, spearman


def flag_redundant_pairs(
    pearson: pd.DataFrame,
    spearman: pd.DataFrame,
    threshold: float = 0.75,
) -> List[dict]:
    """
    Flag condition pairs whose |correlation| exceeds threshold in either measure.

    Returns:
        List of dicts with keys: cond_a, cond_b, pearson_r, spearman_rho,
        flagged_by (measure that triggered), rationale.
    """
    flags: List[dict] = []
    if pearson.empty or spearman.empty:
        return flags

    cond_ids = list(pearson.columns)
    for i in range(len(cond_ids)):
        for j in range(i + 1, len(cond_ids)):
            ci, cj = cond_ids[i], cond_ids[j]
            pr = pearson.loc[ci, cj]
            sr = spearman.loc[ci, cj]

            if pd.isna(pr) or pd.isna(sr):
                continue

            flagged_by = []
            if abs(pr) > threshold:
                flagged_by.append("pearson")
            if abs(sr) > threshold:
                flagged_by.append("spearman")

            if not flagged_by:
                continue

            flags.append({
                "cond_a": ci,
                "cond_b": cj,
                "pearson_r": round(float(pr), 6),
                "spearman_rho": round(float(sr), 6),
                "flagged_by": flagged_by,
                "rationale": _build_rationale(ci, cj, float(pr), float(sr), flagged_by),
            })

    return sorted(flags, key=lambda f: max(abs(f["pearson_r"]), abs(f["spearman_rho"])), reverse=True)


def _build_rationale(
    cond_a: str,
    cond_b: str,
    pearson_r: float,
    spearman_rho: float,
    flagged_by: List[str],
) -> str:
    """Build a human-readable rationale for a flagged pair."""
    max_r = max(abs(pearson_r), abs(spearman_rho))
    measure_str = " and ".join(flagged_by)

    # Known semantic pairs
    semantic_rationales = {
        frozenset(["concentration", "breadth_divergence"]): (
            "Concentration (top-5% turnover share) and breadth divergence (up/down ratio) "
            "both measure market narrowness/concentration — structural overlap expected."
        ),
        frozenset(["turnover_mcap_heat", "concentration"]): (
            "Turnover-to-market-cap heat and concentration both derive from stock-level "
            "turnover data — total turnover and top-5% share are mechanically related."
        ),
        frozenset(["volatility_atr_expansion", "turnover_mcap_heat"]): (
            "Volatility expansion and turnover heat often co-occur during market stress — "
            "high-volume selloffs drive both ATR spikes and elevated turnover ratios."
        ),
        frozenset(["volatility_atr_expansion", "breadth_divergence"]): (
            "ATR expansion and breadth divergence both capture market stress phases — "
            "volatile selloffs produce both elevated ATR and weak breadth."
        ),
        frozenset(["volatility_atr_expansion", "margin_divergence"]): (
            "ATR spikes often accompany margin-based divergence — margin selling "
            "intensifies during volatile drawdowns."
        ),
        frozenset(["winner_rate_pressure", "volatility_atr_expansion"]): (
            "High winner rates (profit-taking pressure) often cluster near volatility "
            "regime shifts — both flag elevated-risk environments."
        ),
    }

    pair = frozenset([cond_a, cond_b])
    if pair in semantic_rationales:
        return f"{semantic_rationales[pair]} Max |r|={max_r:.4f} ({measure_str})."
    else:
        return (
            f"Signals {cond_a} and {cond_b} show max |correlation|={max_r:.4f} "
            f"({measure_str}) — consider merging or selecting the stronger standalone signal."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────


def _format_matrix_df(matrix: pd.DataFrame, cond_info: Dict[str, dict]) -> pd.DataFrame:
    """Format a correlation matrix with short display names."""
    short_names = {cid: info["name"] for cid, info in cond_info.items() if cid in matrix.columns}
    renamed = matrix.rename(index=short_names, columns=short_names)
    return renamed


def generate_csv_matrix(pearson: pd.DataFrame, spearman: pd.DataFrame) -> str:
    """
    Generate a combined CSV matrix string with Pearson (upper) and Spearman (lower).
    """
    if pearson.empty:
        return ""

    combined = pearson.astype(object).copy()
    for ci in pearson.columns:
        for cj in pearson.columns:
            if ci == cj:
                combined.loc[ci, cj] = f"{pearson.loc[ci, cj]:.6f} (P)"
            else:
                combined.loc[ci, cj] = f"{pearson.loc[ci, cj]:.6f} / {spearman.loc[ci, cj]:.6f}"

    return combined.to_csv()


def generate_json_audit(
    condition_ids: List[str],
    pearson: pd.DataFrame,
    spearman: pd.DataFrame,
    flags: List[dict],
    cond_info: Dict[str, dict],
    n_aligned_days: int,
    date_range: Tuple[str, str],
) -> dict:
    """Generate structured JSON audit result."""
    # Build pairwise dict for both measures
    pairwise: Dict[str, dict] = {}
    for ci in condition_ids:
        pairwise[ci] = {}
        for cj in condition_ids:
            if ci == cj:
                continue
            pairwise[ci][cj] = {
                "pearson_r": None if pd.isna(pearson.loc[ci, cj]) else round(float(pearson.loc[ci, cj]), 6),
                "spearman_rho": None if pd.isna(spearman.loc[ci, cj]) else round(float(spearman.loc[ci, cj]), 6),
            }

    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "n_conditions_audited": len(condition_ids),
            "n_aligned_trading_days": n_aligned_days,
            "date_range": f"{date_range[0]} – {date_range[1]}",
            "flagging_threshold": 0.75,
        },
        "conditions_audited": [
            {
                "condition_id": cid,
                "condition_name": cond_info[cid]["name"],
                "tier": cond_info[cid]["tier"],
                "classification": cond_info[cid]["classification"],
                "group": cond_info[cid]["group"],
            }
            for cid in condition_ids
        ],
        "pairwise_correlations": pairwise,
        "flagged_pairs": flags,
        "summary": {
            "n_flagged": len(flags),
            "highest_pearson": max((abs(f["pearson_r"]) for f in flags), default=0.0),
            "highest_spearman": max((abs(f["spearman_rho"]) for f in flags), default=0.0),
        },
    }


def generate_md_report(
    condition_ids: List[str],
    pearson: pd.DataFrame,
    spearman: pd.DataFrame,
    flags: List[dict],
    cond_info: Dict[str, dict],
    n_aligned_days: int,
    date_range: Tuple[str, str],
) -> str:
    """Generate Markdown summary report."""
    lines = []
    lines.append("# Correlation Audit Report")
    lines.append("")
    lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"**Conditions audited**: {len(condition_ids)}  ")
    lines.append(f"**Aligned trading days**: {n_aligned_days}  ")
    lines.append(f"**Date range**: {date_range[0]} – {date_range[1]}  ")
    lines.append(f"**Flagging threshold**: |correlation| > 0.75  ")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    if not flags:
        lines.append("✅ **No redundant pairs detected.** All condition signals are sufficiently independent.")
        lines.append("")
        lines.append("All pairs have |Pearson r| ≤ 0.75 and |Spearman ρ| ≤ 0.75.")
    else:
        lines.append(f"⚠️ **{len(flags)} redundant pair(s) flagged** for human review:")
        lines.append("")
        for f in flags:
            a_name = cond_info[f["cond_a"]]["name"]
            b_name = cond_info[f["cond_b"]]["name"]
            lines.append(
                f"- **{a_name}** ↔ **{b_name}**: "
                f"Pearson r={f['pearson_r']:.4f}, Spearman ρ={f['spearman_rho']:.4f} "
                f"({'+'.join(f['flagged_by'])})"
            )
        lines.append("")

    # Condition table
    lines.append("## Conditions Audited")
    lines.append("")
    lines.append("| # | Condition | Tier | Classification | Signal Rate |")
    lines.append("|---|-----------|------|----------------|------------:|")
    for cid in condition_ids:
        info = cond_info[cid]
        lines.append(f"| {info['group']} | {info['name']} | {info['tier']} | {info['classification']} | — |")
    lines.append("")

    # Flagged pairs details
    if flags:
        lines.append("## Flagged Pairs for Human Review")
        lines.append("")
        lines.append("| Pair | Pearson r | Spearman ρ | Flagged By | Rationale |")
        lines.append("|------|:---------:|:----------:|------------|-----------|")
        for f in flags:
            a_name = cond_info[f["cond_a"]]["name"]
            b_name = cond_info[f["cond_b"]]["name"]
            lines.append(
                f"| {a_name} ↔ {b_name} | {f['pearson_r']:.4f} | {f['spearman_rho']:.4f} | "
                f"{'+'.join(f['flagged_by'])} | {f['rationale']} |"
            )
        lines.append("")

    # Correlation matrix
    lines.append("## Correlation Matrix (Pearson r)")
    lines.append("")
    short_names = {cid: info["name"] for cid, info in cond_info.items() if cid in pearson.columns}
    lines.append(format_markdown_matrix(pearson, short_names))
    lines.append("")

    lines.append("## Correlation Matrix (Spearman ρ)")
    lines.append("")
    lines.append(format_markdown_matrix(spearman, short_names))
    lines.append("")

    # Recommendations
    lines.append("## Recommendations")
    lines.append("")
    if flags:
        lines.append("For each flagged pair, consider:")
        lines.append("1. Which signal has stronger standalone predictive power (composite DD)")
        lines.append("2. Whether one is a subset/derivative of the other (structural redundancy)")
        lines.append("3. Whether removing one significantly degrades ensemble performance")
        lines.append("4. Whether they provide genuinely independent information despite affine relationship")
    else:
        lines.append("All signals are independent enough for ensemble inclusion without redundancy concerns.")
    lines.append("")

    lines.append("---")
    lines.append(f"*Report generated by `scripts/microstructure/correlation_audit.py`*")
    return "\n".join(lines)


def format_markdown_matrix(matrix: pd.DataFrame, short_names: Dict[str, str]) -> str:
    """Format a correlation matrix as a Markdown table with short names."""
    if matrix.empty:
        return "*(empty)*"

    cond_ids = list(matrix.columns)
    header = "| " + " | ".join([short_names.get(c, c) for c in cond_ids]) + " |"
    sep = "|" + "|".join(["---:" for _ in cond_ids]) + "|"

    rows = []
    for ci in cond_ids:
        vals = []
        for cj in cond_ids:
            val = matrix.loc[ci, cj]
            if pd.isna(val):
                vals.append("N/A")
            elif ci == cj:
                vals.append("1.000")
            else:
                vals.append(f"{float(val):.3f}")
        rows.append("| " + short_names.get(ci, ci) + " | " + " | ".join(vals) + " |")

    return header + "\n" + sep + "\n" + "\n".join(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────


def load_all_signals(
    condition_ids: Optional[List[str]] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, str]]:
    """
    Load signal series for specified (or all available) non-blocked conditions.

    Returns:
        (signal_dfs, errors): signal_dfs maps condition_id → DataFrame,
        errors maps condition_id → error message for failed extractions.
    """
    if condition_ids is None:
        condition_ids = list(SIGNAL_EXTRACTORS.keys())

    signal_dfs: Dict[str, pd.DataFrame] = {}
    errors: Dict[str, str] = {}

    for cid in condition_ids:
        extractor = SIGNAL_EXTRACTORS.get(cid)
        if extractor is None:
            errors[cid] = f"No extractor registered for condition '{cid}'"
            continue
        try:
            df = extractor()
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            signal_dfs[cid] = df
        except Exception as e:
            errors[cid] = f"Extraction failed: {type(e).__name__}: {e}"

    return signal_dfs, errors


def run_correlation_audit(
    condition_ids: Optional[List[str]] = None,
    threshold: float = 0.75,
    output_dir: Optional[str] = None,
    save_files: bool = True,
) -> dict:
    """
    Full end-to-end correlation audit.

    Args:
        condition_ids: List of condition IDs to audit (default: all 9 non-blocked).
        threshold: |correlation| above which pairs are flagged.
        output_dir: Directory for output files (default: OUTPUT_DIR).
        save_files: Whether to write CSV, JSON, MD files.

    Returns:
        dict with keys: condition_ids, pearson, spearman, flags, n_aligned_days,
        date_range, errors, saved_files.
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR

    # 1. Load signals
    signal_dfs, errors = load_all_signals(condition_ids)
    if not signal_dfs:
        return {
            "condition_ids": [],
            "pearson": pd.DataFrame(),
            "spearman": pd.DataFrame(),
            "flags": [],
            "n_aligned_days": 0,
            "date_range": ("N/A", "N/A"),
            "errors": errors,
            "saved_files": [],
        }

    # 2. Align on common date index
    aligned, cond_ids = align_signals(signal_dfs)
    n_aligned = len(aligned)

    date_range = ("N/A", "N/A")
    if n_aligned > 0:
        date_range = (
            str(aligned.index.min().date()),
            str(aligned.index.max().date()),
        )

    # 3. Compute correlations
    pearson, spearman = compute_pairwise_correlations(aligned)

    # 4. Flag redundant pairs
    flags = flag_redundant_pairs(pearson, spearman, threshold=threshold)

    saved_files = []
    if save_files and n_aligned > 0:
        os.makedirs(output_dir, exist_ok=True)

        # CSV matrix
        csv_path = os.path.join(output_dir, "correlation_matrix.csv")
        csv_content = generate_csv_matrix(pearson, spearman)
        with open(csv_path, "w") as f:
            f.write(csv_content)
        saved_files.append(csv_path)

        # JSON audit
        json_path = os.path.join(output_dir, "correlation_audit.json")
        audit_json = generate_json_audit(
            cond_ids, pearson, spearman, flags,
            CONDITION_INFO, n_aligned, date_range,
        )
        with open(json_path, "w") as f:
            json.dump(audit_json, f, indent=2, ensure_ascii=False)
        saved_files.append(json_path)

        # MD report
        md_path = os.path.join(output_dir, "correlation_audit.md")
        md_content = generate_md_report(
            cond_ids, pearson, spearman, flags,
            CONDITION_INFO, n_aligned, date_range,
        )
        with open(md_path, "w") as f:
            f.write(md_content)
        saved_files.append(md_path)

    return {
        "condition_ids": cond_ids,
        "pearson": pearson,
        "spearman": spearman,
        "flags": flags,
        "n_aligned_days": n_aligned,
        "date_range": date_range,
        "errors": errors,
        "saved_files": saved_files,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main():
    """CLI entry point."""
    print("=" * 70)
    print("Escape-Top Microstructure — Correlation Audit")
    print("=" * 70)
    print()

    result = run_correlation_audit()

    if result["errors"]:
        print("⚠️  Extraction errors:")
        for cid, err in result["errors"].items():
            print(f"  - {cid}: {err}")
        print()

    print(f"Conditions audited: {len(result['condition_ids'])}")
    print(f"Aligned trading days: {result['n_aligned_days']}")
    print(f"Date range: {result['date_range'][0]} – {result['date_range'][1]}")
    print()

    if result["flags"]:
        print(f"⚠️  {len(result['flags'])} redundant pair(s) flagged (|r| > 0.75):")
        print()
        for f in result["flags"]:
            a_name = CONDITION_INFO[f["cond_a"]]["name"]
            b_name = CONDITION_INFO[f["cond_b"]]["name"]
            print(f"  {a_name} ↔ {b_name}")
            print(f"    Pearson r={f['pearson_r']:.4f}, Spearman ρ={f['spearman_rho']:.4f}")
            print(f"    Flagged: {f['flagged_by']}")
            print(f"    {f['rationale']}")
            print()
    else:
        print("✅ No redundant pairs detected.")
        print()

    for path in result["saved_files"]:
        print(f"Saved: {path}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()