from __future__ import annotations


def render_chanlun_report(
    ts_code: str,
    raw_bars: int,
    merged_bars: int,
    date_start: str,
    date_end: str,
    fractals: list[dict],
    strokes: list[dict],
    centers: list[dict],
    signals: list[dict],
    backtest_result: dict,
) -> str:
    """Render a Markdown report for chanlun analysis.

    Args:
        ts_code: Stock/ETF code (e.g. '588000.SH').
        raw_bars: Number of raw K-line bars.
        merged_bars: Number of bars after inclusion removal.
        date_start: Data start date string.
        date_end: Data end date string.
        fractals: List of fractal dicts.
        strokes: List of stroke dicts.
        centers: List of center dicts.
        signals: List of buy/sell signal dicts.
        backtest_result: Output from backtest_chanlun_signals().

    Returns: Markdown string.
    """
    lines: list[str] = []

    lines.append(f"# {ts_code} 缠论结构分析报告")
    lines.append("")

    lines.append("## 引言")
    lines.append(f"- 标的: {ts_code}")
    lines.append(f"- 数据区间: {date_start} ~ {date_end}，共 {raw_bars} 根日K")
    lines.append(f"- 去包含后 K线数: {merged_bars}")
    lines.append("- 方法: 包含处理 → 分型 → 笔 → 中枢 → 买卖点识别 → 回测")
    lines.append("")

    lines.append("## 结构识别结果")
    lines.append(f"- 分型数量: {len(fractals)}")
    lines.append(f"- 笔数量: {len(strokes)}")
    lines.append(f"- 中枢数量: {len(centers)}")
    lines.append("")

    if strokes:
        lines.append("### 最近 5 笔")
        for s in strokes[-5:]:
            lines.append(
                f"- {s['start_date']} → {s['end_date']}: "
                f"{s['direction']}, {s['start_price']:.3f} → {s['end_price']:.3f}, "
                f"{s['return_pct'] * 100:.2f}%"
            )
        lines.append("")

    if centers:
        lines.append("### 中枢列表")
        for c in centers:
            lines.append(
                f"- {c['start_date']} → {c['end_date']}: "
                f"中枢区间 [{c['zd']:.3f}, {c['zg']:.3f}], "
                f"极值 [{c['dd']:.3f}, {c['gg']:.3f}], "
                f"含 {c['stroke_count']} 笔"
            )
        lines.append("")

    if signals:
        lines.append("## 买卖点信号")
        for sig in signals:
            emoji = "🟢" if sig["type"].startswith("buy") else "🔴"
            lines.append(
                f"- {emoji} {sig['date']} **{sig['type']}** "
                f"@ {sig['price']:.3f} — {sig['reason']}"
            )
        lines.append("")
    else:
        lines.append("## 买卖点信号")
        lines.append("- 未检测到明确的买卖点信号")
        lines.append("")

    summary = backtest_result.get("summary", {})
    trades = backtest_result.get("trades", [])

    lines.append("## 回测结果")
    lines.append(f"- 初始资金: {summary.get('initial_capital', 0):,.0f}")
    lines.append(f"- 最终权益: {summary.get('final_equity', 0):,.2f}")
    lines.append(f"- 总收益率: {summary.get('total_return', 0) * 100:.2f}%")
    lines.append(f"- 交易次数: {summary.get('trade_count', 0)}")
    lines.append(f"- 胜率: {summary.get('win_rate', 0) * 100:.1f}%")
    lines.append(f"- 平均收益: {summary.get('avg_return', 0) * 100:.2f}%")
    lines.append(f"- 最大回撤: {summary.get('max_drawdown', 0) * 100:.2f}%")
    lines.append(f"- 夏普比率: {summary.get('sharpe_ratio', 0):.2f}")
    lines.append("")

    if trades:
        lines.append("### 交易明细")
        for t in trades:
            pnl_emoji = "✅" if t["pnl"] > 0 else "❌"
            lines.append(
                f"- {pnl_emoji} {t['entry_date']} → {t['exit_date']}: "
                f"入 {t['entry_price']:.3f} / 出 {t['exit_price']:.3f}, "
                f"盈亏 {t['pnl']:+,.2f} ({t['pnl_pct'] * 100:+.2f}%)"
            )
        lines.append("")

    lines.append("## 结论")
    if centers:
        last_center = centers[-1]
        lines.append(
            f"- 最近中枢区间 [{last_center['zd']:.3f}, {last_center['zg']:.3f}]，"
            f"时间跨度 {last_center['start_date']} ~ {last_center['end_date']}"
        )
    if strokes:
        last_stroke = strokes[-1]
        lines.append(
            f"- 最近一笔: {last_stroke['direction']} 方向，"
            f"{last_stroke['start_date']} → {last_stroke['end_date']}"
        )
    if signals:
        last_sig = signals[-1]
        lines.append(f"- 最近信号: {last_sig['date']} {last_sig['type']}")
    lines.append("")
    lines.append("> 本报告基于日线级别缠论结构分析，不含多级别共振，仅供参考。")
    lines.append("")

    return "\n".join(lines)
