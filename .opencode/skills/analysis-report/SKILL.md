---
name: analysis-report
description: |
  标准化分析报告生成与归档。Use when: 生成报告、保存分析、分析报告、存储结果、历史报告查询、更新 analysis/_index.json。
argument-hint: "股票代码 + 分析类型 + 简短描述"
user-invocable: true
---

# Analysis Report Skill

Use this skill whenever an analysis, backtest, or strategy review needs to be saved as a durable project artifact.

## Storage Contract

- Root directory: `analysis/`
- Template: `analysis/_template.md`
- Index: `analysis/_index.json`
- Stock report path: `analysis/<stock_code>/<YYYY-MM-DD>_<analysis_type>_<brief_desc>.md`
- Backtest report path: `analysis/<stock_code>/backtests/<YYYY-MM-DD>_<strategy_name>.md`

## Required Report Sections

Every saved report must include the five sections from `_template.md`:

1. Basic Information
2. Strategy / Method Description
3. Key Metrics
4. Signal Timeline
5. Conclusion and Recommendations

## Workflow

1. Read `analysis/_template.md`.
2. Extract the stock/ETF code, name, analysis date, analysis type, method, data sources, metrics, signal timeline, conclusion, and risk notes from the completed analysis.
3. Create `analysis/<stock_code>/` if missing.
4. For backtests, create `analysis/<stock_code>/backtests/` and save the report there.
5. Fill the template without deleting required sections.
6. Update `analysis/_index.json` with a new object:

```json
{
  "stock_code": "601777.SH",
  "name": "",
  "analysis_date": "2026-06-03",
  "analysis_type": "backtest",
  "title": "Alpha158 composite backtest",
  "path": "analysis/601777.SH/backtests/2026-06-03_alpha158_composite.md",
  "tags": ["backtest", "alpha158"],
  "created_at": "2026-06-03T00:00:00Z"
}
```

7. Validate `_index.json` with `python -m json.tool analysis/_index.json`.

## Rules

- Do not store temporary scratch files in `analysis/`.
- Do not omit risk notes.
- Do not overwrite existing reports unless explicitly updating the same analysis artifact.
- Keep filenames concise and stable.
- Keep `_index.json` machine-readable and valid JSON.
