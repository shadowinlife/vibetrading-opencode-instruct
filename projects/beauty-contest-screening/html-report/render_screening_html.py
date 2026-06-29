"""Standalone render_screening_html function extracted from html_renderer.py.

This module can be used independently to generate screening HTML reports.
It requires:
- jinja2
- The templates in ./templates/ (base.html + screening.html)

Usage:
    from render_screening_html import render_screening_html
    html = render_screening_html(
        strategy_name="选美博弈",
        date="2026-06-28",
        funnel={"universe": 5000, "layer1": 878, "layer2": 244, "layer3": 813, "final": 20},
        tier1=[{"ts_code": "300502.SZ", "name": "新易盛", ...}],
        tier2=[...],
        tier3=[...],
        ablation=[{"label": "A: Layer 1 only", "stock_count": 20, "avg_roe": 43.30, "avg_score": 0.85, "delta_score": None}],
        risks=["风险1", "风险2"],
        position_advice="建议仓位...",
    )
    with open("report.html", "w") as f:
        f.write(html)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

# Template directory relative to this file
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


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
