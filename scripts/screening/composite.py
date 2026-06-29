"""Composite scoring: cross-sectional Z-score + multi-factor ranking + Top-N selection.

Combines Layer 1 (fundamental), Layer 2 (narrative), and Layer 3 (flow) results
into a unified ranking using cross-sectional Z-scores.

Usage:
    from scripts.screening.composite import composite_screen
    result = composite_screen()
"""
from __future__ import annotations

import pandas as pd
import numpy as np


def zscore_normalize(series: pd.Series) -> pd.Series:
    """Cross-sectional Z-score normalization."""
    mean = series.mean()
    std = series.std()
    if std == 0 or pd.isna(std):
        return pd.Series(0, index=series.index)
    return (series - mean) / std


def composite_screen(
    layer1_df: pd.DataFrame,
    layer2_sector_df: pd.DataFrame | None = None,
    layer2_volume_df: pd.DataFrame | None = None,
    layer3_df: pd.DataFrame | None = None,
    weights: dict | None = None,
    top_n: int = 20,
) -> dict:
    """Combine multi-layer screening results into a composite ranking.

    Args:
        layer1_df: Layer 1 fundamental screening results.
        layer2_sector_df: Layer 2 sector momentum results (optional).
        layer2_volume_df: Layer 2 volume momentum results (optional).
        layer3_df: Layer 3 capital flow results (optional).
        weights: Dict of layer weights. Default: L1=0.4, L2=0.3, L3=0.3.
        top_n: Number of top stocks to return.

    Returns:
        Dict with keys:
        - ranked_df: DataFrame with composite scores, sorted descending
        - top_n_df: Top N stocks
        - tier1: Strong resonance (all 3 layers pass)
        - tier2: Medium resonance (2 layers pass)
        - tier3: Watch list (1 layer pass, fundamental only)
        - summary: dict with counts per tier
    """
    if weights is None:
        weights = {"layer1": 0.4, "layer2": 0.3, "layer3": 0.3}

    # Start with Layer 1 as base universe
    df = layer1_df[["ts_code", "name", "industry", "roe_waa", "revenue_yoy", "pe_ttm", "pb", "total_mv_yi"]].copy()

    # Z-score Layer 1 factors
    df["z_roe"] = zscore_normalize(df["roe_waa"])
    df["z_growth"] = zscore_normalize(df["revenue_yoy"])
    df["z_valuation"] = zscore_normalize(-df["pe_ttm"])  # lower PE = better
    df["score_l1"] = (df["z_roe"] + df["z_growth"] + df["z_valuation"]) / 3

    # Layer 2 scores
    df["in_top_sector"] = 0
    df["amount_ratio"] = 1.0
    df["score_l2"] = 0.0

    if layer2_sector_df is not None and not layer2_sector_df.empty:
        top_sector_codes = set(layer2_sector_df["ts_code"])
        df["in_top_sector"] = df["ts_code"].isin(top_sector_codes).astype(int)

    if layer2_volume_df is not None and not layer2_volume_df.empty:
        vol_map = layer2_volume_df.set_index("ts_code")["amount_ratio"].to_dict()
        df["amount_ratio"] = df["ts_code"].map(vol_map).fillna(1.0)

    df["z_sector"] = zscore_normalize(df["in_top_sector"].astype(float))
    df["z_volume"] = zscore_normalize(df["amount_ratio"])
    df["score_l2"] = (df["z_sector"] + df["z_volume"]) / 2

    # Layer 3 scores
    df["has_flow_signal"] = 0
    df["score_l3"] = 0.0

    if layer3_df is not None and not layer3_df.empty:
        flow_codes = set(layer3_df["ts_code"])
        df["has_flow_signal"] = df["ts_code"].isin(flow_codes).astype(int)
        df["score_l3"] = zscore_normalize(df["has_flow_signal"].astype(float))

    # Composite score
    df["composite_score"] = (
        weights["layer1"] * df["score_l1"]
        + weights["layer2"] * df["score_l2"]
        + weights["layer3"] * df["score_l3"]
    )

    # Count how many layers each stock passes
    df["layers_passed"] = (
        (df["score_l1"] > 0).astype(int)
        + (df["score_l2"] > 0).astype(int)
        + (df["score_l3"] > 0).astype(int)
    )

    # Sort by composite score
    df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)

    # Tier classification
    tier1 = df[df["layers_passed"] >= 3].head(top_n)
    tier2 = df[(df["layers_passed"] == 2) & (df["score_l1"] > 0)].head(top_n)
    tier3 = df[(df["layers_passed"] <= 1) & (df["score_l1"] > 0)].head(top_n)

    top_n_df = df.head(top_n)

    summary = {
        "total_universe": len(df),
        "tier1_count": len(tier1),
        "tier2_count": len(tier2),
        "tier3_count": len(tier3),
        "top_n": top_n,
    }

    return {
        "ranked_df": df,
        "top_n_df": top_n_df,
        "tier1": tier1,
        "tier2": tier2,
        "tier3": tier3,
        "summary": summary,
    }


if __name__ == "__main__":
    from scripts.screening.layer1_fundamental import screen_fundamental
    l1 = screen_fundamental()
    print(f"Layer 1: {len(l1)} stocks")
    result = composite_screen(l1)
    s = result["summary"]
    print(f"\n=== Composite Ranking ===")
    print(f"Universe: {s['total_universe']}")
    print(f"Tier 1 (强共振): {s['tier1_count']}")
    print(f"Tier 2 (中共振): {s['tier2_count']}")
    print(f"Tier 3 (观察): {s['tier3_count']}")
    print(f"\nTop 10:")
    cols = ["ts_code", "name", "industry", "roe_waa", "revenue_yoy", "composite_score", "layers_passed"]
    available_cols = [c for c in cols if c in result["top_n_df"].columns]
    print(result["top_n_df"][available_cols].head(10).to_string(index=False))
