"""Self-containment and opaque-origin behavior of the rendered reports.

Every renderer must emit a single file that passes the upload boundary's
self-contained check and carries the vendored ECharts build inline. The
theme-toggle script is executed in Node with a DOM stub to prove it
survives an opaque origin where localStorage access throws.
"""

from __future__ import annotations

import re
import subprocess

import pandas as pd
import pytest

from scripts.reports import html_renderer, upload_report

VENDORED_VERSION_MARKER = 't.version="6.1.0"'


def _alpha158_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "strategy": "s1",
                "full_total_ret": 0.41,
                "full_annual_ret": 0.067,
                "full_max_dd": -0.258,
                "full_calmar": 0.26,
                "full_trade_count": 12,
                "full_median_hold_days": 12.0,
                "positive_calmar_fold_ratio": 0.75,
                "excess_total_ret": 0.17,
                "buyhold_total_ret": 0.244,
                "buyhold_annual_ret": 0.04,
                "buyhold_max_dd": -0.3,
                "buyhold_calmar": 0.13,
                "full_max_hold_days": 40,
                "robust_calmar_ex_top3_days": 0.2,
            }
        ]
    )


def _render_all(tmp_path) -> dict[str, str]:
    md_path = tmp_path / "report.md"
    md_path.write_text("# 分析报告\n\n正文 **加粗**。\n", encoding="utf-8")
    return {
        "vibe_backtest": html_renderer.render_vibe_backtest_html(
            ts_code="588000.SH",
            name="科创50ETF",
            strategy_name="双均线",
            kpis={"总收益": {"value": "+41.2%", "change": "+3.8%", "change_positive": True}},
            equity_curve={"dates": ["2024-01", "2025-01"], "strategy": [0, 41.2], "buyhold": [0, 24.4]},
        ),
        "alpha158": html_renderer.render_alpha158_backtest_html(
            summary_df=_alpha158_frame(),
            best_strategy="s1",
            coverage={"start": "2020-01-01", "end": "2025-01-01", "bars": 1200},
            ts_code="588000.SH",
        ),
        "seven_look": html_renderer.render_seven_look_html(
            ts_code="600519.SH",
            name="贵州茅台",
            scores={"profit_quality": 8, "roe": 9},
        ),
        "fundamental": html_renderer.render_fundamental_html(
            ts_code="600519.SH",
            name="贵州茅台",
            analysis_type="基本面",
            metrics={"ROE": "32.8%"},
            sections={"业务构成": "<p>白酒</p>"},
        ),
        "chanlun": html_renderer.render_chanlun_html(
            ts_code="600519.SH",
            name="贵州茅台",
            chart_data={
                "dates": ["2026-01-01", "2026-01-02"],
                "candles": [[1600, 1650, 1580, 1680], [1650, 1620, 1600, 1660]],
                "fractals": [],
                "strokes": [],
                "centers": [],
                "signals": [],
            },
            signals=[{"date": "2026-01-02", "type": "buy", "price": 1620, "reason": "一买"}],
        ),
        "signal": html_renderer.render_signal_html(
            ts_code="588000.SH", signal_type="buy", price=1.05, reason="金叉"
        ),
        "screening": html_renderer.render_screening_html(
            strategy_name="选美博弈",
            date="2026-11-24",
            funnel={"universe": 100, "layer1": 50, "layer2": 20, "layer3": 10, "final": 5},
            tier1=[
                {
                    "ts_code": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "roe": 30,
                    "revenue_yoy": 15,
                    "pe": 25,
                    "composite_score": 2.5,
                }
            ],
            tier2=[],
            tier3=[],
            ablation=[
                {"label": "base", "stock_count": 5, "avg_roe": 30, "avg_score": 2.5, "delta_score": None}
            ],
            risks=["叙事退潮风险"],
            position_advice="单票 ≤ 20%",
        ),
        "markdown": html_renderer.render_from_markdown(str(md_path)),
    }


def test_every_renderer_is_self_contained_with_inline_echarts(tmp_path):
    for name, html in _render_all(tmp_path).items():
        upload_report.assert_self_contained(html)
        assert VENDORED_VERSION_MARKER in html, f"{name}: vendored ECharts not inlined"
        assert "cdn.jsdelivr.net" not in html, f"{name}: CDN reference survived"
        assert "PingFang SC" in html, f"{name}: system CJK font stack missing"


# Minimal DOM stub; the shipped theme script runs against it unmodified.
# mode=opaque: localStorage access throws (CSP sandbox allow-scripts).
# mode=normal: localStorage works and writes are recorded.
_NODE_HARNESS = r"""
const fs = require('fs');
const mode = process.argv[2];
const script = fs.readFileSync(process.argv[3], 'utf8');

function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    toggle: (c, on) => { if (on) set.add(c); else set.delete(c); },
    contains: (c) => set.has(c),
  };
}
function makeButton(theme) {
  return {
    theme,
    classList: makeClassList(),
    handlers: {},
    getAttribute(name) { return name === 'data-theme' ? this.theme : null; },
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
  };
}
const buttons = ['light', 'auto', 'dark'].map(makeButton);
const body = { classList: makeClassList() };
global.document = {
  body,
  getElementById: (id) => (id === 'theme-toggle' ? { querySelectorAll: () => buttons } : null),
  querySelectorAll: () => [],
};
const writes = [];
global.window = {};
if (mode === 'opaque') {
  Object.defineProperty(global.window, 'localStorage', {
    get() { throw new Error('SecurityError: opaque origin'); },
  });
} else {
  const store = new Map();
  global.window.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, v); writes.push([k, v]); },
  };
}
eval(script);
buttons[2].handlers.click.call(buttons[2]);
if (!body.classList.contains('theme-dark')) {
  console.error('FAIL: dark theme not applied after click');
  process.exit(1);
}
if (mode === 'normal' && !writes.some(([k, v]) => k === 'report-theme' && v === 'dark')) {
  console.error('FAIL: theme not persisted when storage is available');
  process.exit(1);
}
console.log('OK');
"""


@pytest.mark.parametrize("mode", ["opaque", "normal"])
def test_theme_toggle_survives_opaque_origin(tmp_path, mode):
    html = html_renderer.render_signal_html(
        ts_code="588000.SH", signal_type="buy", price=1.05, reason="金叉"
    )
    script_blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    theme_script = script_blocks[-1]
    assert "theme-toggle" in theme_script

    harness_path = tmp_path / "harness.js"
    harness_path.write_text(_NODE_HARNESS, encoding="utf-8")
    script_path = tmp_path / "theme.js"
    script_path.write_text(theme_script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness_path), mode, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
