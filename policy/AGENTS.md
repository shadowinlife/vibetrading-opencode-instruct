# policy/ Directory Guide

## 职责
存放个股/ETF 策略研究、回测脚本和历史策略输出。

## 文件结构
```text
policy/
└── <stock_code>/
    ├── README.md
    ├── backtest_*.py
    ├── backtest_*/
    └── signal_builders/ 或引用 scripts/backtest/signal_builders/
```

## 关键约定
- 新股票策略创建独立 `<stock_code>/` 子目录。
- 通用信号逻辑优先从 `scripts/backtest/signal_builders/` 导入。
- 回测报告和正式分析结果应同步沉淀到 `analysis/<stock_code>/`。
- 不重构既有策略目录结构，除非另有明确计划。

## 关联 Skill
- `stock-quant-analysis`: 单股量化分析和策略回测。
- `backtest-framework`: Walk-Forward 回测框架使用。
- `analysis-report`: 归档标准报告。
