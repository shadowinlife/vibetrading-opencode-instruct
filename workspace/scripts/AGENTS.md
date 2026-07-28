# Scripts 目录约定

本目录包含量化分析的核心计算引擎。所有脚本默认通过 ClickHouse 数据仓库获取数据。

## 数据源

- **优先**: data-warehouse Skill（ClickHouse），通过 `query_warehouse(sql)` 查询
- **回退**: vibe-trading MCP 工具（`get_market_data`, `get_financial_statements` 等）
- **实时数据**: akshare / Yahoo Finance / 交易连接 quote

## 目录结构

| 目录 | 用途 | 数据源 |
|------|------|--------|
| `alpha158/` | Alpha158 因子计算 | ClickHouse raw 价格 |
| `backtest/` | Walk-Forward 回测引擎 | ClickHouse HFQ 价格 |
| `chanlun/` | 缠论技术分析 | ClickHouse K线数据 |
| `microstructure/` | 市场微观结构分析 | ClickHouse + 实时数据 |
| `screening/` | 三层选股筛选 | ClickHouse + vibe-trading |
| `memory/` | Agent 记忆管理 | 本地文件系统 |
| `realtime/` | 实时数据适配 | akshare/Yahoo/quote |
| `vibe_bridge/` | Vibe-Trading 适配器 | vibe-trading MCP |

## 关键约束

1. Alpha158 因子用 raw 不复权价格；回测收益用 HFQ 后复权价格，不可混用。
2. 所有脚本通过 data-warehouse Skill 读取 ClickHouse，不用硬编码连接。
3. 临时脚本和中间文件放 `./tmp/<session-id>_*`。
4. 回测前必须确认因子与价格数据存在。
5. 新脚本遵循现有代码风格，添加类型注解和 docstring。