# RD-Agent Integration Reference

## Overview

RD-Agent is Microsoft's autonomous quantitative research framework. It automates hypothesis generation, experiment planning, code synthesis, evaluation, and iterative evolution.

**Current status**: Interface reserved. CLI contract documented for future integration.

## Architecture: Three-Layer Model

```
Layer 3: RD-Agent        — Research Orchestration (what to try next)
Layer 2: Vibe-Trading    — Backtest Execution (how to evaluate)
Layer 1: Qdata           — Local Data / Factor Semantics (data truth)
```

## CLI Commands

| Command | Description |
|---|---|
| `rdagent health_check` | Verify runtime environment |
| `rdagent fin_factor` | Factor evolution |
| `rdagent fin_model` | Model evolution |
| `rdagent fin_quant` | Joint factor+model co-optimization |

## Installation

```bash
conda create -n rdagent python=3.10 -y
conda activate rdagent
pip install -e .
```

Requires Docker for sandboxed code execution and an OpenAI API key.
