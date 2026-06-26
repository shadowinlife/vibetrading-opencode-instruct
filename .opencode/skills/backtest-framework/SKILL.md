---
name: backtest-framework
description: |
  单股票 Walk-Forward 回测框架使用指南。Use when: 回测、backtest、Walk-Forward、策略评估、Alpha158 策略、StrategyConfig、simulate_segment。
argument-hint: "股票代码 + 策略名称"
user-invocable: true
---

# Backtest Framework Skill

This skill explains how to use the local single-stock backtest framework under `scripts/backtest/`.

## Architecture

| Module | Responsibility |
| --- | --- |
| `scripts/backtest/config.py` | `StrategyConfig` strategy parameters |
| `scripts/backtest/metrics.py` | `calc_metrics()` return metrics |
| `scripts/backtest/engine.py` | `simulate_segment()`, `run_fold_evaluation()`, `choose_best()` |
| `scripts/backtest/reporting.py` | `render_report()` markdown output |

## Critical Data Rule

Backtests must keep two separate dataframes:

| Purpose | Source | Price Type |
| --- | --- | --- |
| Alpha158 factors | `stk_alpha158` or `compute_alpha158(raw_df)` | raw, unadjusted |
| Trading returns | `stk_factor_pro.close_hfq` and `daily_ret` | HFQ, adjusted |

Never mix raw factor prices with HFQ return calculation.

## Quick Start

### 1. Compute Alpha158 Factors

```bash
conda activate <your-env>
python -m scripts.alpha158.cli --ts-code <code>
```

### 2. Build Signals

Use reusable builders from `scripts/backtest/signal_builders/`.

### 3. Configure Strategy

```python
from scripts.backtest.config import StrategyConfig

cfg = StrategyConfig(
    name="my_strategy",
    signal_col="SIGNAL_Z",
    entry_z=1.0,
    exit_z=-0.5,
    stop_loss=-0.15,
    max_hold=126,
)
```

### 4. Run Walk-Forward Evaluation

1. Load Alpha158 factors from `stk_alpha158`.
2. Load HFQ price/returns from `stk_factor_pro`.
3. Build signal columns.
4. Merge factor signals with HFQ returns by `trade_date`.
5. Call `run_fold_evaluation()`.
6. Save `fold_metrics.csv`, `summary.json`, and `trade_log.csv`.

## Rules

- Do not reimplement Alpha158 formulas in backtest scripts.
- Do not modify framework code for one-off strategies unless explicitly refactoring.
- Do not use raw prices for return calculation.
- Store new standardized reports through `analysis-report`.
