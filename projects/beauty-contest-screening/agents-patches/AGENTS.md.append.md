# AGENTS.md Patches for beauty-contest-screening

Apply these sections to your project's root `AGENTS.md` file.

---

## Patch 1: Add to "组合能力（多 Skill 工作流）" table

**Insert location**: After the existing rows in the "组合能力" table (around line 100).

```markdown
| 场景 E: 选股策略 | scripts/screening + vibe-trading factor-research → analysis-report → html-report | 筛选 → 验证 → 报告 |
```

**编排原则** (append after existing principles):
```markdown
1. AGENTS.md 场景决定"用什么 Skill、什么顺序"
2. Skill 之间通过 `analysis/` 目录下的文件传递数据
3. 每个场景完成后必须引导用户进入下一个场景
4. 所有分析结果必须持久化到 `analysis/` 并更新 `_index.json`
```

---

## Patch 2: Add "场景 E：选股策略" section

**Insert location**: After 场景 D section, before "能力索引" section (around line 150).

```markdown
## 场景 E：选股策略（多标的筛选）

适用触发词：选股、筛选股票、找股票、选美、资金流选股、叙事选股、板块轮动选股、多因子选股

1. **Step 0: 明确选股范围和策略**
   - 确认选股范围（全 A 股 / 特定板块 / 特定市值 / 特定风格）
   - 确认选股策略（选美博弈 / 价值选股 / 质量选股 / 动量选股 / 自定义）
   - 确认输出数量（Top 5 / Top 10 / Top 20）
   - 若用户意图模糊，用 `question` 工具提供选项

2. **Step 1: 叙事识别（并行）**
   - `get_sector_info(mode="ranking")` → 板块涨幅排名
   - `get_northbound_flow()` → 北向资金方向
   - `screen_market()` → 成交额/涨幅/换手率 Top N
   - 可选: `sector_rotation_team` Swarm → 行业轮动深度分析
   - 可选: `sentiment_intelligence_team` Swarm → 情绪信号综合

3. **Step 2: 候选池生成**
   - 从叙事主线中提取候选标的（20-30 只）
   - **优先从 DuckDB 查询本地数据**（`fin_indicator`, `stk_factor_pro`）
   - 本地不足时用 `get_financial_statements()` 补充

4. **Step 3: 多层筛选**
   - 加载 `beauty-contest-screening` Skill
   - **Layer 1 基本面**: `scripts/screening/layer1_fundamental.py`（ROE >= 8%, 营收 > 0%, 净利 > -20%, OCF > 0, 非 ST）
   - **Layer 2 叙事动量**: 概念热度 + 研报覆盖 + 叙事阶段（四季法则）
   - **Layer 3 资金流共振**: 主力方向 + 北向偏好 + 融资趋势 + 筹码集中度
   - 每层输出通过/淘汰数量和名单

5. **Step 4: 量化验证（可选但推荐）**
   - vibe-trading `factor-research`: 对选股池做 IC/IR 截面分析
   - `scripts/backtest/`: Walk-Forward 回测验证
   - 消融表：每层增量 Sharpe + Fama-MacBeth 显著性

6. **Step 5: 报告输出**
   - `analysis-report`: 保存标准报告到 `analysis/screening_<date>/`
   - 更新 `analysis/_index.json`
   - `html-report`: 生成交互式 HTML 报告
   - 部署到 nginx + 钉钉推送

7. **Step 6: 后续引导**
   - 询问是否对 Top 标的做个股深入分析（→ 场景 A）
   - 询问是否回测验证（→ 场景 B）
   - 询问是否周期执行（→ 场景 D）
```

---

## Patch 3: Add to "能力索引" table

**Insert location**: Append a new row to the capability index table (around line 209).

```markdown
| **选股策略** | **`beauty-contest-screening` + 场景 E** | **选股、筛选、选美、资金流选股、多因子选股** |
```

---

## Patch 4: Add to "可用模板（7+1）" table in html-report section

**Insert location**: Append a new row to the HTML report templates table (around line 55).

```markdown
| 选股策略 | `render_screening_html()` | 漏斗图 + 分层表格 + 消融表 + 雷达图 |
```
