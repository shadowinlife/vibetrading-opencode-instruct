# 选美博弈选股策略 (Beauty Contest Screening)

> 基于凯恩斯选美博弈理论的三层股票筛选框架，面向中国 A 股市场。

## 1. 项目概述

### 这是什么

一套完整的 A 股多层选股系统。它不是选"好公司"，而是选"多数人即将选择的公司"。

三层筛选逻辑：

1. **基本面**是入场券。备选者必须足够"美"。
2. **叙事**是催化剂。故事正在被更多人传播。
3. **资金流**是验证。评委团正在用脚投票。

### 理论来源

凯恩斯在 1936 年提出"选美博弈"比喻：报纸选美比赛的获奖者，不是你认为最美的，而是你认为多数人认为最美的。投资同理。

本系统将这一思想工程化为可执行的三层筛选管线，结合叙事经济学（Shiller 2017）、资金流动量（Lou 2012）和概念动量（BigQuant 2023）等学术研究。

### 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    OpenCode Agent                        │
│  读取 SKILL.md 获取方法论，读取 AGENTS.md 获取路由规则     │
└──────────┬──────────────────────────────────┬────────────┘
           │                                  │
     ┌─────▼─────┐                    ┌──────▼──────┐
     │  SKILL.md  │                    │  AGENTS.md   │
     │  方法论层   │                    │  场景路由层   │
     └─────┬─────┘                    └──────┬──────┘
           │                                  │
     ┌─────▼──────────────────────────────────▼─────┐
     │              scripts/screening/               │
     │  Layer 1 → Layer 2 → Layer 3 → Composite     │
     │  (基本面)   (叙事动量)  (资金流)   (综合排名)   │
     └──────────────────┬───────────────────────────┘
                        │
     ┌──────────────────▼───────────────────────────┐
     │              html-report/                     │
     │  Jinja2 + ECharts 交互式报告                   │
     │  漏斗图 + 雷达图 + 分层表格 + 消融表            │
     └──────────────────────────────────────────────┘
```

## 2. 系统架构

### 三层筛选流程

```
全 A 股 ~6000 只
       │
       ▼
┌──────────────────────────────────┐
│  Layer 1: 基本面筛选              │
│  ROE>=8%, 营收>0%, 净利>-20%     │
│  OCF>0, 非ST, 市值>50亿          │
│  数据源: fin_indicator +          │
│         stk_factor_pro +         │
│         stk_st_daily             │
└──────────┬───────────────────────┘
           │ ~800 只通过
           ▼
┌──────────────────────────────────┐
│  Layer 2: 叙事动量筛选            │
│  申万L2行业涨幅Top10 +           │
│  20日/60日成交额比>1.2 +         │
│  20日/60日换手率比>1.1           │
│  数据源: idx_sw_member_all +     │
│         stk_factor_pro           │
└──────────┬───────────────────────┘
           │ ~200 只通过
           ▼
┌──────────────────────────────────┐
│  Layer 3: 资金流共振              │
│  主力净流入>0 + 5日方向>0 +      │
│  融资余额增长>0                  │
│  数据源: stk_moneyflow +         │
│         stk_moneyflow_ths +      │
│         stk_margin               │
└──────────┬───────────────────────┘
           │ ~100 只通过
           ▼
┌──────────────────────────────────┐
│  Composite: Z-score 综合排名     │
│  权重: L1=0.4, L2=0.3, L3=0.3  │
│  分级: Tier1/2/3                │
└──────────┬───────────────────────┘
           │
           ▼
     Top N 输出 + HTML 报告
```

### 数据流

```
DuckDB (ashare.duckdb)
    │
    ├── fin_indicator ──────┐
    ├── stk_factor_pro ─────┤
    ├── stk_info ───────────┤
    ├── stk_st_daily ───────┤
    ├── idx_sw_member_all ──┼──→ Layer 1/2/3 SQL ──→ pandas DataFrame
    ├── stk_moneyflow ──────┤                              │
    ├── stk_moneyflow_ths ──┤                              ▼
    └── stk_margin ─────────┘                    composite_screen()
                                                         │
                                                         ▼
                                              JSON / CLI / HTML
```

### 文件结构

```
beauty-contest-screening/
├── README.md                              # 本文件
├── requirements.txt                       # Python 依赖
├── scripts/
│   └── screening/
│       ├── __init__.py                    # 模块导出
│       ├── sql_templates.py               # 参数化 DuckDB SQL 模板
│       ├── layer1_fundamental.py          # Layer 1: 基本面筛选
│       ├── layer2_narrative.py            # Layer 2: 叙事动量筛选
│       ├── layer3_flow.py                 # Layer 3: 资金流筛选
│       ├── composite.py                   # Z-score 综合排名 + 分级
│       ├── ablation.py                    # 顺序消融分析
│       └── cli.py                         # CLI 入口
├── skills/
│   └── beauty-contest-screening/
│       └── SKILL.md                       # OpenCode SKILL 方法论文档
├── html-report/
│   ├── templates/
│   │   ├── base.html                      # 基础 HTML 模板（暗/亮主题，CJK 字体）
│   │   └── screening.html                 # 选股报告 Jinja2 模板
│   └── render_screening_html.py           # 独立 HTML 渲染器
└── agents-patches/
    ├── AGENTS.md.append.md                # 追加到目标 AGENTS.md 的内容
    └── scripts-AGENTS.md.append.md        # 追加到目标 scripts/AGENTS.md 的内容
```

### DuckDB 表依赖

| 表名 | 用途 | 关键字段 |
|------|------|---------|
| `fin_indicator` | 财务指标 | `ts_code`, `end_date`, `roe_waa`, `roe`, `or_yoy`, `netprofit_yoy`, `ocfps`, `grossprofit_margin`, `debt_to_assets` |
| `stk_factor_pro` | 日频量价因子 | `ts_code`, `trade_date`, `pe_ttm`, `pb`, `ps_ttm`, `dv_ttm`, `total_mv`, `turnover_rate`, `pct_chg`, `amount` |
| `stk_info` | 股票基本信息 | `ts_code`, `name`, `industry` |
| `stk_st_daily` | ST 状态 | `ts_code`, `trade_date` |
| `stk_moneyflow` | 资金流向 | `ts_code`, `trade_date`, `net_mf_amount`, `buy_elg_amount`, `sell_elg_amount`, `buy_lg_amount`, `sell_lg_amount` |
| `stk_moneyflow_ths` | 同花顺资金流 | `ts_code`, `trade_date`, `net_d5_amount`, `net_amount`, `buy_lg_amount_rate` |
| `stk_margin` | 融资融券 | `ts_code`, `trade_date`, `rzye`, `rzrqye`, `rzmre` |
| `idx_sw_member_all` | 申万行业成员 | `ts_code`, `l2_name` |

## 3. 前置条件

### 必需

| 条件 | 说明 |
|------|------|
| Python >= 3.10 | 推荐通过 conda 管理，环境名 `nanobot` |
| DuckDB 数据库 | 路径: `duckdb/ashare.duckdb`，包含上述 8 张表 |
| pip 依赖 | `duckdb>=1.0`, `pandas>=2.0`, `numpy>=1.24`, `jinja2>=3.0`, `markupsafe>=2.0` |

### 可选

| 条件 | 说明 |
|------|------|
| OpenCode 环境 | 需要 `.opencode/skills/` 目录来安装 SKILL.md |
| nginx | 用于 HTML 报告的 Web 访问 |
| 钉钉 Webhook | 用于选股结果通知推送 |
| html-report Skill | 已部署的 HTML 报告渲染基础设施 |

### 验证前置条件

```bash
# 检查 Python 版本
python --version  # 应 >= 3.10

# 检查 conda 环境
conda activate nanobot

# 检查 DuckDB 数据库存在
ls -lh duckdb/ashare.duckdb

# 检查 DuckDB 表是否齐全
python -c "
import duckdb
db = duckdb.connect('duckdb/ashare.duckdb', read_only=True)
tables = db.execute('SHOW TABLES').fetchall()
required = ['fin_indicator', 'stk_factor_pro', 'stk_info', 'stk_st_daily',
            'stk_moneyflow', 'stk_moneyflow_ths', 'stk_margin', 'idx_sw_member_all']
for t in required:
    status = 'OK' if (t,) in tables else 'MISSING'
    print(f'  {t}: {status}')
db.close()
"
```

## 4. 快速部署（5 分钟）

假设你的项目根目录为 `$PROJECT`（例如 `/opt/qdata`），本项目的源码在 `$SOURCE`（例如 `/opt/qdata/projects/beauty-contest-screening`）。

### Step 1: 安装 Python 依赖

```bash
conda activate nanobot
pip install -r $SOURCE/requirements.txt
```

### Step 2: 复制筛选脚本

```bash
mkdir -p $PROJECT/scripts/screening
cp $SOURCE/scripts/screening/*.py $PROJECT/scripts/screening/
```

### Step 3: 安装 SKILL.md

```bash
mkdir -p $PROJECT/.opencode/skills/beauty-contest-screening
cp $SOURCE/skills/beauty-contest-screening/SKILL.md \
   $PROJECT/.opencode/skills/beauty-contest-screening/SKILL.md
```

### Step 4: 安装 HTML 模板和渲染器

```bash
# 模板文件
mkdir -p $PROJECT/.opencode/skills/html-report/scripts/reports/templates
cp $SOURCE/html-report/templates/base.html \
   $PROJECT/.opencode/skills/html-report/scripts/reports/templates/
cp $SOURCE/html-report/templates/screening.html \
   $PROJECT/.opencode/skills/html-report/scripts/reports/templates/

# 渲染器
cp $SOURCE/html-report/render_screening_html.py \
   $PROJECT/.opencode/skills/html-report/scripts/reports/
```

### Step 5: 验证部署

```bash
# 验证模块可导入
python -c "from scripts.screening import screen_fundamental; print('OK')"

# 运行一次 Layer 1 筛选
python -m scripts.screening.cli --strategy beauty-contest --top-n 5
```

预期输出类似：

```
=== 选美博弈选股策略 ===
策略: beauty-contest
Top N: 5
条件: ROE>=8%, 营收YoY>0%, 净利YoY>-20%

--- Layer 1: 基本面筛选 ---
  通过: 878 只
  ROE 中位数: 15.23%
  ...
```

## 5. 手动部署（逐步）

适合需要精细控制部署过程的场景。每一步都包含验证方法。

### 5.1 复制筛选脚本

```bash
mkdir -p $PROJECT/scripts/screening
cp $SOURCE/scripts/screening/__init__.py           $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/sql_templates.py       $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/layer1_fundamental.py  $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/layer2_narrative.py    $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/layer3_flow.py         $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/composite.py           $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/ablation.py            $PROJECT/scripts/screening/
cp $SOURCE/scripts/screening/cli.py                 $PROJECT/scripts/screening/
```

**验证**: `ls $PROJECT/scripts/screening/` 应显示 8 个 `.py` 文件。

### 5.2 安装 SKILL.md

```bash
mkdir -p $PROJECT/.opencode/skills/beauty-contest-screening
cp $SOURCE/skills/beauty-contest-screening/SKILL.md \
   $PROJECT/.opencode/skills/beauty-contest-screening/SKILL.md
```

**验证**: `cat $PROJECT/.opencode/skills/beauty-contest-screening/SKILL.md | head -5` 应显示 YAML frontmatter。

### 5.3 复制 HTML 模板

```bash
mkdir -p $PROJECT/.opencode/skills/html-report/scripts/reports/templates
cp $SOURCE/html-report/templates/base.html \
   $PROJECT/.opencode/skills/html-report/scripts/reports/templates/
cp $SOURCE/html-report/templates/screening.html \
   $PROJECT/.opencode/skills/html-report/scripts/reports/templates/
```

**验证**: `ls $PROJECT/.opencode/skills/html-report/scripts/reports/templates/` 应包含 `base.html` 和 `screening.html`。

### 5.4 复制 HTML 渲染器

```bash
cp $SOURCE/html-report/render_screening_html.py \
   $PROJECT/.opencode/skills/html-report/scripts/reports/
```

**验证**:

```bash
python -c "
import sys; sys.path.insert(0, '$PROJECT/.opencode/skills/html-report/scripts/reports')
from render_screening_html import render_screening_html
print('render_screening_html 可导入')
"
```

### 5.5 追加 AGENTS.md 补丁

如果 `agents-patches/` 目录中有补丁文件：

```bash
# 追加到项目根 AGENTS.md
if [ -f $SOURCE/agents-patches/AGENTS.md.append.md ]; then
    cat $SOURCE/agents-patches/AGENTS.md.append.md >> $PROJECT/AGENTS.md
fi

# 追加到 scripts/AGENTS.md
if [ -f $SOURCE/agents-patches/scripts-AGENTS.md.append.md ]; then
    cat $SOURCE/agents-patches/scripts-AGENTS.md.append.md >> $PROJECT/scripts/AGENTS.md
fi
```

补丁内容包含：
- 组合能力表中的选股策略行
- 场景 E 路由规则
- 能力索引中的选股策略条目
- HTML 模板表中的选股策略模板行

详见第 12 节"AGENTS.md 集成"。

### 5.6 更新 analysis/_index.json

确保 `analysis/_index.json` 存在。如果不存在，创建空索引：

```bash
mkdir -p $PROJECT/analysis
if [ ! -f $PROJECT/analysis/_index.json ]; then
    echo '{"reports": []}' > $PROJECT/analysis/_index.json
fi
```

## 6. 使用方式

### 6.1 CLI 使用

```bash
# 激活环境
conda activate nanobot

# 默认选美博弈策略
python -m scripts.screening.cli

# 指定策略类型
python -m scripts.screening.cli --strategy value
python -m scripts.screening.cli --strategy quality
python -m scripts.screening.cli --strategy momentum

# 指定日期和数量
python -m scripts.screening.cli --trade-date 2026-06-26 --top-n 10

# 带消融分析
python -m scripts.screening.cli --strategy beauty-contest --ablation

# 输出 JSON 文件
python -m scripts.screening.cli --output analysis/screening_result.json

# 完整参数组合
python -m scripts.screening.cli \
    --strategy beauty-contest \
    --trade-date 2026-06-26 \
    --end-date 2025-12-31 \
    --top-n 20 \
    --ablation \
    --output analysis/screening_20260626.json
```

CLI 参数说明：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--strategy` | `beauty-contest` | 策略类型: `beauty-contest`, `value`, `quality`, `momentum` |
| `--trade-date` | 内置默认 | 交易日期 (YYYY-MM-DD) |
| `--end-date` | 内置默认 | 财报截止日期 (YYYY-MM-DD) |
| `--top-n` | 20 | 输出 Top N 只股票 |
| `--ablation` | 关闭 | 启用消融分析 |
| `--output` | 无 | JSON 输出路径 |

### 6.2 Python API 使用

```python
# 单独调用某一层
from scripts.screening.layer1_fundamental import screen_fundamental
df = screen_fundamental(roe_min=15, trade_date="2026-06-26")
print(f"Layer 1 通过: {len(df)} 只")

# 叙事动量筛选
from scripts.screening.layer2_narrative import screen_narrative
result = screen_narrative(trade_date="2026-06-26")
print(f"板块动量: {result['sector_summary']}")
print(f"量能动量: {result['volume_summary']}")

# 资金流筛选
from scripts.screening.layer3_flow import screen_flow
result = screen_flow(trade_date="2026-06-26")
print(f"资金流通过: {result['flow_summary']['count']} 只")

# 综合排名
from scripts.screening.composite import composite_screen
l1 = screen_fundamental()
ranking = composite_screen(l1, top_n=20)
print(f"Tier 1: {ranking['summary']['tier1_count']}")
print(ranking['top_n_df'][['ts_code', 'name', 'composite_score']])

# 消融分析
from scripts.screening.ablation import run_ablation
abl = run_ablation(l1, top_n=20)
for row in abl['ablation_table']:
    print(f"{row['label']}: {row['stock_count']} 只, Avg ROE={row['avg_roe']}%")
```

### 6.3 HTML 报告生成

```python
from render_screening_html import render_screening_html

html = render_screening_html(
    strategy_name="选美博弈",
    date="2026-06-28",
    funnel={
        "universe": 5000,
        "layer1": 878,
        "layer2": 244,
        "layer3": 813,
        "final": 20,
    },
    tier1=[
        {"ts_code": "300502.SZ", "name": "新易盛", "industry": "通信",
         "roe": 43.3, "revenue_yoy": 128.5, "pe": 25.6, "composite_score": 1.85},
    ],
    tier2=[...],
    tier3=[...],
    ablation=[
        {"label": "A: Layer 1 only", "stock_count": 20,
         "avg_roe": 43.30, "avg_score": 0.85, "delta_score": None},
        {"label": "B: Layer 1 + Layer 2", "stock_count": 20,
         "avg_roe": 44.10, "avg_score": 0.92, "delta_score": 0.07},
        {"label": "C: Layer 1+2+3", "stock_count": 20,
         "avg_roe": 45.20, "avg_score": 1.05, "delta_score": 0.13},
    ],
    risks=["市场流动性收紧风险", "部分行业估值过热"],
    position_advice="建议总仓位 60%，Tier1 配置 40%，Tier2 配置 20%",
)

with open("analysis/screening_report.html", "w", encoding="utf-8") as f:
    f.write(html)
```

### 6.4 钉钉通知集成

选股结果可通过钉钉 Webhook 推送。在 OpenCode agent 中，使用内置的钉钉通知能力。手动推送示例：

```python
import json
import urllib.request

webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
payload = {
    "msgtype": "markdown",
    "markdown": {
        "title": "选美博弈选股结果 2026-06-28",
        "text": "### 选美博弈选股结果\n\n"
                "**Tier 1 (强共振)**: 5 只\n"
                "**Tier 2 (中共振)**: 12 只\n\n"
                "Top 3: 新易盛, 中际旭创, 天孚通信\n\n"
                "详见 HTML 报告: http://your-server/reports/screening/report.html"
    }
}
req = urllib.request.Request(
    webhook_url,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(req)
```

## 7. DuckDB 数据要求

### 7.1 fin_indicator（财务指标）

来源: Tushare `fin_indicator` 接口。

| 字段 | 类型 | 说明 | 用于 |
|------|------|------|------|
| `ts_code` | VARCHAR | 股票代码 | JOIN |
| `end_date` | VARCHAR | 报告期截止日 | 筛选最新年报 |
| `roe_waa` | DOUBLE | 加权平均 ROE (%) | Layer 1: ROE >= 8% |
| `roe` | DOUBLE | 摊薄 ROE (%) | 参考 |
| `or_yoy` | DOUBLE | 营收同比增长率 (%) | Layer 1: 营收 > 0% |
| `netprofit_yoy` | DOUBLE | 净利润同比增长率 (%) | Layer 1: 净利 > -20% |
| `ocfps` | DOUBLE | 每股经营现金流 | Layer 1: OCF > 0 |
| `grossprofit_margin` | DOUBLE | 毛利率 (%) | 参考指标 |
| `debt_to_assets` | DOUBLE | 资产负债率 (%) | 参考指标 |

### 7.2 stk_factor_pro（日频量价因子）

来源: Tushare `stk_factor_pro` 接口。

| 字段 | 类型 | 说明 | 用于 |
|------|------|------|------|
| `ts_code` | VARCHAR | 股票代码 | JOIN |
| `trade_date` | VARCHAR | 交易日期 | 日期筛选 |
| `pe_ttm` | DOUBLE | 滚动市盈率 | 估值参考 |
| `pb` | DOUBLE | 市净率 | 估值参考 |
| `ps_ttm` | DOUBLE | 滚动市销率 | 估值参考 |
| `dv_ttm` | DOUBLE | 滚动股息率 | 分红参考 |
| `total_mv` | DOUBLE | 总市值 (万元) | Layer 1: 市值 > 50亿 |
| `turnover_rate` | DOUBLE | 换手率 (%) | Layer 2: 换手率动量 |
| `pct_chg` | DOUBLE | 涨跌幅 (%) | Layer 2: 行业涨幅 |
| `amount` | DOUBLE | 成交额 (元) | Layer 2: 成交额动量 |

### 7.3 stk_info（股票基本信息）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts_code` | VARCHAR | 股票代码 |
| `name` | VARCHAR | 股票名称 |
| `industry` | VARCHAR | 所属行业 |

### 7.4 stk_st_daily（ST 状态）

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts_code` | VARCHAR | 股票代码 |
| `trade_date` | VARCHAR | 交易日期 |

存在记录即为 ST 股票，用 `LEFT JOIN ... WHERE st.ts_code IS NULL` 排除。

### 7.5 stk_moneyflow（资金流向）

| 字段 | 类型 | 说明 | 用于 |
|------|------|------|------|
| `ts_code` | VARCHAR | 股票代码 | JOIN |
| `trade_date` | VARCHAR | 交易日期 | 日期筛选 |
| `net_mf_amount` | DOUBLE | 主力净流入 (元) | Layer 3: 主力 > 0 |
| `buy_elg_amount` | DOUBLE | 超大单买入 (元) | 计算净超大单 |
| `sell_elg_amount` | DOUBLE | 超大单卖出 (元) | 计算净超大单 |
| `buy_lg_amount` | DOUBLE | 大单买入 (元) | 计算净大单 |
| `sell_lg_amount` | DOUBLE | 大单卖出 (元) | 计算净大单 |

### 7.6 stk_moneyflow_ths（同花顺资金流）

| 字段 | 类型 | 说明 | 用于 |
|------|------|------|------|
| `ts_code` | VARCHAR | 股票代码 | JOIN |
| `trade_date` | VARCHAR | 交易日期 | 日期筛选 |
| `net_d5_amount` | DOUBLE | 5 日主力净流入 (元) | Layer 3: 5日方向 > 0 |
| `net_amount` | DOUBLE | 当日净流入 (元) | 参考 |
| `buy_lg_amount_rate` | DOUBLE | 大单买入占比 (%) | 参考 |

### 7.7 stk_margin（融资融券）

| 字段 | 类型 | 说明 | 用于 |
|------|------|------|------|
| `ts_code` | VARCHAR | 股票代码 | JOIN |
| `trade_date` | VARCHAR | 交易日期 | 日期筛选 |
| `rzye` | DOUBLE | 融资余额 (元) | Layer 3: 融资余额增长 |
| `rzrqye` | DOUBLE | 融资融券余额 (元) | 参考 |
| `rzmre` | DOUBLE | 融资买入额 (元) | 参考 |

### 7.8 idx_sw_member_all（申万行业成员）

| 字段 | 类型 | 说明 | 用于 |
|------|------|------|------|
| `ts_code` | VARCHAR | 股票代码 | JOIN |
| `l2_name` | VARCHAR | 申万 L2 行业名称 | Layer 2: 行业排名 |

## 8. 筛选条件标准

### Layer 1: 基本面筛选

| 条件 | 默认阈值 | 字段 | 可自定义 | 理由 |
|------|---------|------|---------|------|
| ROE (加权平均) | >= 8% | `fin_indicator.roe_waa` | `roe_min` 参数 | 盈利能力底线 |
| 营收同比增长 | > 0% | `fin_indicator.or_yoy` | `revenue_yoy_min` 参数 | 成长性验证 |
| 净利润同比增长 | > -20% | `fin_indicator.netprofit_yoy` | `netprofit_yoy_min` 参数 | 排除业绩恶化 |
| 每股经营现金流 | > 0 | `fin_indicator.ocfps` | `ocfps_min` 参数 | 盈利质量验证 |
| 非 ST | 排除 | `stk_st_daily` | 不可关闭 | 规避退市风险 |
| 总市值 | > 50 亿 | `stk_factor_pro.total_mv` | `mv_min` 参数 (万元) | 排除微盘股 |

### Layer 2: 叙事动量筛选

| 条件 | 默认阈值 | 数据源 | 说明 |
|------|---------|--------|------|
| 行业涨幅排名 | Top 10 行业 | `idx_sw_member_all` + `stk_factor_pro` | 申万 L2 行业近 20 日均涨幅 |
| 成交额比 (20d/60d) | > 1.2 | `stk_factor_pro.amount` | 近期成交活跃度 |
| 换手率比 (20d/60d) | > 1.1 | `stk_factor_pro.turnover_rate` | 换手率上升趋势 |

### Layer 3: 资金流共振

| 条件 | 默认阈值 | 数据源 | 说明 |
|------|---------|--------|------|
| 主力净流入 | > 0 | `stk_moneyflow.net_mf_amount` | 当日大单+超大单净买入 |
| 5 日主力方向 | > 0 | `stk_moneyflow_ths.net_d5_amount` | 5 日主力净流入为正 |
| 融资余额增长 | > 0 | `stk_margin.rzye` | 融资余额较 5 日前增长 |

## 9. 策略预设

系统内置 4 种策略预设，每种策略的 Layer 1 参数不同：

| 策略 | 名称 | ROE 下限 | 营收增长下限 | 净利增长下限 | 适用场景 |
|------|------|---------|------------|------------|---------|
| `beauty-contest` | 选美博弈 | >= 8% | > 0% | > -20% | 趋势行情，平衡基本面与市场情绪 |
| `value` | 价值选股 | >= 10% | > 0% | > 0% | 价值回归，更严格的盈利要求 |
| `quality` | 质量选股 | >= 15% | > 5% | > 5% | 稳健配置，高质量公司筛选 |
| `momentum` | 动量选股 | >= 8% | > 10% | > 10% | 趋势跟踪，侧重高增长标的 |

Layer 2 和 Layer 3 的筛选条件在所有策略中保持一致。差异仅体现在 Layer 1 的基本面门槛上。

策略选择建议：

- 牛市或结构性行情：`beauty-contest` 或 `momentum`
- 震荡市或防御阶段：`quality` 或 `value`
- 不确定时：`beauty-contest`（默认，最平衡）

## 10. 输出格式

### 10.1 CLI 输出

```
=== 选美博弈选股策略 ===
策略: beauty-contest
Top N: 20
条件: ROE>=8%, 营收YoY>0%, 净利YoY>-20%

--- Layer 1: 基本面筛选 ---
  通过: 878 只
  ROE 中位数: 15.23%
  营收增长中位数: 12.45%

--- Layer 2: 叙事动量筛选 ---
  板块动量: 244 只, 10 个行业
  量能动量: 312 只

--- Layer 3: 资金流筛选 ---
  通过: 813 只

--- 综合排名 ---
  Tier 1 (强共振): 5
  Tier 2 (中共振): 12
  Tier 3 (观察): 3

Top 20:
   ts_code    name industry  roe_waa  revenue_yoy  composite_score  layers_passed
300502.SZ   新易盛     通信    43.30       128.50           1.8523              3
...
```

### 10.2 JSON 输出

使用 `--output` 参数生成。结构如下：

```json
{
  "strategy": "beauty-contest",
  "date": "2026-06-26",
  "summary": {
    "total_universe": 878,
    "tier1_count": 5,
    "tier2_count": 12,
    "tier3_count": 3,
    "top_n": 20
  },
  "top_n": [
    {
      "ts_code": "300502.SZ",
      "name": "新易盛",
      "industry": "通信",
      "roe_waa": 43.30,
      "revenue_yoy": 128.50,
      "composite_score": 1.8523,
      "layers_passed": 3
    }
  ],
  "tier1": [...]
}
```

### 10.3 HTML 报告

HTML 报告包含以下区块：

| 区块 | 内容 |
|------|------|
| KPI 头部 | Universe 数量、各层通过数量、最终入选数量 |
| 漏斗图 (ECharts) | 从全市场到最终入选的筛选漏斗 |
| Tier 1 表格 | 强共振股票详情（代码、名称、行业、ROE、增长、PE、综合评分） |
| Tier 2 表格 | 中共振股票详情 |
| Tier 3 表格 | 观察池股票详情 |
| 消融分析表 | 各配置的增量贡献对比 |
| 雷达图 (ECharts) | Top 5 股票的多维度评分对比 |
| 风险提示 | 核心风险预警 |
| 仓位建议 | 配置建议文本 |

支持暗色/亮色主题自动切换，CJK 字体优化。

## 11. 消融分析

### 原理

消融分析（Ablation Study）通过逐步添加筛选层，衡量每一层的增量贡献。

三个配置：

| 配置 | 包含的层 | 含义 |
|------|---------|------|
| A: Layer 1 only | 仅基本面 | 基线，纯基本面排名 |
| B: Layer 1 + Layer 2 | 基本面 + 叙事 | 叙事动量的增量贡献 |
| C: Layer 1+2+3 | 全部三层 | 资金流的增量贡献 |

### 指标

每个配置计算以下指标：

| 指标 | 说明 |
|------|------|
| `stock_count` | Top N 中的股票数量 |
| `avg_roe` | Top N 平均 ROE |
| `avg_revenue_yoy` | Top N 平均营收增长率 |
| `avg_composite_score` | Top N 平均综合评分 |

### 增量贡献

相邻配置之间的指标差值即为增量贡献：

```
delta_score(B vs A) = avg_score(B) - avg_score(A)
delta_score(C vs B) = avg_score(C) - avg_score(B)
```

### 如何解读

- `delta_score > 0`：该层提升了选股质量
- `delta_score < 0`：该层可能引入了噪声，需要审视
- `delta_roe > 0`：该层帮助选出了更高 ROE 的公司
- 如果 Layer 2 的增量接近零，说明叙事动量信号在当前市场环境下较弱

### 使用方式

```bash
python -m scripts.screening.cli --strategy beauty-contest --ablation
```

输出示例：

```
--- 消融分析 ---

配置                       股票数   Avg ROE   Avg Score
-------------------------------------------------------
A: Layer 1 only               20   43.30%     0.8523
B: Layer 1 + Layer 2          20   44.10%     0.9215
C: Layer 1+2+3                20   45.20%     1.0534
```

## 12. AGENTS.md 集成

部署到 OpenCode 项目时，需要在目标项目的 AGENTS.md 中添加以下内容。

### 12.1 组合能力表

在"组合能力（多 Skill 工作流）"表格中添加一行：

```markdown
| 场景 E: 选股策略 | scripts/screening + vibe-trading factor-research → analysis-report → html-report | 筛选 → 验证 → 报告 |
```

### 12.2 场景 E 路由规则

在"客户引导流程"部分添加完整的场景 E 章节。核心流程：

1. 明确选股范围和策略
2. 叙事识别（并行调用板块排名、北向资金、成交额排名）
3. 候选池生成（从叙事主线提取 20-30 只）
4. 三层筛选（Layer 1/2/3）
5. 量化验证（可选，IC/IR 分析 + 回测）
6. 报告输出（analysis-report + html-report）
7. 后续引导（个股分析、回测、周期执行）

### 12.3 能力索引

在能力索引表中添加：

```markdown
| **选股策略** | **`beauty-contest-screening` + 场景 E** | **选股、筛选、选美、资金流选股、多因子选股** |
```

### 12.4 HTML 模板表

在 HTML 报告展示能力的模板表中确认：

```markdown
| 选股策略 | `render_screening_html()` | 漏斗图 + 分层表格 + 消融表 + 雷达图 |
```

### 12.5 scripts/AGENTS.md

在 `scripts/AGENTS.md` 中添加模块说明：

```markdown
- `beauty-contest-screening`: 选美博弈三层筛选策略。
```

## 13. 故障排除

### DuckDB 连接错误

**问题**: `PermissionError: [Errno 13] Permission denied`

**原因**: DuckDB 文件权限不足，或有其他进程正在写入。

**解决**:
```bash
chmod 644 duckdb/ashare.duckdb
# 确保没有同步脚本正在运行
ps aux | grep sync
```

### 找不到表

**问题**: `Catalog Error: Table with name xxx does not exist`

**原因**: DuckDB 数据库中缺少所需的表。

**解决**:
```bash
python -c "
import duckdb
db = duckdb.connect('duckdb/ashare.duckdb', read_only=True)
print([t[0] for t in db.execute('SHOW TABLES').fetchall()])
db.close()
"
```

如果缺少表，运行增量同步：
```bash
./sync/run_incremental_sync.sh --dry-run
./sync/run_incremental_sync.sh
```

### 日期格式错误

**问题**: CLI 报 `日期格式无效`

**原因**: 日期参数必须是 `YYYY-MM-DD` 格式，且为字符串。

**解决**:
```bash
# 正确
python -m scripts.screening.cli --trade-date 2026-06-26

# 错误 (缺少前导零)
python -m scripts.screening.cli --trade-date 2026-6-26
```

### 空结果

**问题**: Layer 1 返回 0 只股票。

**可能原因**:

1. **日期不对**: `trade_date` 不是交易日，或 `end_date` 没有对应的财报数据。
2. **条件过严**: 尝试降低阈值。
3. **数据缺失**: 检查 `fin_indicator` 表中是否有对应 `end_date` 的记录。

**排查**:
```bash
# 检查 fin_indicator 中有哪些 end_date
python -c "
import duckdb
db = duckdb.connect('duckdb/ashare.duckdb', read_only=True)
print(db.execute('SELECT DISTINCT end_date FROM fin_indicator ORDER BY end_date DESC LIMIT 5').fetchall())
db.close()
"

# 放宽条件测试
python -c "
from scripts.screening.layer1_fundamental import screen_fundamental
df = screen_fundamental(roe_min=5, revenue_yoy_min=-10, netprofit_yoy_min=-50)
print(f'放宽后: {len(df)} 只')
"
```

### Layer 2 或 Layer 3 跳过

**问题**: CLI 输出 `Layer 2 跳过: ...` 或 `Layer 3 跳过: ...`

**原因**: 对应的表数据不足或查询出错。Layer 2/3 设计为可选层，跳过不影响整体流程。

**解决**: 检查对应表是否有当日数据：
```bash
python -c "
import duckdb
db = duckdb.connect('duckdb/ashare.duckdb', read_only=True)
print('idx_sw_member_all:', db.execute('SELECT COUNT(*) FROM idx_sw_member_all').fetchone()[0])
print('stk_moneyflow:', db.execute('SELECT MAX(trade_date) FROM stk_moneyflow').fetchone()[0])
print('stk_margin:', db.execute('SELECT MAX(trade_date) FROM stk_margin').fetchone()[0])
db.close()
"
```

### HTML 渲染失败

**问题**: `jinja2.exceptions.TemplateNotFound`

**原因**: 模板文件路径不对。

**解决**: 确保 `base.html` 和 `screening.html` 在 `templates/` 目录下，且 `render_screening_html.py` 与 `templates/` 在同一级目录。

## 14. 学术参考

| 研究 | 作者/年份 | 核心发现 | 对本策略的启示 |
|------|----------|---------|-------------|
| 叙事经济学 | Shiller (2017) | 叙事像病毒传播，驱动资产价格偏离基本面 | Layer 2 选叙事"正在加速"而非"已经最热"的 |
| 资金流动量 | Lou (2012) Review of Financial Studies | 资金流驱动的动量持续 3-12 个月 | Layer 3 跟随资金流方向 |
| 高流入股长期表现 | Frazzini & Lamont (2008) | 高流入股长期跑输 4-10%/年 | Layer 1 基本面筛选是防"击鼓传花"的安全网 |
| 叙事注意力定价 | AFA 2025 | 趋势叙事 vs 衰退叙事产生 7% 年价差 | 叙事动量是独立于传统因子的 alpha 来源 |
| A 股概念动量 | BigQuant (2023) | 概念动量 1.35%/月，独立于 FF5 因子 | A 股概念轮动策略有统计显著性 |
| 凯恩斯选美博弈 | Keynes (1936) General Theory | 投资决策受"多数人预期"驱动 | 三层架构的理论根基 |

## 15. 版本历史

### v2 (2026-06-29)

- 修复全部 14 项代码审查问题
- SQL 模板参数化，消除硬编码日期
- Layer 2 新增量能动量筛选（20d/60d 成交额比 + 换手率比）
- Layer 3 新增融资余额增长信号
- Composite 改用 Z-score 标准化 + 加权合成
- 新增消融分析模块
- CLI 支持 4 种策略预设
- HTML 模板支持暗色主题和 CJK 字体
- 独立 `render_screening_html.py` 渲染器

### v1 (2026-06-28)

- 初始实现
- Layer 1 基本面筛选
- Layer 2 板块动量筛选
- Layer 3 主力资金流筛选
- 基础综合排名
- SKILL.md 方法论文档
