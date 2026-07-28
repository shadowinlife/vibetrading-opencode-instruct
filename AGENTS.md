# 环境
1. Python/数据分析前先 `source /opt/venv/bin/activate`。
2. 主数据源为 ClickHouse，通过 `data-warehouse` Skill 访问（`query_warehouse(sql)` 和 `list_tables()` 工具），不再使用 DuckDB。
3. 可复用 ClickHouse SQL 查询放 `./sql/`，视图定义文档放 `./docs/views/`。
4. 临时脚本、中间文件、下载材料放 `./tmp/<session-id>_*`。

# 数据采集能力
1. A 股分析优先用 `data-warehouse` Skill 查询 ClickHouse，覆盖 199 列 T-1 历史数据。
2. 当日 OHLCV 数据（T 日）由 ClickHouse 无法覆盖，使用 Vibe-Trading `get_market_data(source="auto"/"tencent"/"akshare")` 实时补数。
3. 外部资料优先用 MCP 通用搜索（`search mcp` 的 `search`/`fetch_page`/`search_with_template`）；A 股年报/季报/公告/研报用 `search mcp` 的新浪财经工具。
4. 本地 ClickHouse 找不到标的时，立即搜索判断是否为 ETF、港股、美股或其他市场代码，再选数据源。
5. 跨市场数据优先用 Vibe-Trading 数据能力：A 股可用 `tencent`/`tushare`/`akshare`，港美股优先 `yfinance`/`akshare`，不确定时用 `auto`。
6. 交易时间内若需实时信号，历史 K 线不足以判断，需补充 `akshare`/Yahoo Finance/交易连接 quote 尝试获取实时或近实时数据。
7. Vibe-Trading 的 `get_fund_flow`、`get_margin_trading`、`get_northbound_flow`、`get_sector_info` 等 Flow 工具可作为 ClickHouse 数据的补充维度。
8. 18:00 前当日盘后数据可能不可用，上游会收敛到上一开放交易日。

# 数据同步
ClickHouse 数据由外部同步进程维护，本容器内不包含同步逻辑。如需手动触发同步，请在同步进程所在环境执行。数据联邦模式：ClickHouse 提供 T-1 历史全量数据 + 网络源提供当日 OHLCV。

# 数据分析诉求
1. 对分析诉求尽量用 `data-warehouse` Skill 的 `query_warehouse()` 做最新定量分析；先写 Python 脚本再总结。
2. 定性分析必须给出明确结论、逻辑链和可靠来源；区分证据与预测。
3. 报告结构需包含引言、数据分析方法、分析结果、结论与风险。
4. 多个可选标的或需求含糊时，及时 human-in-the-loop 澄清。
5. 正式报告保存到 `analysis/<stock_code>/`，并更新 `analysis/_index.json`。
6. **分析完成后必须主动问询用户是否需要生成 HTML 交互报告**（详见下方 HTML 报告展示能力）。

# 投资决策纪律
1. **信号分级体系**（机械执行，逐级过滤）：
   - 信号层（Signal）：量化因子/技术指标产生的原始买卖信号，纯规则驱动，不受主观影响。
   - 规则层（Rule）：信号经过组合规则过滤（如多因子共振、基本面门槛），形成可执行候选。
   - 模型层（Model）：候选进入回测/风险模型评估，输出预期收益/风险/胜率。
   - LLM 判断层（Judgment）：LLM 仅做定性综合（如政策环境、市场情绪、极端事件），**不做数学计算**。
2. **五级评级体系**：强力买入（A+）/ 买入（A）/ 中性（B）/ 卖出（C）/ 强力卖出（C-），每级有明确量化门槛。
3. **LLM 禁止做数学**：LLM 不得自行计算收益率、估值倍数、回撤幅度等。所有数值必须来自量化工具输出，LLM 仅做引用和解读。
4. **输出自检清单**：每次投资建议输出前，LLM 必须自检：信号来源是否可追溯？数值是否有工具输出支撑？评级是否匹配量化门槛？

# 回测方法论底线
1. **基准对比强制**：所有回测必须与基准指数（沪深300/中证500/科创50）对比，报告超额收益、信息比率。
2. **Walk-Forward 验证**：回测必须使用 Walk-Forward 滚动窗口，训练集/测试集严格分离，禁止未来信息泄露。
3. **过拟合防护**：
   - 参数数量 ≤ 样本量的 1/10（参数越多，样本窗口越长）
   - 禁止在测试集上做参数调优后再报告"样本外"结果
   - 必须报告参数敏感性分析（参数 ±20% 后回测表现变化）
4. **滑点与成本建模**：必须计入交易成本（佣金+印花税+滑点），滑点用线性或平方根冲击模型。
5. **回测归因层级**：
   - 交易级归因：逐笔交易的盈亏拆解（赢家/输家）
   - Beta 归因：基准 Beta 回归，分离市场收益与 Alpha
   - 市场状态归因：牛/熊/震荡市分段表现
   - 蒙特卡洛置换检验：验证策略超额收益的统计显著性

# 风险管理硬约束
1. **仓位限制**：单票仓位 ≤ 20%，单行业仓位 ≤ 40%，总仓位 ≤ 100%（可转债/ETF 上限可适当放宽）。
2. **回撤限制**：策略最大回撤超过 20% 时强制暂停，回撤超过 30% 时强制清仓并复盘。
3. **交易前检查清单**（Pre-Trade Checklist）：
   - 信号是否在有效期内（当日/次日有效，过期作废）
   - 标的是否存在 ST/退市风险（`ashare-pre-st-filter` Skill 检查）
   - 是否存在限售解禁/大股东减持/监管处罚等负面事件
   - 仓位是否在限制范围内，保证金是否充足
4. **预警阈值**：
   - 单日亏损 ≥ 5% → 黄色预警，暂停新开仓
   - 单周亏损 ≥ 10% → 红色预警，减仓至 50%
   - 单月亏损 ≥ 15% → 黑色预警，清仓并强制复盘

# HTML 报告展示能力（html-report Skill）

## 概述
已安装 `html-report` Skill（来源: github.com/shadowinlife/vibetrading-html-report），可将分析/回测结果渲染为带 ECharts 交互图表的美观 HTML 页面，通过本地 nginx 提供服务。

## 基础设施
- **Skill 位置**: `.opencode/skills/html-report/`
- **渲染引擎**: `scripts/reports/html_renderer.py`（Jinja2 + ECharts）
- **部署脚本**: `scripts/reports/deploy_report.py`（默认本地部署到 nginx）
- **HTML 存放目录**: `./reports/<stock_code>/`
- **Nginx 配置**: 部署时按需配置
- **访问地址**: `http://<host>:8088/reports/<stock_code>/<report_name>.html`
- **Python 依赖**: jinja2, pandas, markupsafe（已在 venv 环境中安装）

## 可用模板（7+1）
| 模板 | 渲染函数 | 适用场景 |
|------|---------|---------|
| Vibe-Trading 回测 | `render_vibe_backtest_html()` | 12 KPI + 收益曲线 + 回撤 + 交易表 |
| Alpha158 Walk-Forward | `render_alpha158_backtest_html()` | 多策略对比 + 最优策略高亮 |
| 七看八问 | `render_seven_look_html()` | 雷达图 + 财务趋势 + 同行对比 |
| 基本面分析 | `render_fundamental_html()` | KPI 卡片 + 财务趋势 + 业务饼图 |
| 缠论分析 | `render_chanlun_html()` | K 线蜡烛图 + 分型/笔/中枢标注 |
| 信号报告 | `render_signal_html()` | 信号摘要 + 价格图标记 |
| 选股策略 | `render_screening_html()` | 漏斗图 + 分层表格 + 消融表 + 雷达图 |
| Markdown 转换 | `render_from_markdown()` | 现有 .md 报告转 HTML |

## 主动展示规则（MANDATORY）
**每次完成以下类型的分析后，必须主动问询用户是否需要生成 HTML 交互报告：**
1. 量化回测完成（场景 B）→ 提供回测报告 HTML
2. 基本面/投资分析完成（场景 A）→ 提供基本面分析或七看八问 HTML
3. 缠论分析完成 → 提供缠论分析 HTML
4. 信号扫描完成 → 提供信号报告 HTML
5. 事件驱动/行业分析完成 → 提供 Markdown 转 HTML

**问询话术示例**：
> "分析已完成。是否需要生成交互式 HTML 报告？可通过浏览器查看 ECharts 图表、暗色主题切换。"

## 模板自我迭代规则
当分析结果的数据结构或展示需求**无法被现有 7 种模板满足**时：
1. **主动告知用户**当前模板的局限性，并提出扩展建议。
2. **提出自我更新方案**：修改 Jinja2 模板文件或新增渲染函数。
3. **征求用户确认后执行**：直接编辑 `.opencode/skills/html-report/scripts/reports/templates/` 下的模板文件，或在 `html_renderer.py` 中新增函数。
4. **迭代后必须验证**：生成测试报告 → 部署 → HTTP 访问确认。

## 部署命令速查
```bash
# 生成并部署（默认本地 nginx）
python .opencode/skills/html-report/scripts/reports/deploy_report.py <html_path> --stock <stock_code>

# 仅生成不部署（保存到 analysis/ 目录）
# 直接 write() 到 analysis/<stock_code>/backtests/<date>_<strategy>.html
```

# 组合能力（多 Skill 工作流）

以下场景需要组合多个 Skill 完成，AGENTS.md 场景层负责编排，Skill 层负责执行：

| 场景 | 组合的 Skill | 数据流向 |
|------|-------------|---------|
| 场景 A: 个股分析 | data-warehouse → vibe-trading 基本面/估值 Skill → html-report | ClickHouse → 分析 → 报告 → HTML |
| 场景 B: 量化回测 | data-warehouse → strategy-generate / factor-research → backtest-diagnose → html-report | 因子 → 回测 → 诊断 → HTML |
| 场景 B2: Shadow Account | vibe-trading analyze_trade_journal → extract_shadow_strategy → run_shadow_backtest → render_shadow_report | 交割单 → 规则 → 回测 → 报告 |
| 场景 E: 选股策略 | data-warehouse + fundamental-filter → multi-factor / factor-research → html-report | 筛选 → 验证 → 报告 |
| 场景 D: 周期执行 | 任意分析 Skill + cron_jobs/manage.py + notifier | 分析 → 定时 → 通知 |

**编排原则**:
1. AGENTS.md 场景决定"用什么 Skill、什么顺序"
2. **数据优先从 data-warehouse Skill 获取**，本地不足时用 Vibe-Trading MCP 工具补数
3. 每个场景完成后必须引导用户进入下一个场景
4. 所有分析结果必须持久化到 `analysis/` 并更新 `_index.json`

# 客户引导流程

## 通用前置检查
用户提及任何股票/ETF 时，先检查：
1. `analysis/<stock_code>/` 历史分析报告；
2. `analysis/<stock_code>/backtests/` 历史回测；
3. `cron_jobs/registry.json` 中的周期任务。
若 ClickHouse 找不到标的，先搜索确认是否为 ETF、港股、美股或代码格式问题；若存在历史记录，先汇报摘要，再确认继续追踪还是发起新分析。

## 场景 A：股票/ETF 分析
1. Step 0：执行通用前置检查。
2. Step 1：必须做量化回测询问：是否需要对该标的进行量化策略回测？是则进入场景 B。
3. Step 2：数据源选择：A 股优先 data-warehouse Skill（ClickHouse）→ Vibe-Trading `get_market_data(source="tencent"/"akshare"/"tushare")` 补当日数据；ETF/港股/美股若 ClickHouse 无数据，用 Vibe-Trading `get_market_data(source="auto"/"yfinance"/"akshare")` 或交易 quote 能力补数。
4. Step 3：分析方法选择：基本面用 `2min-company-analysis`（七看八问），估值用 `valuation-model`，风险预警用 `escape-top-microstructure`，专家团队必须主动提供 `vibe-trading_run_swarm`（如 investment_committee/quant_strategy_desk），内置方法用 `vibe-trading_list_skills`/`vibe-trading_load_skill`。
5. Step 4：交易时间内或用户问"现在能不能买/卖"时，补充 akshare/Yahoo/quote 近实时数据，给出实时信号与数据延迟说明。
6. Step 5：完成后必须用 `report-generate` 生成标准报告。
7. Step 6：**主动问询是否需要生成 HTML 交互报告**（加载 `html-report` Skill）。
8. Step 7：Skills 发现：询问是否需要了解当前可用的所有分析 Skills。

## 场景 B：量化回测
1. Step 0：执行通用前置检查，重点看历史回测。
2. Step 1：必须询问是否使用 Vibe-Trading 的全套因子回测策略；说明增量能力包括 `alpha-zoo`、`technical-basic`、`ml-strategy`、`factor-research`、`multi-factor`、`backtest-diagnose`、`pine-script`、`vnpy-export`。
3. Step 2：加载 `strategy-generate` 并使用 vibe-trading 回测引擎（`vibe-trading_backtest`）。
4. Step 3：用 `report-generate` 保存回测报告到 `analysis/<stock_code>/backtests/`。
5. Step 4：**主动问询是否需要生成 HTML 交互回测报告**（加载 `html-report` Skill，使用 `render_vibe_backtest_html()` 或 `render_alpha158_backtest_html()`）。
6. Step 5：询问是否需要回测诊断（`backtest-diagnose`）、实盘策略导出（`pine-script`/`vnpy-export`）或风险评估（`risk-analysis`）。
7. Step 6：若回测推导出后续买入/卖出位，询问是否让 crontab 在次日/交易时间用实时数据监控触发。
8. Step 7：若结果可跟踪，询问是否周期性自动执行并提醒。

## 场景 B2：Shadow Account（交割单诊断）
1. Step 0：用户上传交割单 CSV/Excel 文件。
2. Step 1：加载 `trade-journal` Skill，调用 `vibe-trading_analyze_trade_journal` 解析交易行为。
3. Step 2：调用 `vibe-trading_extract_shadow_strategy` 提炼盈利模式（3-5 条人话规则）。
4. Step 3：调用 `vibe-trading_run_shadow_backtest` 跨市场回测验证。
5. Step 4：调用 `vibe-trading_render_shadow_report` 生成 8-section PDF/HTML 报告。
6. Step 5：引导用户进入场景 B（对 Shadow 策略做深度回测）或场景 D（周期执行）。

## 场景 C：开放性问题
1. 对"最近买什么股票好""有什么投资机会"等开放性问题，必须启动 OMO Prometheus 多轮收敛。
2. 逐步缩小范围：市场、风格、行业、风险偏好、周期、资金规模。
3. 可映射能力：`sector-rotation`、`multi-factor`、`asset-allocation`、`risk-analysis`、`fundamental-filter`。
4. 收敛后给 2-3 个可执行方案，再进入场景 A 或 B。

## 场景 D：策略周期执行
1. 对满意的分析/回测策略，询问是否周期性自动执行并提醒。
2. 确认执行频率、监控标的、通知方式、信号阈值；若是实时信号，确认盘中频率、数据源（akshare/Yahoo/quote）和延迟容忍度。
3. 使用 `cron_jobs/manage.py` 注册、验证和管理任务。

## 场景 E：选股策略（多标的筛选）

适用触发词：选股、筛选股票、找股票、选美、资金流选股、叙事选股、板块轮动选股、多因子选股

**理论基础**：凯恩斯选美博弈 — 选股不是选"好公司"，而是选"多数人即将选择的公司"。
- 基本面是入场券（备选者必须足够"美"）→ 使用 VT `fundamental-filter`
- 叙事是催化剂（故事正在被更多人传播）→ 本场景独特方法论
- 资金流是验证（评委团正在用脚投票）→ VT flow 工具

**学术支撑**：Shiller (2017) 叙事经济学 / Lou (2012 RFS) 资金流动量 / AFA 2025 叙事注意力定价 / BigQuant 2023 A 股概念动量

---

### Step 0: 明确选股范围和策略
- 确认选股范围（全 A 股 / 特定板块 / 特定市值 / 特定风格）
- 确认选股策略（见下方策略模板）
- 确认输出数量（Top 5 / Top 10 / Top 20）
- 若用户意图模糊，用 `question` 工具提供选项

### Step 1: 叙事识别（并行执行）
- `get_sector_info(mode="ranking")` → 板块涨幅排名
- `get_northbound_flow()` → 北向资金方向
- `screen_market(sort_by="amount")` → 成交额 Top N
- 可选: `sector_rotation_team` Swarm → 行业轮动深度分析

### Step 2: 候选池生成
- 从叙事主线中提取候选标的（20-30 只）
- **优先从 data-warehouse Skill 查询 ClickHouse**（fin_indicator, stk_factor_pro 等）
- ClickHouse 不足时用 `get_financial_statements()` 补充

### Step 3: 三层筛选

**Layer 1 — 基本面（硬门槛，不可跳过）**：

| 条件 | 阈值 | 理由 |
|------|------|------|
| ROE（加权平均） | >= 8% | 盈利能力底线 |
| 营收同比增长 | > 0% | 成长性验证 |
| 净利润同比增长 | > -20% | 排除业绩恶化 |
| 经营现金流/股 | > 0 | 盈利质量验证 |
| ST 排除 | — | 规避退市风险 |
| 市值下限 | > 50 亿 | 排除微盘股 |

使用 `fundamental-filter` Skill + `scripts/screening/layer1_fundamental.py` 执行。

**Layer 2 — 叙事动量**：

| 条件 | 数据源 | 说明 |
|------|--------|------|
| 行业动量 | ClickHouse `idx_sw_classify` + `stk_factor_pro` | 申万行业涨幅排名 Top 30% |
| 成交额变化率 | ClickHouse `stk_factor_pro.amount` | 近 20 日 vs 近 60 日成交额比 |
| 换手率变化率 | ClickHouse `stk_factor_pro.turnover_rate` | 近 20 日 vs 近 60 日换手率比 |
| 研报覆盖 | `get_research_reports()` | 近 30 日新增研报数 |

**叙事阶段判断（招商证券"四季法则"）**：

| 阶段 | 特征 | 操作 |
|------|------|------|
| 乘势期 | 少数人讲，股价开始反应 | 建仓（Tier 2） |
| 造势期 | 媒体扩散，资金跟进 | 持有/加仓（Tier 1） |
| 退势期 | 散户蜂拥，研报密集 | 减仓/回避 |
| 休耕期 | 叙事耗尽，无人提起 | 回避 |

**Layer 3 — 资金流共振**：

| 条件 | 数据源 | 说明 |
|------|--------|------|
| 主力净流入 | `get_fund_flow()` | 大单+超大单净流入 > 0 |
| 融资余额增长 | `get_margin_trading()` | 近 5 日融资余额增长 > 0 |
| 北向资金 | `get_northbound_flow()` | 北向近 20 日净买入 |

### Step 4: 量化验证（可选但推荐）
- VT `factor-research`: 对选股池做 IC/IR 截面分析
- `scripts/backtest/`: Walk-Forward 回测验证
- 消融表：每层增量 Sharpe + Fama-MacBeth 显著性

### Step 5: 报告输出与分级

**Tier 分级**：
| 级别 | 条件 | 建议 |
|------|------|------|
| **Tier 1 强共振** | 三层全部通过 + 叙事处于造势期 | 核心配置 |
| **Tier 2 中共振** | 三层通过 + 叙事处于乘势期 | 配置 |
| **Tier 3 观察** | 基本面通过 + 叙事非主峰 | 等待叙事加速 |

**必须包含**：每层通过/淘汰名单、叙事阶段标注、资金流共振评分（⭐1-4）、仓位建议、止损/止盈规则、核心风险预警。

- `report-generate`: 保存标准报告到 `analysis/screening_<date>/`
- 更新 `analysis/_index.json`
- `html-report`: 使用 `render_screening_html()` 生成交互式 HTML

### Step 6: 后续引导
- 询问是否对 Top 标的做个股深入分析（→ 场景 A）
- 询问是否回测验证（→ 场景 B）
- 询问是否周期执行（→ 场景 D）

### 策略模板速查

| 策略 | Layer 1 侧重 | Layer 2 侧重 | Layer 3 侧重 | 适用场景 |
|------|-------------|-------------|-------------|---------|
| 选美博弈 | ROE+增长+现金流 | 叙事动量+阶段 | 资金流共振 | 趋势行情 |
| 价值选股 | PE/PB/股息率 | 行业景气度 | 北向+融资 | 价值回归 |
| 质量选股 | ROE+毛利率+现金流 | 研报覆盖 | 筹码集中 | 稳健配置 |
| 动量选股 | 营收增速+利润增速 | 概念热度 | 主力净流入 | 趋势跟踪 |

### 关键约束
- 选股结果必须标注叙事阶段，避免推荐退势期标的
- 基本面筛选是硬门槛，不可因叙事热度跳过
- 必须给出仓位建议和止损规则
- 优先使用 data-warehouse Skill（ClickHouse），本地不足时再用 API

# 能力索引
| 能力 | Skill/工具 | 触发词 |
|---|---|---|
| **ClickHouse 数据仓库** | **`data-warehouse`（query_warehouse + list_tables）** | **取数、查数据、数据查询、ClickHouse** |
| 因子研究 | `factor-research` (vibe-trading) | IC/IR、因子分析、截面分析 |
| 多因子选股 | `multi-factor` (vibe-trading) | 多因子、截面排名、组合构建 |
| 策略生成与回测 | `strategy-generate` (vibe-trading) + `vibe-trading_backtest` | 回测、backtest、策略、Walk-Forward |
| 回测诊断 | `backtest-diagnose` (vibe-trading) | 回测失败、诊断、策略优化 |
| Alpha Zoo | `alpha-zoo` (vibe-trading) | alpha bench、因子库、alpha101、gtja191 |
| 基本面筛选 | `fundamental-filter` (vibe-trading) | 选股、PE/PB/ROE 筛选 |
| 估值模型 | `valuation-model` (vibe-trading) | 估值、DCF、PE-Band、DDM |
| 投资分析 | `2min-company-analysis` | 财务分析、A股基本面、七看八问 |
| 标准报告 | `report-generate` (vibe-trading) | 生成报告、保存分析、分析报告 |
| **HTML 交互报告** | **`html-report`** | **HTML 报告、交互图表、ECharts、可视化展示** |
| 周期执行 | `cron_jobs/manage.py` | 定时运行、cron、自动提醒 |
| 逃顶预警 | `escape-top-microstructure` | 顶部预警、拥挤度、两融背离 |
| SWARM 团队 | `vibe-trading_run_swarm` | investment_committee、quant_strategy_desk、risk |
| 跨市场数据 | `vibe-trading_get_market_data` | tencent、akshare、yfinance、tushare、auto |
| Finance Skills | `vibe-trading_list_skills/load_skill` | factor、strategy、risk、technical |
| **选股策略** | **fundamental-filter + multi-factor + 场景 E** | **选股、筛选、选美、资金流选股、多因子选股** |
| 技术分析 | `technical-basic` / `candlestick` / `ichimoku` / `elliott-wave` / `harmonic` / `smc` (vibe-trading) | 技术面、K线形态、缠论 |
| 风险管理 | `risk-analysis` (vibe-trading) | VaR、CVaR、最大回撤、压力测试 |
| **OMO 任务规划** | **oh-my-openagent（Prometheus 分解 + 并行子代理）** | **复杂任务、多步骤、并行执行** |
| **VT 记忆能力** | **memory-lifecycle（reflections/MCP adapter/persistent/hierarchy）** | **记忆、反思、跨会话、经验积累** |

# OMO 任务规划与子代理并行

## 概述
本容器运行 OpenCode + oh-my-openagent (OMO) 插件，支持任务分解与并行子代理执行。OMO 的 Prometheus 规划器将复杂任务拆解为原子子任务，分配给多个子代理并行执行，最后汇总结果。

## 何时使用 OMO
1. **多步骤复杂任务**：涉及 3+ 个独立步骤的任务（如"分析 5 只股票并比较"）。
2. **并行可分解任务**：各步骤之间无数据依赖的任务（如同时查询多个数据源）。
3. **需要多视角分析**：如 bull/bear 双面分析、多因子并行回测。
4. **用户明确要求**：当用户说"用并行方式"、"同时处理"、"加快速度"等。

## OMO 执行规则
1. **Prometheus 先规划后执行**：复杂任务必须先让 Prometheus 分解，形成子任务 DAG，确认后再执行。
2. **子代理类型选择**：
   - 数据探索/代码搜索 → `explore` 子代理
   - 文档/知识整理 → `librarian` 子代理
   - 代码编写/回测 → `build` 子代理
   - 质量验证/审查 → `oracle` 子代理
3. **并行度控制**：同时运行的子代理不超过 5 个，避免资源争抢。
4. **结果汇总**：所有子代理完成后，必须汇总输出统一报告，不得直接输出原始子代理返回。
5. **任务独立检查**：每个子任务必须有明确的输入/输出边界，禁止子任务间隐式依赖。

## OMO 禁止场景
1. 简单单步查询（如"查一下贵州茅台的 PE"）→ 直接用 data-warehouse
2. 需要严格顺序依赖的任务（如"先回测再根据结果调参再回测"）→ 顺序执行
3. 用户明确要求顺序执行

# VT 记忆能力（Memory Lifecycle）— Hook 自动触发

## 概述
Vibe-Trading 内置记忆生命周期管理系统，支持跨会话知识积累与经验复用。**记忆操作由 FastMCP middleware（MemoryGuard）自动触发，不依赖 LLM 手动调用。**

### 自动触发机制（MemoryGuard Middleware）

每次 OpenCode 通过 MCP 调用 VT 工具时，middleware 自动执行：

| 阶段 | 动作 | 覆盖范围 |
|------|------|---------|
| 每次工具调用后 | `memory_save`（工具名、参数、结果、耗时） | 所有 59 个 VT MCP 工具 |
| 回测/因子分析/交易日志后 | `memory_reflect`（sharpe、max_drawdown 等） | backtest、factor_analysis、analyze_trade_journal 等 |
| 容器启动时 | `memory_status` 验证（entrypoint 日志） | 启动阶段 |

**记忆存储位置**：`/workspace/.vt-memory/`（通过 `VT_MEMORY_BASE_DIR` 环境变量配置，docker-compose volume 持久化）

### 1. 反思课程存储（Reflections Store）
- **存储格式**: JSONL append-only 文件，位于 `~/.vibe-trading/memory/reflections/`
- **内容**: 每次回测/分析完成后的经验教训、策略优化记录、失败原因分析
- **自动触发**: 回测完成后自动生成反思（通过 `backtest-diagnose` Skill 钩子）

### 2. MCP 记忆工具（5 个工具）
启用方式：`VT_MEMORY=full` + `VT_MEMORY_MCP_TOOLS=1`（镜像已预置，见 Dockerfile 和 entrypoint.sh）。

| 工具 | 功能 | 使用场景 |
|------|------|---------|
| `memory_save` | 保存结构化记忆（名称+描述+内容+类型） | 策略发现、市场洞察、用户偏好 |
| `memory_recall` | 关键词检索记忆（top_k + type_filter） | 新任务前检索相关经验 |
| `memory_reinforce` | 强化/削弱记忆质量评分（event + source） | 经验被验证/推翻时 |
| `memory_reflect` | 从回测结果提取反思课程（strategy_type + outcome） | 回测完成后自动/手动反思 |
| `memory_status` | 报告记忆库统计（entry_count、avg_quality、gc_pending） | 记忆盘点、健康检查 |

### 3. 生命周期管理
- **质量评分（Quality Scoring）**: 每条记忆有质量评分，基于来源可靠性、验证次数、时间衰减
- **艾宾浩斯遗忘曲线（Ebbinghaus Decay）**: 长时间未使用的记忆自动降权，模拟自然遗忘
- **归档 GC（Archive-only GC）**: 低质量记忆移入归档区，不在主上下文中注入，但可被显式检索

### 4. 持久化存储与层级路由
- **Tier 2 结构组织**: 记忆按主题层级化存储（市场/策略/标的/风险）
- **文件名修复**: 层级路由支持中文文件名，避免 CJK 字符碰撞
- **跨会话持久化**: 所有记忆存储在 `~/.vibe-trading/memory/`，容器重启不丢失

## 记忆使用规则
1. **每次回测/分析自动触发反思**：Middleware 自动调用 `memory_save` + `memory_reflect`，无需手动操作。
2. **新任务前可手动检索**：调用 `memory_recall` 检查是否有相关历史经验（可选，非必须）。
3. **记忆引用必须标注来源**：引用记忆中的结论时，注明记忆名称和保存时间。
4. **策略失效时手动标记**：当发现某条经验不再适用，调用 `memory_reinforce(name="...", event="user_reject")` 降低质量评分。
5. **用户偏好优先记忆**：用户明确表达的偏好（如"我偏好低估值策略"）必须保存为高权重记忆。

# 周期任务触发规范（CRITICAL）

## 空壳 Session 陷阱（已发生事故 2026-06-10）
`curl POST /session` 只创建 session 记录，不触发 agent 执行。所有通过此方式创建的 session token 用量为 0，agent 从未运行。

**正确做法**: 必须使用 `opencode run --attach <url>` CLI 触发，它会连接运行中的 server、发送消息、等待 agent 完成。

## 创建新任务时的强制验证
1. 通过 `manage.py add` 创建任务后，系统自动在 5 分钟后调度一次性测试 cron。
2. 测试时间到达后，运行 `manage.py verify-test <task_id>` 检查日志。
3. 验证通过标准：日志存在 + agent 实际执行（token > 0 或输出非空）+ 无致命错误。
4. 验证通过后自动清理测试 cron 行。
5. **未通过验证的 cron 任务视为未部署。**

## 日志完整性要求
- 每个 cron 执行日志必须包含：PROMPT、SESSION_ID、EXIT_CODE、STDOUT、STDERR。
- 日志文件命名：`{task_id}_{ISO_timestamp}.log`，存放在 `cron_jobs/logs/`。
- 可通过 `grep "tokens" cron_jobs/logs/{task_id}_*.log` 快速检查 agent 是否真正执行。

## 每次触发必须通知（CRITICAL）
所有周期任务**每次执行都必须发送钉钉通知**，无论结果是否有变化、信号是否触发。
- **禁止条件通知**：不得写"仅在信号变化时通知"、"无变化则跳过"之类的逻辑。
- **目的**：建立完整的历史追踪记录，方便事后复盘和审计。
- **通知正文必须包含执行日期**（YYYY-MM-DD）。
- 创建新任务时，prompt 中必须包含 `CRITICAL: 每次执行都必须发送钉钉通知，无论结果如何` 的明确指令。

# 复盘与持续改进

## 交易复盘（每次交易后）
1. 实际成交价 vs 信号触发价，计算滑点。
2. 持仓期间最大浮盈/浮亏 vs 最终盈亏，评估出场时机。
3. 是否遵守了交易前检查清单？未遵守的原因是什么？
4. 将复盘结果保存为 VT 记忆（`memory_save`），标记为 `reflection` 类型。

## 周期性自检（每周/每月）
1. 统计本周/本月所有策略信号的胜率、盈亏比、夏普比率。
2. 对比基准指数表现，计算超额收益。
3. 检测策略衰减：IC/IR 是否持续下降？是否需要重新调参？
4. 更新 `analysis/_index.json` 中的回测表现摘要。

# 关键约束速查
1. **数据源优先级**: data-warehouse Skill (ClickHouse T-1) > 网络源 (当日 OHLCV) > Vibe-Trading 历史数据。不得使用 DuckDB。
2. Alpha158 因子用 raw 不复权价格；回测收益用 HFQ 后复权价格，双 DataFrame 不可混用。
3. 回测前必须确认因子与价格数据存在；预热窗口不足时不得过度解读。
4. 历史 K 线信号与盘中实时信号分开表述；实时信号必须说明数据源、时间戳和延迟风险。
5. **LLM 禁止做数学**: 所有数值计算必须通过量化工具完成，LLM 仅做引用和解读。
6. **信号覆盖规则**: 量化信号 > 规则判断 > LLM 定性判断。LLM 不得推翻量化信号，只能标注"信号与定性判断不一致"的风险提示。
7. 新分析报告写 `analysis/`；新周期任务写 `cron_jobs/registry.json`。
8. 不修改 `.env`、ClickHouse 连接配置、既有同步排除规则，除非用户明确要求。
9. `analysis/`、`scripts/`、`cron_jobs/`、`policy/`、`sql/` 各有子目录 AGENTS.md，进入目录后遵守局部约定。
10. **Python 虚拟环境**: 所有 Python 脚本必须在 `/opt/venv` 环境中执行，使用 `source /opt/venv/bin/activate`。