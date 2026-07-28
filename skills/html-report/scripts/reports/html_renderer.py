"""HTML report renderers for analysis, backtest, and signal reports.

Each function renders a single-file HTML report using Jinja2 templates
with ECharts charts, CJK fonts, and dual-theme (light/dark) support.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

# Template directory relative to this file
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── 1. Vibe-Trading Backtest ──

def render_vibe_backtest_html(
    ts_code: str,
    name: str,
    strategy_name: str,
    kpis: dict[str, Any],
    version_comparison: list[dict] | None = None,
    version_headers: list[str] | None = None,
    equity_curve: dict | None = None,
    drawdown_curve: dict | None = None,
    trades: list[dict] | None = None,
    ohlcv_data: dict | None = None,
    strategy_params: list[str] | None = None,
    improvements: list[str] | None = None,
    current_status: dict | None = None,
    coverage: dict | None = None,
    strategies: list[dict] | None = None,
) -> str:
    """Render Vibe-Trading backtest HTML report.

    Args:
        ts_code: Stock code (e.g. '588000.SH').
        name: Stock/ETF name.
        strategy_name: Strategy name.
        kpis: Dict of KPI label -> value. Each value can be a string or a dict
              with keys {value, change, change_positive}.
        version_comparison: List of dicts with {metric, values: [...], change, note, highlight}.
        version_headers: List of version names (e.g. ['v4', 'v5']).
        equity_curve: Dict with {dates: [...], strategy: [...], buyhold: [...]}.
        drawdown_curve: Dict with {dates: [...], values: [...]}.
        trades: List of dicts with {date, direction, price, pnl, hold_days, trigger}.
        ohlcv_data: Dict with {dates, candles, signals} for ECharts candlestick.
        strategy_params: List of strategy parameter description strings.
        improvements: List of improvement description strings.
        coverage: Dict with {start, end, bars}.
        strategies: Optional list of strategy dicts for multi-strategy tab display.
                    Each dict: {name, is_best, kpis, equity_curve, drawdown_curve,
                    trades, strategy_params, improvements}. When provided with 2+
                    items, renders tabbed multi-strategy view.

    Returns:
        Rendered HTML string.
    """
    # Normalize KPIs to list of dicts for template
    kpi_list = []
    for label, val in kpis.items():
        if isinstance(val, dict):
            kpi_list.append({"label": label, **val})
        else:
            kpi_list.append({"label": label, "value": str(val), "change": None})

    template = _env.get_template("backtest/vibe_backtest.html")
    return template.render(
        title=f"{name} ({ts_code}) {strategy_name} 回测报告",
        ts_code=ts_code,
        name=name,
        date=coverage.get("end", _now_str()) if coverage else _now_str(),
        generated_at=_now_str(),
        data_source="Vibe-Trading Backtest Engine",
        kpis=kpi_list,
        version_comparison=version_comparison,
        version_headers=version_headers or [],
        equity_curve=equity_curve,
        drawdown_curve=drawdown_curve,
        trades=trades,
        ohlcv_data=ohlcv_data,
        strategy_params=strategy_params,
        improvements=improvements,
        current_status=current_status,
        strategies=strategies,
    )


# ── 2. Alpha158 Walk-Forward Backtest ──

def render_alpha158_backtest_html(
    summary_df: pd.DataFrame,
    best_strategy: str,
    coverage: dict,
    ts_code: str,
    strategy_details: list[str] | None = None,
    max_hold_days: int = 126,
    one_way_cost: float = 0.0015,
    qlib_notes: dict | None = None,
    strategy_count: int = 3,
    current_status: dict | None = None,
) -> str:
    """Render Alpha158 Walk-Forward backtest HTML report.

    Args:
        summary_df: DataFrame with columns: strategy, full_total_ret, full_annual_ret,
                    full_max_dd, full_calmar, full_trade_count, full_median_hold_days,
                    positive_calmar_fold_ratio, excess_total_ret, buyhold_total_ret,
                    buyhold_annual_ret, buyhold_max_dd, buyhold_calmar,
                    full_max_hold_days, robust_calmar_ex_top3_days.
        best_strategy: Name of the best-performing strategy.
        coverage: Dict with {start, end, bars}.
        ts_code: Stock code.
        strategy_details: Optional strategy description lines.
        max_hold_days: Max holding period.
        one_way_cost: Per-side transaction cost.
        qlib_notes: Optional dict with {loader, handler}.
        strategy_count: Number of strategies tested.

    Returns:
        Rendered HTML string.
    """
    strategies = []
    for _, row in summary_df.sort_values("full_calmar", ascending=False).iterrows():
        strategies.append({
            "name": row["strategy"],
            "is_best": row["strategy"] == best_strategy,
            "total_ret": f"{row['full_total_ret'] * 100:.2f}%",
            "annual_ret": f"{row['full_annual_ret'] * 100:.2f}%",
            "max_dd": f"{row['full_max_dd'] * 100:.2f}%",
            "calmar": f"{row['full_calmar']:.3f}",
            "trade_count": str(int(row["full_trade_count"])),
            "median_hold": f"{row['full_median_hold_days']:.1f}天",
            "positive_fold_ratio": f"{row['positive_calmar_fold_ratio'] * 100:.1f}%",
        })

    best_row = summary_df.set_index("strategy").loc[best_strategy]
    best = {
        "name": best_strategy,
        "total_ret": f"{best_row['full_total_ret'] * 100:.2f}%",
        "excess_ret": f"{best_row['excess_total_ret'] * 100:.2f}%",
        "max_dd": f"{best_row['full_max_dd'] * 100:.2f}%",
        "calmar": f"{best_row['full_calmar']:.3f}",
        "trade_count": str(int(best_row["full_trade_count"])),
        "median_hold": f"{best_row['full_median_hold_days']:.1f}天",
        "max_hold": f"{int(best_row['full_max_hold_days'])}天",
        "robust_calmar": f"{best_row['robust_calmar_ex_top3_days']:.3f}",
    }

    buyhold = {
        "total_ret": f"{best_row['buyhold_total_ret'] * 100:.2f}%",
        "annual_ret": f"{best_row['buyhold_annual_ret'] * 100:.2f}%",
        "max_dd": f"{best_row['buyhold_max_dd'] * 100:.2f}%",
        "calmar": f"{best_row['buyhold_calmar']:.3f}",
    }

    # Chart data for strategy comparison bar chart
    chart_data = {
        "names": [s["name"] for s in strategies],
        "categories": ["OOS总收益", "年化收益", "最大回撤"],
        "series": [
            {
                "name": s["name"],
                "values": [
                    float(s["total_ret"].rstrip("%")),
                    float(s["annual_ret"].rstrip("%")),
                    float(s["max_dd"].rstrip("%")),
                ],
            }
            for s in strategies
        ],
    }

    template = _env.get_template("backtest/alpha158.html")
    return template.render(
        title=f"{ts_code} Alpha158 回测报告",
        ts_code=ts_code,
        date=coverage.get("end", _now_str()),
        generated_at=_now_str(),
        data_source="Alpha158 Walk-Forward Engine",
        strategies=strategies,
        best=best,
        buyhold=buyhold,
        chart_data=chart_data,
        strategy_details=strategy_details,
        coverage=coverage,
        max_hold_days=max_hold_days,
        max_hold_months=max_hold_days // 21,
        one_way_cost_pct=f"{one_way_cost * 100:.2f}",
        qlib_notes=qlib_notes,
        current_status=current_status,
    )


# ── 3. Seven-Look Eight-Question (七看八问) ──

def render_seven_look_html(
    ts_code: str,
    name: str,
    scores: dict[str, float],
    score_details: dict[str, dict] | None = None,
    financial_trends: dict | None = None,
    peer_comparison: list[dict] | None = None,
    peer_columns: list[str] | None = None,
    moat: dict | None = None,
    risks: list[dict] | None = None,
    eight_questions: list[dict] | None = None,
) -> str:
    """Render seven-look eight-question HTML report.

    Args:
        ts_code: Stock code.
        name: Stock name.
        scores: Dict of dimension_name -> score (0-10).
                Keys: profit_quality, cost_structure, growth, business_mix,
                balance_sheet, efficiency, roe.
        score_details: Dict of dimension_name -> {score, status(green/yellow/red),
                       summary, key_metrics: {metric_name: value}}.
        financial_trends: Dict with {years: [...], metrics: [...],
                          series: [{name, values: [...]}]}.
        peer_comparison: List of dicts for peer comparison table.
        peer_columns: Column names for peer table.
        moat: Dict with {brand, tech, cost, scale} each 0-5.
        risks: List of dicts with {title, severity(high/medium/low), description}.
        eight_questions: List of dicts with {question, answer, evidence}.

    Returns:
        Rendered HTML string.
    """
    dim_names_cn = {
        "profit_quality": "盈收与利润质量",
        "cost_structure": "费用成本结构",
        "growth": "增长率趋势",
        "business_mix": "业务构成与市场分布",
        "balance_sheet": "资产负债健康度",
        "efficiency": "投入产出效率",
        "roe": "收益率与资本回报",
    }

    dimensions = []
    for key, score in scores.items():
        detail = (score_details or {}).get(key, {})
        dimensions.append({
            "name": dim_names_cn.get(key, key),
            "score": score,
            "status": detail.get("status", "yellow"),
            "summary": detail.get("summary", ""),
            "key_metrics": detail.get("key_metrics", {}),
        })

    template = _env.get_template("analysis/seven_look.html")
    return template.render(
        title=f"{name} ({ts_code}) 七看八问分析报告",
        ts_code=ts_code,
        name=name,
        date=_now_str(),
        generated_at=_now_str(),
        data_source="七看八问分析框架",
        dimensions=dimensions,
        financial_trends=financial_trends,
        peer_comparison=peer_comparison,
        peer_columns=peer_columns or ["名称", "代码", "ROE", "毛利率", "净利率", "资产负债率"],
        moat=moat,
        risks=risks,
        eight_questions=eight_questions,
    )


# ── 4. Fundamental Analysis ──

def render_fundamental_html(
    ts_code: str,
    name: str,
    analysis_type: str,
    metrics: dict[str, Any],
    sections: dict[str, str],
    financial_trends: dict | None = None,
    business_mix: dict | None = None,
) -> str:
    """Render fundamental analysis HTML report.

    Args:
        ts_code: Stock code.
        name: Stock name.
        analysis_type: Analysis type label.
        metrics: Dict of metric_name -> value.
        sections: Dict of section_title -> HTML content string.
        financial_trends: Dict with {years, metrics, series}.
        business_mix: Dict of category -> percentage.

    Returns:
        Rendered HTML string.
    """
    template = _env.get_template("analysis/fundamental.html")
    return template.render(
        title=f"{name} ({ts_code}) {analysis_type}分析报告",
        ts_code=ts_code,
        name=name,
        date=_now_str(),
        generated_at=_now_str(),
        data_source="基本面分析",
        metrics=metrics,
        sections={k: Markup(v) for k, v in sections.items()} if sections else {},
        financial_trends=financial_trends,
        business_mix=business_mix,
    )


# ── 5. Chanlun (缠论) Analysis ──

def render_chanlun_html(
    ts_code: str,
    name: str,
    chart_data: dict,
    macd_data: dict | None = None,
    signals: list[dict] | None = None,
    summary: dict | None = None,
) -> str:
    """Render chanlun (缠论) analysis HTML report.

    Args:
        ts_code: Stock code.
        name: Stock name.
        chart_data: Dict with {dates, candles, fractals, strokes, centers, signals}
                    for ECharts candlestick with chanlun overlays.
                    - dates: list of date strings
                    - candles: list of [open, close, low, high] arrays
                    - fractals: list of {date, type(top/bottom), price}
                    - strokes: list of [date, price] pairs for the bi line
                    - centers: list of {start_date, end_date, high, low, level}
                    - signals: list of {date, type(buy/sell), price, reason}
        macd_data: Dict with {dates, dif, dea, macd} for MACD sub-chart.
        signals: List of dicts with {date, type(buy/sell), price, level, reason}.
        summary: Dict with {current_level, center_count, last_signal, trend}.

    Returns:
        Rendered HTML string.
    """
    template = _env.get_template("technical/chanlun.html")
    return template.render(
        title=f"{name} ({ts_code}) 缠论分析报告",
        ts_code=ts_code,
        name=name,
        date=_now_str(),
        generated_at=_now_str(),
        data_source="缠论分析引擎",
        chart_data=chart_data,
        macd_data=macd_data,
        signals=signals,
        summary=summary,
    )


# ── 6. Signal Report ──

def render_signal_html(
    ts_code: str,
    signal_type: str,
    price: float,
    reason: str,
    chart_data: dict | None = None,
    name: str = "",
) -> str:
    """Render signal HTML report.

    Args:
        ts_code: Stock code.
        signal_type: 'buy' or 'sell'.
        price: Trigger price.
        reason: Trigger reason description.
        chart_data: Optional dict with {dates, prices, signal_date, signal_price, signal_type}.
        name: Optional stock name.

    Returns:
        Rendered HTML string.
    """
    template = _env.get_template("signal.html")
    return template.render(
        title=f"{ts_code} 信号报告",
        ts_code=ts_code,
        name=name,
        date=_now_str(),
        generated_at=_now_str(),
        data_source="信号扫描引擎",
        signal_type=signal_type,
        price=f"{price:.2f}",
        reason=reason,
        chart_data=chart_data,
    )


# ── 7. Markdown Fallback ──

def render_from_markdown(md_path: str) -> str:
    """Convert an existing Markdown report to styled HTML using base.html layout.

    Args:
        md_path: Path to the Markdown file.

    Returns:
        Rendered HTML string.

    Raises:
        FileNotFoundError: If md_path does not exist.
    """
    path = Path(md_path)
    if not path.exists():
        raise FileNotFoundError(f"Markdown file not found: {md_path}")

    md_text = path.read_text(encoding="utf-8")

    try:
        import markdown as md_lib
        html_body = md_lib.markdown(md_text, extensions=["tables", "fenced_code", "toc"])
    except ImportError:
        # Fallback: basic conversion
        import re
        html_body = md_text
        # Headers
        html_body = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html_body, flags=re.MULTILINE)
        html_body = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html_body, flags=re.MULTILINE)
        html_body = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html_body, flags=re.MULTILINE)
        # Bold
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        # Line breaks
        html_body = html_body.replace("\n\n", "</p><p>")
        html_body = f"<p>{html_body}</p>"

    # Extract title from first h1
    title = "分析报告"
    for line in md_text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # Extract ts_code if present
    ts_code = ""
    for line in md_text.splitlines():
        if "Stock/ETF Code" in line or "标的" in line:
            parts = line.split(":")
            if len(parts) > 1:
                ts_code = parts[-1].strip().strip("*").strip()
                break

    template = _env.get_template("markdown_report.html")
    return template.render(
        title=title,
        ts_code=ts_code,
        date=_now_str(),
        generated_at=_now_str(),
        data_source="Markdown conversion",
        content=Markup(html_body),
    )


# ── 8. Stock Screening (选股策略) ──

def render_screening_html(
    strategy_name: str,
    date: str,
    funnel: dict,
    tier1: list[dict],
    tier2: list[dict],
    tier3: list[dict],
    ablation: list[dict],
    risks: list[str],
    position_advice: str,
) -> str:
    """Render stock screening HTML report.

    Args:
        strategy_name: Strategy name (e.g. '选美博弈').
        date: Report date string (e.g. '2026-06-28').
        funnel: Dict with keys {universe, layer1, layer2, layer3, final} (int counts).
        tier1: List of stock dicts with {ts_code, name, industry, roe, revenue_yoy, pe, composite_score}.
        tier2: Same format as tier1.
        tier3: Same format as tier1.
        ablation: List of config dicts with {label, stock_count, avg_roe, avg_score, delta_score}.
                  delta_score can be None for the baseline config.
        risks: List of risk warning strings.
        position_advice: Position recommendation text.

    Returns:
        Rendered HTML string.
    """
    template = _env.get_template("screening.html")
    return template.render(
        title=f"{strategy_name} 选股报告 {date}",
        ts_code="",
        name="",
        strategy_name=strategy_name,
        date=date,
        generated_at=_now_str(),
        data_source="选股策略引擎",
        funnel=funnel,
        tier1=tier1,
        tier2=tier2,
        tier3=tier3,
        ablation=ablation,
        risks=risks,
        position_advice=position_advice,
    )
