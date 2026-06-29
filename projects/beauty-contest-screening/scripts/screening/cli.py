"""CLI entry point for the screening pipeline.

Usage:
    python -m scripts.screening.cli                          # default: beauty-contest
    python -m scripts.screening.cli --strategy value         # value screening
    python -m scripts.screening.cli --top-n 10               # top 10
    python -m scripts.screening.cli --trade-date 2026-06-26  # specific date
"""
from __future__ import annotations

import argparse
import re
import sys
import json
from datetime import datetime, timedelta


def main():
    parser = argparse.ArgumentParser(description="Stock screening pipeline")
    parser.add_argument("--strategy", default="beauty-contest",
                        choices=["beauty-contest", "value", "quality", "momentum"],
                        help="Screening strategy type")
    parser.add_argument("--trade-date", default=None, help="Trade date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=None, help="Financial report end date")
    parser.add_argument("--top-n", type=int, default=20, help="Number of top stocks")
    parser.add_argument("--ablation", action="store_true", help="Run ablation study")
    parser.add_argument("--output", default=None, help="Output JSON path")
    args = parser.parse_args()

    # Date format validation
    if args.trade_date:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', args.trade_date):
            print(f"❌ 错误: --trade-date 格式无效 '{args.trade_date}'，应为 YYYY-MM-DD")
            sys.exit(1)

    if args.end_date:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', args.end_date):
            print(f"❌ 错误: --end-date 格式无效 '{args.end_date}'，应为 YYYY-MM-DD")
            sys.exit(1)

    # Strategy-specific parameters
    strategy_params = {
        "beauty-contest": {"roe_min": 8, "revenue_yoy_min": 0, "netprofit_yoy_min": -20},
        "value": {"roe_min": 10, "revenue_yoy_min": 0, "netprofit_yoy_min": 0},
        "quality": {"roe_min": 15, "revenue_yoy_min": 5, "netprofit_yoy_min": 5},
        "momentum": {"roe_min": 8, "revenue_yoy_min": 10, "netprofit_yoy_min": 10},
    }
    params = strategy_params.get(args.strategy, strategy_params["beauty-contest"])

    strategy_names = {
        "beauty-contest": "选美博弈",
        "value": "价值选股",
        "quality": "质量选股",
        "momentum": "动量选股",
    }
    strategy_name = strategy_names.get(args.strategy, args.strategy)
    print(f"=== {strategy_name}选股策略 ===")
    print(f"策略: {args.strategy}")
    print(f"Top N: {args.top_n}")
    print(f"条件: ROE>={params['roe_min']}%, 营收YoY>{params['revenue_yoy_min']}%, 净利YoY>{params['netprofit_yoy_min']}%")
    print()

    # ── Layer 1: Fundamental ──
    print("--- Layer 1: 基本面筛选 ---")
    from scripts.screening.layer1_fundamental import screen_fundamental, screen_fundamental_summary
    l1_kwargs = {}
    if args.trade_date: l1_kwargs["trade_date"] = args.trade_date
    if args.end_date: l1_kwargs["end_date"] = args.end_date
    l1_kwargs["roe_min"] = params["roe_min"]
    l1_kwargs["revenue_yoy_min"] = params["revenue_yoy_min"]
    l1_kwargs["netprofit_yoy_min"] = params["netprofit_yoy_min"]

    l1_df = screen_fundamental(**l1_kwargs)
    l1_summary = screen_fundamental_summary(l1_df)
    print(f"  通过: {l1_summary['count']} 只")
    if l1_summary['count'] > 0:
        print(f"  ROE 中位数: {l1_summary['roe_median']}%")
        print(f"  营收增长中位数: {l1_summary['revenue_yoy_median']}%")
    print()

    # ── Layer 2: Narrative ──
    print("--- Layer 2: 叙事动量筛选 ---")
    try:
        from scripts.screening.layer2_narrative import screen_narrative
        l2_result = screen_narrative(trade_date=args.trade_date)
        l2_sector_df = l2_result["sector_df"]
        l2_volume_df = l2_result["volume_df"]
        ss = l2_result["sector_summary"]
        vs = l2_result["volume_summary"]
        print(f"  板块动量: {ss.get('stock_count', 0)} 只, {ss.get('industry_count', 0)} 个行业")
        print(f"  量能动量: {vs.get('count', 0)} 只")
    except Exception as e:
        print(f"  ⚠️ Layer 2 跳过: {e}")
        l2_sector_df = None
        l2_volume_df = None
    print()

    # ── Layer 3: Flow ──
    print("--- Layer 3: 资金流筛选 ---")
    trade_date_prev = None
    if args.trade_date:
        try:
            td = datetime.strptime(args.trade_date, "%Y-%m-%d")
            trade_date_prev = (td - timedelta(days=7)).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        from scripts.screening.layer3_flow import screen_flow
        l3_result = screen_flow(trade_date=args.trade_date, trade_date_prev=trade_date_prev)
        l3_df = l3_result["flow_df"]
        l3_summary = l3_result["flow_summary"]
        print(f"  通过: {l3_summary['count']} 只")
    except Exception as e:
        print(f"  ⚠️ Layer 3 跳过: {e}")
        l3_df = None
    print()

    # ── Composite Ranking ──
    print("--- 综合排名 ---")
    from scripts.screening.composite import composite_screen
    result = composite_screen(
        l1_df,
        layer2_sector_df=l2_sector_df,
        layer2_volume_df=l2_volume_df,
        layer3_df=l3_df,
        top_n=args.top_n,
    )
    s = result["summary"]
    print(f"  Tier 1 (强共振): {s['tier1_count']}")
    print(f"  Tier 2 (中共振): {s['tier2_count']}")
    print(f"  Tier 3 (观察): {s['tier3_count']}")
    print()

    # Print top N
    cols = ["ts_code", "name", "industry", "roe_waa", "revenue_yoy", "composite_score", "layers_passed"]
    available_cols = [c for c in cols if c in result["top_n_df"].columns]
    print(f"Top {args.top_n}:")
    print(result["top_n_df"][available_cols].head(args.top_n).to_string(index=False))

    # ── Ablation (optional) ──
    if args.ablation:
        print(f"\n--- 消融分析 ---")
        from scripts.screening.ablation import run_ablation
        abl = run_ablation(l1_df, l2_sector_df, l2_volume_df, l3_df, top_n=args.top_n)
        print(f"\n{'配置':<25s} {'股票数':>6s} {'Avg ROE':>8s} {'Avg Score':>10s}")
        print("-" * 55)
        for row in abl["ablation_table"]:
            roe = f"{row['avg_roe']:.2f}%" if row.get('avg_roe') is not None else "N/A"
            score = f"{row['avg_composite_score']:.4f}" if row.get('avg_composite_score') is not None else "N/A"
            print(f"{row['label']:<25s} {row['stock_count']:>6d} {roe:>8s} {score:>10s}")

    # ── Output JSON (optional) ──
    if args.output:
        try:
            output_data = {
                "strategy": args.strategy,
                "date": args.trade_date or "default",
                "summary": s,
                "top_n": result["top_n_df"][available_cols].to_dict(orient="records"),
                "tier1": result["tier1"][available_cols].to_dict(orient="records") if not result["tier1"].empty else [],
            }
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2, default=str)
            print(f"\n结果已保存到: {args.output}")
        except (OSError, IOError) as e:
            print(f"\n⚠️ 警告: 无法写入文件 '{args.output}': {e}")


if __name__ == "__main__":
    main()
