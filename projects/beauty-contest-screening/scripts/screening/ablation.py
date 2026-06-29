"""Sequential ablation: measure incremental contribution of each screening layer.

Runs Layer 1 alone, Layer 1+2, Layer 1+2+3 and compares portfolio metrics.

Usage:
    from scripts.screening.ablation import run_ablation
    result = run_ablation()
"""
from __future__ import annotations

import pandas as pd
import numpy as np


def calc_portfolio_metrics(df: pd.DataFrame, label: str) -> dict:
    """Calculate basic portfolio metrics for a stock selection result.

    Uses available columns to compute proxy metrics.
    """
    metrics = {
        "label": label,
        "stock_count": len(df),
    }

    if df.empty:
        metrics.update({
            "avg_roe": None,
            "avg_revenue_yoy": None,
            "avg_pe": None,
            "avg_composite_score": None,
        })
        return metrics

    if "roe_waa" in df.columns:
        metrics["avg_roe"] = round(df["roe_waa"].mean(), 2)
    if "revenue_yoy" in df.columns:
        metrics["avg_revenue_yoy"] = round(df["revenue_yoy"].mean(), 2)
    if "pe_ttm" in df.columns:
        metrics["avg_pe"] = round(df["pe_ttm"].mean(), 2)
    if "composite_score" in df.columns:
        metrics["avg_composite_score"] = round(df["composite_score"].mean(), 4)

    metrics["avg_score"] = metrics.get("avg_composite_score")

    return metrics


def run_ablation(
    layer1_df: pd.DataFrame,
    layer2_sector_df: pd.DataFrame | None = None,
    layer2_volume_df: pd.DataFrame | None = None,
    layer3_df: pd.DataFrame | None = None,
    top_n: int = 20,
) -> dict:
    """Run sequential ablation to measure each layer's incremental contribution.

    Configurations:
    - A: Baseline (Layer 1 only)
    - B: Layer 1 + Layer 2
    - C: Layer 1 + Layer 2 + Layer 3

    Returns:
        Dict with:
        - ablation_table: list of dicts with metrics per configuration
        - incremental: list of dicts with delta metrics between configs
    """
    from scripts.screening.composite import composite_screen

    # Config A: Layer 1 only
    result_a = composite_screen(layer1_df, top_n=top_n)
    metrics_a = calc_portfolio_metrics(result_a["top_n_df"], "A: Layer 1 only")

    # Config B: Layer 1 + Layer 2
    result_b = composite_screen(
        layer1_df,
        layer2_sector_df=layer2_sector_df,
        layer2_volume_df=layer2_volume_df,
        top_n=top_n,
    )
    metrics_b = calc_portfolio_metrics(result_b["top_n_df"], "B: Layer 1 + Layer 2")

    # Config C: Layer 1 + Layer 2 + Layer 3
    result_c = composite_screen(
        layer1_df,
        layer2_sector_df=layer2_sector_df,
        layer2_volume_df=layer2_volume_df,
        layer3_df=layer3_df,
        top_n=top_n,
    )
    metrics_c = calc_portfolio_metrics(result_c["top_n_df"], "C: Layer 1+2+3")

    ablation_table = [metrics_a, metrics_b, metrics_c]

    # Compute incremental deltas
    incremental = []
    for i in range(1, len(ablation_table)):
        prev = ablation_table[i - 1]
        curr = ablation_table[i]
        delta = {"step": f"{prev['label']} → {curr['label']}"}
        for key in ["avg_roe", "avg_revenue_yoy", "avg_pe", "avg_composite_score"]:
            if prev.get(key) is not None and curr.get(key) is not None:
                delta[f"delta_{key}"] = round(curr[key] - prev[key], 4)
            else:
                delta[f"delta_{key}"] = None
        delta["delta_score"] = delta.get("delta_avg_composite_score")
        delta["delta_stock_count"] = curr["stock_count"] - prev["stock_count"]
        incremental.append(delta)

    return {
        "ablation_table": ablation_table,
        "incremental": incremental,
        "result_a": result_a,
        "result_b": result_b,
        "result_c": result_c,
    }


if __name__ == "__main__":
    from scripts.screening.layer1_fundamental import screen_fundamental

    print("Running ablation study...\n")
    l1 = screen_fundamental()
    print(f"Layer 1 universe: {len(l1)} stocks\n")

    result = run_ablation(l1)

    print("=== 消融表 (Ablation Table) ===\n")
    print(f"{'配置':<25s} {'股票数':>6s} {'Avg ROE':>8s} {'Avg 增长':>8s} {'Avg Score':>10s}")
    print("-" * 65)
    for row in result["ablation_table"]:
        roe = f"{row['avg_roe']:.2f}%" if row.get('avg_roe') is not None else "N/A"
        growth = f"{row['avg_revenue_yoy']:.2f}%" if row.get('avg_revenue_yoy') is not None else "N/A"
        score = f"{row['avg_composite_score']:.4f}" if row.get('avg_composite_score') is not None else "N/A"
        print(f"{row['label']:<25s} {row['stock_count']:>6d} {roe:>8s} {growth:>8s} {score:>10s}")

    print(f"\n=== 增量贡献 (Incremental) ===\n")
    for delta in result["incremental"]:
        print(f"{delta['step']}")
        for key, val in delta.items():
            if key.startswith("delta_") and val is not None:
                print(f"  {key}: {val:+.4f}")
