"""tests/test_announcements.py — list_announcements / get_announcement_text 单元测试"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import nano_search_mcp.tools.announcements as ann_mod
from nano_search_mcp.config import CacheSettings, Settings
from nano_search_mcp.tools.announcements import (
    _classify_ann_type,
    _extract_detail_text,
    _parse_announcement_list,
    _strip_market_suffix,
    _validate_detail_url,
    _validate_stockid,
    fetch_announcement_list,
    fetch_announcement_text,
    register_announcement_tools,
)

# ── 测试 HTML 固件 ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_cache_settings(tmp_path):
    """将缓存路由到 tmp_path，实现测试隔离。"""
    settings = Settings(cache=CacheSettings(cache_dir=str(tmp_path)))
    with patch("nano_search_mcp.tools.announcements.get_settings", return_value=settings):
        yield


_LIST_HTML = """\
<html><body>
<div class="datelist"><ul>
2026-04-18 <a href="/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9001" target="_blank">关于收到行政处罚事先告知书的公告</a><br/>
2026-01-10 <a href="/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9002" target="_blank">2025年年度业绩预告</a><br/>
2025-12-27 <a href="/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9003" target="_blank">关于收到中国证监会立案告知书的公告</a><br/>
2025-06-01 <a href="/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9004" target="_blank">关于聘请会计师事务所的公告</a><br/>
</ul></div>
</body></html>
"""

_LIST_HTML_WITH_NEXT = """\
<html><body>
<div class="datelist"><ul>
2026-04-18 <a href="/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9001" target="_blank">关于收到行政处罚事先告知书的公告</a><br/>
</ul></div>
<a href="/corp/go.php/vCB_AllBulletin/stockid/688270/page/2.phtml">下一页</a>
</body></html>
"""

_LIST_HTML_P2 = """\
<html><body>
<div class="datelist"><ul>
2025-06-01 <a href="/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9002" target="_blank">关于董事会决议公告</a><br/>
</ul></div>
</body></html>
"""

_DETAIL_HTML = """\
<html><body>
<div id="con02-7">
<div id="content">
  浙江臻镭科技股份有限公司关于收到立案告知书的公告

  本公司董事会及全体董事保证本公告内容不存在任何虚假记载。
  公司于2025年12月26日收到中国证券监督管理委员会出具的《立案告知书》。
</div>
</div>
</body></html>
"""


# ── 辅助 ────────────────────────────────────────────────

def _make_tool_fn(name: str) -> Any:
    """从 FastMCP 注册的工具中取出函数。"""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test-ann")
    register_announcement_tools(mcp)
    for t in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]
        if t.name == name:
            return t.fn
    raise AssertionError(f"Tool '{name}' not found")


# ── 输入校验 ─────────────────────────────────────────────

def test_validate_stockid_ok() -> None:
    assert _validate_stockid("688270") == "688270"


@pytest.mark.parametrize("bad", ["12345", "1234567", "abc123", "12345.SH", ""])
def test_validate_stockid_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        _validate_stockid(bad)


def test_strip_market_suffix_variants() -> None:
    assert _strip_market_suffix("688270.SH") == "688270"
    assert _strip_market_suffix("300750.SZ") == "300750"
    assert _strip_market_suffix("430047.BJ") == "430047"


def test_validate_detail_url_ok() -> None:
    url = "http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=12345"
    assert _validate_detail_url(url) == url


def test_validate_detail_url_https() -> None:
    url = "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300750&id=99999"
    assert _validate_detail_url(url) == url


@pytest.mark.parametrize("bad_url", [
    "https://evil.com/steal?stockid=688270&id=1",
    "http://vip.stock.finance.sina.com.cn/other/path?stockid=688270&id=1",
    "ftp://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=1",
    "",
])
def test_validate_detail_url_bad(bad_url: str) -> None:
    with pytest.raises(ValueError):
        _validate_detail_url(bad_url)


# ── ann_type 分类 ─────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("关于收到上海证券交易所问询函的公告", "inquiry"),
    ("关于收到监管工作函的公告", "inquiry"),
    ("天健会计师事务所关于差错更正情况的鉴证报告", "audit"),
    ("关于聘请会计师事务所的公告", "accountant_change"),
    ("关于收到行政处罚事先告知书的公告", "penalty"),
    ("关于前期会计差错更正的公告", "restatement"),
    ("关于重大诉讼进展的公告", "litigation"),
    ("关于2025年第三季度报告的公告", "other"),
    ("关于回购股份实施结果暨股份变动的公告", "other"),
])
def test_classify_ann_type(title: str, expected: str) -> None:
    assert _classify_ann_type(title) == expected


# ── HTML 解析 ─────────────────────────────────────────────

def test_parse_announcement_list_basic() -> None:
    entries = _parse_announcement_list(_LIST_HTML, "688270")
    assert len(entries) == 4
    assert entries[0]["ann_date"] == "2026-04-18"
    assert entries[0]["ann_type"] == "penalty"   # 行政处罚 → penalty
    assert "id=9001" in entries[0]["source_url"]
    assert entries[0]["pdf_url"] is None


def test_parse_announcement_list_accountant_change() -> None:
    entries = _parse_announcement_list(_LIST_HTML, "688270")
    # 第 4 条：关于聘请会计师事务所
    accountant_entry = next(e for e in entries if "id=9004" in e["source_url"])
    assert accountant_entry["ann_type"] == "accountant_change"


def test_parse_announcement_list_empty_datelist() -> None:
    html = "<html><body><p>no datelist</p></body></html>"
    entries = _parse_announcement_list(html, "688270")
    assert entries == []


def test_extract_detail_text_content_div() -> None:
    text = _extract_detail_text(_DETAIL_HTML)
    assert "立案告知书" in text
    assert "证券监督管理委员会" in text


# ── fetch_announcement_list（mock HTTP）────────────────────

@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_fetch_list_date_filter(mock_get: MagicMock, tmp_path: Path) -> None:
    """只返回 start_date ~ end_date 范围内的公告。"""
    mock_get.return_value = _LIST_HTML


    entries = fetch_announcement_list("688270", "2026-01-01", "2026-12-31")
    dates = [e["ann_date"] for e in entries]
    assert all(d >= "2026-01-01" and d <= "2026-12-31" for d in dates)
    # 2025 年的条目不应出现
    assert not any(d.startswith("2025") for d in dates)


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_fetch_list_no_filter_returns_all(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.return_value = _LIST_HTML


    entries = fetch_announcement_list("688270", "2025-01-01", "2026-12-31")
    assert len(entries) == 4


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_fetch_list_pagination(mock_get: MagicMock, tmp_path: Path) -> None:
    """列表页有"下一页"时自动翻页。"""
    mock_get.side_effect = [_LIST_HTML_WITH_NEXT, _LIST_HTML_P2]


    entries = fetch_announcement_list("688270", "2025-01-01", "2026-12-31")
    assert len(entries) == 2
    assert mock_get.call_count == 2


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_fetch_list_cache_hit(mock_get: MagicMock, tmp_path: Path) -> None:
    """第二次调用命中缓存，不发起 HTTP 请求。"""
    mock_get.return_value = _LIST_HTML


    fetch_announcement_list("688270", "2025-01-01", "2026-12-31")
    fetch_announcement_list("688270", "2025-01-01", "2026-12-31")
    assert mock_get.call_count == 1  # 第二次命中缓存


# ── fetch_announcement_text（mock HTTP）───────────────────

_VALID_URL = (
    "http://vip.stock.finance.sina.com.cn"
    "/corp/view/vCB_AllBulletinDetail.php?stockid=688270&id=9003"
)


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_fetch_detail_returns_text(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.return_value = _DETAIL_HTML


    text = fetch_announcement_text(_VALID_URL)
    assert "立案告知书" in text


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_fetch_detail_cache_hit(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.return_value = _DETAIL_HTML


    fetch_announcement_text(_VALID_URL)
    fetch_announcement_text(_VALID_URL)
    assert mock_get.call_count == 1


def test_fetch_detail_bad_url() -> None:
    with pytest.raises(ValueError):
        fetch_announcement_text("https://evil.com/steal?stockid=688270&id=1")


# ── MCP 工具函数（注册后调用）────────────────────────────

@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_list_announcements_tool_success(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.return_value = _LIST_HTML


    fn = _make_tool_fn("list_announcements")
    result = fn(ts_code="688270.SH", start_date="2025-01-01", end_date="2026-12-31")

    assert result["source"] == "sina"
    assert result["ts_code"] == "688270.SH"
    assert len(result["announcements"]) == 4


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_list_announcements_ann_types_filter(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.return_value = _LIST_HTML


    fn = _make_tool_fn("list_announcements")
    result = fn(
        ts_code="688270.SH",
        start_date="2025-01-01",
        end_date="2026-12-31",
        ann_types=["penalty"],
    )
    assert result["source"] == "sina"
    assert all(a["ann_type"] == "penalty" for a in result["announcements"])


def test_list_announcements_invalid_ts_code() -> None:
    fn = _make_tool_fn("list_announcements")
    result = fn(ts_code="INVALID", start_date="2025-01-01", end_date="2026-12-31")
    assert result["source"] == "unavailable"
    assert "error" in result


def test_list_announcements_unknown_ann_type() -> None:
    fn = _make_tool_fn("list_announcements")
    result = fn(ts_code="688270.SH", ann_types=["unknown_type"])
    assert result["source"] == "unavailable"
    assert "error" in result


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_list_announcements_network_error(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.side_effect = RuntimeError("Connection refused")


    fn = _make_tool_fn("list_announcements")
    result = fn(ts_code="688270.SH", start_date="2026-01-01", end_date="2026-12-31")
    assert result["source"] == "unavailable"
    assert "error" in result


@patch("nano_search_mcp.tools.announcements._http_get_gbk")
def test_get_announcement_text_tool_success(mock_get: MagicMock, tmp_path: Path) -> None:
    mock_get.return_value = _DETAIL_HTML


    fn = _make_tool_fn("get_announcement_text")
    result = fn(source_url=_VALID_URL)

    assert "立案告知书" in result["full_text"]
    assert result["extracted_at"] != ""
    assert result["source_url"] == _VALID_URL


def test_get_announcement_text_tool_bad_url() -> None:
    fn = _make_tool_fn("get_announcement_text")
    result = fn(source_url="https://evil.com/steal")
    assert result["full_text"] == ""
    assert "error" in result
