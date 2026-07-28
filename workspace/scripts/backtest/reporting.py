"""Parameterized Markdown report rendering for backtest results."""

from __future__ import annotations

import pandas as pd


def render_report(
    summary_df: pd.DataFrame,
    best_strategy: str,
    coverage: dict,
    ts_code: str,
    strategy_details: list[str] | None = None,
    max_hold_days: int = 126,
    one_way_cost: float = 0.0015,
    qlib_notes: dict | None = None,
    strategy_count: int = 3,
) -> str:
    """Render a Markdown backtest report.

    Args:
        summary_df: DataFrame with one row per strategy, must include columns:
                    strategy, full_total_ret, full_annual_ret, full_max_dd,
                    full_calmar, full_trade_count, full_median_hold_days,
                    positive_calmar_fold_ratio, excess_total_ret, buyhold_total_ret,
                    buyhold_annual_ret, buyhold_max_dd, buyhold_calmar,
                    full_max_hold_days, robust_calmar_ex_top3_days.
        best_strategy: Name of the best-performing strategy (must appear in summary_df).
        coverage: Dict with keys 'start', 'end', 'bars'.
        ts_code: Tushare-format stock code (e.g. '688693.SH').
        strategy_details: Optional extra lines to inject into the strategy section.
        max_hold_days: Maximum holding period constraint shown in the report.
        one_way_cost: Per-side transaction cost used in the report.
        qlib_notes: Optional dict with 'loader' and 'handler' keys for Qlib/Alpha158 notes.

    Returns:
        Markdown report as a string.
    """
    best = summary_df.set_index("strategy").loc[best_strategy]
    lines: list[str] = []

    lines.append(f"# {ts_code} Alpha158 风格量化回测报告")
    lines.append("")
    lines.append("## 引言")
    lines.append(f"- 标的: {ts_code}")
    lines.append(f"- 数据区间: {coverage['start']} ~ {coverage['end']} ({coverage['bars']} 个交易日)")
    lines.append(f"- 约束: 持股期不超过 {max_hold_days} 个交易日（约 {max_hold_days // 21} 个月）")
    lines.append("- 因子来源: 参考 qlib Alpha158 定义，在本地 DuckDB 上复刻 158 个价格/量能因子")
    if strategy_count > 4:
        lines.append(f"- 设计原则: {strategy_count} 种策略覆盖 6 大策略族（动量/反转/量价/K线形态/复合/资金流），避免单策略过拟合")
    else:
        lines.append("- 设计原则: 按 Oracle 建议只测试 3 种策略，避免在单股票短样本上过拟合")
    lines.append("")
    lines.append("## 数据分析方法")
    lines.append("1. 使用 `stk_factor_pro` 的后复权 OHLCV 数据生成 Alpha158 风格 158 因子。")
    lines.append("2. 跳过上市初期前 120 个交易日，避免 IPO 波动扭曲。")
    if strategy_count > 4:
        lines.append("3. 根据策略信号类型采用不同 OOS 起点（60/126/246/315 交易日），做每 21 交易日滚动、每折 126 交易日的稳健性评估。")
    else:
        lines.append("3. 从第 246 个交易日起作为连续 OOS 回测起点，并做每 21 交易日滚动、每折 126 交易日的稳健性评估。")
    lines.append(f"4. 成本假设: 单边 {one_way_cost * 100:.2f}%，所有进出场均计入交易成本。")
    lines.append("5. 策略模式固定，不做阈值网格搜索。")
    lines.append("")

    if qlib_notes:
        lines.append("### Qlib / Alpha158 复用要点")
        lines.append(f"- Alpha158 主定义: `{qlib_notes['loader']}`")
        lines.append(f"- 默认处理器包装: `{qlib_notes['handler']}`")
        lines.append("- 注意: qlib Alpha158 默认偏横截面用途；本次单股票回测采用时间序列 Z-score，避免单票退化的横截面归一化。")
        lines.append("")

    lines.append("## 策略搜索空间")
    if strategy_details:
        for detail in strategy_details:
            lines.append(detail)
    else:
        lines.append("- **base_composite**: Composite_Z > 1.0 入场；Z < -0.5 / 跌破 -15% / 持有超 126 天出场。")
        lines.append("- **vol_target_composite**: 同 base，但入场仓位 = min(1.0, 0.30 / 20日年化波动率)。")
        lines.append("- **roc20_momentum**: 用 20 日收益 Z-score 代替 Composite_Z，其余规则相同。")
    lines.append("")

    lines.append("## 分析结果")
    lines.append("")
    lines.append(
        "| 策略 | OOS总收益 | 年化收益 | 最大回撤 | Calmar | 交易次数 | 持有中位数 | 正Calmar折数占比 |"
    )
    lines.append(
        "|---|---:|---:|---:|---:|---:|---:|---:|"
    )
    for _, row in summary_df.sort_values("full_calmar", ascending=False).iterrows():
        lines.append(
            f"| {row['strategy']} | {row['full_total_ret']*100:.2f}% | "
            f"{row['full_annual_ret']*100:.2f}% | {row['full_max_dd']*100:.2f}% | "
            f"{row['full_calmar']:.3f} | {int(row['full_trade_count'])} | "
            f"{row['full_median_hold_days']:.1f}天 | {row['positive_calmar_fold_ratio']*100:.1f}% |"
        )
    lines.append("")

    lines.append("### 买入持有基准（同 OOS 区间）")
    lines.append(f"- 总收益: {best['buyhold_total_ret']*100:.2f}%")
    lines.append(f"- 年化收益: {best['buyhold_annual_ret']*100:.2f}%")
    lines.append(f"- 最大回撤: {best['buyhold_max_dd']*100:.2f}%")
    lines.append(f"- Calmar: {best['buyhold_calmar']:.3f}")
    lines.append("")

    lines.append(f"### 最优模式: **{best_strategy}**")
    lines.append(f"- OOS 总收益: {best['full_total_ret']*100:.2f}%")
    lines.append(f"- 相对买入持有超额收益: {best['excess_total_ret']*100:.2f}%")
    lines.append(f"- 最大回撤: {best['full_max_dd']*100:.2f}%")
    lines.append(f"- Calmar: {best['full_calmar']:.3f}")
    lines.append(f"- 交易次数: {int(best['full_trade_count'])}")
    lines.append(f"- 持有期中位数: {best['full_median_hold_days']:.1f} 天")
    lines.append(f"- 最大持有期: {int(best['full_max_hold_days'])} 天")
    lines.append(f"- 去除最极端 3 个交易日后的稳健 Calmar: {best['robust_calmar_ex_top3_days']:.3f}")
    lines.append("")

    lines.append("## 结论")
    if best["full_calmar"] > 0 and best["excess_total_ret"] > 0:
        lines.append(f"- 在本次受限搜索中，**{best_strategy}** 是最优的量化投资模式。")
        lines.append("- 该模式在不超过 6 个月持有期的约束下，兼顾了收益和回撤控制。")
    else:
        lines.append(f"- 在本次受限搜索中，**{best_strategy}** 虽然名义上最优，但未显示出可靠的正向 OOS 优势。")
        lines.append("- 更可能的结论是：Alpha158 风格时间序列择时在该股票上优势有限，至少不足以稳定战胜买入持有。")
    lines.append("- 对这类上市时间较短、有效独立样本有限的标的，应更关注回撤控制而非追求参数优化后的高收益。")
    lines.append("")

    return "\n".join(lines) + "\n"