"""新浪财经 IR 会议/调研纪要抓取工具（vCB_AllBulletin lsgg 临时公告过滤）

列表页 URL（已实地验证）：
  页 1：http://money.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/{stockid}.phtml?ftype=lsgg
  翻页：http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletin.php?stockid={stockid}&Page={n}&ftype=lsgg

详情页 URL（与 announcements.py 相同）：
  http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid={stockid}&id={id}

IR 标题关键词（2026-04 实地探查确认）：
  - 投资者关系活动记录表  — 通用格式（深市多见）
  - 投资者关系管理信息    — 平安银行等大盘变体
  - 业绩说明会            — 网络业绩说明会/电话会
  - 投资者说明会          — 投资者关系说明会变体
  - 调研活动信息表        — 部分公司使用

meeting_type 分类（按标题关键词）：
  earnings_call  — 业绩说明会 / 业绩交流会 / 电话会
  site_visit     — 实地调研 / 现场参观 / 参观
  research       — 其余 IR 标题（机构调研默认）
  other          — 兜底
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── 域名常量（SSRF 防护） ──────────────────────────────────
_LIST_BASE = "http://money.finance.sina.com.cn"
_PAGE_BASE = "http://vip.stock.finance.sina.com.cn"
_DETAIL_BASE_HTTP = "http://vip.stock.finance.sina.com.cn"
_DETAIL_BASE_HTTPS = "https://vip.stock.finance.sina.com.cn"
_DETAIL_PATH = "/corp/view/vCB_AllBulletinDetail.php"

# ── URL 模板 ──────────────────────────────────────────────
_LIST_URL_P1 = (
    _LIST_BASE
    + "/corp/go.php/vCB_AllBulletin/stockid/{stockid}.phtml?ftype=lsgg"
)
_LIST_URL_PN = (
    _PAGE_BASE
    + "/corp/view/vCB_AllBulletin.php?stockid={stockid}&Page={page}&ftype=lsgg"
)

# ── HTTP 请求配置 ─────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REFERER = "https://finance.sina.com.cn/"

# ── 输入校验正则 ──────────────────────────────────────────
_STOCKID_RE = re.compile(r"^\d{6}$")
_BULLETIN_ID_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ── IR 标题关键词（命中其一即视为 IR 条目） ──────────────
_IR_TITLE_KEYWORDS = [
    "投资者关系活动记录",
    "投资者关系管理信息",
    "业绩说明会",
    "投资者说明会",
    "调研活动信息表",
    "投资者开放日",
    "网上业绩说明会",
    "电话会议纪要",
    "投资者关系活动",
]

# ── meeting_type 分类规则（先匹配先胜） ──────────────────
_MEETING_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("earnings_call", [
        "业绩说明会", "业绩交流会", "电话会议", "网上业绩",
        "业绩交流", "投资者说明会", "业绩发布",
    ]),
    ("site_visit", [
        "实地调研", "现场参观", "参观", "现场考察",
    ]),
    ("research", [
        "调研", "机构投资者", "投资者关系活动记录",
        "投资者关系管理信息", "调研活动信息表",
        "投资者关系活动", "投资者开放日",
    ]),
]

# ── 参与机构提取正则（从详情页正文中解析） ───────────────
_PARTICIPANTS_RE = re.compile(
    r"(?:参会|参加|与会|接待|来访)[^：:]*[：:]\s*([^\n。；;]{5,200})"
)
_ORG_SPLITTER_RE = re.compile(r"[、，,；;]+")

# 机构名特征词（满足其一才视为合法参与机构）
_ORG_KEYWORDS = (
    "证券", "资本", "基金", "银行", "资产", "投资", "保险", "信托",
    "公司", "研究所", "研究院", "财富", "期货", "私募", "家族办公室",
)
# 明显的占位词/噪声词（出现即丢弃）
_ORG_BLOCKLIST = (
    "其他", "详见", "附件", "见下", "名单", "等机构", "以上", "不限于",
    "投资者", "等人员", "共计",
)

_last_request_time: float = 0.0


# ─────────────────────────────────────────────────────────
# 输入校验
# ─────────────────────────────────────────────────────────

def _validate_stockid(stockid: str) -> str:
    if not isinstance(stockid, str) or not _STOCKID_RE.fullmatch(stockid):
        raise ValueError(f"stockid 必须是 6 位纯数字字符串，收到: {stockid!r}")
    return stockid


def _validate_bulletin_id(bid: str) -> str:
    if not isinstance(bid, str) or not _BULLETIN_ID_RE.fullmatch(bid):
        raise ValueError(f"公告 id 必须是纯数字字符串，收到: {bid!r}")
    return bid


def _validate_detail_url(url: str) -> str:
    """校验 source_url 属于新浪财经公告详情页，防止 SSRF。"""
    if not isinstance(url, str):
        raise ValueError("source_url 必须是字符串")
    allowed = (
        _DETAIL_BASE_HTTP + _DETAIL_PATH,
        _DETAIL_BASE_HTTPS + _DETAIL_PATH,
    )
    if not any(url.startswith(p) for p in allowed):
        raise ValueError(
            f"source_url 必须是新浪财经公告详情页（{_DETAIL_PATH}），收到: {url!r}"
        )
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    stockid = params.get("stockid", [""])[0]
    bid = params.get("id", [""])[0]
    _validate_stockid(stockid)
    _validate_bulletin_id(bid)
    return url


def _validate_date(d: str, field: str) -> str:
    if not _DATE_RE.fullmatch(d):
        raise ValueError(f"{field} 格式应为 YYYY-MM-DD，收到: {d!r}")
    return d


def _strip_market_suffix(ts_code: str) -> str:
    code = ts_code.split(".")[0]
    return _validate_stockid(code)


def _validate_meeting_types(meeting_types: list[str]) -> list[str]:
    valid = {"research", "earnings_call", "site_visit", "other"}
    unknown = set(meeting_types) - valid
    if unknown:
        raise ValueError(f"未知 meeting_type: {unknown}，合法值: {sorted(valid)}")
    return meeting_types


# ─────────────────────────────────────────────────────────
# HTTP 请求层
# ─────────────────────────────────────────────────────────

def _throttle() -> None:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    interval = get_settings().http.request_interval
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_request_time = time.monotonic()


def _http_get_gbk(url: str, timeout: int = 15) -> str:
    """抓取 GBK 编码页面，指数退避重试，仅允许新浪域名（SSRF 防护）。"""
    allowed_prefixes = (_LIST_BASE, _PAGE_BASE, _DETAIL_BASE_HTTPS)
    if not any(url.startswith(p) for p in allowed_prefixes):
        raise ValueError(f"禁止访问非新浪财经域名: {url!r}")

    cfg = get_settings()
    last_error: Exception | None = None
    for attempt in range(cfg.http.max_retries):
        if attempt > 0:
            backoff = cfg.http.backoff_base ** attempt + random.uniform(0.2, 0.8)
            logger.warning(
                "[ir_meetings] 抓取失败，第 %d 次重试，退避 %.1fs: %s",
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
        f"抓取新浪公告页失败，已重试 {cfg.http.max_retries} 次: {url}。最后错误: {last_error}"
    ) from last_error


# ─────────────────────────────────────────────────────────
# 缓存层
# ─────────────────────────────────────────────────────────

def _cache_path_list(stockid: str, page: int) -> Path:
    return get_settings().ir_meetings_cache_dir / f"{stockid}_p{page}.json"


def _cache_path_detail(bulletin_id: str) -> Path:
    return get_settings().ir_meetings_cache_dir / "detail" / f"{bulletin_id}.txt"


def _is_fresh(path: Path, ttl_secs: int) -> bool:
    try:
        mtime = path.stat().st_mtime
        return (time.time() - mtime) < ttl_secs
    except FileNotFoundError:
        return False


def _write_cache(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_cache(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────
# 分类逻辑
# ─────────────────────────────────────────────────────────

def _is_ir_title(title: str) -> bool:
    """判断标题是否属于 IR 活动记录。"""
    return any(kw in title for kw in _IR_TITLE_KEYWORDS)


def _classify_meeting_type(title: str) -> str:
    """根据标题关键词分类 meeting_type，未匹配返回 'other'。"""
    for mtype, keywords in _MEETING_TYPE_RULES:
        if any(kw in title for kw in keywords):
            return mtype
    return "other"


def _extract_participants(text: str) -> list[str]:
    """从纪要正文中提取参与机构列表。

    查找"参会/接待：机构A、机构B"等模式，拆分后做机构名特征过滤。
    只保留含机构名特征词（证券/资本/基金/...）且不含占位词的条目。
    """
    participants: list[str] = []
    for m in _PARTICIPANTS_RE.finditer(text):
        raw = m.group(1).strip()
        parts = _ORG_SPLITTER_RE.split(raw)
        for p in parts:
            p = p.strip()
            if not p or len(p) > 30 or len(p) < 2:
                continue
            # 丢弃占位/噪声词
            if any(bad in p for bad in _ORG_BLOCKLIST):
                continue
            # 必须含机构名特征词
            if not any(kw in p for kw in _ORG_KEYWORDS):
                continue
            participants.append(p)
    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for p in participants:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


# ─────────────────────────────────────────────────────────
# 解析层
# ─────────────────────────────────────────────────────────

def _build_detail_url(stockid: str, bulletin_id: str) -> str:
    _validate_stockid(stockid)
    _validate_bulletin_id(bulletin_id)
    return (
        _DETAIL_BASE_HTTP
        + _DETAIL_PATH
        + f"?stockid={stockid}&id={bulletin_id}"
    )


def _parse_ir_list(html: str, stockid: str) -> list[dict]:
    """解析列表页，仅提取 IR 标题条目。

    返回字段：meeting_date, title, meeting_type, source_url
    """
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    seen_ids: set[str] = set()

    datelist = soup.find("div", class_="datelist")
    if not datelist:
        logger.warning("[ir_meetings] 未找到 div.datelist，页面结构可能已变更")
        return entries

    for a in datelist.find_all("a", href=re.compile(r"vCB_AllBulletinDetail")):
        href = a.get("href", "")
        id_match = re.search(r"[?&]id=(\d+)", href)
        if not id_match:
            continue
        bulletin_id = id_match.group(1)
        if bulletin_id in seen_ids:
            continue

        title = a.get_text(strip=True)
        if not _is_ir_title(title):
            continue

        seen_ids.add(bulletin_id)

        # 日期在 <a> 前面的文本节点中
        prev = a.previous_sibling
        date_str = ""
        if prev and isinstance(prev, str):
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", prev)
            if date_match:
                date_str = date_match.group(1)

        entries.append({
            "meeting_date": date_str,
            "title": title,
            "meeting_type": _classify_meeting_type(title),
            "source_url": _build_detail_url(stockid, bulletin_id),
            "participants": [],
            "summary": "",
        })

    return entries


def _find_oldest_date(html: str) -> str:
    """从页面中找最旧的日期（用于早停判断）。"""
    dates = re.findall(r"(\d{4}-\d{2}-\d{2})", html)
    return min(dates) if dates else ""


def _extract_ir_text(html: str) -> str:
    """从详情页提取 IR 纪要正文纯文本。"""
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("content", "con02-7", "box"):
        container = soup.find(id=selector)
        if container:
            return container.get_text(separator="\n", strip=True)
    body = soup.find("body") or soup
    return body.get_text(separator="\n", strip=True)  # type: ignore[union-attr]


# ─────────────────────────────────────────────────────────
# 公共接口
# ─────────────────────────────────────────────────────────

def fetch_ir_meeting_list(
    stockid: str,
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    """抓取指定股票的 IR 会议/调研纪要列表，支持日期区间过滤。

    - 自动翻页（支持配置 `ir_meetings.max_pages`）
    - 遇到最旧条目早于 start_date 提前停止
    - 列表页缓存（支持配置 `cache.list_cache_ttl`）
    """
    _validate_stockid(stockid)
    if start_date:
        _validate_date(start_date, "start_date")
    if end_date:
        _validate_date(end_date, "end_date")

    cfg = get_settings()
    all_entries: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(1, cfg.ir_meetings.max_pages + 1):
        if page == 1:
            url = _LIST_URL_P1.format(stockid=stockid)
        else:
            url = _LIST_URL_PN.format(stockid=stockid, page=page)

        cache_p = _cache_path_list(stockid, page)
        if _is_fresh(cache_p, cfg.cache.list_cache_ttl):
            logger.debug("[ir_meetings] 命中列表页缓存: %s p%d", stockid, page)
            cached = json.loads(_read_cache(cache_p))
            entries: list[dict] = cached.get("entries", [])
            oldest_date: str = cached.get("oldest_date", "")
            has_next: bool = cached.get("has_next", False)
        else:
            logger.info("[ir_meetings] 抓取列表页: %s", url)
            html = _http_get_gbk(url)
            entries = _parse_ir_list(html, stockid)
            oldest_date = _find_oldest_date(html)
            # 判断是否有下一页（页面中出现 "下一页" 链接）
            has_next = "下一页" in html
            _write_cache(cache_p, json.dumps(
                {"entries": entries, "oldest_date": oldest_date, "has_next": has_next},
                ensure_ascii=False,
            ))

        for entry in entries:
            url_key = entry["source_url"]
            if url_key in seen_urls:
                continue
            seen_urls.add(url_key)
            mdate = entry["meeting_date"]
            if end_date and mdate and mdate > end_date:
                continue
            if start_date and mdate and mdate < start_date:
                continue
            all_entries.append(entry)

        # 早停：该页最旧日期已早于 start_date
        if start_date and oldest_date and oldest_date < start_date:
            logger.debug(
                "[ir_meetings] 早停: 页 %d 最旧日期 %s < start_date %s",
                page, oldest_date, start_date,
            )
            break

        if not has_next:
            break

    return all_entries


def fetch_ir_meeting_text(source_url: str) -> str:
    """抓取单条 IR 纪要全文，结果缓存 7 天。"""
    _validate_detail_url(source_url)

    parsed = urllib.parse.urlparse(source_url)
    params = urllib.parse.parse_qs(parsed.query)
    bulletin_id = params.get("id", [""])[0]
    cache_p = _cache_path_detail(bulletin_id)

    if _is_fresh(cache_p, get_settings().cache.detail_cache_ttl):
        logger.debug("[ir_meetings] 命中详情缓存: id=%s", bulletin_id)
        return _read_cache(cache_p)

    logger.info("[ir_meetings] 抓取详情页: %s", source_url)
    html = _http_get_gbk(source_url)
    text = _extract_ir_text(html)
    _write_cache(cache_p, text)
    return text


# ─────────────────────────────────────────────────────────
# MCP 工具注册
# ─────────────────────────────────────────────────────────

def register_ir_meeting_tools(mcp: FastMCP) -> None:
    """向 FastMCP 实例注册 list_ir_meetings 和 get_ir_meeting_text。"""

    @mcp.tool()
    def list_ir_meetings(
        ts_code: str,
        start_date: str = "",
        end_date: str = "",
        meeting_types: list[str] | None = None,
    ) -> dict:
        """获取 A 股上市公司投资者关系活动记录（来源：新浪财经临时公告 lsgg 分类）。

        包含机构调研记录表、业绩说明会、投资者说明会等 IR 活动类公告。

        Args:
            ts_code:       Tushare 格式股票代码，如 "000001.SZ"
            start_date:    起始日期（含），格式 YYYY-MM-DD；默认近 6 个月
            end_date:      结束日期（含），格式 YYYY-MM-DD；默认今日
            meeting_types: 过滤会议类型列表，默认全部返回。可选值：
                             - ``research``       机构调研 / 投资者关系活动记录表
                             - ``earnings_call``  业绩说明会 / 业绩发布会 / 年度业绩交流
                             - ``site_visit``     现场参观 / 实地考察
                             - ``other``          未归入以上分类的其它 IR 活动

        Returns:
            {
              "ts_code": "000001.SZ",
              "source": "sina",
              "meetings": [
                {
                  "meeting_date": "2026-03-20",
                  "meeting_type": "research",
                  "participants": ["中信证券", "高瓴资本"],
                  "title": "投资者关系活动记录表",
                  "summary": "",
                  "source_url": "http://vip.stock.finance.sina.com.cn/..."
                }
              ]
            }
            失败时 source 为 "unavailable"，附带 error 字段。
        """
        today = date.today().isoformat()
        default_start = (date.today() - timedelta(days=180)).isoformat()
        if not start_date:
            start_date = default_start
        if not end_date:
            end_date = today

        try:
            stockid = _strip_market_suffix(ts_code)
            if meeting_types:
                _validate_meeting_types(meeting_types)
            entries = fetch_ir_meeting_list(stockid, start_date, end_date)
        except ValueError as exc:
            return {
                "ts_code": ts_code,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
            }
        except RuntimeError as exc:
            logger.error("[list_ir_meetings] 网络错误: %s", exc)
            return {
                "ts_code": ts_code,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
            }

        if meeting_types:
            entries = [e for e in entries if e["meeting_type"] in meeting_types]

        # 按 meeting_date 降序排列
        entries.sort(key=lambda x: x["meeting_date"], reverse=True)

        return {
            "ts_code": ts_code,
            "source": "sina",
            "meetings": entries,
        }

    @mcp.tool()
    def get_ir_meeting_text(source_url: str) -> dict:
        """抓取单条 IR 纪要 / 调研记录表全文（来源：新浪财经公告详情页）。

        Args:
            source_url: 由 ``list_ir_meetings`` 返回条目的 ``source_url``

        Returns:
            dict:
              成功：{
                "source_url":   str,
                "full_text":    str,        # 会议纪要正文纯文本
                "participants": list[str],  # 从正文抽取的参会机构/个人名称，去重
                "extracted_at": str         # ISO8601 UTC 时间戳
              }
              失败：同上结构，``full_text`` 为 ``""``、``participants`` 为 ``[]``，附带 ``error`` 字段

        Notes:
            本工具不抛异常；所有错误经由返回字典的 ``error`` 字段传递。
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            text = fetch_ir_meeting_text(source_url)
        except ValueError as exc:
            return {
                "source_url": source_url,
                "full_text": "",
                "participants": [],
                "extracted_at": now_utc,
                "error": str(exc),
            }
        except RuntimeError as exc:
            logger.error("[get_ir_meeting_text] 网络错误: %s", exc)
            return {
                "source_url": source_url,
                "full_text": "",
                "participants": [],
                "extracted_at": now_utc,
                "error": str(exc),
            }

        participants = _extract_participants(text)
        return {
            "source_url": source_url,
            "full_text": text,
            "participants": participants,
            "extracted_at": now_utc,
        }
