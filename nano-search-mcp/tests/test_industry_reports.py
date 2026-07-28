from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import nano_search_mcp.tools.industry_reports as ir_mod
from nano_search_mcp.config import CacheSettings, Settings
from nano_search_mcp.tools.industry_reports import (
    _extract_report_text,
    _normalize_keywords,
    _parse_report_list,
    _validate_date,
    _validate_report_url,
    fetch_industry_report_list,
    fetch_report_text,
    register_industry_report_tools,
)

@pytest.fixture(autouse=True)
def _mock_cache_settings(tmp_path):
    """将缓存路由到 tmp_path，实现测试隔离。"""
    settings = Settings(cache=CacheSettings(cache_dir=str(tmp_path)))
    with patch("nano_search_mcp.tools.industry_reports.get_settings", return_value=settings):
        yield


_LIST_HTML = """\
<html><body>
<table>
<tr>
  <td>2026-04-18</td>
  <td>中信证券</td>
  <td><a href="/stock/go.php/vReport_Show/kind/industry/rptid/1001/index.phtml">汽车玻璃行业深度报告</a></td>
</tr>
<tr>
  <td>2026-03-10</td>
  <td>国泰君安证券</td>
  <td><a href="/stock/go.php/vReport_Show/kind/industry/rptid/1002/index.phtml">新能源设备景气跟踪</a></td>
</tr>
</table>
</body></html>
"""

_DETAIL_HTML = """\
<html><body>
<div class="blk_container">
  <p>行业观点：供需结构持续改善。</p>
  <p>重点公司估值处于历史中位。</p>
</div>
</body></html>
"""

_VALID_URL = (
    "https://stock.finance.sina.com.cn/stock/go.php/"
    "vReport_Show/kind/industry/rptid/1001/index.phtml"
)


def _get_tool_fn(name: str):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-industry")
    register_industry_report_tools(mcp)
    for t in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]
        if t.name == name:
            return t.fn
    raise AssertionError(f"tool not found: {name}")


def test_validate_date_ok() -> None:
    assert _validate_date("2026-04-22", "start_date") == "2026-04-22"


@pytest.mark.parametrize("bad", ["2026/04/22", "2026-4-2", "", "abc"])
def test_validate_date_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        _validate_date(bad, "start_date")


def test_validate_report_url_ok() -> None:
    assert _validate_report_url(_VALID_URL) == _VALID_URL


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.com/stock/go.php/vReport_Show/kind/industry/rptid/1/index.phtml",
        "https://stock.finance.sina.com.cn/other/path",
        "",
    ],
)
def test_validate_report_url_bad(bad_url: str) -> None:
    with pytest.raises(ValueError):
        _validate_report_url(bad_url)


def test_normalize_keywords() -> None:
    assert _normalize_keywords([" 汽车 ", "汽车", " ", "玻璃"]) == ["汽车", "玻璃"]


def test_parse_report_list_basic() -> None:
    rows = _parse_report_list(_LIST_HTML, "汽车零部件", ["玻璃"])
    assert len(rows) == 2
    assert rows[0]["report_date"] == "2026-04-18"
    assert rows[0]["publisher"] == "中信证券"
    assert rows[0]["title"] == "汽车玻璃行业深度报告"
    assert rows[0]["source_url"].startswith("https://stock.finance.sina.com.cn/stock/go.php/vReport_Show")
    assert "汽车零部件" in rows[0]["industry_tags"]


def test_extract_report_text() -> None:
    text = _extract_report_text(_DETAIL_HTML)
    assert "供需结构持续改善" in text


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_fetch_industry_report_list_date_filter(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _LIST_HTML

    rows = fetch_industry_report_list(
        industry_sw_l2="",
        keywords=None,
        start_date="2026-04-01",
        end_date="2026-04-30",
        limit=50,
    )
    assert len(rows) == 1
    assert rows[0]["report_date"] == "2026-04-18"


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_fetch_industry_report_list_limit(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _LIST_HTML

    rows = fetch_industry_report_list(limit=1)
    assert len(rows) == 1


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_fetch_industry_report_list_cache_hit(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _LIST_HTML

    fetch_industry_report_list(limit=2)
    fetch_industry_report_list(limit=2)
    assert mock_get.call_count == 1


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_fetch_report_text_ok(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _DETAIL_HTML

    text = fetch_report_text(_VALID_URL)
    assert "行业观点" in text


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_fetch_report_text_cache_hit(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _DETAIL_HTML

    fetch_report_text(_VALID_URL)
    fetch_report_text(_VALID_URL)
    assert mock_get.call_count == 1


def test_fetch_report_text_bad_url() -> None:
    with pytest.raises(ValueError):
        fetch_report_text("https://evil.com/a")


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_list_industry_reports_tool_success(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _LIST_HTML

    fn = _get_tool_fn("list_industry_reports")
    result = fn(industry_sw_l2="汽车零部件", keywords=["玻璃"], start_date="2026-01-01", end_date="2026-12-31", limit=10)
    assert result["source"] == "sina"
    assert len(result["reports"]) >= 1


def test_list_industry_reports_tool_invalid_date() -> None:
    fn = _get_tool_fn("list_industry_reports")
    result = fn(start_date="2026/01/01")
    assert result["source"] == "unavailable"
    assert "error" in result


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_list_industry_reports_tool_network_error(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.side_effect = RuntimeError("network down")

    fn = _get_tool_fn("list_industry_reports")
    result = fn(limit=5)
    assert result["source"] == "unavailable"
    assert "error" in result


@patch("nano_search_mcp.tools.industry_reports._http_get_gbk")
def test_get_report_text_tool_success(mock_get: MagicMock, tmp_path: Path) -> None:

    mock_get.return_value = _DETAIL_HTML

    fn = _get_tool_fn("get_report_text")
    result = fn(source_url=_VALID_URL)
    assert "行业观点" in result["full_text"]
    assert result["extracted_at"] != ""


def test_get_report_text_tool_bad_url() -> None:
    fn = _get_tool_fn("get_report_text")
    result = fn(source_url="https://evil.com/report")
    assert result["full_text"] == ""
    assert "error" in result
