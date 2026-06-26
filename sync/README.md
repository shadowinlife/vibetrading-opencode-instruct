# 增量同步套件

面向 `duckdb/ashare.duckdb` 的本地增量同步方案。

## 依赖

上游同步脚本来自 [nano_quant_skills](https://github.com/shadowinlife/nano_quant_skills) 的 `ts2ck` 模块。

## 快速开始

```bash
# 1. dry-run
./sync/run_incremental_sync.sh --dry-run

# 2. 执行
./sync/run_incremental_sync.sh
```

## 默认行为

- trade_date：最近 7 天窗口
- financial：最近 4 个报告期
- snapshot：全量 refresh
- verify：自动执行

## 环境要求

- `TUSHARE_TOKEN` 在 `.env` 中
- Python 3.10+ conda 环境
