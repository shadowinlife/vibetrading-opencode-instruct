---
name: html-report
description: |
  生成带 ECharts 交互图表的美观 HTML 报告。Use when: 生成报告、HTML输出、ECharts、图表、回测报告、分析报告、缠论报告、七看八问。
  支持 6 种专用模板：Vibe-Trading 回测、Alpha158 Walk-Forward、七看八问、基本面分析、缠论分析、信号报告。
argument-hint: "报告类型 + 股票代码"
user-invocable: true
triggers:
  - html report
  - HTML 报告
  - 生成 HTML
  - html-report
---

# HTML Report Skill

Use this skill to generate beautiful, interactive HTML reports with ECharts charts.

## When to Use

After completing any analysis, backtest, or signal scan that produces a Markdown report, **also generate an HTML report** using the appropriate renderer.

## Available Templates

| Template | Renderer Function | Best For |
|----------|-------------------|----------|
| Vibe-Trading 回测 | `render_vibe_backtest_html()` | 12 KPI cards + equity curve + drawdown + trades table |
| Alpha158 Walk-Forward | `render_alpha158_backtest_html()` | Multi-strategy comparison + best strategy highlight |
| 七看八问 | `render_seven_look_html()` | Radar chart + financial trends + peer comparison |
| 基本面分析 | `render_fundamental_html()` | KPI cards + financial trends + business mix pie |
| 缠论分析 | `render_chanlun_html()` | K-line candlestick + fractals/strokes/centers + buy/sell signals |
| 信号报告 | `render_signal_html()` | Signal summary + price chart with markers |
| Markdown fallback | `render_from_markdown()` | Convert existing .md reports to styled HTML |

## Design Constraints (Hard Rules)

Every generated HTML must follow these constraints:

### Typography
- **CJK-first font stack**: `"Noto Sans SC", "Source Han Sans SC", "PingFang SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`
- **Monospace**: `"JetBrains Mono", "Fira Code", "SF Mono", monospace`
- **8px baseline grid**: All spacing, line-height, and font-size must be multiples of 8px

### Colors
- **No pure black/white**: Use `#fafaf9` (light bg) and `#1c1917` (dark bg)
- **Contrast ratio ≥ 4.5** (WCAG AA) for all text
- **Semantic colors**: success `#16a34a`, warning `#d97706`, danger `#dc2626`, accent `#2563eb`

### Layout
- **Single-file HTML**: All CSS and JS inline, no external dependencies except ECharts CDN
- **ECharts CDN**: `https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js`
- **Chart containers must have fixed height**: `<div style="position:relative;height:280px">` (KPI sparklines ~40px, main charts ~280px)
- **Responsive**: Works from 375px mobile to 1440px desktop
- **Dual theme**: Light/dark via `prefers-color-scheme` media query

### Anti-Slop Rules
- Must use real data — no lorem ipsum or placeholder content
- No decorative blobs, waves, or geometric patterns
- No emoji as primary visual elements
- No purple/blue gradient backgrounds as default
- Rounded corners (`--radius: 8px`) + soft shadows

## Workflow

1. Determine the report type from the analysis context.
2. Prepare the data structure matching the renderer function's parameters.
3. Call the appropriate renderer from `scripts/reports/html_renderer.py`:
   ```python
   import sys; sys.path.insert(0, "/workspace/.opencode/skills/html-report")
   from scripts.reports.html_renderer import render_vibe_backtest_html
   html = render_vibe_backtest_html(ts_code="588000.SH", name="科创50ETF", ...)
   ```
4. Save the HTML to `analysis/<stock_code>/backtests/<date>_<strategy>.html` (alongside the .md file).
5. Deploy to local nginx:
   ```bash
   python /workspace/.opencode/skills/html-report/scripts/reports/deploy_report.py <html_path> --stock <stock_code>
   ```
6. Record the deployed URL in `analysis/_index.json` under `html_url`.
7. If DingTalk notification is configured, the notifier will include the report URL as an ActionCard button.

## Fallback: Markdown to HTML

For existing Markdown reports that don't have structured data:
```python
from scripts.reports.html_renderer import render_from_markdown
html = render_from_markdown("analysis/588000/backtests/2026-06-04_dual_ma_momentum.md")
```

## Storage Contract

- HTML reports live alongside Markdown reports in the same directory
- File extension: `.html` (same base name as the `.md` file)
- Both formats are kept — Markdown for archival, HTML for interactive viewing
- **Local nginx URL**: `http://localhost:8088/reports/<stock_code>/<filename>.html`
- **Nginx root**: `/workspace/reports/`
- **Remote nginx URL** (optional): `http://your-server.example.com/reports/<stock_code>/<filename>.html`

## Rules

- Always generate HTML after generating Markdown (never skip the HTML step).
- Never use external CSS frameworks (Bootstrap, Tailwind CDN).
- Never use CDN fonts (system font stack is sufficient).
- Never generate multi-file output (everything in one `.html` file).
- Chart containers must always have explicit height to prevent ECharts resize loops.
