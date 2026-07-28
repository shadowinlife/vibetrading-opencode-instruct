# opencode-serve 镜像使用说明书

> **版本**: v2.0.0-mymain  
> **架构**: AMD64 (Linux)  
> **基础镜像**: Ubuntu 22.04 + Python 3.12 + Node.js 20

---

## 目录

1. [概述](#1-概述)
2. [架构](#2-架构)
3. [前置条件](#3-前置条件)
4. [快速开始](#4-快速开始)
5. [构建镜像](#5-构建镜像)
6. [配置](#6-配置)
7. [部署](#7-部署)
    - 7.4 [持久化存储（挂盘）](#74-持久化存储挂盘)
8. [记忆生命周期](#8-记忆生命周期)
9. [服务验证](#9-服务验证)
10. [周期任务](#10-周期任务)
11. [ECS 部署](#11-ecs-部署)
12. [镜像仓库](#12-镜像仓库)
13. [故障排查](#13-故障排查)
14. [维护与升级](#14-维护与升级)

---

## 1. 概述

`opencode-serve` 是一个面向 A 股量化研究的 OpenCode Web Server 容器镜像。它打包了完整的 AI 代理研究环境：

- **OpenCode** Web Server（HTTP API + WebSocket），端口 4096
- **OMO**（oh-my-openagent）并行任务分解插件
- **Vibe-Trading**（mymain 分支）— 59 个 MCP 工具的量化研究引擎
- **nano-search-mcp** — 中国金融数据搜索（新浪财经、百炼 WebSearch）
- **data-warehouse** Skill — ClickHouse 数据仓库查询接口
- **html-report** Skill — ECharts 交互式 HTML 报告生成
- **MemoryGuard** Middleware — 自动记忆管理（Hook 触发，零 LLM 依赖）

### 核心特性

| 特性 | 说明 |
|------|------|
| 记忆自动管理 | FastMCP Middleware 拦截所有工具调用，自动 `memory_save` + `memory_reflect` |
| 记忆持久化 | `/workspace/.vt-memory/`，docker-compose volume 挂载，容器重启不丢失 |
| 分层镜像 | base（4.19GB，极少重建）→ app（每次部署重建） |
| ClickHouse 集成 | 199 列 T-1 A 股历史全量数据 |
| 实时数据补数 | 当日 OHLCV 通过 tencent/akshare/yfinance 实时获取 |
| 周期任务 | cron 定时执行策略，钉钉通知 |

---

## 2. 架构

```
┌─────────────────────────────────────────────────────┐
│                  opencode-serve (4096)               │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ OpenCode  │  │   OMO    │  │ Vibe-Trading MCP │  │
│  │  Server   │  │  Plugin  │  │  (59 tools)      │  │
│  │           │  │          │  │  ┌─────────────┐ │  │
│  │           │  │          │  │  │MemoryGuard  │ │  │
│  │           │  │          │  │  │Middleware   │ │  │
│  │           │  │          │  │  └──────┬──────┘ │  │
│  └──────────┘  └──────────┘  └─────────┼────────┘  │
│                                         │            │
│  ┌──────────────────┐                   │            │
│  │ nano-search-mcp   │                  │            │
│  │ (新浪财经/百炼)    │                  │            │
│  └──────────────────┘                   │            │
│                                         │            │
│  ┌──────────────────┐   ┌───────────────▼──────────┐ │
│  │  Skills (3)       │   │  /workspace/.vt-memory/  │ │
│  │  - data-warehouse │   │  - reflections/          │ │
│  │  - html-report    │   │  - persistent store      │ │
│  │  - periodic-exec  │   │  - quality scoring       │ │
│  └──────────────────┘   └──────────────────────────┘ │
└─────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
   ┌──────────┐       ┌──────────────┐
   │ClickHouse│       │  DashScope    │
   │(T-1 数据) │       │ (qwen3.7-max) │
   └──────────┘       └──────────────┘
```

### 镜像分层策略

```
┌─────────────────────────────────────────────┐
│ opencode-serve:v2.0.0-mymain  (~5GB)       │
│ ├── OMO plugin                              │
│ ├── Vibe-Trading mymain (editable install)  │
│ ├── nano-search-mcp                         │
│ ├── 项目文件 (configs, skills, scripts)      │
│ └── entrypoint.sh                           │
├─────────────────────────────────────────────┤
│ opencode-serve-base:latest  (4.19GB)        │
│ ├── Ubuntu 22.04                            │
│ ├── Python 3.12 (deadsnakes PPA)            │
│ ├── Node.js 20                              │
│ ├── OpenCode CLI                            │
│ ├── playwright + chromium                   │
│ └── pip 预装包 (plotly, ta, loguru, etc.)   │
└─────────────────────────────────────────────┘
```

---

## 3. 前置条件

### 必需

| 项目 | 说明 |
|------|------|
| Docker 20.10+ | 支持 `--platform linux/amd64` |
| DashScope API Key | 阿里云百炼，LLM 推理 |
| ClickHouse 实例 | A 股数据仓库 |

### 可选

| 项目 | 说明 |
|------|------|
| 钉钉机器人 Webhook | 周期任务通知 |
| 阿里云容器镜像服务 | 镜像推送/拉取 |
| Tushare Token | 数据同步 |
| OSS Bucket | 跨机 VT 源码传输 |

---

## 4. 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/shadowinlife/vibetrading-opencode-instruct.git
cd vibetrading-opencode-instruct

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际值

# 3. 拉取基础镜像（跳过本地构建 base）
docker pull registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve-base:latest
docker tag registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve-base:latest opencode-serve-base:latest

# 4. 构建 app 镜像
# 方式 A: 从本地 VT 源码
VT_SOURCE=../Vibe-Trading ./build.sh --app --tag v2.0.0-mymain

# 方式 B: 从 GitHub clone
VT_SOURCE=https://github.com/shadowinlife/Vibe-Trading.git ./build.sh --app --tag v2.0.0-mymain

# 5. 启动
docker compose up -d

# 6. 访问
curl http://localhost:4096/health
```

---

## 5. 构建镜像

### 5.1 build.sh 脚本

```bash
./build.sh [--base|--app] [--tag TAG] [--push] [--dry-run]
```

| 参数 | 说明 |
|------|------|
| `--base` | 构建基础镜像（极少使用） |
| `--app` | 构建 app 镜像（默认） |
| `--tag TAG` | 镜像标签，默认 `latest` |
| `--push` | 构建后推送到 registry |
| `--dry-run` | 仅打印命令，不执行 |

### 5.2 构建基础镜像

```bash
# 仅当基础依赖变更时执行（新增系统包/Python 包/Node 包）
./build.sh --base --tag latest
```

基础镜像包含 Ubuntu 22.04 + Python 3.12 + Node.js 20 + OpenCode CLI + playwright + 预装 pip 包（plotly, ta, loguru 等）。构建时间约 15-20 分钟。

### 5.3 构建 app 镜像

```bash
# VT_SOURCE 环境变量指定 Vibe-Trading 源码位置
VT_SOURCE=../Vibe-Trading ./build.sh --app --tag v2.0.0-mymain        # 本地目录
VT_SOURCE=https://github.com/shadowinlife/Vibe-Trading.git ./build.sh # GitHub URL
VT_SOURCE=vendor/Vibe-Trading ./build.sh --app --tag v2.0.0-mymain    # 预解压目录
```

app 镜像构建时间约 5-8 分钟（基础镜像已缓存）。

### 5.4 跨平台构建

```bash
# 在 ARM64 (Apple Silicon) 上构建 AMD64 镜像
DOCKER_PLATFORM=linux/amd64 VT_SOURCE=../Vibe-Trading ./build.sh --app --tag v2.0.0-mymain
```

---

## 6. 配置

### 6.1 环境变量 (.env)

| 变量 | 必需 | 默认值 | 说明 |
|------|------|--------|------|
| `DASHSCOPE_API_KEY` | ✅ | — | DashScope API Key |
| `OPENCODE_SERVER_PASSWORD` | ✅ | — | Web Server 密码 |
| `CLICKHOUSE_HOST` | ✅ | — | ClickHouse 地址 |
| `CLICKHOUSE_PORT` | ❌ | `8123` | ClickHouse HTTP 端口 |
| `CLICKHOUSE_DATABASE` | ❌ | `ashare` | 数据库名 |
| `CLICKHOUSE_USER` | ❌ | `default` | 用户名 |
| `CLICKHOUSE_PASSWORD` | ❌ | — | 密码 |
| `DINGTALK_WEBHOOK` | ❌ | — | 钉钉机器人 Webhook |
| `SMTP_HOST` | ❌ | — | SMTP 服务器 |
| `SMTP_AUTH_CODE` | ❌ | — | SMTP 授权码 |
| `LANGCHAIN_PROVIDER` | ❌ | `dashscope` | LLM 提供商 |
| `LANGCHAIN_MODEL_NAME` | ❌ | `qwen3.7-max` | 模型名称 |
| `LANGCHAIN_TEMPERATURE` | ❌ | `0.3` | 生成温度 |
| `TUSHARE_TOKEN` | ❌ | — | Tushare Pro Token |

### 6.2 记忆配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VT_MEMORY` | `full` | 记忆模式：`full` 启用全部 |
| `VT_MEMORY_MCP_TOOLS` | `1` | 启用 MCP 记忆工具 |
| `VT_MEMORY_BASE_DIR` | `/workspace/.vt-memory` | 记忆存储路径 |

这些变量在 Dockerfile 中预设，通常无需手动修改。

---

## 7. 部署

### 7.1 docker-compose（推荐）

```yaml
# docker-compose.yml
services:
  opencode-web:
    image: opencode-serve:v2.0.0-mymain
    container_name: opencode-web
    restart: unless-stopped
    ports:
      - "4096:4096"
    env_file:
      - .env
    volumes:
      - ./volumes/cron-state:/workspace/cron_jobs/state
      - ./volumes/cron-logs:/workspace/cron_jobs/logs
      - ./volumes/vt-memory:/workspace/.vt-memory
    deploy:
      resources:
        limits:
          memory: 6G
          cpus: '0.8'
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
```

```bash
# 启动
docker compose up -d

# 查看日志
docker compose logs -f

# 停止
docker compose down
```

### 7.2 docker run

```bash
docker run -d \
  --name opencode-web \
  -p 4096:4096 \
  --env-file .env \
  -v ./volumes/cron-state:/workspace/cron_jobs/state \
  -v ./volumes/cron-logs:/workspace/cron_jobs/logs \
  -v ./volumes/vt-memory:/workspace/.vt-memory \
  --memory 6g \
  --cpus 0.8 \
  --restart unless-stopped \
  opencode-serve:v2.0.0-mymain
```

### 7.3 从 Registry 部署

```bash
# 拉取镜像
docker pull registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain

# 标记本地
docker tag registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain opencode-serve:v2.0.0-mymain

# 启动
docker compose up -d
```

### 7.4 持久化存储（挂盘）⚠️ 关键

容器本身是无状态的——删除容器即丢失所有内部数据。以下三类数据**必须**通过 volume 挂载到宿主机持久化：

| 容器内路径 | 宿主机路径 | 数据内容 | 丢失后果 |
|-----------|-----------|---------|---------|
| `/workspace/.vt-memory/` | `./volumes/vt-memory` | 记忆库（persistent、reflections、archive） | 所有跨会话经验、策略反思、回测教训全部丢失 |
| `/workspace/cron_jobs/state/` | `./volumes/cron-state` | 周期任务状态（registry.json、任务锁） | 任务注册信息丢失，需重新注册 |
| `/workspace/cron_jobs/logs/` | `./volumes/cron-logs` | 周期任务执行日志 | 历史执行记录丢失，无法审计 |

#### 挂盘检查清单

```bash
# 1. 确认宿主机目录存在
ls -la volumes/vt-memory volumes/cron-state volumes/cron-logs

# 2. 启动后确认容器内挂载生效
docker exec opencode-web df -h | grep workspace
# 预期输出：宿主机磁盘而非 overlay

# 3. 确认记忆目录可写
docker exec opencode-web touch /workspace/.vt-memory/.write-test && \
  docker exec opencode-web rm /workspace/.vt-memory/.write-test && \
  echo "OK: vt-memory writable"

# 4. 确认宿主机能看到写入数据
docker exec opencode-web ls /workspace/.vt-memory/
ls volumes/vt-memory/
# 两边内容应一致
```

#### 未挂盘的后果

如果**不挂载**以上 volume：

- **记忆数据**：容器重启后丢失所有历史记忆，AI 每次都是"零经验"开始
- **周期任务**：`registry.json` 丢失，所有定时策略停止执行
- **执行日志**：无法回溯历史执行记录，问题排查困难

#### 可选挂载（按需）

| 容器内路径 | 用途 | 建议场景 |
|-----------|------|---------|
| `/workspace/analysis/` | 分析报告（Markdown/JSON） | 需要持久化保存分析结果 |
| `/workspace/reports/` | HTML 交互报告 | 需要 nginx 对外服务 |
| `/workspace/runs/` | 回测运行目录（config + artifacts） | 需要保留回测记录 |
| `/workspace/tmp/` | 临时文件 | 调试排查 |

挂载方式（在 `docker-compose.yml` 或 `docker run -v` 中添加）：

```yaml
volumes:
  - ./volumes/vt-memory:/workspace/.vt-memory
  - ./volumes/cron-state:/workspace/cron_jobs/state
  - ./volumes/cron-logs:/workspace/cron_jobs/logs
  # 可选
  - ./volumes/analysis:/workspace/analysis
  - ./volumes/reports:/workspace/reports
```

---

## 8. 记忆生命周期

### 8.1 自动触发机制（MemoryGuard Middleware）

MemoryGuard 是 mymain 分支的核心特性，在 FastMCP 层面拦截所有 Vibe-Trading 工具调用，自动执行记忆管理，**完全不依赖 LLM 提示词**。

| 阶段 | 触发条件 | 动作 |
|------|---------|------|
| 每次工具调用 | 所有 59 个 VT MCP 工具 | `memory_save`（工具名、参数、结果、耗时） |
| 回测/因子分析/交易日志后 | `backtest`、`factor_analysis` 等 | `memory_reflect`（sharpe、max_drawdown 等指标） |
| 容器启动时 | entrypoint 阶段 | `memory_status` 验证 |

### 8.2 记忆存储

```
/workspace/.vt-memory/
├── persistent/          # 持久化记忆（JSON）
├── reflections/         # 反思课程（JSONL append-only）
├── archive/             # 归档区（低质量记忆）
└── index.json           # 记忆索引
```

### 8.3 记忆管理特性

- **质量评分**：基于来源可靠性、验证次数、时间衰减
- **艾宾浩斯遗忘曲线**：长时间未使用自动降权
- **归档 GC**：低质量记忆移入归档区，不注入主上下文
- **跨会话持久化**：volume 挂载保证容器重启不丢失

### 8.4 验证记忆是否正常工作

```bash
# 进入容器
docker exec -it opencode-web bash

# 检查记忆目录
ls -la /workspace/.vt-memory/

# 检查 entrypoint 日志
docker logs opencode-web 2>&1 | grep "VT_MEMORY"

# 预期输出：
# [entrypoint] VT_MEMORY=full, VT_MEMORY_MCP_TOOLS=1 → memory tools enabled
# [entrypoint] VT_MEMORY_BASE_DIR=/workspace/.vt-memory
```

---

## 9. 服务验证

### 9.1 启动日志检查

```bash
docker logs opencode-web 2>&1 | grep "\[entrypoint\]"
```

预期输出：
```
[entrypoint] Fixed venv python symlinks → /usr/bin/python3
[entrypoint] opencode.json rendered from template → /home/opencode/.opencode/opencode.json
[entrypoint] Plugin cache symlinked
[entrypoint] ClickHouse OK — warming schema cache
[entrypoint] VT MCP server OK — 59 tools registered
[entrypoint] VT_MEMORY=full, VT_MEMORY_MCP_TOOLS=1 → memory tools enabled
```

### 9.2 健康检查

```bash
curl http://localhost:4096/health
# 预期: 200 OK

curl -u "opencode:${OPENCODE_SERVER_PASSWORD}" http://localhost:4096/api/v1/status
# 预期: JSON 状态信息
```

### 9.3 ClickHouse 连通性

```bash
docker exec opencode-web python3 -c "
from clickhouse_driver import Client
c = Client(host='${CLICKHOUSE_HOST}', port=${CLICKHOUSE_PORT:-8123}, user='${CLICKHOUSE_USER:-default}', password='${CLICKHOUSE_PASSWORD}')
print(c.execute('SELECT 1'))
"
```

### 9.4 VT MCP Server 验证

```bash
docker exec opencode-web /opt/venv/bin/python3 -c "
import sys; sys.path.insert(0, '/opt/vibe-trading/agent')
from mcp_server import mcp
print(f'Tools: {len(mcp._tool_manager._tools)}')
print(f'MemoryGuard: OK' if hasattr(mcp, '_middleware') else 'MemoryGuard: MISSING')
"
```

---

## 10. 周期任务

### 10.1 管理命令

```bash
# 进入容器
docker exec -it opencode-web bash
source /opt/venv/bin/activate

# 列出任务
python cron_jobs/manage.py list

# 注册任务
python cron_jobs/manage.py add \
  --name "daily-screening" \
  --cron "0 20 * * 1-5" \
  --prompt "执行场景 E 选股策略，筛选全 A 股 Top 10"

# 暂停 / 恢复 / 删除
python cron_jobs/manage.py pause <task_id>
python cron_jobs/manage.py resume <task_id>
python cron_jobs/manage.py remove <task_id>

# 验证测试任务
python cron_jobs/manage.py verify-test <task_id>
```

### 10.2 关键规则

1. **每次执行必须通知**（钉钉），无论结果如何
2. 通知正文必须包含执行日期（YYYY-MM-DD）
3. 使用 `opencode run --attach <url>` 触发 agent 执行（不可用 `curl POST /session`）
4. 新任务自动安排 5 分钟测试 cron，需用 `verify-test` 验证

### 10.3 日志

```bash
# 查看 cron 执行日志
ls -la volumes/cron-logs/
docker exec opencode-web cat /workspace/cron_jobs/logs/<task_id>_<timestamp>.log
```

---

## 11. ECS 部署

### 11.1 一键构建脚本

```bash
# 在 ECS (AMD64) 上执行
cd ~ && \
wget https://raw.githubusercontent.com/shadowinlife/vibetrading-opencode-instruct/main/deploy/ecs-build.sh && \
chmod +x ecs-build.sh && \
./ecs-build.sh v2.0.0-mymain
```

### 11.2 手动构建

```bash
# 1. 克隆仓库
git clone https://github.com/shadowinlife/vibetrading-opencode-instruct.git
git clone -b mymain https://github.com/shadowinlife/Vibe-Trading.git

# 2. 基础镜像（如已推送可跳过）
cd vibetrading-opencode-instruct
docker pull registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve-base:latest
docker tag registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve-base:latest opencode-serve-base:latest

# 3. App 镜像
VT_SOURCE=../Vibe-Trading ./build.sh --app --tag v2.0.0-mymain

# 4. 推送
docker tag opencode-serve:v2.0.0-mymain registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain
docker push registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain
```

### 11.3 OSS 桥接（当 GitHub clone 超时时）

```bash
# 本地打包 VT 源码
cd ~/Vibe-Trading && \
tar --exclude='.git' --exclude='frontend' --exclude='node_modules' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
    -czf /tmp/vt-mymain.tar.gz . && \
ossutil cp /tmp/vt-mymain.tar.gz oss://invest-assistant-v1/build/vt-mymain.tar.gz

# ECS 下载
cd ~/vibetrading-opencode-instruct && \
wget -O /tmp/vt-mymain.tar.gz "https://<your-oss-bucket>.<your-oss-endpoint>/build/vt-mymain.tar.gz" && \
mkdir -p vendor/Vibe-Trading && \
tar -xzf /tmp/vt-mymain.tar.gz -C vendor/Vibe-Trading/ && \
VT_SOURCE=vendor/Vibe-Trading ./build.sh --app --tag v2.0.0-mymain
```

### 11.4 ECS 踩坑要点

| 问题 | 解决方案 |
|------|---------|
| ARM64 本地不可构建 | 必须在 AMD64 ECS 上构建（Bun AVX 指令集需求） |
| GitHub clone 超时 | 使用 OSS 桥接 |
| PEP 668 保护 | 使用 `python3 -m venv` 创建虚拟环境 |
| playwright 下载慢 | `PLAYWRIGHT_DOWNLOAD_HOST=npmmirror` |
| Ubuntu 22.04 无 Python 3.12 | deadsnakes PPA |

---

## 12. 镜像仓库

### 12.1 阿里云容器镜像服务

| 镜像 | 地址 |
|------|------|
| 基础镜像 | `registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve-base:latest` |
| App 镜像 | `registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain` |

### 12.2 推送镜像

```bash
# 登录
docker login --username=<阿里云账号> registry.cn-hangzhou.aliyuncs.com

# 推送
docker tag opencode-serve:v2.0.0-mymain registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain
docker push registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain
```

### 12.3 拉取镜像

```bash
docker pull registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:v2.0.0-mymain
```

---

## 13. 故障排查

### 13.1 容器启动失败

```bash
# 查看完整日志
docker logs opencode-web 2>&1 | tail -50

# 常见问题：
# - "opencode.json rendered" 失败 → 检查 Jinja2 模板是否正确
# - "VT MCP server import failed" → 检查 /opt/vibe-trading/agent/ 是否存在
# - "ClickHouse unreachable" → 检查 CLICKHOUSE_HOST 和网络连通性
```

### 13.2 记忆未写入

```bash
# 1. 检查环境变量
docker exec opencode-web env | grep VT_MEMORY

# 2. 检查目录权限
docker exec opencode-web ls -la /workspace/.vt-memory/

# 3. 检查 middleware 注册
docker exec opencode-web /opt/venv/bin/python3 -c "
import sys; sys.path.insert(0, '/opt/vibe-trading/agent')
from mcp_server import mcp
print('Middleware:', [m.__class__.__name__ for m in mcp._middleware])
"
# 预期: Middleware: ['MemoryGuardMiddleware']
```

### 13.3 ClickHouse 连接失败

```bash
# 测试连通性
docker exec opencode-web curl -s "http://${CLICKHOUSE_HOST}:${CLICKHOUSE_PORT}/ping"

# 检查配置
docker exec opencode-web cat /home/opencode/.opencode/opencode.json | python3 -m json.tool | grep -A5 CLICKHOUSE
```

### 13.4 内存不足

```bash
# 调整 docker-compose.yml 内存限制
# deploy.resources.limits.memory: 6G → 8G

# 或调整 OMO 并行度
# config/oh-my-openagent.json 中 max_concurrent_agents
```

### 13.5 镜像拉取慢

```bash
# 配置 Docker 镜像加速器
# /etc/docker/daemon.json
{
  "registry-mirrors": ["https://<your-mirror>.mirror.aliyuncs.com"]
}
```

---

## 14. 维护与升级

### 14.1 升级 App 镜像

```bash
# 1. 拉取最新镜像
docker pull registry.cn-hangzhou.aliyuncs.com/<your-registry-namespace>/opencode-serve:latest

# 2. 更新 docker-compose.yml 中的 tag

# 3. 重启
docker compose down && docker compose up -d

# 4. 验证
docker compose logs -f
```

### 14.2 升级基础镜像

基础镜像极少变更，仅当以下情况需重建：

- 新增系统级依赖（apt 包）
- 新增 Python 预装包
- 新增 Node.js 全局包
- Ubuntu 安全更新

```bash
./build.sh --base --tag latest --push
```

### 14.3 数据备份

```bash
# 备份记忆数据
tar -czf vt-memory-backup-$(date +%Y%m%d).tar.gz volumes/vt-memory/

# 备份 cron 状态
tar -czf cron-state-backup-$(date +%Y%m%d).tar.gz volumes/cron-state/

# 备份分析报告
tar -czf analysis-backup-$(date +%Y%m%d).tar.gz volumes/analysis/  # 如挂载
```

### 14.4 清理

```bash
# 清理旧镜像
docker image prune -a --filter "until=72h"

# 清理日志
docker exec opencode-web find /workspace/cron_jobs/logs -mtime +30 -delete

# 清理 Docker 构建缓存
docker builder prune
```

---

## 附录 A：端口映射

| 端口 | 服务 | 说明 |
|------|------|------|
| 4096 | OpenCode Web Server | HTTP API + WebSocket |

## 附录 B：文件路径速查

| 路径 | 说明 |
|------|------|
| `/opt/venv/` | Python 虚拟环境 |
| `/opt/vibe-trading/` | Vibe-Trading 源码（editable install） |
| `/opt/vibe-trading/agent/mcp_server.py` | VT MCP Server 入口 |
| `/opt/vibe-trading/agent/src/memory/memory_guard.py` | MemoryGuard Middleware |
| `/opt/nano-search-mcp/` | nano-search-mcp 源码 |
| `/workspace/.opencode/opencode.json.tmpl` | OpenCode 配置模板 |
| `/workspace/.opencode/skills/` | 3 个 Skill |
| `/workspace/scripts/` | 量化分析脚本 |
| `/workspace/cron_jobs/` | 周期任务管理 |
| `/workspace/analysis/` | 分析报告输出 |
| `/workspace/reports/` | HTML 报告 |
| `/workspace/runs/` | 回测运行目录 |
| `/workspace/tmp/` | 临时文件 |
| `/workspace/.vt-memory/` | VT 记忆存储 |
| `/home/opencode/.opencode/opencode.json` | 运行时配置（由 entrypoint 渲染） |

## 附录 C：构建时序

```
1. Dockerfile.base (1次 / 极少)
   ├── Ubuntu 22.04
   ├── Python 3.12 (deadsnakes)
   ├── Node.js 20
   ├── OpenCode CLI
   ├── pip 预装包
   ├── playwright + chromium
   └── opencode 用户

2. Dockerfile (每次部署)
   ├── FROM opencode-serve-base:latest
   ├── opencode-ai@latest
   ├── OMO plugin
   ├── Vibe-Trading mymain (editable install)
   ├── nano-search-mcp (editable install)
   ├── 项目文件 (configs, skills, scripts)
   └── entrypoint.sh

3. entrypoint.sh (容器启动)
   ├── 激活 venv
   ├── 设置 VT_MEMORY 环境变量
   ├── 修复 venv symlinks
   ├── 渲染 opencode.json (Jinja2)
   ├── 探测 ClickHouse 连通性
   ├── 建立 plugin 缓存 symlink
   ├── 验证 VT MCP Server 可导入
   └── 启动 opencode serve
```

---

> **文档版本**: 1.0  
> **适用镜像**: opencode-serve:v2.0.0-mymain  
> **维护者**: Sisyphus via OpenCode  
> **最后更新**: 2026-07-28