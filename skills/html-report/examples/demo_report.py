#!/usr/bin/env python3
"""Demo script showing how to generate HTML reports with each template."""

import sys
sys.path.insert(0, ".")

from scripts.reports.html_renderer import (
    render_vibe_backtest_html,
    render_alpha158_backtest_html,
    render_seven_look_html,
    render_fundamental_html,
    render_chanlun_html,
    render_signal_html,
    render_from_markdown,
)
import pandas as pd


def demo_vibe_backtest():
    """Generate a Vibe-Trading backtest report with current status."""
    html = render_vibe_backtest_html(
        ts_code="588000.SH",
        name="科创50ETF（华夏）",
        strategy_name="双均线+动量评分 v5",
        kpis={
            "总收益": {"value": "+41.2%", "change": "+3.8%", "change_positive": True},
            "年化收益": {"value": "+6.7%", "change": "+0.6%", "change_positive": True},
            "最大回撤": {"value": "-25.8%", "change": "+12.1%", "change_positive": True},
            "Sharpe": {"value": "0.60", "change": "+0.15", "change_positive": True},
            "Sortino": {"value": "0.49", "change": "+0.08", "change_positive": True},
            "Calmar": {"value": "0.26", "change": "+0.10", "change_positive": True},
        },
        equity_curve={
            "dates": ["2020-01", "2021-01", "2022-01", "2023-01", "2024-01", "2025-01"],
            "strategy": [0, 12, 18, 28, 35, 41.2],
            "buyhold": [0, 8, 12, 18, 20, 24.4],
        },
        drawdown_curve={
            "dates": ["2020-01", "2021-01", "2022-01", "2023-01", "2024-01", "2025-01"],
            "values": [0, -5, -15, -8, -25.8, -5],
        },
        current_status={
            "signal": "买入",
            "signal_date": "2025-06-26",
            "position": 80,
            "position_reason": "基于 v5 策略评分 >= 3",
            "reason": "MA10 上穿 MA30（金叉），RSI(14) = 58.3 > 50，MACD 柱为正。当前处于上升趋势，动量确认。",
            "key_metrics": {"MA10": "1.120", "MA30": "1.095", "RSI": "58.3", "MACD": "+0.012"},
        },
        coverage={"start": "2019-12-01", "end": "2025-06-26", "bars": 1350},
    )
    with open("demo_vibe_backtest.html", "w") as f:
        f.write(html)
    print(f"Generated: demo_vibe_backtest.html ({len(html)} bytes)")


def demo_multi_strategy():
    """Generate a multi-strategy comparison report."""
    strategies = [
        {
            "name": "v5 双均线+动量",
            "is_best": True,
            "kpis": [
                {"label": "总收益", "value": "+41.2%", "change": "+3.8%", "change_positive": True},
                {"label": "Sharpe", "value": "0.60", "change": "+0.15", "change_positive": True},
            ],
            "equity_curve": {
                "dates": ["2020-01", "2021-01", "2022-01", "2023-01", "2024-01", "2025-01"],
                "strategy": [0, 12, 18, 28, 35, 41.2],
                "buyhold": [0, 8, 12, 18, 20, 24.4],
            },
        },
        {
            "name": "v4 双均线",
            "is_best": False,
            "kpis": [
                {"label": "总收益", "value": "+37.4%"},
                {"label": "Sharpe", "value": "0.45"},
            ],
            "equity_curve": {
                "dates": ["2020-01", "2021-01", "2022-01", "2023-01", "2024-01", "2025-01"],
                "strategy": [0, 10, 15, 22, 30, 37.4],
                "buyhold": [0, 8, 12, 18, 20, 24.4],
            },
        },
    ]
    html = render_vibe_backtest_html(
        ts_code="588000.SH", name="科创50ETF", strategy_name="多策略对比",
        kpis={}, strategies=strategies,
        coverage={"start": "2019-12-01", "end": "2025-06-26", "bars": 1350},
    )
    with open("demo_multi_strategy.html", "w") as f:
        f.write(html)
    print(f"Generated: demo_multi_strategy.html ({len(html)} bytes)")


def demo_seven_look():
    """Generate a seven-look eight-question report."""
    html = render_seven_look_html(
        ts_code="600519.SH",
        name="贵州茅台",
        scores={
            "profit_quality": 8, "cost_structure": 7, "growth": 9,
            "business_mix": 6, "balance_sheet": 8, "efficiency": 7, "roe": 9,
        },
        score_details={
            "profit_quality": {"score": 8, "status": "green", "summary": "毛利率稳定在 90%+", "key_metrics": {"毛利率": "91.5%", "净利率": "52.3%"}},
            "roe": {"score": 9, "status": "green", "summary": "ROE 连续 5 年 > 30%", "key_metrics": {"ROE": "32.8%", "ROA": "25.1%"}},
        },
        moat={"brand": 5, "tech": 3, "cost": 4, "scale": 5},
    )
    with open("demo_seven_look.html", "w") as f:
        f.write(html)
    print(f"Generated: demo_seven_look.html ({len(html)} bytes)")


if __name__ == "__main__":
    demo_vibe_backtest()
    demo_multi_strategy()
    demo_seven_look()
    print("All demo reports generated.")
