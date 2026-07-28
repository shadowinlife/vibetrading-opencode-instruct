"""新浪财经违规处理（监管处罚）抓取工具（vGP_GetOutOfLine）

数据源页面 URL（已实地验证）：
  https://vip.stock.finance.sina.com.cn/corp/go.php/vGP_GetOutOfLine/stockid/{stockid}.phtml

HTML 结构（2026-04 实地抓取确认）：
  主表：<table id="collectFund_1">
  每条记录：
    <thead><tr><th colspan="2"><a name="YYYY-MM-DD-N"></a>TYPE  公告日期:YYYY-MM-DD</th></tr></thead>
    <tr><td><strong>标题</strong></td><td>TITLE</td></tr>
    <tr><td><strong>批复原因</strong></td><td>REASON</td></tr>
    <tr><td><strong>批复内容</strong></td><td>CONTENT</td></tr>
    <tr><td><strong>处理人</strong></td><td>ISSUER</td></tr>
  分隔行：<tr><td colspan="2"></td></tr>

  source_url 为主页 URL（无逐条详情页链接）
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from nano_search_mcp.config import get_settings

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ── 域名常量（用于 SSRF 防护） ─────────────────────────────
_ALLOWED_BASE = "https://vip.stock.finance.sina.com.cn"
_PAGE_PATH = "/corp/go.php/vGP_GetOutOfLine/stockid/{stockid}.phtml"

# ── HTTP 请求配置 ─────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REFERER = "https://finance.sina.com.cn/"

# ── 输入校验 ─────────────────────────────────────────────
_STOCKID_RE = re.compile(r"^\d{6}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_last_request_time: float = 0.0


# ─────────────────────────────────────────────────────────
# 输入校验
# ─────────────────────────────────────────────────────────

def _validate_stockid(stockid: str) -> str:
    """校验 stockid 为 6 位数字，防止 URL 注入 / SSRF。"""
    if not isinstance(stockid, str) or not _STOCKID_RE.fullmatch(stockid):
        raise ValueError(f"stockid 必须是 6 位纯数字字符串，收到: {stockid!r}")
    return stockid


def _strip_market_suffix(ts_code: str) -> str:
    """将 Tushare 格式 ts_code（如 688270.SH）去掉市场后缀，返回 6 位股票代码。"""
    code = ts_code.split(".")[0]
    return _validate_stockid(code)


def _validate_date(d: str, field: str) -> str:
    if not _DATE_RE.fullmatch(d):
        raise ValueError(f"{field} 格式应为 YYYY-MM-DD，收到: {d!r}")
    return d


# ─────────────────────────────────────────────────────────
# HTTP 请求层
# ─────────────────────────────────────────────────────────

def _throttle() -> None:
    """确保相邻请求之间至少间隔 `http.request_interval` 秒。"""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    interval = get_settings().http.request_interval
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_request_time = time.monotonic()


def _http_get_gbk(url: str, timeout: int = 15) -> str:
    """抓取 GBK 编码页面，内置指数退避重试（支持配置 `http.max_retries`）。

    仅允许 vip.stock.finance.sina.com.cn 域名，防止 SSRF。
    """
    import urllib.error
    import urllib.request

    if not url.startswith(_ALLOWED_BASE):
        raise ValueError(f"禁止访问非新浪财经违规处理页域名: {url!r}")

    cfg = get_settings()
    last_error: Exception | None = None
    for attempt in range(cfg.http.max_retries):
        if attempt > 0:
            backoff = cfg.http.backoff_base ** attempt + random.uniform(0.2, 0.8)
            logger.warning(
                "[penalties] 抓取失败，第 %d 次重试，退避 %.1fs: %s",
                attempt, backoff, url,
            )
            time.sleep(backoff)
        _throttle()
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": _UA, "Referer": _REFERER},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode("gbk", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise RuntimeError(
        f"抓取新浪处罚页失败，已重试 {cfg.http.max_retries} 次: {url}。最后错误: {last_error}"
    ) from last_error


# ─────────────────────────────────────────────────────────
# 缓存层
# ─────────────────────────────────────────────────────────

def _cache_path(stockid: str) -> Path:
    return get_settings().penalties_cache_dir / f"{stockid}.json"


def _is_fresh(path: Path, ttl_secs: int) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < ttl_secs
    except OSError:
        return False


def _load_cache(path: Path) -> list[dict] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _save_cache(path: Path, data: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("[penalties] 缓存写入失败: %s", exc)


# ─────────────────────────────────────────────────────────
# HTML 解析层
# ─────────────────────────────────────────────────────────

def _parse_penalty_list(html: str, source_url: str) -> list[dict]:
    """解析 vGP_GetOutOfLine 页面，返回处罚记录列表。

    每条记录包含：
      punish_date, event_type, title, reason, content, issuer, source_url
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="collectFund_1")
    if table is None:
        logger.warning("[penalties] 未找到 collectFund_1 表格")
        return []

    records: list[dict] = []
    current: dict | None = None

    for child in table.descendants:
        # 检测块起始：<th colspan="2"> 包含日期和类型
        if child.name == "th" and child.get("colspan") == "2":
            # 保存上一条
            if current and current.get("punish_date"):
                records.append(current)
            current = _new_record(child.get_text(strip=True), source_url)
            continue

        # 解析字段行：<tr> 包含 <strong>标签</strong> 和值
        if child.name == "tr" and current is not None:
            tds = child.find_all("td", recursive=False)
            if len(tds) == 2:
                label_cell = tds[0].find("strong")
                if label_cell:
                    label = label_cell.get_text(strip=True)
                    value = tds[1].get_text(separator=" ", strip=True)
                    _apply_field(current, label, value)

    # 保存最后一条
    if current and current.get("punish_date"):
        records.append(current)

    return records


def _new_record(th_text: str, source_url: str) -> dict:
    """从 th 文本中提取 punish_date 和 event_type，初始化记录字典。

    th_text 格式：'处罚决定  公告日期:2026-04-18'
    """
    record: dict = {
        "punish_date": "",
        "event_type": "",
        "title": "",
        "reason": "",
        "reason_raw": "",
        "content": "",
        "issuer": "",
        "issuer_raw": "",
        "source_url": source_url,
    }
    # 提取日期
    m = re.search(r"公告日期[:：]\s*(\d{4}-\d{2}-\d{2})", th_text)
    if m:
        record["punish_date"] = m.group(1)
    # 提取类型（冒号前的部分）
    type_part = th_text.split("公告日期")[0].strip()
    record["event_type"] = type_part.strip()
    return record


_ISSUER_RULES: list[tuple[str, str]] = [
    ("上海证券交易所", "上交所"),
    ("深圳证券交易所", "深交所"),
    ("北京证券交易所", "北交所"),
]

_PROVINCE_RE = re.compile(
    r"(北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|"
    r"山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|内蒙古|广西|"
    r"西藏|宁夏|新疆)(?:监管局)?"
)

_REASON_KEYWORDS: list[str] = [
    "信息披露违规", "内幕交易", "市场操纵", "操纵市场", "财务造假", "欺诈发行",
    "违规减持", "短线交易", "占用资金", "违规担保", "延迟披露", "虚假陈述",
]


def _extract_issuer(raw: str) -> str:
    """将原始处理人文本标准化为简称，识别不到则返回 'unknown'。"""
    for keyword, abbr in _ISSUER_RULES:
        if keyword in raw:
            return abbr
    if "证券监督管理委员会" in raw:
        m = _PROVINCE_RE.search(raw)
        if m:
            province = m.group(1)
            return f"{province}证监局"
        return "证监会"
    return "unknown"


def _extract_reason(raw: str) -> str:
    """从原始批复原因文本中提取标准化关键词，识别不到则返回 'unknown'。"""
    for kw in _REASON_KEYWORDS:
        if kw in raw:
            return kw
    return "unknown"


def _apply_field(record: dict, label: str, value: str) -> None:
    """将解析出的字段值写入 record，issuer/reason 同时保留原文与标准化值。"""
    if label == "标题":
        record["title"] = value
    elif label == "批复原因":
        record["reason"] = value
        record["reason_raw"] = value
        record["reason_normalized"] = _extract_reason(value)
    elif label == "批复内容":
        record["content"] = value
    elif label == "处理人":
        record["issuer_raw"] = value
        record["issuer"] = _extract_issuer(value)


# ─────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────

def fetch_penalty_list(
    ts_code: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """从新浪财经 vGP_GetOutOfLine 页抓取监管处罚记录。

    Args:
        ts_code: Tushare 格式股票代码，如 "688270.SH"
        start_date: 起始日期 YYYY-MM-DD（含），默认不限
        end_date: 结束日期 YYYY-MM-DD（含），默认不限

    Returns:
        {
          "ts_code": "688270.SH",
          "source": "sina",
          "penalties": [{punish_date, event_type, title, reason, content, issuer, source_url}]
        }
        失败时返回 {ts_code, source:"unavailable", error:"..."}
    """
    # 1. 输入校验
    try:
        if not isinstance(ts_code, str) or not ts_code:
            raise ValueError("ts_code 不能为空")
        stockid = _strip_market_suffix(ts_code)
        if start_date:
            _validate_date(start_date, "start_date")
        if end_date:
            _validate_date(end_date, "end_date")
    except ValueError as exc:
        return {
            "ts_code": ts_code,
            "source": "unavailable",
            "error": str(exc),
            "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
        }

    page_url = _ALLOWED_BASE + _PAGE_PATH.format(stockid=stockid)

    # 2. 缓存命中检查
    cache_file = _cache_path(stockid)
    if _is_fresh(cache_file, get_settings().cache.list_cache_ttl):
        cached = _load_cache(cache_file)
        if cached is not None:
            logger.debug("[penalties] 缓存命中: %s", stockid)
            penalties = _apply_date_filter(cached, start_date, end_date)
            return {"ts_code": ts_code, "source": "sina", "penalties": penalties}

    # 3. 抓取页面
    try:
        html = _http_get_gbk(page_url)
    except (RuntimeError, ValueError) as exc:
        logger.error("[penalties] 网络错误: %s", exc)
        return {
            "ts_code": ts_code,
            "source": "unavailable",
            "error": str(exc),
            "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
        }

    # 4. 解析
    all_records = _parse_penalty_list(html, page_url)
    _save_cache(cache_file, all_records)

    # 5. 日期过滤
    penalties = _apply_date_filter(all_records, start_date, end_date)

    return {
        "ts_code": ts_code,
        "source": "sina",
        "penalties": penalties,
    }


def _apply_date_filter(
    records: list[dict],
    start_date: str | None,
    end_date: str | None,
) -> list[dict]:
    """按 punish_date 过滤，缺失日期的记录始终保留。"""
    result = []
    for r in records:
        d = r.get("punish_date", "")
        if not d:
            result.append(r)
            continue
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        result.append(r)
    return result


# ─────────────────────────────────────────────────────────
# MCP 工具注册
# ─────────────────────────────────────────────────────────

def register_regulatory_penalty_tools(mcp: FastMCP) -> None:
    """向 FastMCP 实例注册监管处罚相关工具。"""

    @mcp.tool()
    def list_regulatory_penalties(
        ts_code: str,
        start_date: str = "",
        end_date: str = "",
    ) -> dict:
        """列出 A 股上市公司的监管处罚 / 违规处理记录（来源：新浪财经违规处理专页）。

        数据来源：https://vip.stock.finance.sina.com.cn/corp/go.php/vGP_GetOutOfLine/

        Args:
            ts_code: Tushare 格式股票代码，如 ``"688270.SH"``、``"000001.SZ"``
            start_date: 起始日期（含），格式 YYYY-MM-DD；默认不限
            end_date: 结束日期（含），格式 YYYY-MM-DD；默认不限

        Returns:
            dict:
              成功：{
                "ts_code": str,
                "source": "sina",
                "penalties": [
                  {
                    "punish_date": str,   # 公告日期 YYYY-MM-DD
                    "event_type": str,    # 来源页面原文，如 "处罚决定" / "立案调查" / "警示" / "整改通知" / "问讯"
                    "title":      str,    # 违规事件标题
                    "reason":     str,    # 批复原因
                    "content":    str,    # 批复内容摘要
                    "issuer":     str,    # 处理机构（如 "浙江证监局" / "上交所"）
                    "source_url": str     # 来源页面 URL（处罚列表页，无单条详情页）
                  }
                ]
              }
              失败：{"ts_code", "source": "unavailable", "error", "fetch_time"}

        Notes:
            - ``event_type`` 为新浪页面原始文本，未做受控枚举归一化；
              如需筛选，请在客户端按关键词匹配。
            - ``source_url`` 指向列表页而非单条详情页（数据源不提供逐条链接）。
        """
        sd = start_date if start_date else None
        ed = end_date if end_date else None
        try:
            return fetch_penalty_list(ts_code, sd, ed)
        except Exception as exc:  # noqa: BLE001
            logger.error("[list_regulatory_penalties] 未预期错误: %s", exc)
            return {
                "ts_code": ts_code,
                "source": "unavailable",
                "error": f"未预期错误: {exc}",
                "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
            }
