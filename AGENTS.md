# 环境
1. Python/数据分析前先 `conda activate <your-env-name>`。
2. 本地 DuckDB: `./duckdb/ashare.duckdb`；表结构说明见 `./docs/tables`。
3. 可复用 DuckDB view 的 SQL 放 `./duckdb/sql/`，文档放 `./docs/views/`。
4. 临时脚本、中间文件、下载材料放 `./tmp/<session-id>_*`。
5. Twitter API 凭证存于 `.env`：`TWITTER_CONSUMER_KEY`、`TWITTER_CONSUMER_SECRET`、`TWITTER_BEARER_TOKEN`。使用时从 `.env` 读取，不要硬编码或读取外传。

# 数据采集能力
1. 外部资料优先用 MCP 通用搜索；A 股分析优先查本地 DuckDB。
2. 本地数据不足时再用 Tushare；Token 来自 `.env`，不要硬编码或读取外传。
3. 标的代码本地查不到时，立即搜索判断是否为 ETF、港股、美股或其他市场代码，再选数据源。
4. 跨市场/实时数据优先用 Vibe-Trading 数据能力：A 股可用 `tushare`/`akshare`，港美股优先 `yfinance`/`akshare`，不确定时用 `auto`。
5. 交易时间内若需实时信号，历史 K 线不足以判断，需补充 `akshare`/Yahoo Finance/交易连接 quote 尝试获取实时或近实时数据。
6. `stk_cyq_chips` 不再同步；只能用库内历史数据。
7. `idx_weight` 不在日常增量同步；仅用户明确要求时运行。
8. `stk_auction_open` 不在日常增量同步；仅用户明确要求时运行。
9. 18:00 前当日盘后数据可能不可用，上游会收敛到上一开放交易日。

# 增量同步速查
1. 标准流程：先 `./sync/run_incremental_sync.sh --dry-run`，确认后再执行 `./sync/run_incremental_sync.sh`。
2. 默认 group: `trade-date, financial, snapshot, verify`。
3. 成功标志：`trade_remaining=0`、`period_remaining=0`、`snapshot_remaining=0`、`verification_result=OK`。
4. 若需特殊 group（`index-weight` / `auction-open`），必须由用户明确要求。

# 数据分析诉求
1. 对分析诉求尽量用 DuckDB 做最新定量分析；先写 Python 脚本再总结。
2. 定性分析必须给出明确结论、逻辑链和可靠来源；区分证据与预测。
3. 报告结构需包含引言、数据分析方法、分析结果、结论与风险。
4. 多个可选标的或需求含糊时，及时 human-in-the-loop 澄清。
5. 正式报告保存到 `analysis/<stock_code>/`，并更新 `analysis/_index.json`。
6. 所有分析/回测结果必须同时输出 HTML 报告：
   - Markdown 报告归档到 `analysis/<code>/`（现有流程不变）
   - HTML 报告使用 `html-report` Skill 生成，保存到同目录（.html 后缀）
   - HTML 报告通过 `scripts/reports/deploy_report.py` 部署到远端 nginx
   - DingTalk 推送时附带报告 URL（ActionCard 按钮）

# 客户引导流程

## 通用前置检查
用户提及任何股票/ETF 时，先检查：
1. `analysis/<stock_code>/` 历史分析报告；
2. `analysis/<stock_code>/backtests/` 历史回测；
3. `cron_jobs/registry.json` 中的周期任务。
若本地 DuckDB 找不到标的，先搜索确认是否为 ETF、港股、美股或代码格式问题；若存在历史记录，先汇报摘要，再确认继续追踪还是发起新分析。

## 场景 A：股票/ETF 分析
1. Step 0：执行通用前置检查。
2. Step 1：必须做量化回测询问：是否需要对该标的进行量化策略回测？是则进入场景 B。
3. Step 2：数据源选择：A 股优先 DuckDB→Tushare/akshare；ETF/港股/美股若本地无数据，用 Vibe-Trading `get_market_data(source="auto"/`yfinance`/`akshare`)` 或交易 quote 能力补数。
4. Step 3：分析方法选择：基本面用 `2min-company-analysis`，投资估值用 `stock-analysis-workflow`，风险预警用 `escape-top-microstructure`，专家团队必须主动提供 `vibe-trading_run_swarm`（如 investment_committee/quant_strategy_desk），内置方法用 `vibe-trading_list_skills`/`vibe-trading_load_skill`。
5. Step 4：交易时间内或用户问"现在能不能买/卖"时，补充 akshare/Yahoo/quote 近实时数据，给出实时信号与数据延迟说明。
6. Step 5：完成后必须用 `analysis-report` 生成标准报告。
7. Step 6：Skills 发现：询问是否需要了解当前可用的所有分析 Skills。

## 场景 B：量化回测
1. Step 0：执行通用前置检查，重点看历史回测。
2. Step 1：必须询问是否使用 Vibe-Trading 的全套因子回测策略；说明增量能力包括 `alpha-zoo`、`technical-basic`、`ml-strategy`、`factor-research`、`multi-factor`、`backtest-diagnose`、`pine-script`、`vnpy-export`。
3. 若用户选择 Vibe-Trading，加载 `strategy-generate` 并使用 vibe-trading 回测引擎。
4. 若用户选择本地框架，加载 `stock-quant-analysis` 和 `backtest-framework`。
5. Step 3：用 `analysis-report` 保存回测报告到 `analysis/<stock_code>/backtests/`。
6. Step 4：询问是否需要回测诊断、实盘策略导出或风险评估。
7. Step 5：若回测推导出后续买入/卖出位，询问是否让 crontab 在次日/交易时间用实时数据监控触发。
8. Step 6：若结果可跟踪，询问是否周期性自动执行并提醒。

## 场景 C：开放性问题
1. 对"最近买什么股票好""有什么投资机会"等开放性问题，必须启动 Prometheus 多轮收敛。
2. 逐步缩小范围：市场、风格、行业、风险偏好、周期、资金规模。
3. 可映射能力：`sector-rotation`、`multi-factor`、`asset-allocation`、`risk-analysis`、`fundamental-filter`。
4. 收敛后给 2-3 个可执行方案，再进入场景 A 或 B。

## 场景 D：策略周期执行
1. 对满意的分析/回测策略，询问是否周期性自动执行并提醒。
2. 确认执行频率、监控标的、通知方式、信号阈值；若是实时信号，确认盘中频率、数据源（akshare/Yahoo/quote）和延迟容忍度。
3. 加载 `periodic-execution`，通过 `cron_jobs/manage.py` 注册、验证和管理任务。

# 能力索引
| 能力 | Skill/目录 | 触发词 |
|---|---|---|
| 单股量化分析 | `stock-quant-analysis` | 量化分析、Alpha158、缠论、技术面 |
| 回测框架 | `backtest-framework` | 回测、backtest、Walk-Forward、策略评估 |
| 投资分析 | `stock-analysis-workflow` | 投资价值、估值、基本面分析 |
| 标准报告 | `analysis-report` | 生成报告、保存分析、分析报告 |
| 周期执行 | `periodic-execution` | 定时运行、cron、自动提醒 |
| 七看八问 | `2min-company-analysis` | 财务分析、A股基本面、七看八问 |
| 逃顶预警 | `escape-top-microstructure` | 顶部预警、拥挤度、两融背离 |
| SWARM 团队 | `vibe-trading_run_swarm` | investment_committee、quant_strategy_desk、risk |
| 跨市场数据 | `vibe-trading_get_market_data` | tushare、akshare、yfinance、auto |
| Finance Skills | `vibe-trading_list_skills/load_skill` | factor、strategy、risk、technical |
| HTML 报告 | `html-report` | 生成报告、HTML输出、ECharts、图表 |

# 关键约束速查
1. Alpha158 因子用 raw 不复权价格；回测收益用 HFQ 后复权价格，双 DataFrame 不可混用。
2. 回测前必须确认因子与价格数据存在；预热窗口不足时不得过度解读。
3. 历史 K 线信号与盘中实时信号分开表述；实时信号必须说明数据源、时间戳和延迟风险。
4. 新分析报告写 `analysis/`；新周期任务写 `cron_jobs/registry.json`。
5. 不修改 `.env`、`duckdb/` 大文件、既有同步排除规则，除非用户明确要求。
6. `analysis/`、`scripts/`、`cron_jobs/`、`policy/` 各有子目录 AGENTS.md，进入目录后遵守局部约定。
