# 表/视图索引

DuckDB 路径: `./duckdb/ashare.duckdb`

| 模块 | 表名 | 列数 | 粒度 | 同步维度 | 描述 |
|---|---|---:|---|---|---|
| basic | `stk_info` | 10 | 全量快照 | `none` | A股基本信息 |
| basic | `stk_name_history` | 6 | 标的-报告期 | `none` | 股票历史曾用名 |
| basic | `stk_st_daily` | 5 | 标的-交易日 | `trade_date` | ST/退市风险状态 |
| basic | `trade_calendar` | 4 | 全量快照 | `none` | SSE 交易日历 |
| factor | `stk_alpha158` | 160 | 标的-交易日 | `derived` | Alpha158 日频量价因子 |
| factor | `stk_factor_pro` | 199 | 标的-交易日 | `trade_date` | 日行情+估值+复权+技术指标 |
| factor | `stk_margin` | 11 | 标的-交易日 | `trade_date` | 融资融券明细 |
| factor | `stk_moneyflow` | 20 | 标的-交易日 | `trade_date` | 大中小单资金净流向 |
| finance | `fin_balance` | 158 | 标的-报告期 | `period` | 资产负债表 |
| finance | `fin_cashflow` | 97 | 标的-报告期 | `period` | 现金流量表 |
| finance | `fin_income` | 94 | 标的-报告期 | `period` | 利润表 |
| finance | `fin_indicator` | 168 | 标的-报告期 | `period` | 财务指标 |
| finance | `fin_top10_holders` | 9 | 标的-报告期-股东 | `period` | 前十大股东 |
| index | `idx_daily_dc` | 12 | 标的-交易日 | `trade_date` | 指数日行情 |
| index | `idx_info` | 8 | 全量快照 | `none` | 指数基本信息 |
| index | `idx_sw_classify` | 7 | 行业层级字典 | `none` | 申万行业分类 |
