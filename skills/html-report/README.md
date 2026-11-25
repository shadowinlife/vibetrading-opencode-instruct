# VibeTrading HTML Report

为金融分析报告生成美观的交互式 HTML 页面，支持 ECharts 图表、暗色/亮色主题切换、CJK 字体。

## 功能特性

- **6 种专用模板**: Vibe-Trading 回测、Alpha158 Walk-Forward、七看八问、基本面分析、缠论分析、信号报告
- **ECharts 交互图表**: 蜡烛图、收益曲线、回撤图、雷达图、饼图
- **主题切换**: 右上角按钮切换浅色/深色/跟随系统
- **当前状态头**: 回测报告顶部显示当前信号、建议仓位、判断依据
- **多策略 Tab**: Vibe-Trading 回测支持多策略对比展示
- **RM 直传交付**: STS 临时凭据直传私有 OSS，用户在 RM 报告列表查看/分享（`docs/report-delivery.md`）
- **自包含单文件**: ECharts 内联（vendored Apache-2.0 构建）+ 系统 CJK 字体，不透明源 CSP 下可渲染

## 适用范围

- 量化回测结果可视化（Alpha158、Vibe-Trading、缠论等）
- 基本面分析报告（七看八问、财务报表）
- 交易信号报告（买入/卖出信号 + 价格图表）
- 任何需要美观 HTML 报告的金融分析场景

## 环境依赖

- Python >= 3.10
- Jinja2 >= 3.0
- pandas >= 1.5
- markupsafe >= 2.0
- oss2 >= 2.18 (仅交付上传需要；渲染不需要)
- ECharts 6.1.0 已内联于 `scripts/reports/assets/echarts.min.js`（Apache-2.0），**无需网络/CDN**

## 安装

```bash
pip install jinja2 pandas markupsafe oss2
```

## 快速开始

### 1. 生成回测报告

```python
from scripts.reports.html_renderer import render_vibe_backtest_html

html = render_vibe_backtest_html(
    ts_code="588000.SH",
    name="科创50ETF",
    strategy_name="双均线+动量评分 v5",
    kpis={
        "总收益": {"value": "+41.2%", "change": "+3.8%", "change_positive": True},
        "年化收益": {"value": "+6.7%", "change": "+0.6%", "change_positive": True},
        "最大回撤": {"value": "-25.8%", "change": "+12.1%", "change_positive": True},
        "Sharpe": {"value": "0.60", "change": "+0.15", "change_positive": True},
    },
    equity_curve={
        "dates": ["2020-01", "2021-01", "2022-01", "2023-01", "2024-01", "2025-01"],
        "strategy": [0, 12, 18, 28, 35, 41.2],
        "buyhold": [0, 8, 12, 18, 20, 24.4],
    },
    current_status={
        "signal": "买入",
        "signal_date": "2025-06-26",
        "position": 80,
        "position_reason": "基于 v5 策略评分 >= 3",
        "reason": "MA10 上穿 MA30（金叉），RSI(14) = 58.3 > 50，MACD 柱为正。",
        "key_metrics": {"MA10": "1.120", "MA30": "1.095", "RSI": "58.3"},
    },
    coverage={"start": "2019-12-01", "end": "2025-06-26", "bars": 1350},
)

with open("report.html", "w") as f:
    f.write(html)
```

### 2. 多策略对比

```python
strategies = [
    {
        "name": "v5 双均线+动量",
        "is_best": True,
        "kpis": [{"label": "总收益", "value": "+41.2%", "change": "+3.8%", "change_positive": True}],
        "equity_curve": {"dates": [...], "strategy": [...], "buyhold": [...]},
        "trades": [{"date": "2024-03-15", "direction": "买入", "price": "1.052", "pnl": "+6.5%", "hold_days": "12", "trigger": "金叉"}],
    },
    {
        "name": "v4 双均线",
        "is_best": False,
        "kpis": [{"label": "总收益", "value": "+37.4%"}],
        "equity_curve": {"dates": [...], "strategy": [...], "buyhold": [...]},
    },
]

html = render_vibe_backtest_html(
    ts_code="588000.SH", name="科创50ETF", strategy_name="多策略对比",
    kpis={}, strategies=strategies,
)
```

### 3. 七看八问报告

```python
from scripts.reports.html_renderer import render_seven_look_html

html = render_seven_look_html(
    ts_code="600519.SH",
    name="贵州茅台",
    scores={"profit_quality": 8, "cost_structure": 7, "growth": 9, "business_mix": 6, "balance_sheet": 8, "efficiency": 7, "roe": 9},
    score_details={
        "profit_quality": {"score": 8, "status": "green", "summary": "毛利率稳定在 90%+", "key_metrics": {"毛利率": "91.5%", "净利率": "52.3%"}},
    },
    moat={"brand": 5, "tech": 3, "cost": 4, "scale": 5},
)
```

### 4. 交付给用户（RM STS 直传私有 OSS）

```bash
# 沙箱内交付（读取预置的 agent token 与 RM_AGENT_API_URL）
python scripts/reports/upload_report.py report.html

# 显式指定 RM 地址 / token 文件
python scripts/reports/upload_report.py report.html --rm-url http://rm.internal:8900

# 只做自包含校验与流程预览（零网络调用）
python scripts/reports/upload_report.py report.html --dry-run
```

成功后打印 `report_id`；用户在 RM Web 报告列表查看或签发分享链。
契约细节见 `docs/report-delivery.md`。

### 5. DingTalk 推送

报告交付后由 RM 侧生成查看/分享链接；沙箱不再持有可达 URL。通知中引用
`report_id`，或使用 RM 签发的分享链（勿编造 localhost/nginx 地址）。

```python
from cron_jobs.notifier import send_dingtalk_actioncard

send_dingtalk_actioncard(
    webhook="https://oapi.dingtalk.com/robot/send?access_token=xxx",
    title="588000.SH 回测报告",
    markdown="## 科创50ETF 回测报告\n- 总收益: +41.2%\n- Sharpe: 0.60\n- report_id: rp_xxx",
    single_title="查看完整报告",
    single_url="<RM 签发的分享链，例如 https://<主站>/s/{token}>",
)
```

## 模板说明

| 模板 | 函数 | 适用场景 |
|------|------|---------|
| Vibe-Trading 回测 | `render_vibe_backtest_html()` | 12 KPI + 收益曲线 + 交易表 |
| Alpha158 Walk-Forward | `render_alpha158_backtest_html()` | 多策略对比 + 最优策略高亮 |
| 七看八问 | `render_seven_look_html()` | 雷达图 + 财务趋势 + 同行对比 |
| 基本面分析 | `render_fundamental_html()` | KPI + 饼图 |
| 缠论分析 | `render_chanlun_html()` | K 线 + 分型/笔/中枢标注 |
| 信号报告 | `render_signal_html()` | 信号摘要 + 价格图 |
| Markdown 转换 | `render_from_markdown()` | 现有 .md 报告转 HTML |

## 交付契约

参见 `docs/report-delivery.md`（RM STS 直传私有 OSS + 自包含 HTML 约束）。

## 安全说明

- Jinja2 模板启用 autoescape，防止 XSS（内联 ECharts 以 `Markup` 显式豁免）
- agent token 与 STS 凭据只读一次、仅存内存，任何输出/错误信息不携带凭据
- OSS 错误只回显 status/code/request_id，不回显可能含 AccessKeyId 的原始报文
- 上传前强制自包含校验，拒绝外部脚本/样式/字体引用
- DingTalk API 错误会被检测并抛出异常

## License

Apache 2.0
