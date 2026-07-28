# vibetrading-opencode-instruct

Docker build project for `opencode-serve` — OpenCode Web Server with Vibe-Trading AI, configured for Chinese A-share quantitative research with ClickHouse data warehouse.

## Overview

This project packages the OpenCode Web Server research environment into a reproducible Docker image. It extends the base `opencode-serve` image with:

- **OpenCode CLI 1.18.5** + OMO (oh-my-openagent) plugin
- **nano-search-mcp** — local MCP server for Chinese financial data (新浪财经, 百炼 WebSearch)
- **3 OpenCode skills**: data-warehouse (ClickHouse), html-report (ECharts), periodic-execution (cron)
- **Full AGENTS.md** with behavior instructions for 5 scenarios (A through E)
- **Quantitative scripts**: Walk-Forward backtest, Chanlun analysis, market microstructure, multi-layer screening, agent memory
- **Cron job infrastructure** with CLI management and DingTalk/email notification

## Quick Start

### Prerequisites

- Docker 20.10+
- DashScope API key (for LLM inference)
- ClickHouse instance (for data warehouse)

### Build

```bash
git clone https://github.com/shadowinlife/vibetrading-opencode-instruct.git
cd vibetrading-opencode-instruct
./build.sh
```

### Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

### Run

```bash
docker compose up -d
# Access at http://localhost:4096
```

## Directory Structure

```
vibetrading-opencode-instruct/
├── Dockerfile                  # Multi-stage build (13 steps)
├── Dockerfile.amd64            # AMD64 variant with full Python 3.12 venv
├── build.sh                    # Build script
├── docker-compose.yml          # Docker Compose deployment
├── entrypoint.sh               # Container entrypoint (Jinja2 config render + ClickHouse probe)
├── .env.example                # Environment variable template
├── AGENTS.md                   # Agent behavior instructions
│
├── config/                     # OpenCode configuration
│   ├── opencode.json.tmpl      # Jinja2 template (rendered at runtime with ClickHouse creds)
│   ├── oh-my-openagent.json    # Agent/category model assignments
│   ├── tui.json                # TUI plugin configuration
│   ├── package.json            # OpenCode plugin dependencies
│   └── vibe-trading-tools.json # Vibe-Trading tools configuration
│
├── nano-search-mcp/            # Local MCP server for Chinese financial search
│   ├── pyproject.toml
│   ├── src/nano_search_mcp/    # 12 MCP tools (search, reports, announcements, etc.)
│   └── tests/
│
├── skills/                     # OpenCode skills (3)
│   ├── data-warehouse/         # ClickHouse query interface (query_warehouse, list_tables)
│   ├── html-report/            # Interactive HTML reports with ECharts (7+1 templates)
│   └── periodic-execution/     # Cron job management (manage.py, notifier)
│
└── workspace/                  # Runtime files
    ├── pyproject.toml
    ├── scripts/                # Core computation engines
    │   ├── backtest/           # Walk-Forward backtest engine (engine, metrics, HPO, portfolio)
    │   ├── chanlun/            # Chanlun (缠论) technical analysis
    │   ├── microstructure/     # Market microstructure (escape top, concentration, margin, flow)
    │   ├── screening/          # 3-layer stock screening (fundamental, narrative, flow)
    │   ├── memory/             # Agent memory management (decay, evolution, scoring, injection)
    │   ├── realtime/           # Quote adapter + signal scanner
    │   ├── experiment/         # Experiment runner
    │   └── vibe_bridge/        # Vibe-Trading adapter
    └── cron_jobs/              # Periodic task management
        ├── manage.py           # CLI management tool
        ├── notifier.py         # DingTalk/email notification
        ├── trigger.sh          # Cron invocation entry point
        ├── registry.json       # Task registry (example)
        └── watchlist.json      # Watchlist configuration (example)
```

## Skills Reference

### 1. `data-warehouse` — ClickHouse Data Warehouse

Query interface for ClickHouse-based A-share data warehouse. Provides `query_warehouse(sql)` and `list_tables()` tools.

### 2. `html-report` — Interactive HTML Reports

Generates interactive HTML reports with ECharts. Templates: backtest, Alpha158, fundamental analysis, Chanlun, signal, screening, markdown conversion.

### 3. `periodic-execution` — Cron Job Management

Manages periodic strategy execution via `cron_jobs/manage.py`. Supports register/pause/resume/remove tasks with DingTalk/email notification.

## Scripts Reference

### `scripts/backtest/` — Walk-Forward Backtest Engine

Production-grade backtest framework with Walk-Forward validation, hyperparameter optimization, portfolio management, and 8 signal builder modules.

### `scripts/chanlun/` — Chanlun (缠论) Analysis

Fractal detection, stroke construction, and central hub identification for Chinese technical analysis.

### `scripts/microstructure/` — Market Microstructure

30+ modules: escape top预警, concentration, margin/borrow divergence, flow analysis, breadth, macro indicators, validation.

### `scripts/screening/` — Multi-Layer Stock Screening

Three-layer pipeline: fundamental (ROE, growth, OCF), narrative momentum (concept heat, research coverage), capital flow resonance.

### `scripts/memory/` — Agent Memory Management

Context injection, Ebbinghaus decay, evolution cycles, relevance scoring, and swarm bridge for multi-agent coordination.

### `scripts/realtime/` — Real-Time Data

Unified quote adapter (tushare/akshare/yfinance) and real-time signal scanner.

## Cron Jobs

### Management CLI

```bash
python cron_jobs/manage.py list              # List all tasks
python cron_jobs/manage.py add --name "..." --cron "..." --prompt "..."  # Register
python cron_jobs/manage.py pause <task_id>   # Pause
python cron_jobs/manage.py resume <task_id>  # Resume
python cron_jobs/manage.py remove <task_id>  # Remove
```

### Key Rules

1. Every execution must send notification (DingTalk) — no conditional notification
2. Notification must include execution date (YYYY-MM-DD)
3. Use `opencode run --attach <url>` to trigger agent execution (not `curl POST /session`)
4. New tasks auto-schedule a 5-minute test cron; verify with `manage.py verify-test`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DASHSCOPE_API_KEY` | Yes | DashScope API key for LLM inference |
| `OPENCODE_SERVER_PASSWORD` | Yes | Web server password |
| `CLICKHOUSE_HOST` | Yes | ClickHouse host |
| `CLICKHOUSE_PORT` | No | ClickHouse port (default: 8123) |
| `CLICKHOUSE_USER` | No | ClickHouse user (default: default) |
| `CLICKHOUSE_PASSWORD` | No | ClickHouse password |
| `CLICKHOUSE_DATABASE` | No | ClickHouse database (default: ashare) |
| `DINGTALK_WEBHOOK` | No | DingTalk robot webhook for notifications |
| `SMTP_HOST` / `SMTP_AUTH_CODE` | No | Email notification |

## Deployment

### docker-compose (Recommended)

```bash
cp .env.example .env   # Edit with your credentials
docker compose up -d
```

### Docker Run

```bash
docker run -d --name opencode-web -p 4096:4096 \
  --env-file .env \
  -v ./volumes/cron-state:/workspace/cron_jobs/state \
  -v ./volumes/cron-logs:/workspace/cron_jobs/logs \
  opencode-serve:latest
```

## Build Variants

| Dockerfile | Use Case |
|-----------|----------|
| `Dockerfile` | Based on `opencode-serve:latest`, upgrades to OpenCode 1.18.5 |
| `Dockerfile.amd64` | Based on `opencode-serve:0.0.6`, installs Node.js 20 + Python 3.12 from apt |

## License

- `skills/html-report/` — from [shadowinlife/vibetrading-html-report](https://github.com/shadowinlife/vibetrading-html-report)
- `nano-search-mcp/` — proprietary
- Other components — see individual file headers