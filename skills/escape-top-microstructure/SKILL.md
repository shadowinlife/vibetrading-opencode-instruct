---
name: escape-top-microstructure
description: |
  A 股市场逃顶（顶部预警）微观结构信号框架。Use when: 顶部预警、逃顶信号、市场拥挤度、两融背离、
  成交集中度、市场过热判断、大盘顶部风险、择时风控。
  提供 ~15 个信号族 + 7 门验证框架 + 集成判定（RED/YELLOW/GREEN），数据层为 Vibe-Trading ClickHouse connector。
argument-hint: "--preset strong|balanced|early|extended"
user-invocable: true
---

# Escape-Top Microstructure Skill

A 股大盘顶部检测信号框架：约 15 个信号族（成交集中度、两融背离、换手/市值过热、波动扩张、估值分位、
行业拥挤、大单衰竭、北向背离、ETF 热度、流动性收紧、宏观信用、国家队持仓、宽度背离等），
每个信号经 7 门验证框架（覆盖度 / 选择性 / 方向性 / 统计分离 / 稳健性 / 子周期 / 相关性）分级，
集成层以 AND / VOTE_K_OF_M / WEIGHTED_SCORE 输出 RED / YELLOW / GREEN 市场状态判定。

## 数据要求

- **ClickHouse ashare 仓库**：`CLICKHOUSE_HOST` / `CLICKHOUSE_PORT` / `CLICKHOUSE_USER` /
  `CLICKHOUSE_PASSWORD` / `CLICKHOUSE_DATABASE`（经 Vibe-Trading `src.clickhouse_connector` 读取，
  绝不硬编码凭据）。ClickHouse 不可达时所有 CLI 输出
  `{"available": false, "reason": "ClickHouse unreachable"}` 并以退出码 0 结束。
- **Tushare 外部端点**（仓库外数据，需 `TUSHARE_TOKEN`）：估值分位（index_dailybasic）、
  北向背离（moneyflow_hsgt）、流动性收紧（shibor / shibor_lpr）、宏观信用（cn_m / sf_month）。

## CLI 入口（均从 /workspace 运行）

```bash
# 集成判定（推荐入口）：按预设组合已验证信号，输出 RED/YELLOW/GREEN
python -m scripts.microstructure.escape_top_cli --preset strong      # 高选择性，宁缺毋滥
python -m scripts.microstructure.escape_top_cli --preset balanced    # 均衡
python -m scripts.microstructure.escape_top_cli --preset early       # 早预警，容忍误报
python -m scripts.microstructure.escape_top_cli --preset extended    # 含研究级信号

# 单信号族
python -m scripts.microstructure.concentration_cli        # 成交集中度（top5% 成交额份额）
python -m scripts.microstructure.margin_buy_vs_sse_cli    # 融资买入/两市成交背离

# 联合模式与调参
python -m scripts.microstructure.joint_escape_top_cli     # 按 condition_manifest.json 联合判定
python -m scripts.microstructure.tune_escape_top_cli      # 网格调参（按信号后前瞻回撤排序）
```

## 输出契约

- JSON 报告（stdout 或 `--output` 指定路径，默认 `tmp/microstructure/`），含信号值、阈值、
  触发状态、数据覆盖区间。
- 集成判定：`RED`（顶部风险高）/ `YELLOW`（预警）/ `GREEN`（正常）。
- 验证报告（`validate_*` 脚本）输出 ValidationReport，分类为
  `VALIDATED` / `REJECTED` / `RESEARCH_ONLY` / `BLOCKED_BY_DATA` / `BLOCKED_BY_PERMISSION`。

## 验证状态（必须随信号一并报告）

- **已验证（VALIDATED）**：`margin_divergence`（两融背离）、`volatility_atr_expansion`（波动扩张）
  —— 通过全部 7 门验证。
- **其余信号族**：RESEARCH_ONLY 或 BLOCKED_BY_DATA（如 `winner_rate_pressure` 依赖的
  stk_cyq_perf 已停止同步）—— 引用时必须标注其验证分类。

## 硬性规则

1. 信号是**决策辅助**，不是交易指令；任何信号输出必须附带其验证分类与数据覆盖区间。
2. 数据层错误必须向上传播（fail loud）：ClickHouse / Tushare 不可达时明说，绝不静默降级为中性结论。
3. 单位换算规则（千元/万元/元、手/股）见 `scripts/microstructure/source_registry.py`，不得绕过。
4. 集成判定只消费 `condition_manifest.json` 中登记的已验证条件；新增条件必须先过
   `generic_validator.py` 7 门验证。
