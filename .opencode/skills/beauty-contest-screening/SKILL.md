---
name: beauty-contest-screening
description: |
  选美博弈选股策略：基本面 × 叙事动量 × 资金流三层筛选。
  触发词：选股、筛选股票、选美、资金流选股、叙事选股、多因子选股、板块轮动选股。
  Use when: 用户要求从全市场或特定范围中筛选股票，使用多层过滤策略。
argument-hint: "选股范围 + 策略类型 + 输出数量"
user-invocable: true
---

# 选美博弈选股策略 Skill

基于凯恩斯选美博弈理论的三层股票筛选框架。核心理念：选股不是选"好公司"，而是选"多数人即将选择的公司"。

## 理论基础

> "投资如同报纸选美比赛：获奖的不是你认为最美的，而是你认为**多数人认为最美**的。" — John Maynard Keynes

三层筛选逻辑：
- **基本面**是入场券（备选者必须足够"美"）
- **叙事**是催化剂（故事正在被更多人传播）
- **资金流**是验证（评委团正在用脚投票）

### 学术支撑

| 研究 | 核心发现 | 对本策略的启示 |
|------|---------|-------------|
| Shiller (2017) 叙事经济学 | 叙事像病毒传播，驱动资产价格 | 选叙事"正在加速"而非"已经最热"的 |
| Lou (2012) RFS | 资金流驱动的动量持续 3-12 月 | 跟随资金流方向，但注意反转 |
| Frazzini & Lamont (2008) | 高流入股长期跑输 4-10%/年 | 基本面筛选是防"击鼓传花"的关键 |
| AFA 2025 叙事注意力定价 | 趋势叙事 vs 衰退叙事：7% 年价差 | 叙事动量是独立于传统因子的 alpha |
| BigQuant 2023 概念动量 | 概念动量 1.35%/月，独立于 FF5 因子 | A 股概念轮动策略有统计显著性 |

## 执行流程

```
Phase 0  前置检查
   │  ├── 检查 analysis/_index.json 是否有历史选股报告
   │  └── 确认选股范围和策略类型
   │
Phase 1  叙事识别（并行）
   │  ├── get_sector_info(mode="ranking")     → 板块涨幅排名
   │  ├── get_northbound_flow()               → 北向资金方向
   │  ├── screen_market(sort_by="amount")     → 成交额 Top N
   │  └── 可选: sector_rotation_team Swarm    → 行业轮动深度分析
   │
Phase 2  候选池生成
   │  ├── 从叙事主线中提取候选标的（20-30 只）
   │  └── 优先从 DuckDB 查询本地数据
   │
Phase 3  三层筛选
   │  ├── Layer 1: 基本面 → scripts/screening/layer1_fundamental.py
   │  ├── Layer 2: 叙事动量 → scripts/screening/layer2_narrative.py
   │  └── Layer 3: 资金流 → scripts/screening/layer3_flow.py
   │
Phase 4  量化验证（可选）
   │  ├── vibe-trading factor-research → IC/IR 截面分析
   │  ├── scripts/backtest/ → Walk-Forward 回测
   │  └── 消融表：每层增量 Sharpe + 显著性
   │
Phase 5  报告输出
   │  ├── analysis-report → 保存标准报告 + 更新 _index.json
   │  ├── html-report → 生成交互式 HTML
   │  └── 钉钉推送
   │
Phase 6  后续引导
   │  ├── 个股深入分析 → 场景 A
   │  ├── 回测验证 → 场景 B
   │  └── 周期执行 → 场景 D
```

## 筛选条件标准

### Layer 1: 基本面筛选

通过 `scripts/screening/layer1_fundamental.py` 执行，使用 DuckDB 本地数据。

| 条件 | 阈值 | 字段 | 理由 |
|------|------|------|------|
| ROE（加权平均） | >= 8% | `fin_indicator.roe_waa` | 盈利能力底线 |
| 营收同比增长 | > 0% | `fin_indicator.or_yoy` | 成长性验证 |
| 净利润同比增长 | > -20% | `fin_indicator.netprofit_yoy` | 排除业绩恶化 |
| 经营现金流/股 | > 0 | `fin_indicator.ocfps` | 盈利质量验证 |
| ST 排除 | — | `stk_st_daily` | 规避退市风险 |
| 市值下限 | > 50 亿 | `stk_factor_pro.total_mv` | 排除微盘股 |

**调用方式**:
```python
from scripts.screening.layer1_fundamental import screen_fundamental
df = screen_fundamental()  # 默认参数
df = screen_fundamental(roe_min=15, trade_date="2026-06-27")  # 自定义
```

### Layer 2: 叙事动量筛选

| 条件 | 数据源 | 说明 |
|------|--------|------|
| 行业动量 | `idx_sw_classify` + `stk_factor_pro` | 申万行业涨幅排名 Top 30% |
| 成交额变化率 | `stk_factor_pro.amount` | 近 20 日 vs 近 60 日成交额比 |
| 换手率变化率 | `stk_factor_pro.turnover_rate` | 近 20 日 vs 近 60 日换手率比 |
| 研报覆盖 | `get_research_reports()` API | 近 30 日新增研报数 |

**叙事阶段判断**（招商证券"四季法则"）：

| 阶段 | 特征 | 操作 |
|------|------|------|
| 乘势期 | 少数人讲，股价开始反应 | 建仓 |
| 造势期 | 媒体扩散，资金跟进 | 持有/加仓 |
| 退势期 | 散户蜂拥，研报密集 | 减仓 |
| 休耕期 | 叙事耗尽，无人提起 | 回避 |

### Layer 3: 资金流共振

| 条件 | 数据源 | 说明 |
|------|--------|------|
| 主力净流入 | `stk_moneyflow.net_mf_amount` | 大单+超大单净流入 > 0 |
| 5 日主力方向 | `stk_moneyflow_ths.net_d5_amount` | 5 日主力净流入 > 0 |
| 融资余额增长 | `stk_margin.rzye` | 融资余额近 5 日增长 > 0 |
| 北向资金 | `get_northbound_flow()` API | 北向近 20 日净买入 |

## 输出格式

### 选股池分级

| 级别 | 条件 | 建议 |
|------|------|------|
| **Tier 1 强共振** | 三层全部通过 + 叙事处于造势期 | 核心配置 |
| **Tier 2 中共振** | 三层通过 + 叙事处于乘势期 | 配置 |
| **Tier 3 观察** | 基本面通过 + 叙事非主峰 | 等待叙事加速 |

### 必须包含的内容

1. 每层筛选的通过/淘汰数量和名单
2. 叙事阶段标注（乘势/造势/退势/休耕）
3. 资金流共振评分（⭐1-4）
4. 仓位建议（初始仓位 + Tier 分配）
5. 止损/止盈规则
6. 核心风险预警

## 选股策略模板

| 策略名 | Layer 1 | Layer 2 | Layer 3 | 适用场景 |
|--------|---------|---------|---------|---------|
| 选美博弈 | ROE+增长+现金流 | 叙事动量+阶段 | 资金流共振 | 趋势行情 |
| 价值选股 | PE/PB/股息率 | 行业景气度 | 北向+融资 | 价值回归 |
| 质量选股 | ROE+毛利率+现金流 | 研报覆盖 | 筹码集中 | 稳健配置 |
| 动量选股 | 营收增速+利润增速 | 概念热度 | 主力净流入 | 趋势跟踪 |

## 关键约束

- 选股结果必须标注叙事阶段，避免推荐退势期标的
- 必须给出仓位建议和止损规则
- 必须区分 Tier 1（强共振）和 Tier 2（中共振）
- 基本面筛选是硬门槛，不可因叙事热度跳过
- 优先使用 DuckDB 本地数据，本地不足时再用 API
- 完成后必须更新 `analysis/_index.json`

## 关联 Skill 和工具

| 用途 | Skill/工具 |
|------|-----------|
| 基本面筛选 | `scripts/screening/layer1_fundamental.py` |
| 因子 IC 分析 | vibe-trading `factor-research` |
| 多因子合成 | vibe-trading `multi-factor` |
| 回测验证 | `scripts/backtest/` (Walk-Forward + HPO) |
| 报告保存 | `analysis-report` |
| HTML 报告 | `html-report` (`render_from_markdown` 或 `render_screening_html`) |
| 周期执行 | `periodic-execution` |
| 行业轮动分析 | `sector_rotation_team` Swarm |
| 情绪信号分析 | `sentiment_intelligence_team` Swarm |
