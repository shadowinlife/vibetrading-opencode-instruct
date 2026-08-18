# Scripts 目录约定

本目录包含 harness 自研的量化分析引擎（独有方法论资产）。**回测、缠论、记忆等通用能力已由
Vibe-Trading 内置提供**（VT 回测引擎 / VT chanlun skill / VT 记忆 F1–F4），本目录只保留
VT 无对应物的独有资产。

## 数据层

- **ClickHouse ashare 仓库**：经 Vibe-Trading connector 读取 ——
  `from src.clickhouse_connector import ClickHouseConnector`，连接参数一律来自 `CLICKHOUSE_*`
  环境变量（容器预置），**绝不硬编码凭据/主机**。
- **Tushare 外部端点**（仓库外数据）：经 `microstructure/source_registry.py` +
  `external_probes.py` 管理（shibor / lpr / cn_m / sf_month / moneyflow_hsgt /
  index_dailybasic / etf_share_size / opt_daily 等，含单位/滞后/覆盖元数据）。
- **行情联邦**（realtime）：VT `src.market_data.fetch_market_data`（CH T-1 + 网络源当日）。
- **降级契约**：数据源不可达时，CLI 输出 `{"available": false, "reason": "..."}` 并以
  退出码 0 结束 —— 绝不抛裸 traceback，绝不静默降级为中性结论。

## 目录结构

| 目录 | 用途 | 数据层 |
|------|------|--------|
| `microstructure/` | A 股逃顶信号框架（~15 信号族 + 7 门验证 + RED/YELLOW/GREEN 集成判定） | ClickHouse + Tushare 外部端点 |
| `screening/` | 三层选股（基本面 / 叙事动量 / 资金流共振，Z-score 合成 + 消融） | ClickHouse |
| `realtime/` | 行情适配（VT 联邦）+ watchlist 信号扫描（cron 联动） | VT market_data 联邦 |
| `vibe_bridge/` | 自研信号构建器 → VT 回测契约（`generate(data_map)`），含 22 个 signal builders | 无 I/O（纯适配层） |

已删除（由 VT 内置替代）：`backtest/`（VT 回测引擎，自研信号经 vibe_bridge 接入）、
`chanlun/`（VT chanlun skill）、`memory/`（VT 记忆 F1–F4）、`experiment/`（占位脚手架）。

## 关键约束

1. **价格口径分离**：因子计算与回测收益的价格口径（raw / HFQ）严格分离，经 vibe_bridge
   接入 VT 回测时同样适用，不可混用。
2. **连接零硬编码**：一律 `CLICKHOUSE_*` env / VT connector；Tushare token 走 `TUSHARE_TOKEN`。
3. 临时脚本和中间文件放 `./tmp/<session-id>_*`；信号/验证报告默认写 `tmp/microstructure/`。
4. 跑信号/回测前必须确认数据存在（覆盖区间足够），预热不足不得过度解读结论。
5. 新脚本遵循现有代码风格：类型注解 + docstring；SQL 用 ClickHouse 方言
   （注意：ORDER BY 必须与 LIMIT 同层、整型除法写字面量浮点、median→quantile(0.5)）。
6. **单位换算是资产**：千元/万元/元、手/股的换算规则集中在 `source_registry.py` 与各
   docstring，修改数据层时必须随代码保留。
