---
name: stock-quant-analysis
description: |
  单股量化分析入口：Alpha158因子回测、传统信号回测、缠论评估、自定义策略规划、RD-Agent强化学习预留。
  触发词：量化分析、回测、选股、技术面、缠论、Alpha158、均线策略、动量策略、HPO超参优化。
  Use when: 用户要求对单个股票/ETF进行全面量化分析、策略回测、技术面评估。
argument-hint: "股票代码 (如 588000.SH, 601777.SH)"
user-invocable: true
---

# 单股量化分析 Skill

对单只 A 股或 ETF 执行全面量化分析，覆盖因子回测、传统信号、缠论结构三大维度。

## 执行流程

```
Phase 0  问询自定义策略
   │
   ├── 有自定义策略 ──→ Phase 1a  Prometheus 规划
   │
   └── 无自定义策略 ──→ Phase 1b  标准三模块并行
                            │
                            ├── M1: Alpha158 因子回测 + HPO
                            ├── M2: 传统信号回测
                            └── M3: 缠论结构分析
                            │
Phase 2  汇总分模块报告 ←──┘
```

## Phase 1b: 标准三模块并行分析

### M1: Alpha158 因子回测 + HPO
- 从 `stk_alpha158` 表加载 158 个量价因子
- 从 `stk_factor_pro` 加载后复权价格用于收益计算
- 构建多组信号策略，执行 Walk-Forward 回测
- HPO 超参搜索

### M2: 传统信号回测
- 基于经典技术指标构建信号 (MA交叉、MACD、RSI、布林带等)
- 每个信号独立回测，输出绩效指标

### M3: 缠论结构分析 + 买卖点回测
- K线合并与分型识别
- 笔、线段、中枢自动划分
- 三类买卖点识别与回测

## Phase 2: 汇总分模块报告
- 横向对比各策略的 Sharpe、年化收益、最大回撤、胜率
- 生成 `summary_report.md`

## 依赖与前置条件

| 依赖项 | 说明 |
|--------|------|
| Alpha158 因子 | M1 模块必需 |
| stk_factor_pro | 所有模块必需 (OHLCV + 后复权价格) |
| conda 环境 | Python 3.10+ |
| DuckDB | `./duckdb/ashare.duckdb` |
