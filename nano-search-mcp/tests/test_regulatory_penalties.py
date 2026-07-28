"""监管处罚工具单元测试。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from nano_search_mcp.tools.regulatory_penalties import (
    _apply_date_filter,
    _extract_issuer,
    _extract_reason,
    _new_record,
    _parse_penalty_list,
    _strip_market_suffix,
    _validate_date,
    _validate_stockid,
    fetch_penalty_list,
    register_regulatory_penalty_tools,
)


# ─────────────────────────────────────────────────────────
# 4.1-4.5 输入校验
# ─────────────────────────────────────────────────────────

def test_validate_stockid_valid():
    assert _validate_stockid("688270") == "688270"
    assert _validate_stockid("000001") == "000001"


def test_validate_stockid_invalid():
    with pytest.raises(ValueError):
        _validate_stockid("68827X")
    with pytest.raises(ValueError):
        _validate_stockid("68827")   # 5 位
    with pytest.raises(ValueError):
        _validate_stockid("6882701")  # 7 位
    with pytest.raises(ValueError):
        _validate_stockid("")


def test_strip_market_suffix():
    assert _strip_market_suffix("688270.SH") == "688270"
    assert _strip_market_suffix("000001.SZ") == "000001"
    assert _strip_market_suffix("300750.SZ") == "300750"


def test_validate_date_valid():
    assert _validate_date("2026-04-22", "start_date") == "2026-04-22"
    assert _validate_date("2024-01-01", "end_date") == "2024-01-01"


def test_validate_date_invalid():
    with pytest.raises(ValueError):
        _validate_date("2026/04/22", "start_date")
    with pytest.raises(ValueError):
        _validate_date("20260422", "start_date")
    with pytest.raises(ValueError):
        _validate_date("2026-4-1", "start_date")


# ─────────────────────────────────────────────────────────
# 4.6-4.11 issuer / reason 提取（基于 th 文本和 issuer 字段）
# ─────────────────────────────────────────────────────────

def test_extract_issuer_csrc():
    assert _extract_issuer("中国证券监督管理委员会浙江监管局") == "浙江证监局"


def test_extract_issuer_sse():
    assert _extract_issuer("上海证券交易所") == "上交所"


def test_extract_issuer_szse():
    assert _extract_issuer("深圳证券交易所") == "深交所"


def test_extract_issuer_bse():
    assert _extract_issuer("北京证券交易所") == "北交所"


def test_extract_issuer_unknown():
    assert _extract_issuer("某未知监管机构") == "unknown"


def test_extract_reason_disclosure():
    assert _extract_reason("公司因信息披露违规被处罚") == "信息披露违规"


def test_extract_reason_unknown():
    assert _extract_reason("董事长因个人原因辞职") == "unknown"


# ─────────────────────────────────────────────────────────
# 4.12-4.13 HTML 解析
# ─────────────────────────────────────────────────────────

_MOCK_HTML = """
<html><body>
<table id="collectFund_1">
  <thead><tr><th colspan="2"><a name="2026-04-18-1"></a>处罚决定  公告日期:2026-04-18</th></tr></thead>
  <tr><td><strong>标题</strong></td><td>关于收到行政处罚事先告知书的公告</td></tr>
  <tr><td><strong>批复原因</strong></td><td>信息披露违规</td></tr>
  <tr><td><strong>批复内容</strong></td><td>责令改正，警告并处罚款50万元</td></tr>
  <tr><td><strong>处理人</strong></td><td>中国证券监督管理委员会浙江监管局</td></tr>
  <tr><td colspan="2"></td></tr>
  <thead><tr><th colspan="2"><a name="2025-03-10-1"></a>立案调查  公告日期:2025-03-10</th></tr></thead>
  <tr><td><strong>标题</strong></td><td>收到立案告知书</td></tr>
  <tr><td><strong>批复原因</strong></td><td>涉嫌内幕交易</td></tr>
  <tr><td><strong>批复内容</strong></td><td>依法对公司进行立案调查</td></tr>
  <tr><td><strong>处理人</strong></td><td>中国证券监督管理委员会</td></tr>
</table>
</body></html>
"""

_MOCK_HTML_EMPTY = """
<html><body>
<table id="collectFund_1">
</table>
</body></html>
"""

_MOCK_HTML_NO_TABLE = "<html><body><p>无数据</p></body></html>"


def test_parse_penalty_list_normal():
    records = _parse_penalty_list(_MOCK_HTML, "https://vip.stock.finance.sina.com.cn/test")
    assert len(records) == 2
    r0 = records[0]
    assert r0["punish_date"] == "2026-04-18"
    assert r0["event_type"] == "处罚决定"
    assert r0["title"] == "关于收到行政处罚事先告知书的公告"
    assert r0["reason"] == "信息披露违规"
    assert r0["content"] == "责令改正，警告并处罚款50万元"
    assert r0["issuer"] == "浙江证监局"


def test_parse_penalty_list_empty():
    records = _parse_penalty_list(_MOCK_HTML_EMPTY, "https://vip.stock.finance.sina.com.cn/test")
    assert records == []


def test_parse_penalty_list_no_table():
    records = _parse_penalty_list(_MOCK_HTML_NO_TABLE, "https://vip.stock.finance.sina.com.cn/test")
    assert records == []


# ─────────────────────────────────────────────────────────
# 4.14 日期过滤
# ─────────────────────────────────────────────────────────

def test_fetch_penalty_list_date_filter():
    records = [
        {"punish_date": "2026-04-18", "title": "A"},
        {"punish_date": "2025-03-10", "title": "B"},
        {"punish_date": "2024-01-05", "title": "C"},
    ]
    result = _apply_date_filter(records, "2025-01-01", "2025-12-31")
    assert len(result) == 1
    assert result[0]["title"] == "B"


def test_apply_date_filter_no_bounds():
    records = [{"punish_date": "2026-01-01"}, {"punish_date": "2024-06-01"}]
    assert len(_apply_date_filter(records, None, None)) == 2


def test_apply_date_filter_missing_date():
    records = [{"punish_date": ""}, {"punish_date": "2024-01-01"}]
    result = _apply_date_filter(records, "2025-01-01", "2025-12-31")
    # 无日期的记录始终保留
    assert any(r["punish_date"] == "" for r in result)


# ─────────────────────────────────────────────────────────
# 4.15 缓存命中
# ─────────────────────────────────────────────────────────

def test_fetch_penalty_list_cache_hit(tmp_path):
    cached_data = [
        {"punish_date": "2026-04-18", "event_type": "处罚决定",
         "title": "cached", "reason": "", "content": "", "issuer": "", "source_url": ""}
    ]
    cache_file = tmp_path / "688270.json"
    cache_file.write_text(json.dumps(cached_data), encoding="utf-8")

    with (
        patch("nano_search_mcp.tools.regulatory_penalties._cache_path", return_value=cache_file),
        patch("nano_search_mcp.tools.regulatory_penalties._is_fresh", return_value=True),
        patch("nano_search_mcp.tools.regulatory_penalties._http_get_gbk") as mock_http,
    ):
        result = fetch_penalty_list("688270.SH")

    mock_http.assert_not_called()
    assert result["source"] == "sina"
    assert result["penalties"][0]["title"] == "cached"


# ─────────────────────────────────────────────────────────
# 4.16 网络失败降级
# ─────────────────────────────────────────────────────────

def test_fetch_penalty_list_network_error():
    with (
        patch("nano_search_mcp.tools.regulatory_penalties._is_fresh", return_value=False),
        patch("nano_search_mcp.tools.regulatory_penalties._http_get_gbk",
              side_effect=RuntimeError("HTTP 500")),
    ):
        result = fetch_penalty_list("688270.SH")

    assert result["source"] == "unavailable"
    assert "error" in result


# ─────────────────────────────────────────────────────────
# 4.17 非法 ts_code
# ─────────────────────────────────────────────────────────

def test_fetch_penalty_list_invalid_ts_code():
    result = fetch_penalty_list("INVALID")
    assert result["source"] == "unavailable"
    assert "error" in result


def test_fetch_penalty_list_empty_ts_code():
    result = fetch_penalty_list("")
    assert result["source"] == "unavailable"
    assert "error" in result


# ─────────────────────────────────────────────────────────
# 4.18-4.19 MCP 工具包装
# ─────────────────────────────────────────────────────────

def test_mcp_tool_list_penalties_success():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    register_regulatory_penalty_tools(mcp)

    mock_result = {
        "ts_code": "688270.SH",
        "source": "sina",
        "penalties": [
            {"punish_date": "2026-04-18", "event_type": "处罚决定",
             "title": "测试处罚", "reason": "", "content": "", "issuer": "", "source_url": ""}
        ],
    }
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    fn = tools["list_regulatory_penalties"]

    with patch(
        "nano_search_mcp.tools.regulatory_penalties.fetch_penalty_list",
        return_value=mock_result,
    ):
        result = fn(ts_code="688270.SH", start_date="2026-01-01", end_date="2026-12-31")

    assert result["source"] == "sina"
    assert len(result["penalties"]) == 1


def test_mcp_tool_list_penalties_error():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    register_regulatory_penalty_tools(mcp)

    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}
    fn = tools["list_regulatory_penalties"]

    with (
        patch("nano_search_mcp.tools.regulatory_penalties._is_fresh", return_value=False),
        patch("nano_search_mcp.tools.regulatory_penalties._http_get_gbk",
              side_effect=RuntimeError("网络错误")),
    ):
        result = fn(ts_code="688270.SH")

    assert result["source"] == "unavailable"
    assert "error" in result
