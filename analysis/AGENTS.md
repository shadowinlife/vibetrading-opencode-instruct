# analysis/ Directory Guide

## 职责
存储所有标准化股票/ETF 分析报告，按标的代码组织。

## 文件结构
```text
analysis/
├── README.md
├── _template.md
├── _index.json
└── <stock_code>/
    ├── <YYYY-MM-DD>_<type>_<desc>.md
    └── backtests/
        └── <YYYY-MM-DD>_<strategy>.md
```

## 关键约定
- 所有正式报告必须基于 `_template.md` 的五个章节。
- 新增报告必须同步更新 `_index.json`，并保持 JSON 有效。
- 回测类报告放入 `<stock_code>/backtests/`。
- 临时脚本和中间数据不得放入本目录，应放入 `tmp/`。

## 关联 Skill
- `analysis-report`: 生成报告、更新索引。
- `stock-analysis-workflow`: 基本面/估值分析后写入报告。
- `stock-quant-analysis`: 量化回测后写入报告。
