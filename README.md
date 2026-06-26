# VibeTrading OpenCode Instruct

> **一键搭建 AI 金融分析工作站** — 基于 [OpenCode](https://github.com/opencode-ai/opencode) + [Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) + [Oh-My-OpenAgent](https://github.com/code-yeongyu/oh-my-openagent)

本仓库是一个**完整的工作区模板**，包含 AGENTS.md 层级约束体系、自定义 Skills、MCP 配置、周期任务框架和示例输出。第三方用户按照本指南操作，即可在本地或服务器上复现整套 AI 金融分析环境。

---

## 架构概览

```
┌─────────────────────────────────────────────────────────┐
│                    OpenCode (AI Agent)                   │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────────┐  │
│  │ AGENTS.md│  │  Skills  │  │  Oh-My-OpenAgent      │  │
│  │ 层级约束  │  │ 自定义    │  │  (Prometheus/Sisyphus │  │
│  │ 体系     │  │ 金融分析  │  │   /Oracle/Metis...)   │  │
│  └──────────┘  └──────────┘  └───────────────────────┘  │
│         │            │                  │                │
│  ┌──────┴────────────┴──────────────────┴─────────────┐ │
│  │                   MCP Servers                       │ │
│  │  ┌──────────────┐  ┌────────────────────────────┐  │ │
│  │  │ vibe-trading  │  │ nano-search-mcp            │  │ │
│  │  │ 74 skills     │  │ 公告/研报/IR/通用搜索       │  │ │
│  │  │ 29 swarm      │  │ Playwright 页面抓取         │  │ │
│  │  │ 27 tools      │  │ A股标的解析                 │  │ │
│  │  └──────────────┘  └────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────┘ │
│         │                                               │
│  ┌──────┴────────────────────────────────────────────┐  │
│  │              Local Data Layer                       │ │
│  │  DuckDB (ashare.duckdb)  ·  Tushare  ·  AKShare   │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## 目录结构

```
.
├── AGENTS.md                          # 根级 AI Agent 上下文约束
├── .opencode/
│   ├── opencode.json                  # OpenCode + MCP 配置
│   ├── oh-my-openagent.json           # OMO agent/category 模型配置
│   └── skills/                        # 5 个自定义金融分析 Skills
│       ├── analysis-report/           #   报告生成与归档
│       ├── backtest-framework/        #   Walk-Forward 回测框架
│       ├── periodic-execution/        #   周期任务管理
│       ├── stock-analysis-workflow/   #   投资分析工作流
│       └── stock-quant-analysis/      #   单股量化分析入口
├── analysis/                          # 分析报告存储（按标的代码）
│   ├── AGENTS.md                      #   子目录约束
│   ├── _template.md                   #   报告模板
│   └── _index.json                    #   报告索引
├── cron_jobs/                         # 周期任务框架
│   ├── AGENTS.md                      #   子目录约束
│   ├── manage.py                      #   CLI 管理工具
│   ├── notifier.py                    #   钉钉/邮件通知
│   ├── trigger.sh                     #   cron 触发入口
│   ├── registry.json                  #   任务注册表（空模板）
│   └── watchlist.json                 #   监控标的列表
├── scripts/                           # 量化脚本骨架
│   ├── AGENTS.md                      #   子目录约束
│   └── memory/                        #   记忆进化系统（24 个模块）
│       ├── schema.py                  #     MemoryEntry 数据结构
│       ├── reflexion.py               #     Reflexion 失败检测与反思
│       ├── expel.py                   #     ExpeL 洞察提取与计数器
│       ├── evolution.py               #     COPRO/A-Evolve 指令进化
│       ├── decay.py                   #     记忆衰减与相关性评分
│       ├── injection.py               #     Worker prompt 注入
│       └── ... (18 more modules)      #     完整模块列表见下文
├── policy/                            # 策略研究目录
│   └── AGENTS.md                      #   子目录约束
├── docs/                              # DuckDB 表文档
│   ├── README.md
│   └── tables/_index.md
├── sync/                              # 增量同步套件
│   └── README.md
├── examples/                          # 脱敏示例输出
│   ├── dingtalk_nasdaq_signal.md      #   钉钉信号通知示例
│   ├── dingtalk_escape_top.md         #   逃顶预警通知示例
│   ├── backtest_summary.json          #   回测 summary 格式
│   └── cron_registry_example.json     #   注册表配置示例
├── duckdb/                            # DuckDB 数据目录（空）
└── tmp/                               # 临时文件目录（空）
```

---

## 安装指南

### 前置条件

| 组件 | 版本要求 | 安装方式 |
|------|---------|---------|
| Python | >= 3.10 | conda / pyenv |
| Node.js | >= 18 | nvm / brew |
| OpenCode | latest | `npm install -g opencode-ai` |
| DuckDB | >= 0.10 | `pip install duckdb` |

### Step 1: 克隆本仓库

```bash
git clone https://github.com/shadowinlife/vibetrading-opencode-instruct.git
cd vibetrading-opencode-instruct
```

### Step 2: 安装 Vibe-Trading MCP

Vibe-Trading 提供 74 个金融分析 skills、29 个 swarm 多 agent 编排预设、27 个数据工具。

```bash
# 方式 A: pip 安装（推荐）
pip install vibe-trading-ai

# 方式 B: 源码安装（开发模式）
git clone https://github.com/shadowinlife/Vibe-Trading.git
cd Vibe-Trading
pip install -e .
cd ..
```

### Step 3: 安装 Nano Search MCP

提供 A 股公告/年报/研报/IR 纪要搜索 + 通用网页搜索 + Playwright 页面抓取。

```bash
git clone https://github.com/shadowinlife/vibetrading-search-mcp.git
cd vibetrading-search-mcp
pip install -e ".[dev]"
playwright install chromium
cd ..
```

### Step 4: 安装 Oh-My-OpenAgent 插件

提供 Prometheus（规划）、Sisyphus（执行）、Oracle（咨询）、Metis（预审）、Momus（QA）等 agent 体系。

```bash
# 在项目目录下执行
opencode plugin install oh-my-openagent@latest
```

### Step 5: 配置环境变量

创建 `.env` 文件（**不要提交到 git**）：

```bash
# Tushare Pro API Token（A股数据同步必需）
TUSHARE_TOKEN=your_tushare_token_here

# 钉钉机器人 Webhook（周期任务通知必需）
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN

# Vibe-Trading 数据源（可选，按需配置）
# AKSHARE 无需 token
# YFINANCE 无需 token
```

### Step 6: 配置模型

编辑 `.opencode/opencode.json`，将 `model` 字段替换为你使用的模型：

```json
{
  "model": "your-model-id"
}
```

同步编辑 `.opencode/oh-my-openagent.json` 中各 agent 和 category 的 `model` 字段。

常用模型示例：
- `alibaba-cn/qwen3.7-max`
- `anthropic/claude-sonnet-4-20250514`
- `openai/gpt-4o`

### Step 7: 初始化 DuckDB 数据（可选）

如果需要 A 股历史数据，使用 [nano_quant_skills](https://github.com/shadowinlife/nano_quant_skills) 的 `ts2ck` 模块同步 Tushare 数据到本地 DuckDB：

```bash
git clone https://github.com/shadowinlife/nano_quant_skills.git
cd nano_quant_skills
uv sync --all-packages
uv run ts2ck init
# 按文档配置 config.yaml 后执行同步
```

### Step 8: 启动 OpenCode

```bash
opencode
```

OpenCode 会自动加载：
- 根 `AGENTS.md` 作为上下文约束
- `.opencode/opencode.json` 中的 MCP 配置
- `.opencode/skills/` 中的自定义 Skills
- Oh-My-OpenAgent 插件的 agent 体系

---

## 使用方式

### 场景 A: 股票/ETF 分析

```
> 分析一下 601777 的投资价值
```

Agent 会自动：
1. 检查本地 DuckDB 数据
2. 调用 Vibe-Trading MCP 获取补充数据
3. 使用 `stock-analysis-workflow` Skill 执行分析
4. 通过 `analysis-report` Skill 生成标准报告

### 场景 B: 量化回测

```
> 对 588000.SH 做 Alpha158 因子回测
```

Agent 会：
1. 加载 `stock-quant-analysis` Skill
2. 计算 Alpha158 因子 + 构建信号 + Walk-Forward 回测
3. 输出 Sharpe、年化收益、最大回撤等指标

### 场景 C: 周期任务

```
> 设置一个每天 14:50 的纳斯达克 ETF 信号检查任务
```

Agent 会：
1. 加载 `periodic-execution` Skill
2. 通过 `cron_jobs/manage.py` 注册任务
3. 配置钉钉通知

### 场景 D: Swarm 多 Agent 分析

```
> 用 investment_committee 团队分析贵州茅台
```

Agent 会调用 Vibe-Trading 的 swarm 预设，启动多 agent 协作分析。

---

## AGENTS.md 层级体系

本仓库的核心设计是 **AGENTS.md 层级约束**：

| 层级 | 文件 | 作用域 |
|------|------|--------|
| 根级 | `AGENTS.md` | 全局环境、数据源、分析流程、约束速查 |
| 子目录 | `analysis/AGENTS.md` | 报告模板、索引规范、存储约定 |
| 子目录 | `scripts/AGENTS.md` | 因子计算、回测框架、禁止事项 |
| 子目录 | `cron_jobs/AGENTS.md` | 任务管理、通知规范、日志格式 |
| 子目录 | `policy/AGENTS.md` | 策略目录结构、信号逻辑复用 |

当 AI Agent 进入某个子目录工作时，会同时加载根级和子目录的 AGENTS.md，确保遵守局部约定。

---

## 自定义 Skills

| Skill | 触发词 | 功能 |
|-------|--------|------|
| `analysis-report` | 生成报告、保存分析 | 标准化报告生成 + `_index.json` 更新 |
| `backtest-framework` | 回测、Walk-Forward | 单股 Walk-Forward 回测框架使用指南 |
| `periodic-execution` | 定时运行、cron | 周期任务注册、触发、通知管理 |
| `stock-analysis-workflow` | 投资价值、估值 | 基本面 + 定性分析工作流 |
| `stock-quant-analysis` | 量化分析、Alpha158 | 三模块并行量化分析入口 |

---

## 记忆进化系统

`scripts/memory/` 包含完整的记忆进化系统，实现 AI Agent 的长期记忆、失败反思和自动优化能力。该系统基于学术论文 Reflexion、ExpeL、COPRO/A-Evolve 实现，已在生产环境验证。

### 核心模块（24 个文件）

| 模块 | 功能 | 学术基础 |
|------|------|----------|
| `schema.py` | MemoryEntry 数据结构、YAML frontmatter 序列化 | - |
| `reflexion.py` | 失败检测、反思生成、错误笔记本追加 | Reflexion (Shinn et al. 2023) |
| `expel.py` | 跨任务洞察提取、计数器机制（ADD/AGREE/DISAGREE） | ExpeL (Zhao et al. 2023) |
| `evolution.py` | COPRO 风格候选生成、评分、选择最优指令更新 | COPRO (McLevey et al. 2024) |
| `attribution.py` | GEPA 风格失败归因、指令级缺陷分类 | GEPA (Zelikman et al. 2024) |
| `decay.py` | 时间衰减、使用频率加权、粘性条目机制 | - |
| `scoring.py` | 回测指标评分、LLM-as-Judge 评分 | - |
| `injection.py` | Worker prompt 注入、记忆块格式化 | - |
| `swarm_bridge.py` | Swarm worker 记忆桥接 | - |
| `feedback_reset.py` | 进化后反馈重置、解决旧错误条目 | - |
| `stability.py` | 稳定性检测、发布分支管理 | - |
| `sanity.py` | 变异后 sanity check（6 项检查） | - |
| `scope_controller.py` | 渐进式范围控制（4 级强度） | - |
| `git_committer.py` | Git 提交器、sanity gate、DingTalk 通知 | - |
| `truncation.py` | 渐进式截断（4 级 fallback） | - |
| `hooks.py` | 会话/run 完成钩子 | - |
| `utils.py` | 共享工具函数 | - |
| `parsers/` | 轨迹解析器（OpenCode、Swarm） | - |
| `config/` | 配置文件（swarm_memory.json） | - |

### 工作流程

```
会话完成
  ↓
hooks.py: 加载轨迹 → 检测失败
  ↓
reflexion.py: 生成反思 → 追加到 mistakes.md
  ↓
scoring.py: 评分轨迹（回测指标 / LLM-Judge）
  ↓
expel.py: 提取洞察 → 更新 insights.md（计数器机制）
  ↓
evolution.py: 生成候选指令更新 → 评分 → 选择最优
  ↓
sanity.py: 6 项 sanity check
  ↓
git_committer.py: 提交变更 → DingTalk 通知
  ↓
stability.py: 检测稳定性 → 创建发布分支（如适用）
```

### 关键特性

1. **失败驱动反思**：仅在失败时生成反思，避免过度学习
2. **计数器机制**：洞察通过社区支持度（ADD/AGREE/DISAGREE）动态调整
3. **渐进式范围控制**：根据近期表现自动调整进化强度
4. **Sanity Gate**：变异前必须通过 6 项检查（行数、重复、完整性等）
5. **稳定性检测**：连续 N 次稳定后创建发布分支
6. **记忆衰减**：时间衰减 + 使用频率加权 + 粘性条目（高频使用不衰减）

### 使用示例

```python
from scripts.memory import MemoryEntry, relevance_score
from scripts.memory.reflexion import detect_failure, generate_reflection
from scripts.memory.expel import extract_insights

# 1. 检测失败
failure = detect_failure(trajectory)
if failure:
    reflection = generate_reflection(trajectory, failure)
    # 追加到 ~/.vibe-trading/memory/stocks/{code}/mistakes.md

# 2. 提取洞察
insights = extract_insights(success_trajectories, failure_trajectories)
# 更新 ~/.vibe-trading/memory/global/insights.md

# 3. 计算相关性
entry = MemoryEntry(...)
score = relevance_score(entry)  # 考虑时间衰减 + 使用频率
```

### 配置

记忆系统使用 `~/.vibe-trading/memory/` 作为根目录：

```
~/.vibe-trading/memory/
├── stocks/                    # 按股票代码隔离的记忆
│   └── {code}/
│       ├── mistakes.md        # 错误笔记本
│       └── insights.md        # 个股洞察
├── global/                    # 全局记忆
│   ├── mistakes.md            # 全局错误
│   └── insights.md            # 跨股洞察
└── evolution/                 # 进化历史
    ├── candidates/            # 候选指令更新
    └── releases/              # 发布分支记录
```

---

## 示例输出

`examples/` 目录包含脱敏后的真实运行示例：

- **[钉钉信号通知](examples/dingtalk_nasdaq_signal.md)** — 纳斯达克100ETF 每日 S4+S5 双策略收盘信号
- **[逃顶预警通知](examples/dingtalk_escape_top.md)** — A股微观结构逃顶分析每日预警
- **[回测 Summary](examples/backtest_summary.json)** — Walk-Forward 回测 JSON 输出格式
- **[注册表配置](examples/cron_registry_example.json)** — 周期任务注册表示例

---

## 依赖仓库

| 仓库 | 用途 | 安装 |
|------|------|------|
| [Vibe-Trading](https://github.com/shadowinlife/Vibe-Trading) | 交易 Agent MCP（74 skills, 29 swarm, 27 tools） | `pip install vibe-trading-ai` |
| [vibetrading-search-mcp](https://github.com/shadowinlife/vibetrading-search-mcp) | 搜索 MCP（公告/研报/IR/通用搜索） | `pip install -e ".[dev]"` |
| [vibetrading-html-report](https://github.com/shadowinlife/vibetrading-html-report) | ECharts HTML 报告生成 | `pip install jinja2 pandas` |
| [nano_quant_skills](https://github.com/shadowinlife/nano_quant_skills) | 数据同步（ts2ck）+ 七看八问 | `uv sync --all-packages` |
| [RSSHub-MCP](https://github.com/shadowinlife/RSSHub-MCP) | 微博/雪球言论观测 MCP | `pip install -e .` |
| [Oh-My-OpenAgent](https://github.com/code-yeongyu/oh-my-openagent) | Agent 编排插件 | `opencode plugin install oh-my-openagent@latest` |

---

## 常见问题

**Q: 模型如何选择？**
A: 编辑 `.opencode/opencode.json` 和 `.opencode/oh-my-openagent.json` 中的 `model` 字段。推荐使用具备强推理能力的模型。

**Q: 没有 Tushare Token 能用吗？**
A: 可以。Vibe-Trading 内置 AKShare（免费）和 yfinance（免费）数据源。Tushare 仅用于本地 DuckDB 增量同步。

**Q: 如何部署到远程服务器？**
A: 在服务器上 clone 本仓库，安装依赖，用 `opencode serve --port 4096` 启动 Web Server，配合 systemd 管理。

**Q: 钉钉通知如何配置？**
A: 1) 在钉钉群创建自定义机器人，获取 Webhook URL；2) 在 `cron_jobs/registry.json` 的 `notify.dingtalk` 字段填入 URL；3) 消息正文必须以机器人安全关键词开头。

---

## License

MIT
