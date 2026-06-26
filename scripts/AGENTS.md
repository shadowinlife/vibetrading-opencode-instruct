# scripts/ Directory Guide

## 职责
存放核心计算引擎、共享模块和可复用工具脚本。

## 文件结构
```text
scripts/
├── alpha158/              # Alpha158 因子计算
├── backtest/              # Walk-Forward 回测框架
├── chanlun/               # 缠论相关工具
├── microstructure/        # 市场微观结构分析
└── stock_quant_analysis/  # 单股量化分析编排
```

## 关键约定
- 运行 Python 脚本前激活 conda 环境。
- Alpha158 因子使用 raw 不复权价格。
- 回测收益使用 `stk_factor_pro` 的 HFQ 后复权价格。
- Alpha158 与回测价格数据必须保持双 DataFrame，不可混用。
- 共享模块不可复制实现；新增功能优先扩展模块或新增文件。

## 关联 Skill
- `backtest-framework`: 使用 `scripts/backtest/`。
- `stock-quant-analysis`: 单股量化分析入口。
- `analysis-report`: 生成标准分析报告。

## 禁止事项
- 不在新脚本中重新实现 Alpha158 公式。
- 不把一次性研究脚本放在本目录根部。
