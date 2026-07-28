"""新浪财经临时公告抓取工具（vCB_AllBulletin）

列表页 URL（已实地验证）：
  http://money.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/{stockid}/page/{page}.phtml

详情页 URL（已实地验证）：
  http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid={stockid}&id={id}

HTML 结构（2026-04 实地抓取确认）：
  列表页：<div class="datelist"><ul>
          DATE_TEXT <a href="/corp/view/vCB_AllBulletinDetail.php?...">TITLE</a><br/>...
  详情页：div#content（最精确）→ 回退 div#con02-7 → div#box
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings

logger = logging.getLogger(__name__)

# ── 域名常量（用于 SSRF 防护） ─────────────────────────────
_LIST_BASE = "http://money.finance.sina.com.cn"
_DETAIL_BASE_HTTP = "http://vip.stock.finance.sina.com.cn"
_DETAIL_BASE_HTTPS = "https://vip.stock.finance.sina.com.cn"
_DETAIL_PATH = "/corp/view/vCB_AllBulletinDetail.php"

# ── URL 模板 ──────────────────────────────────────────────
_LIST_URL_P1 = _LIST_BASE + "/corp/go.php/vCB_AllBulletin/stockid/{stockid}.phtml"
_LIST_URL_PN = _LIST_BASE + "/corp/go.php/vCB_AllBulletin/stockid/{stockid}/page/{page}.phtml"

# ── HTTP 请求配置 ─────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REFERER = "https://finance.sina.com.cn/"

# ── 输入校验正则（防 SSRF / URL 注入） ───────────────────
_STOCKID_RE = re.compile(r"^\d{6}$")
_BULLETIN_ID_RE = re.compile(r"^\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ── ann_type 关键词分类表（顺序优先，先匹配先胜） ──────────
# 注意: litigation 必须前置于 penalty，否则 "诉讼立案" 会被 "立案" 吃掉
_ANN_TYPE_RULES: list[tuple[str, list[str]]] = [
    ("inquiry",           ["问询函", "监管工作函", "问询回复", "关注函", "问询函回复"]),
    ("audit",             ["审计报告", "审计意见", "关键审计事项", "非标准审计",
                           "保留意见", "无法表示意见", "鉴证报告"]),
    ("accountant_change", ["会计师事务所变更", "更换会计师", "续聘会计师", "聘请会计师"]),
    ("litigation",        ["诉讼", "仲裁", "法律纠纷"]),
    ("penalty",           ["行政处罚", "纪律处分", "监管处罚", "监管措施",
                           "立案调查", "立案告知", "被立案", "警示函",
                           "通报批评", "告知书"]),
    ("restatement",       ["差错更正", "财报重述", "追溯调整", "前期会计差错",
                           "前期差错更正", "前期财务"]),
]

_last_request_time: float = 0.0


# ─────────────────────────────────────────────────────────
# 输入校验
# ─────────────────────────────────────────────────────────

def _validate_stockid(stockid: str) -> str:
    """校验 stockid 为 6 位数字，防止 URL 注入 / SSRF。"""
    if not isinstance(stockid, str) or not _STOCKID_RE.fullmatch(stockid):
        raise ValueError(f"stockid 必须是 6 位纯数字字符串，收到: {stockid!r}")
    return stockid


def _validate_bulletin_id(bid: str) -> str:
    """校验公告 id 为纯数字。"""
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
    # 额外校验 query 参数只含合法字段
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
    """将 Tushare 格式 ts_code（如 688270.SH）去掉市场后缀，返回 6 位股票代码。"""
    code = ts_code.split(".")[0]
    return _validate_stockid(code)


# ─────────────────────────────────────────────────────────
# HTTP 请求层
# ─────────────────────────────────────────────────────────

def _throttle() -> None:
    """确保相邻请求之间至少间隔 `http.request_interval` 秒。"""
    global _last_request_time
    interval = get_settings().http.request_interval
    elapsed = time.monotonic() - _last_request_time
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_request_time = time.monotonic()


def _http_get_gbk(url: str, timeout: int = 15) -> str:
    """抓取 GBK 编码页面，内置指数退避重试（支持配置 `http.max_retries`）。

    仅允许 sina 域名，防止 SSRF。
    """
    allowed_prefixes = (_LIST_BASE, _DETAIL_BASE_HTTP, _DETAIL_BASE_HTTPS)
    if not any(url.startswith(p) for p in allowed_prefixes):
        raise ValueError(f"禁止访问非新浪财经域名: {url!r}")

    cfg = get_settings()
    max_retries = cfg.http.max_retries
    backoff_base = cfg.http.backoff_base

    last_error: Exception | None = None
    for attempt in range(max_retries):
        if attempt > 0:
            backoff = backoff_base ** attempt + random.uniform(0.2, 0.8)
            logger.warning(
                "[announcements] 抓取失败，第 %d 次重试，退避 %.1fs: %s",
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
        f"抓取新浪公告页失败，已重试 {max_retries} 次: {url}。最后错误: {last_error}"
    ) from last_error


# ─────────────────────────────────────────────────────────
# 缓存层
# ─────────────────────────────────────────────────────────

def _cache_path_list(stockid: str, page: int) -> Path:
    return get_settings().announcements_cache_dir / f"{stockid}_p{page}.json"


def _cache_path_detail(bulletin_id: str) -> Path:
    return get_settings().announcements_cache_dir / "detail" / f"{bulletin_id}.txt"


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
# 解析层
# ─────────────────────────────────────────────────────────

def _classify_ann_type(title: str) -> str:
    """根据标题关键词分类 ann_type，未匹配返回 'other'。"""
    for ann_type, keywords in _ANN_TYPE_RULES:
        if any(kw in title for kw in keywords):
            return ann_type
    return "other"


def _build_detail_url(stockid: str, bulletin_id: str) -> str:
    """构造规范的公告详情页 URL。"""
    _validate_stockid(stockid)
    _validate_bulletin_id(bulletin_id)
    return (
        _DETAIL_BASE_HTTP
        + _DETAIL_PATH
        + f"?stockid={stockid}&id={bulletin_id}"
    )


def _parse_announcement_list(html: str, stockid: str) -> list[dict]:
    """解析列表页，提取公告条目列表。

    返回字段：ann_date, title, ann_type, source_url, pdf_url（暂为 null）
    """
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []
    seen_ids: set[str] = set()

    datelist = soup.find("div", class_="datelist")
    if not datelist:
        logger.warning("[announcements] 未找到 div.datelist，页面结构可能已变更")
        return entries

    # 遍历 datelist 中所有 <a> 链接；日期在 <a> 的前一个文本节点中
    for a in datelist.find_all("a", href=re.compile(r"vCB_AllBulletinDetail")):
        href = a.get("href", "")
        id_match = re.search(r"[?&]id=(\d+)", href)
        if not id_match:
            continue
        bulletin_id = id_match.group(1)
        if bulletin_id in seen_ids:
            continue
        seen_ids.add(bulletin_id)

        title = a.get_text(strip=True)
        # 日期在 <a> 前面的文本节点中（格式 "YYYY-MM-DD "）
        prev = a.previous_sibling
        date_str = ""
        if prev and isinstance(prev, str):
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", prev)
            if date_match:
                date_str = date_match.group(1)

        entries.append({
            "ann_date": date_str,
            "title": title,
            "ann_type": _classify_ann_type(title),
            "source_url": _build_detail_url(stockid, bulletin_id),
            "pdf_url": None,
        })

    return entries


def _find_next_page_url(html: str) -> str | None:
    """在页面中查找"下一页"链接，返回绝对 URL 或 None。"""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a"):
        if "下一页" in a.get_text():
            href = a.get("href", "")
            if href and not href.startswith("http"):
                href = _LIST_BASE + href
            return href if href else None
    return None


def _extract_detail_text(html: str) -> str:
    """从详情页提取公告正文纯文本。

    容器优先级（已实地验证）：
      1. div#content  — 最精确的正文区域
      2. div#con02-7  — 外层包含正文的容器
      3. div#box      — 备用
      4. body         — 最终兜底
    """
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

def fetch_announcement_list(
    stockid: str,
    start_date: str = "",
    end_date: str = "",
) -> list[dict]:
    """抓取指定股票代码的公告列表，支持日期区间过滤。

    - stockid: 6 位纯数字
    - start_date / end_date: YYYY-MM-DD，空字符串表示不限制
    - 自动翻页（支持配置 `announcements.max_pages`）
    - 列表页结果缓存（支持配置 `cache.list_cache_ttl`）
    """
    _validate_stockid(stockid)
    if start_date:
        _validate_date(start_date, "start_date")
    if end_date:
        _validate_date(end_date, "end_date")

    cfg = get_settings()
    max_pages = cfg.announcements.max_pages
    list_ttl = cfg.cache.list_cache_ttl

    all_entries: list[dict] = []
    seen_ids: set[str] = set()
    current_url = _LIST_URL_P1.format(stockid=stockid)
    page = 1
    stop_early = False

    while current_url and page <= max_pages:
        # 检查列表页缓存
        cache_p = _cache_path_list(stockid, page)
        if _is_fresh(cache_p, list_ttl):
            logger.debug("[announcements] 命中列表页缓存: %s p%d", stockid, page)
            cached_data = json.loads(_read_cache(cache_p))
            entries: list[dict] = cached_data.get("entries", [])
            next_url: str | None = cached_data.get("next_url")
        else:
            logger.info("[announcements] 抓取列表页: %s", current_url)
            html = _http_get_gbk(current_url)
            entries = _parse_announcement_list(html, stockid)
            next_url = _find_next_page_url(html)
            # 写缓存
            _write_cache(cache_p, json.dumps(
                {"entries": entries, "next_url": next_url},
                ensure_ascii=False,
            ))

        for entry in entries:
            uid = entry["source_url"]
            if uid in seen_ids:
                continue
            seen_ids.add(uid)

            ann_date = entry["ann_date"]
            # 日期过滤（假设列表按时间降序）
            if end_date and ann_date and ann_date > end_date:
                continue  # 跳过比 end_date 更新的条目
            if start_date and ann_date and ann_date < start_date:
                stop_early = True
                break      # 后续条目只会更早，提前停止
            all_entries.append(entry)

        if stop_early:
            break

        current_url = next_url or ""
        page += 1

    return all_entries


def fetch_announcement_text(source_url: str) -> str:
    """抓取单条公告的正文纯文本，结果缓存（支持配置 `cache.detail_cache_ttl`）。"""
    _validate_detail_url(source_url)

    # 从 URL 中提取 id 作为缓存 key
    parsed = urllib.parse.urlparse(source_url)
    params = urllib.parse.parse_qs(parsed.query)
    bulletin_id = params.get("id", [""])[0]
    cache_p = _cache_path_detail(bulletin_id)

    detail_ttl = get_settings().cache.detail_cache_ttl
    if _is_fresh(cache_p, detail_ttl):
        logger.debug("[announcements] 命中详情缓存: id=%s", bulletin_id)
        return _read_cache(cache_p)

    logger.info("[announcements] 抓取详情页: %s", source_url)
    html = _http_get_gbk(source_url)
    text = _extract_detail_text(html)
    _write_cache(cache_p, text)
    return text


# ─────────────────────────────────────────────────────────
# MCP 工具注册
# ─────────────────────────────────────────────────────────

def register_announcement_tools(mcp: FastMCP) -> None:
    """向 FastMCP 实例注册 list_announcements 和 get_announcement_text。"""

    @mcp.tool()
    def list_announcements(
        ts_code: str,
        start_date: str = "",
        end_date: str = "",
        ann_types: list[str] | None = None,
    ) -> dict:
        """获取 A 股上市公司临时公告列表（来源：新浪财经 vCB_AllBulletin）。

        Args:
            ts_code:    Tushare 格式股票代码，如 "688270.SH"
            start_date: 起始日期（含），格式 YYYY-MM-DD；默认当年 1 月 1 日
            end_date:   结束日期（含），格式 YYYY-MM-DD；默认今日
            ann_types:  过滤公告类型列表，默认全部返回。可选值（按语义分类）：
                          - ``inquiry``           问询函 / 监管工作函 / 关注函及其回复
                          - ``audit``             审计报告 / 审计意见 / 非标准审计意见 / 鉴证报告
                          - ``accountant_change`` 会计师事务所变更 / 续聘 / 聘请
                          - ``litigation``        诉讼 / 仲裁 / 法律纠纷
                          - ``penalty``           行政处罚 / 纪律处分 / 监管措施 / 立案调查 / 警示函
                          - ``restatement``       差错更正 / 财报重述 / 追溯调整
                          - ``other``             未归入以上分类的其它公告

        Returns:
            {
              "ts_code": "688270.SH",
              "source": "sina",
              "announcements": [
                {
                  "ann_date": "2025-04-15",
                  "title": "关于...",
                  "ann_type": "inquiry",
                  "source_url": "http://vip.stock.finance.sina.com.cn/...",
                  "pdf_url": null
                }
              ]
            }
            失败时 source 为 "unavailable"，附带 error 字段。
        """
        # 默认日期
        today = date.today().isoformat()
        year_start = today[:4] + "-01-01"
        if not start_date:
            start_date = year_start
        if not end_date:
            end_date = today

        try:
            stockid = _strip_market_suffix(ts_code)
            entries = fetch_announcement_list(stockid, start_date, end_date)
        except ValueError as exc:
            return {
                "ts_code": ts_code,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
            }
        except RuntimeError as exc:
            logger.error("[list_announcements] 网络错误: %s", exc)
            return {
                "ts_code": ts_code,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
            }

        # ann_types 过滤
        if ann_types:
            valid_types = {r[0] for r in _ANN_TYPE_RULES} | {"other"}
            unknown = set(ann_types) - valid_types
            if unknown:
                return {
                    "ts_code": ts_code,
                    "source": "unavailable",
                    "error": f"未知 ann_type: {unknown}，合法值: {sorted(valid_types)}",
                    "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
                }
            entries = [e for e in entries if e["ann_type"] in ann_types]

        return {
            "ts_code": ts_code,
            "source": "sina",
            "announcements": entries,
        }

    @mcp.tool()
    def get_announcement_text(source_url: str) -> dict:
        """抓取单条公告全文（来源：新浪财经公告详情页）。

        Args:
            source_url: 由 ``list_announcements`` 返回条目的 ``source_url``

        Returns:
            dict:
              成功：{
                "source_url":   str,
                "full_text":    str,   # 提取后的纯文本正文
                "extracted_at": str    # ISO8601 UTC 时间戳
              }
              失败：同上结构，``full_text`` 为 ``""`` 且附带 ``error`` 字段

        Notes:
            本工具不抛异常；所有错误经由返回字典的 ``error`` 字段传递。
        """
        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            text = fetch_announcement_text(source_url)
        except ValueError as exc:
            return {
                "source_url": source_url,
                "full_text": "",
                "extracted_at": now_utc,
                "error": str(exc),
            }
        except RuntimeError as exc:
            logger.error("[get_announcement_text] 网络错误: %s", exc)
            return {
                "source_url": source_url,
                "full_text": "",
                "extracted_at": now_utc,
                "error": str(exc),
            }

        extracted_at = now_utc
        return {
            "source_url": source_url,
            "full_text": text,
            "extracted_at": extracted_at,
        }
