---
name: stock-analysis-workflow
description: |
  股票/ETF 投资分析工作流：本地 DuckDB 定量分析 + 可靠来源定性分析 + 标准报告。Use when: 分析、投资价值、估值、基本面分析、ETF估值、股票研究。
argument-hint: "股票/ETF代码或名称 + 分析目标"
user-invocable: true
---

# Stock Analysis Workflow Skill

This skill is for investment decision analysis. It complements `stock-quant-analysis`, which focuses on quantitative strategy backtesting.

## Scope Boundary

- Use this skill for: valuation, fundamentals, industry context, bull/bear thesis, qualitative logic, and written reports.
- Use `stock-quant-analysis` for: Alpha158 backtests, technical signal backtests, Chanlun evaluation, and strategy optimization.
- Use `analysis-report` after finishing to store the final report in `analysis/<stock_code>/`.

## Standard Workflow

1. Clarify the target stock/ETF if ambiguous.
2. Check local historical reports in `analysis/<stock_code>/` and `_index.json`.
3. Prefer local DuckDB data for quantitative analysis.
4. Write a Python script under `./tmp/<session_id>_*` for calculations.
5. Use external sources only for information that cannot be quantified locally.
6. For external information, prefer reliable primary sources and cite them.
7. Synthesize results into a structured report.
8. Save the report with `analysis-report`.

## Report Structure

Reports must include:

1. Introduction
2. Data and Methodology
3. Quantitative Results
4. Qualitative Analysis
5. Conclusion and Risks

## Rules

- Do not skip local DuckDB checks when data may exist.
- Do not use unverified web claims as evidence.
- Do not write final reports outside `analysis/` except temporary drafts in `tmp/`.
- Do not make unsupported price predictions.
