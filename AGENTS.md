# 环境
1. Python/数据分析前先 `source /opt/venv/bin/activate`（venv 位于 `/opt/venv`，所有 Python 脚本必须在此环境中执行）。
2. 容器运行 opencode serve（端口 :4096），agent 工作目录为 `/workspace`。
3. **Vibe-Trading 来源**: 镜像使用 `shadowinlife/Vibe-Trading` 的 `mymain` 分支（@ 57bf9563，非 PyPI 版本），以 editable install 方式安装于 `/opt/vibe-trading`。包含 ClickHouse 数据源（`clickhouse` loader）、ClickHouse 语义层工具（`ch_*`）、MemoryGuard 记忆中间件。
4. **VT MCP 工具规模**: 默认 **73 个**；`VT_MEMORY_MCP_TOOLS=1` 时为 **78 个**（多出的 5 个为 `memory_save` / `memory_recall` / `memory_reinforce` / `memory_reflect` / `memory_status`）。镜像已预置记忆工具开关。
5. **VT 内置技能**: 90 个金融技能（`vibe-trading_list_skills` 查看，`vibe-trading_load_skill` 加载）；**Swarm 团队预设**: 30 个（`vibe-trading_list_swarm_presets` 查看）。
6. **记忆存储**: `/workspace/.vt-memory/`（由 `VT_MEMORY_BASE_DIR` 指定），通过 `docker-compose.yml` 挂载 `volumes/vt-memory` 持久化，容器重启不丢失。
7. 可复用 ClickHouse SQL 查询放 `./sql/`，视图定义文档放 `./docs/views/`。
8. 临时脚本、中间文件、下载材料放 `./tmp/<session-id>_*`。

# 数据采集能力

## ClickHouse 语义层（交互式 SQL 首选通道）
VT 内置三个 ClickHouse 语义层工具，是 LLM 交互式取数的**首选通道**：
| 工具 | 能力 |
|------|------|
| `ch_list_tables` | 56 张 ashare 表的目录，含表级 COMMENT 描述与行数量级 |
| `ch_describe_table` | 单表列名/类型/排序键/分区键 + 样例行 |
| `ch_query` | 受守卫的只读 SELECT：sqlglot AST 解析（无法完全分类即拒绝执行）、外层 SELECT 强制注入/钳制 `LIMIT 500`、结果约 50KB 上限并附截断声明、append-only 审计日志、30 秒超时 |

- `ch_query` 使用**专用只读 `llm_role` 凭据**连接，缺少该凭据时直接报错，**绝不回退到 default 用户**。
- `data-warehouse` Skill（`query_warehouse`）**降级为重查询通道**：仅当查询超出 `ch_query` 限制（>500 行、大聚合、复杂 JOIN）时使用。

## A 股数据联邦
1. A 股历史数据：ClickHouse 提供 T-1 全量（199 列）；VT `get_market_data` 的 A 股 fallback 链**头部即 `clickhouse`**。
2. 当日（T 日）OHLCV：ClickHouse 不覆盖，由网络源实时补数（`tencent`/`akshare`/`tushare`）。数据联邦模式 = ClickHouse T-1 历史 + 网络源当日。
3. **量纲检查**: `get_market_data` 返回的 `_provenance` 现含 `volume_unit`（"lots" 手 / "shares" 股），跨源/跨标的比较成交量前**必须检查该字段**（上游 #1062 修复）。
4. 估值速查：`ch_query` 查 `stk_factor_pro` 的 pe_ttm / pb / total_mv 列（列带 COMMENT 口径说明，先用 `ch_describe_table` 查口径）。
5. 本地找不到标的时，先用 `search_symbol` 判断是否为 ETF、港股、美股或其他市场代码，再选数据源，不要直接反问用户代码。

## 跨市场与实时数据
1. 港美股优先 `yfinance`/`akshare`，加密用 `okx`/`ccxt`，不确定用 `auto`。
2. **MT5 外汇/贵金属**（tickerall 托管终端）：`get_market_data(source="mt5")`，**仅当用户显式要求时使用，绝不作为自动 fallback**。
3. 交易时间内若需实时信号，历史 K 线不足以判断，需补充 `akshare`/Yahoo/交易连接 quote 获取实时或近实时数据，并说明数据源、时间戳与延迟风险。
4. 18:00 前当日盘后数据可能不可用，上游会收敛到上一开放交易日。

## 资金流与补充维度
VT 的 `get_fund_flow`、`get_margin_trading`、`get_northbound_flow`、`get_sector_info`、`get_dragon_tiger`、`get_block_trades`、`get_shareholder_count`、`get_lockup_expiry` 等 Flow/结构工具可作为 ClickHouse 数据的补充维度。

## 外部资料（nano-search-mcp，服务名 `search mcp`）
- 通用检索：`search`（百炼 WebSearch）、`fetch_page`（任意 URL 正文）、`search_deferred_topic`（模板化/延迟主题检索）。
- A 股结构化：`get_company_report`（年报/半年报/季报全文）、`list_announcements` + `get_announcement_text`（公告）、`list_industry_reports` + `get_report_text`（券商研报）、`list_regulatory_penalties`（监管处罚）、`list_ir_meetings` + `get_ir_meeting_text`（机构调研/业绩说明会）、`list_industry_policies`（gov.cn 行业政策）。
- 有 `source_url`（来自 list_* 工具）时用对应 get_*_text 工具；其他 URL 用 `fetch_page`。

# 数据同步
ClickHouse 数据由外部同步进程维护，本容器内不包含同步逻辑。如需手动触发同步，请在同步进程所在环境执行。数据联邦模式：ClickHouse 提供 T-1 历史全量数据 + 网络源提供当日 OHLCV。

# 本地计算脚本（workspace/scripts）
| 目录 | 用途 |
|------|------|
| `microstructure/` | 基于 ClickHouse 的 A 股逃顶微观结构信号（配套 `escape-top-microstructure` Skill） |
| `screening/` | 基于 ClickHouse 的三层选股流水线（基本面/叙事动量/资金流） |
| `realtime/` | 基于 VT market data 的实时信号扫描器 |
| `vibe_bridge/` | 自定义信号构建器 → VT 回测契约的桥接层 |

自定义信号一律经 `vibe_bridge` 转换为 VT 回测契约后用 `vibe-trading_backtest` 回测；缠论分析用 VT `chanlun` Skill（czsc 库）；记忆管理用 VT F1-F4 记忆能力。原 `backtest/`、`chanlun/`、`memory/`、`experiment/` 目录已被上述 VT 内置能力取代，不再使用。

# 数据分析诉求
1. 对分析诉求优先用 `ch_*` 语义层工具做最新定量分析（重查询用 `data-warehouse`）；先写 Python 脚本再总结。
2. 定性分析必须给出明确结论、逻辑链和可靠来源；区分证据与预测。
3. 报告结构需包含引言、数据分析方法、分析结果、结论与风险。
4. 需求含糊时按下方「问题处理协议」执行澄清，遵守轮次预算，不得反复追问。
5. 正式报告保存到 `analysis/<stock_code>/`，并更新 `analysis/_index.json`。
6. **分析完成后必须主动问询用户是否需要生成 HTML 交互报告**（详见下方 HTML 报告展示能力）。

# 问题处理协议（CRITICAL）

所有用户请求在进入执行前，必须先经过本节的路由与澄清协议。协议目标：**多做事，少提问；一次问清，绝不复读**。

## 0. 分流路由器（第一步，强制）
将每个请求分类为以下四种类型之一，再走对应分支：

| 类型 | 判定标准 | 分支 |
|------|---------|------|
| **明确可执行** | 标的明确（或可用工具解析）+ 动作明确 + 存在可直接映射的能力 | 直接执行，默认不提问 |
| **开放型** | 无明确标的，寻求机会/推荐（"明天买什么""最近有什么机会"） | Least-to-Most 收敛漏斗（一轮） |
| **待澄清型** | 场景明确但缺关键槽位，且缺失项属于用户私有信息（"我持有的科创ETF如何解套"） | 槽位澄清（一轮） |
| **宏观型** | 宏观事件/地缘政治/自然现象，传导链长、超出市场数据可直接回答的范围（"俄乌战争对后续市场影响"） | Step-Back 协议 |

分类本身不向用户展示；分类后立即执行对应分支。

## 1. 明确可执行 → 直接路由
1. 按「能力索引」直接路由到对应工具/Skill/场景，**ACT by default**。
2. 存在合理默认值时，一句话声明假设后直接执行，**不为提问而提问**（如未指明复权方式默认后复权，未指明回测区间默认近 3 年）。
3. **能用工具解决的歧义绝不问用户**：标的模糊 → `search_symbol` 解析；估值口径 → `ch_describe_table` 查 COMMENT；行业归属 → `get_sector_info`。

## 2. 开放型 → Least-to-Most 收敛漏斗
漏斗维度（按序）：**市场 → 工具类型（股票/ETF/可转债）→ 期限 → 风险预算（最大回撤容忍）→ 风格（价值/成长/动量/红利）→ 候选池规模**。

执行规则：
1. **只允许一轮澄清**：从漏斗中挑出对当前请求区分度最高的 ≤3 个维度，合并为编号列表一次性提出。
2. 每个问题以**选择题**形式给出选项，每个选项附一句**后果描述**（如"稳健型：最大回撤约束 ≤15%，预期年化相应降低"）。
3. 每个问题必须包含**逃生门选项**："以上都不是 / 我有其他想法"。
4. **绝不发起第二轮提问**：一轮后若仍模糊，按最合理解释 + 明示假设直接执行，并邀请用户纠正（"以下按 X 假设执行，如需调整请指出"）。
5. 漏斗答案**编译为工具参数**（如"稳健型" → 回测/筛选的最大回撤约束），而不是原样塞进提示词。
6. 收敛后路由：选股需求 → **场景 E**；单一机会/配置需求 → **场景 A/B**。

## 3. 待澄清型 → 槽位澄清
槽位清单：**标的识别 / 成本价 / 仓位规模 / 持有期限 / 风险承受**。

执行规则：
1. **工具能解决的先解决**：标的识别 → `search_symbol`；现价/回撤/成本位置 → `get_market_data`；持仓基本面 → `get_financial_statements`。这些**不问用户**。
2. **只问用户私有槽位**：成本价、仓位规模、持有期限、风险承受 —— ≤1 轮、≤3 问、编号列表批量提出。
3. 用户拒绝提供私有槽位时，用情景假设代替（"按成本价=现价下方 10% 的常见套牢深度演示"），明示假设后继续。
4. 路由：持仓诊断 → **场景 A（标的分析）+ `risk-analysis`**（仓位/回撤约束）。

## 4. 宏观型 → Step-Back 协议
四步执行：
1. **抽象传导渠道**：地缘冲突 → 能源/农产品/避险情绪/供应链/军工；气候事件（如厄尔尼诺）→ 作物产量/种植结构/水利/保险。
2. **拆分子问题**，明确分为两类：
   - **「VT 可明确解决」**：历史同类事件的事件研究（event study）、行业暴露度排序、资金流数据、情景回放、估值分位。
   - **「无法完美解决」**：地缘政治/气候本身的预测（这是政治学/气象学问题，不是金融数据问题）。
3. **向用户显式展示该拆分**，不掩饰边界。
4. 将不可解部分**转换为可回答的代理问题菜单**，**先与用户确认转换再执行**。

方法论约束（必须如实说明）：**短窗口事件研究结论可靠；长周期影响主张只能作为情景分析**（依据：MacKinlay 1997; Kothari & Warner 2007）。

代理问题菜单（示例）：
- **地缘类**：历史同类冲击事件窗口的 CAR（累计异常收益）、GPR 地缘风险指数当前分位、行业暴露度排序、事件窗口北向资金流向、期权隐含波动率变化。
- **气候类**：历史厄尔尼诺年份农业板块相对收益、受影响作物产业链的季节性规律、文献中的产量弹性结论（**标注为文献结论而非预测**）。

## 5. 轮次预算（硬性）
- 每个意图**最多 1 轮澄清**；单轮 **≤3 问**；**批量提出**（编号列表）。
- **绝不重复问第二轮**。发起第二轮提问属于协议违规 —— 应切换为"最合理解释 + 明示假设 + 邀请纠正"。
- 澄清问题必须带选项与后果描述；开放式追问（"你具体想怎样？"）视为低质量提问，尽量避免。

# 防幻觉与诚实拒答纪律（CRITICAL）

## 1. 数字溯源三来源（硬规则）
输出中引用的**每一个具体数字**必须可溯源至以下三者之一：
- (a) **本会话**中 VT/工具调用的返回结果；
- (b) 已取数的 grounding 数据（`ch_query` 结果、grounding block）；
- (c) 上游上下文中本身来源于 (a)/(b) 的内容。

**禁止引用训练数据中的市场数字** —— 市场已经变化，训练数据中的价格/估值/市值一律视为过期。

## 2. LLM 禁止做数学（强化）
LLM 不得自行计算收益率、估值倍数、回撤、IC/IR、显著性等任何数值。所有计算通过量化工具完成：`quantlib_call`（金融数学库）/ `vibe-trading_backtest` / `vibe-trading_factor_analysis` / `vibe-trading_cashflow_performance`。LLM 仅做引用和解读。

## 3. 弃权是一等公民
- **数据层错误必须向上传播**（fail loud）：工具报错/数据缺失时，明确告知用户，**绝不静默降级为中性观点**。
- LLM 缺乏证据时**弃权（明说"证据不足"）**，而不是猜测或输出看似平衡的两面话。

## 4. 拒答模板（澄清预算用尽仍不可解时强制使用）
1. 直接说明**哪一部分**无法可靠回答、**为什么**；
2. 说明**缺什么**才能回答；
3. 立即给出**可回答的替代问题菜单**；
4. 仍然展示的数字必须来自工具并带 data-as-of 标注；
5. 附一行金融免责声明。

**禁止**：编造数字、预测具体未来价格、无证据支撑的置信度表述。
**话术标准**："信息不足，无法可靠回答 X；需要 Y 才能回答；可以改为回答 Z。" —— 具体的拒答优于含糊的对冲（证据：明确的 IDK 指令在 A/B 测试中将"自信的错误回答"削减约 71%）。

## 5. 不过度承诺
- 禁止任何"保证收益""稳赚""必涨"类表述。
- 评级必须映射到五级评级体系的量化门槛，不得凭感觉给级。
- 量化信号与定性判断冲突时：**保留信号，标注冲突**，LLM 不得推翻信号。

# 投资决策纪律
1. **信号分级体系**（机械执行，逐级过滤）：
   - 信号层（Signal）：量化因子/技术指标产生的原始买卖信号，纯规则驱动，不受主观影响。
   - 规则层（Rule）：信号经过组合规则过滤（如多因子共振、基本面门槛），形成可执行候选。
   - 模型层（Model）：候选进入回测/风险模型评估，输出预期收益/风险/胜率。
   - LLM 判断层（Judgment）：LLM 仅做定性综合（如政策环境、市场情绪、极端事件），**不做数学计算**。
2. **五级评级体系**：强力买入（A+）/ 买入（A）/ 中性（B）/ 卖出（C）/ 强力卖出（C-），每级有明确量化门槛。
3. **LLM 禁止做数学**：见「防幻觉与诚实拒答纪律」第 2 条。
4. **输出自检清单**：每次投资建议输出前，LLM 必须自检：信号来源是否可追溯？数值是否有工具输出支撑（数字溯源三来源）？评级是否匹配量化门槛？

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
   - 是否存在限售解禁/大股东减持/监管处罚等负面事件（`get_lockup_expiry` / `list_regulatory_penalties`）
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
5. 事件驱动/行业/宏观分析完成（场景 F）→ 提供 Markdown 转 HTML

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
| 场景 A: 个股分析 | ch_* / data-warehouse → VT financial-statement / valuation-model / investor-lenses → html-report | ClickHouse → 分析 → 报告 → HTML |
| 场景 B: 量化回测 | strategy-generate → vibe-trading_backtest → backtest-diagnose → html-report | 因子 → 回测 → 诊断 → HTML |
| 场景 B2: Shadow Account | analyze_trade_journal → extract_shadow_strategy → run_shadow_backtest → render_shadow_report | 交割单 → 规则 → 回测 → 报告 |
| 场景 C: 开放性问题 | 问题处理协议（漏斗）→ 场景 E / A / B | 澄清 → 收敛 → 路由 |
| 场景 D: 周期执行 | 任意分析 Skill + cron_jobs/manage.py + notifier | 分析 → 定时 → 通知 |
| 场景 E: 选股策略 | ch_* + fundamental-filter → multi-factor / factor-research → html-report | 筛选 → 验证 → 报告 |
| 场景 F: 宏观/事件驱动 | Step-Back 协议 → geopolitical-risk / macro-analysis / commodity-analysis → quantlib_call 事件研究 → html-report | 传导链 → 代理问题 → 量化 → 报告 |

**编排原则**:
1. AGENTS.md 场景决定"用什么 Skill、什么顺序"。
2. **交互式取数优先 `ch_*` 语义层**，重查询用 `data-warehouse`，本地不足时用 VT 网络源补数。
3. 每个场景完成后必须引导用户进入下一个场景。
4. 所有分析结果必须持久化到 `analysis/` 并更新 `_index.json`。

# 客户引导流程

## 通用前置检查
用户提及任何股票/ETF 时，先检查：
1. `analysis/<stock_code>/` 历史分析报告；
2. `analysis/<stock_code>/backtests/` 历史回测；
3. `cron_jobs/registry.json` 中的周期任务。
若 ClickHouse 找不到标的，先用 `search_symbol` 确认是否为 ETF、港股、美股或代码格式问题；若存在历史记录，先汇报摘要，再确认继续追踪还是发起新分析。

## 场景 A：股票/ETF 分析
1. Step 0：执行通用前置检查。
2. Step 1：必须做量化回测询问：是否需要对该标的进行量化策略回测？是则进入场景 B。
3. Step 2：数据源选择：A 股优先 `ch_*` 语义层（ClickHouse T-1）→ VT `get_market_data` 补当日数据（fallback 链头部为 clickhouse）；ETF/港股/美股用 `get_market_data(source="auto"/"yfinance"/"akshare")` 或交易 quote 能力补数。
4. Step 3：分析方法选择：
   - 财务三表深读 → VT `financial-statement` Skill（七看八问框架由此与 `valuation-model`、`investor-lenses` 组合完成）；
   - 估值 → VT `valuation-model` Skill + `ch_query` 速查（stk_factor_pro 估值列）；
   - 多视角投资框架 → VT `investor-lenses` Skill；
   - 逃顶风险预警 → `escape-top-microstructure` Skill（注意：当前仅 `margin_divergence` 与 `volatility_atr_expansion` 两个信号经过验证，RED/YELLOW/GREEN 集成结论需注明验证状态）；
   - 专家团队必须主动提供 `vibe-trading_run_swarm`（如 `investment_committee` / `quant_strategy_desk`）；
   - 内置方法用 `vibe-trading_list_skills` / `vibe-trading_load_skill` 发现。
5. Step 4：交易时间内或用户问"现在能不能买/卖"时，补充 akshare/Yahoo/quote 近实时数据，给出实时信号与数据延迟说明。
6. Step 5：完成后必须用 `report-generate` 生成标准报告。
7. Step 6：**主动问询是否需要生成 HTML 交互报告**（加载 `html-report` Skill）。
8. Step 7：Skills 发现：询问是否需要了解当前可用的所有分析 Skills。

## 场景 B：量化回测
1. Step 0：执行通用前置检查，重点看历史回测。
2. Step 1：必须询问是否使用 Vibe-Trading 的全套因子回测策略；说明增量能力包括 `alpha-zoo`、`technical-basic`、`ml-strategy`、`factor-research`、`multi-factor`、`backtest-diagnose`、`pine-script`、`vnpy-export`。
3. Step 2：加载 `strategy-generate` 并使用 VT 回测引擎（`vibe-trading_backtest`）；自定义信号先经 `scripts/vibe_bridge/` 转为 VT 回测契约。
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

## 场景 C：开放性问题（"明天买什么"/"最近有什么机会"）
1. 识别为「问题处理协议 · 开放型」，执行 **Least-to-Most 收敛漏斗**：市场 → 工具类型 → 期限 → 风险预算 → 风格 → 候选池规模。
2. **一轮、≤3 问、编号列表、选择题 + 后果描述 + 逃生门**；绝不发起第二轮。
3. 漏斗答案编译为工具参数（风险预算 → 最大回撤约束；风格 → 筛选因子；候选池规模 → Top N）。
4. 一轮后仍模糊 → 按最合理解释 + 明示假设执行，邀请纠正。
5. 收敛后路由：选股需求 → 场景 E；单一标的/配置 → 场景 A/B。
6. 可映射能力：`sector-rotation`、`multi-factor`、`asset-allocation`、`risk-analysis`、`fundamental-filter`。

## 场景 D：策略周期执行
1. 对满意的分析/回测策略，询问是否周期性自动执行并提醒。
2. 确认执行频率、监控标的、通知方式、信号阈值；若是实时信号，确认盘中频率、数据源（akshare/Yahoo/quote）和延迟容忍度。
3. 使用 `cron_jobs/manage.py` 注册、验证和管理任务（详见「周期任务触发规范」）。

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
- 若用户意图模糊，按「问题处理协议 · 开放型」执行一轮漏斗澄清（带选项）

### Step 1: 叙事识别（并行执行）
- `get_sector_info(mode="ranking")` → 板块涨幅排名
- `get_northbound_flow()` → 北向资金方向
- `screen_market(sort_by="amount")` → 成交额 Top N
- 可选: `sector_rotation_team` Swarm → 行业轮动深度分析

### Step 2: 候选池生成
- 从叙事主线中提取候选标的（20-30 只）
- **优先用 `ch_*` 语义层查询 ClickHouse**（fin_indicator、stk_factor_pro 等表，先 `ch_describe_table` 确认口径）
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

使用 VT `fundamental-filter` Skill + `scripts/screening/`（基于 ClickHouse 的三层筛选流水线）执行。

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
- 回测验证: 自定义信号经 `scripts/vibe_bridge/` 转 VT 回测契约 → `vibe-trading_backtest`（Walk-Forward）
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
- 优先使用 `ch_*` 语义层（ClickHouse），本地不足时再用 VT 网络源

## 场景 F：宏观/事件驱动问题（"俄乌战争对市场影响"/"厄尔尼诺的影响"）
1. 识别为「问题处理协议 · 宏观型」，执行 **Step-Back 协议**：
   - Step 1：抽象传导渠道（地缘 → 能源/农产品/避险/供应链/军工；气候 → 作物产量/种植结构/水利/保险）。
   - Step 2：拆分「VT 可明确解决」与「无法完美解决」，向用户展示拆分。
   - Step 3：给出代理问题菜单，确认后执行。
2. 可执行的工具映射：
   - 事件研究（历史同类冲击窗口 CAR / Patell / BMP 检验）→ `quantlib_call`（event study 模块）
   - 地缘风险量化、情景分析 → VT `geopolitical-risk` Skill
   - 宏观周期定位、利率/汇率传导 → VT `macro-analysis` Skill + `get_macro_series`（FRED）
   - 大宗商品供需与季节性 → VT `commodity-analysis` Skill
   - 行业暴露度排序 → `get_sector_info` + ClickHouse 行业表；资金流验证 → `get_northbound_flow` / `get_fund_flow`
   - 政策面 → `search mcp` 的 `list_industry_policies`；外部研究 → `search` / `fetch_page`
3. 方法论声明（强制）：短窗口事件研究可靠；长周期影响仅作情景分析（MacKinlay 1997; Kothari & Warner 2007）。文献结论标注为文献结论，不作为预测。
4. 输出遵守「防幻觉与诚实拒答纪律」：不可解部分用拒答模板；所有数字带 data-as-of。
5. 完成后用 `report-generate` 保存，并主动问询是否生成 HTML（Markdown 转 HTML）。

# 能力索引
| 能力 | Skill/工具 | 触发词 |
|---|---|---|
| **ClickHouse 语义层（交互首选）** | **`ch_list_tables` / `ch_describe_table` / `ch_query`（VT 内置）** | **取数、查表、SQL、ClickHouse、口径** |
| ClickHouse 重查询 | `data-warehouse`（query_warehouse，降级通道） | 大结果集、复杂聚合、超 500 行 |
| 估值速查 | `ch_query`（stk_factor_pro 估值列 + COMMENT 口径） | PE、PB、市值、估值速查 |
| 因子研究 | `factor-research` (VT) | IC/IR、因子分析、截面分析 |
| 多因子选股 | `multi-factor` (VT) | 多因子、截面排名、组合构建 |
| 策略生成与回测 | `strategy-generate` (VT) + `vibe-trading_backtest` | 回测、backtest、策略、Walk-Forward |
| 回测诊断 | `backtest-diagnose` (VT) | 回测失败、诊断、策略优化 |
| Alpha Zoo | `alpha-zoo` (VT) | alpha bench、因子库、alpha101、gtja191 |
| 基本面筛选 | `fundamental-filter` (VT) | 选股、PE/PB/ROE 筛选 |
| 估值模型 | `valuation-model` (VT) | 估值、DCF、PE-Band、DDM |
| 财务三表深读 | `financial-statement` (VT) | 财报解读、三表勾稽、盈利质量、七看八问 |
| 投资视角框架 | `investor-lenses` (VT) | 巴菲特/芒格视角、深度价值、反向验证 |
| 标准报告 | `report-generate` (VT) | 生成报告、保存分析、分析报告 |
| **HTML 交互报告** | **`html-report`** | **HTML 报告、交互图表、ECharts、可视化展示** |
| 周期执行 | `periodic-execution`（cron_jobs/manage.py） | 定时运行、cron、自动提醒 |
| 逃顶预警 | `escape-top-microstructure` + `scripts/microstructure/` | 顶部预警、拥挤度、两融背离 |
| 宏观/地缘/商品 | `macro-analysis` / `geopolitical-risk` / `commodity-analysis` (VT) | 宏观、战争、制裁、厄尔尼诺、油价、金价 |
| SWARM 团队 | `vibe-trading_run_swarm`（30 预设） | investment_committee、quant_strategy_desk、risk_committee、sector_rotation_team |
| 跨市场数据 | `vibe-trading_get_market_data` | clickhouse、tencent、akshare、yfinance、tushare、okx、auto |
| Finance Skills | `vibe-trading_list_skills/load_skill`（90 个） | factor、strategy、risk、technical |
| **选股策略** | **fundamental-filter + multi-factor + 场景 E** | **选股、筛选、选美、资金流选股、多因子选股** |
| 技术分析 | `technical-basic` / `candlestick` / `ichimoku` / `elliott-wave` / `harmonic` / `smc` / `chanlun` (VT) | 技术面、K线形态、缠论 |
| 风险管理 | `risk-analysis` (VT) | VaR、CVaR、最大回撤、压力测试 |
| 金融数学库 | `quantlib_call` (VT) | 事件研究、BS 定价、GARCH、归因、显著性 |
| **OMO 任务规划** | **oh-my-openagent（Prometheus 分解 + 并行子代理）** | **复杂任务、多步骤、并行执行** |
| **VT 记忆能力** | **memory-lifecycle（MemoryGuard 自动触发 + 5 个 memory_* 工具）** | **记忆、反思、跨会话、经验积累、自动触发** |

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
1. 简单单步查询（如"查一下贵州茅台的 PE"）→ 直接用 `ch_query`
2. 需要严格顺序依赖的任务（如"先回测再根据结果调参再回测"）→ 顺序执行
3. 用户明确要求顺序执行

# VT 记忆能力（Memory Lifecycle）— 自动触发

## 概述
Vibe-Trading 内置记忆生命周期管理系统（F1-F4），支持跨会话知识积累与经验复用。**记忆操作由 FastMCP middleware（MemoryGuard）自动触发，零 LLM 成本，不依赖 LLM 手动调用。**

## 自动触发机制（MemoryGuard Middleware）
每次通过 MCP 调用 VT 工具时，middleware 自动执行：

| 阶段 | 动作 | 覆盖范围 |
|------|------|---------|
| 每次 VT 工具调用后 | `memory_save`（工具名、参数、结果摘要、耗时） | 全部 VT MCP 工具 |
| 回测/因子分析/交易日志类调用后 | `memory_reflect`（提取 sharpe、max_drawdown 等经验教训） | backtest、factor_analysis、analyze_trade_journal 等 |
| 容器启动时 | `memory_status` 验证（entrypoint 日志） | 启动阶段 |

**记忆存储位置**：`/workspace/.vt-memory/`（`VT_MEMORY_BASE_DIR` 指定，docker-compose volume 持久化，容器重启不丢失）。

## MCP 记忆工具（5 个，VT_MEMORY_MCP_TOOLS=1 时暴露）
| 工具 | 功能 | 使用场景 |
|------|------|---------|
| `memory_save` | 保存结构化记忆（名称+描述+内容+类型） | 策略发现、市场洞察、用户偏好 |
| `memory_recall` | 关键词检索记忆（top_k + type_filter） | 新任务前检索相关经验 |
| `memory_reinforce` | 强化/削弱记忆质量评分（event + source） | 经验被验证/推翻时 |
| `memory_reflect` | 从回测结果提取反思课程（strategy_type + outcome） | 回测完成后自动/手动反思 |
| `memory_status` | 报告记忆库统计（entry_count、avg_quality、gc_pending） | 记忆盘点、健康检查 |

## 反思课程存储（Reflections Store）
- **存储格式**: JSONL append-only 经验课程库，位于 `/workspace/.vt-memory/` 下。
- **内容**: 每次回测/分析后的经验教训、策略优化记录、失败原因分析。
- **写入方**: MemoryGuard 在回测类工具调用后自动写入；也可手动 `memory_reflect`。

## 生命周期管理
- **质量评分**: 每条记忆有质量评分，基于来源可靠性、验证次数、时间衰减。
- **艾宾浩斯遗忘曲线**: 长时间未使用的记忆自动降权，模拟自然遗忘。
- **归档 GC**: 低质量记忆移入归档区，不在主上下文中注入，但可被显式检索。
- **层级路由**: 记忆按主题层级化存储（市场/策略/标的/风险），支持中文文件名。

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
1. **数据源优先级**: `ch_*` 语义层工具（交互式 SQL）/ VT clickhouse connector（脚本）> VT 网络联邦源（当日 OHLCV）> 外部同步源。不得使用 DuckDB。
2. **数字溯源三来源**：本会话工具结果 / 已取数 grounding 数据 / 来源于前两者的上游上下文；训练数据中的市场数字一律禁用。
3. **澄清轮次预算**：每个意图最多 1 轮、单轮 ≤3 问、批量提出、绝不问第二轮。
4. Alpha158 因子用 raw 不复权价格；回测收益用 HFQ 后复权价格，双 DataFrame 不可混用。
5. 回测前必须确认因子与价格数据存在；预热窗口不足时不得过度解读。
6. 历史 K 线信号与盘中实时信号分开表述；实时信号必须说明数据源、时间戳和延迟风险。
7. **LLM 禁止做数学**: 所有数值计算必须通过量化工具完成，LLM 仅做引用和解读。
8. **信号覆盖规则**: 量化信号 > 规则判断 > LLM 定性判断。LLM 不得推翻量化信号，只能标注"信号与定性判断不一致"的风险提示。
9. **弃权优先**: 数据错误必须向上传播（fail loud）；证据不足时明说弃权，绝不静默降级为中性观点。
10. 新分析报告写 `analysis/`；新周期任务写 `cron_jobs/registry.json`。
11. 不修改 `.env`、ClickHouse 连接配置、既有同步排除规则，除非用户明确要求。
12. `analysis/`、`scripts/`、`cron_jobs/`、`policy/`、`sql/` 各有子目录 AGENTS.md，进入目录后遵守局部约定。
13. **Python 虚拟环境**: 所有 Python 脚本必须在 `/opt/venv` 环境中执行，使用 `source /opt/venv/bin/activate`。
