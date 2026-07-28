"""新浪财经行业研报抓取工具。"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings

logger = logging.getLogger(__name__)

_BASE = "https://stock.finance.sina.com.cn"
_LIST_PATH = "/stock/go.php/vReport_List/kind/industry/index.phtml"
_SEARCH_PATH = "/stock/go.php/vReport_List/kind/search/index.phtml"
_DETAIL_PATH_PREFIX = "/stock/go.php/vReport_Show/"
_STOCK_PAGE_BASE = "https://finance.sina.com.cn"
# 匹配同一 URL 内的 industry=swN_NNNNNN 与 t1=NN 参数，
# 兼容 HTML 转义（&amp;）与原生 & 两种分隔形式。
_SW2_RE = re.compile(r"industry=(sw[23]_\d+)(?:&amp;|&)t1=(\d+)")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REFERER = "https://finance.sina.com.cn/"

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

_last_request_time: float = 0.0


def _validate_date(d: str, field: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ValueError(f"{field} 格式应为 YYYY-MM-DD，收到: {d!r}")
    return d


def _ts_code_to_sina_code(ts_code: str) -> str:
    """将 Tushare ts_code 转换为新浪个股代码。

    例：``603129.SH`` → ``sh603129``，``002594.SZ`` → ``sz002594``
    """
    ts_code = ts_code.strip()
    if not re.fullmatch(r"\d{6}\.[A-Z]{2}", ts_code):
        raise ValueError(f"ts_code 格式无效（应为 6位数字.2位市场后缀）: {ts_code!r}")
    num, market = ts_code.split(".")
    return market.lower() + num


def _resolve_sw2_from_ts_code(ts_code: str) -> tuple[str, str]:
    """从新浪个股页面提取申万二级行业代码和 t1 参数。

    返回 ``(sw2_code, t1_value)``，如 ``('sw2_280400', '33')``。
    失败时抛出 ``RuntimeError``。
    """
    sina_code = _ts_code_to_sina_code(ts_code)
    url = f"{_STOCK_PAGE_BASE}/realstock/company/{sina_code}/nc.shtml"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _UA, "Referer": _REFERER},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"获取个股页面失败 {url}: {exc}") from exc

    m = _SW2_RE.search(html)
    if not m:
        # 页面结构变化时的宽松降级：仅提取 sw2 代码，t1 默认 33（行业研报）
        m2 = re.search(r"industry=(sw[23]_\d+)", html)
        if m2:
            # 在 m2 命中的位置附近再找一次 t1，避免跨无关字段匹配
            window = html[m2.end() : m2.end() + 200]
            t1_m = re.search(r"t1=(\d+)", window)
            return m2.group(1), t1_m.group(1) if t1_m else "33"
        raise RuntimeError(f"未能从个股页面提取 sw2 代码: {url}")
    return m.group(1), m.group(2)


def _normalize_keywords(keywords: list[str] | None) -> list[str]:
    if not keywords:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for k in keywords:
        k2 = k.strip()
        if not k2 or k2 in seen:
            continue
        seen.add(k2)
        out.append(k2)
    return out


def _validate_report_url(source_url: str) -> str:
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("source_url 必须是非空字符串")
    if not source_url.startswith(_BASE + _DETAIL_PATH_PREFIX):
        raise ValueError(f"source_url 必须以 {_BASE + _DETAIL_PATH_PREFIX!r} 开头")
    return source_url


def _throttle() -> None:
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    interval = get_settings().http.request_interval
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_request_time = time.monotonic()


def _http_get_gbk(url: str, timeout: int = 15) -> str:
    if not url.startswith(_BASE):
        raise ValueError(f"禁止访问非新浪域名: {url!r}")

    cfg = get_settings()
    last_error: Exception | None = None
    for attempt in range(cfg.http.max_retries):
        if attempt > 0:
            backoff = cfg.http.backoff_base ** attempt + random.uniform(0.2, 0.8)
            logger.warning(
                "[industry_reports] 抓取失败，第 %d 次重试，退避 %.1fs: %s",
                attempt,
                backoff,
                url,
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
        f"抓取新浪行业研报页失败，已重试 {cfg.http.max_retries} 次: {url}。最后错误: {last_error}"
    ) from last_error


def _cache_path_list(query_key: str) -> Path:
    digest = hashlib.sha1(query_key.encode("utf-8")).hexdigest()
    return get_settings().industry_reports_cache_dir / "list" / f"{digest}.json"


def _cache_path_detail(source_url: str) -> Path:
    digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()
    return get_settings().industry_reports_cache_dir / "detail" / f"{digest}.txt"


def _is_fresh(path: Path, ttl_secs: int) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < ttl_secs
    except FileNotFoundError:
        return False


def _write_cache(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_cache(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _make_absolute_url(href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return _BASE + href
    return urllib.parse.urljoin(_BASE + "/", href)


def _normalize_industry_tags(
    title: str,
    industry_sw_l2: str,
    keywords: list[str],
) -> list[str]:
    tags: list[str] = []
    if industry_sw_l2:
        tags.append(industry_sw_l2)
    for kw in keywords:
        if kw in title and kw not in tags:
            tags.append(kw)
    return tags


def _parse_report_list(
    html: str,
    industry_sw_l2: str,
    keywords: list[str],
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()

    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"vReport_Show"))
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not isinstance(href, str) or not href:
            continue
        source_url = _make_absolute_url(href)
        if source_url in seen:
            continue
        seen.add(source_url)

        row_text = tr.get_text(" ", strip=True)
        m = _DATE_RE.search(row_text)
        report_date = m.group(1) if m else ""

        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        publisher = ""
        for cell in tds:
            if any(k in cell for k in ("证券", "研究所", "投资", "基金", "银行", "国际")):
                publisher = cell
                break

        out.append(
            {
                "report_date": report_date,
                "publisher": publisher,
                "title": title,
                "industry_tags": _normalize_industry_tags(title, industry_sw_l2, keywords),
                "source_url": source_url,
                "summary": "",
            }
        )

    return out


def _extract_report_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        ("div", {"class": "blk_container"}),
        ("div", {"id": "content"}),
        ("div", {"class": "content"}),
    ]
    for name, attrs in selectors:
        el = soup.find(name, attrs=attrs)
        if el:
            txt = el.get_text(separator="\n", strip=True)
            if txt:
                return txt
    body = soup.find("body") or soup
    return body.get_text(separator="\n", strip=True)  # type: ignore[union-attr]


def fetch_industry_report_list(
    industry_sw_l2: str = "",
    keywords: list[str] | None = None,
    start_date: str = "",
    end_date: str = "",
    limit: int = 50,
    ts_code: str = "",
) -> list[dict]:
    if start_date:
        _validate_date(start_date, "start_date")
    if end_date:
        _validate_date(end_date, "end_date")

    keywords2 = _normalize_keywords(keywords)
    limit = max(1, min(200, int(limit)))

    # ts_code → sw2 自动路由
    resolved_sw2 = ""
    resolved_t1 = "33"
    if ts_code:
        resolved_sw2, resolved_t1 = _resolve_sw2_from_ts_code(ts_code)

    query_key = json.dumps(
        {
            "industry_sw_l2": industry_sw_l2,
            "keywords": keywords2,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
            "ts_code": ts_code,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_p = _cache_path_list(query_key)
    cfg = get_settings()
    if _is_fresh(cache_p, cfg.cache.list_cache_ttl):
        return json.loads(_read_cache(cache_p))

    all_entries: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(1, cfg.industry_reports.max_pages + 1):
        if resolved_sw2:
            # 使用个股所属申万行业搜索 URL
            base_search = f"{_BASE}{_SEARCH_PATH}?industry={resolved_sw2}&t1={resolved_t1}"
            url = base_search if page == 1 else base_search + f"&p={page}"
        elif page == 1:
            url = _BASE + _LIST_PATH
        else:
            url = _BASE + _LIST_PATH + f"?p={page}"

        html = _http_get_gbk(url)
        entries = _parse_report_list(html, industry_sw_l2, keywords2)
        if not entries:
            break

        for e in entries:
            if e["source_url"] in seen_urls:
                continue
            seen_urls.add(e["source_url"])

            d = e["report_date"]
            if end_date and d and d > end_date:
                continue
            if start_date and d and d < start_date:
                continue
            if industry_sw_l2 and industry_sw_l2 not in e["title"] and industry_sw_l2 not in " ".join(e["industry_tags"]):
                if keywords2:
                    if not any(kw in e["title"] for kw in keywords2):
                        continue
            elif keywords2 and not any(kw in e["title"] for kw in keywords2):
                continue

            # cache_key 仅用于内部追踪，不暴露给调用方（契约干净）
            e["_cache_key"] = hashlib.sha1(
                json.dumps(
                    {
                        "industry_sw_l2": industry_sw_l2,
                        "keywords": keywords2,
                        "report_date": e["report_date"],
                        "source_url": e["source_url"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            all_entries.append(e)

            if len(all_entries) >= limit:
                break
        if len(all_entries) >= limit:
            break

    # 返回前剔除内部字段，保持与需求文档 §3.1 契约一致
    for e in all_entries:
        e.pop("_cache_key", None)
    _write_cache(cache_p, json.dumps(all_entries, ensure_ascii=False))
    return all_entries


def fetch_report_text(source_url: str) -> str:
    source_url = _validate_report_url(source_url)
    cache_p = _cache_path_detail(source_url)
    if _is_fresh(cache_p, get_settings().cache.detail_cache_ttl):
        return _read_cache(cache_p)

    html = _http_get_gbk(source_url)
    text = _extract_report_text(html)
    _write_cache(cache_p, text)
    return text


def register_industry_report_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def list_industry_reports(
        industry_sw_l2: str = "",
        keywords: list[str] | None = None,
        start_date: str = "",
        end_date: str = "",
        limit: int = 50,
        ts_code: str = "",
    ) -> dict:
        """列出券商发布的行业研究报告（来源：新浪财经）。

        支持两种定位方式（任选其一）：
          1. 传入 ``ts_code`` 自动路由至该公司所属的申万二级行业
          2. 直接指定 ``industry_sw_l2``（行业名）与 ``keywords``（关键词过滤）

        默认返回近 1 年（365 天）内发布的研报；发布日期越界的条目会被过滤掉。

        Args:
            industry_sw_l2: 申万二级行业名，如 ``"汽车零部件"``、``"光伏设备"``
            keywords: 标题关键词白名单，任一命中即保留
            start_date: 起始日期（含），格式 YYYY-MM-DD；默认当日前推 365 天
            end_date: 结束日期（含），格式 YYYY-MM-DD；默认今日
            limit: 返回条数上限，范围 [1, 200]，默认 50
            ts_code: Tushare 格式股票代码，如 ``"603129.SH"``；若提供，则忽略
                     ``industry_sw_l2`` 参数并改用该公司所属申万二级行业

        Returns:
            dict:
              成功：{
                "industry_sw_l2": str,
                "source": "sina",
                "reports": [
                  {
                    "report_date":    str,        # 发布日期 YYYY-MM-DD
                    "publisher":      str,        # 发布机构/券商名称
                    "title":          str,        # 研报标题
                    "industry_tags":  list[str],  # 标注的申万行业标签
                    "source_url":     str,        # 详情页 URL
                    "summary":        str         # 来源页提供的简短摘要（可能为空）
                  }
                ]
              }
              失败：{"industry_sw_l2", "source": "unavailable",
                     "error", "fetch_time"}
        """
        today = date.today()
        if not end_date:
            end_date = today.isoformat()
        if not start_date:
            start_date = (today - timedelta(days=365)).isoformat()

        try:
            reports = fetch_industry_report_list(
                industry_sw_l2=industry_sw_l2,
                keywords=keywords,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                ts_code=ts_code,
            )
        except (ValueError, RuntimeError) as exc:
            return {
                "industry_sw_l2": industry_sw_l2,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": datetime.now(tz=timezone.utc).isoformat(),
            }

        return {
            "industry_sw_l2": industry_sw_l2,
            "source": "sina",
            "reports": reports,
        }

    @mcp.tool()
    def get_report_text(source_url: str) -> dict:
        """抓取单条行业研报全文正文（来源：新浪财经详情页）。

        Args:
            source_url: 由 ``list_industry_reports`` 返回条目的 ``source_url``

        Returns:
            dict:
              成功：{
                "source_url":   str,
                "full_text":    str,   # 研报正文纯文本
                "extracted_at": str    # ISO8601 UTC 时间戳
              }
              失败：同上结构，``full_text`` 为 ``""`` 且附带 ``error`` 字段

        Notes:
            本工具不抛异常；所有错误经由返回字典的 ``error`` 字段传递。
        """
        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            text = fetch_report_text(source_url)
        except (ValueError, RuntimeError) as exc:
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
