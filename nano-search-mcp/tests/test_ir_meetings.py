"""IR 会议/调研纪要工具单元测试。"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from nano_search_mcp.config import CacheSettings, Settings
from nano_search_mcp.tools.ir_meetings import (
    _classify_meeting_type,
    _extract_participants,
    _is_ir_title,
    _parse_ir_list,
    _strip_market_suffix,
    _validate_date,
    _validate_detail_url,
    _validate_meeting_types,
    _validate_stockid,
    fetch_ir_meeting_list,
    fetch_ir_meeting_text,
    register_ir_meeting_tools,
)


@pytest.fixture(autouse=True)
def _mock_cache_settings(tmp_path):
    """将缓存路由到 tmp_path，实现测试隔离。"""
    settings = Settings(cache=CacheSettings(cache_dir=str(tmp_path)))
    with patch("nano_search_mcp.tools.ir_meetings.get_settings", return_value=settings):
        yield


# ─────────────────────────────────────────────────────────
# 5.1 输入校验
# ─────────────────────────────────────────────────────────

def test_validate_stockid_valid():
    assert _validate_stockid("000001") == "000001"
    assert _validate_stockid("688270") == "688270"


def test_validate_stockid_invalid():
    with pytest.raises(ValueError):
        _validate_stockid("00000X")
    with pytest.raises(ValueError):
        _validate_stockid("12345")   # 5 位
    with pytest.raises(ValueError):
        _validate_stockid("")


def test_strip_market_suffix():
    assert _strip_market_suffix("000001.SZ") == "000001"
    assert _strip_market_suffix("688270.SH") == "688270"


def test_validate_date_valid():
    assert _validate_date("2026-04-22", "start_date") == "2026-04-22"


def test_validate_date_invalid():
    with pytest.raises(ValueError):
        _validate_date("2026/04/22", "start_date")
    with pytest.raises(ValueError):
        _validate_date("20260422", "start_date")


def test_validate_detail_url_valid():
    url = "http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=000001&id=12345678"
    assert _validate_detail_url(url) == url


def test_validate_detail_url_invalid_domain():
    with pytest.raises(ValueError):
        _validate_detail_url("http://evil.com/corp/view/vCB_AllBulletinDetail.php?stockid=000001&id=123")


def test_validate_detail_url_invalid_stockid():
    with pytest.raises(ValueError):
        _validate_detail_url(
            "http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=EVIL&id=123"
        )


def test_validate_meeting_types_valid():
    assert _validate_meeting_types(["research", "earnings_call"]) == ["research", "earnings_call"]


def test_validate_meeting_types_invalid():
    with pytest.raises(ValueError):
        _validate_meeting_types(["unknown_type"])


# ─────────────────────────────────────────────────────────
# 5.2 IR 标题过滤
# ─────────────────────────────────────────────────────────

def test_is_ir_title_record():
    assert _is_ir_title("北部湾港：2026年4月16日投资者关系活动记录表") is True


def test_is_ir_title_management_info():
    assert _is_ir_title("平安银行：投资者关系管理信息") is True


def test_is_ir_title_earnings_call():
    assert _is_ir_title("某公司：网上业绩说明会") is True


def test_is_ir_title_investor_meeting():
    assert _is_ir_title("某公司：投资者说明会") is True


def test_is_ir_title_non_ir():
    assert _is_ir_title("某公司：2025年年度报告") is False
    assert _is_ir_title("某公司：关于收购资产的公告") is False
    assert _is_ir_title("某公司：问询函回复") is False


# ─────────────────────────────────────────────────────────
# 5.3 meeting_type 分类
# ─────────────────────────────────────────────────────────

def test_classify_earnings_call():
    assert _classify_meeting_type("网上业绩说明会投资者交流") == "earnings_call"
    assert _classify_meeting_type("2026年业绩交流会") == "earnings_call"


def test_classify_site_visit():
    assert _classify_meeting_type("机构投资者实地调研记录") == "site_visit"


def test_classify_research():
    assert _classify_meeting_type("投资者关系活动记录表（机构调研）") == "research"
    assert _classify_meeting_type("投资者关系管理信息2026Q1") == "research"


def test_classify_other():
    # 纯无关标题不匹配任何规则
    assert _classify_meeting_type("某公司：临时公告关于重大事项") == "other"


# ─────────────────────────────────────────────────────────
# 5.4 参与机构提取
# ─────────────────────────────────────────────────────────

def test_extract_participants_normal():
    text = "接待机构：中信证券、高瓴资本、易方达基金\n其余内容..."
    participants = _extract_participants(text)
    assert "中信证券" in participants
    assert "高瓴资本" in participants
    assert "易方达基金" in participants


def test_extract_participants_empty():
    text = "本次调研无机构参与，由个人投资者参加。"
    # 无标准格式，返回空列表
    result = _extract_participants(text)
    assert isinstance(result, list)


def test_extract_participants_dedup():
    text = "参会机构：中信证券、中信证券、招商证券"
    participants = _extract_participants(text)
    assert participants.count("中信证券") == 1


# ─────────────────────────────────────────────────────────
# 5.5 HTML 解析
# ─────────────────────────────────────────────────────────

def _make_list_html(entries: list[tuple[str, str, str]]) -> str:
    """构造仿新浪公告列表页 HTML（date, url_id, title）。"""
    lis = ""
    for date_str, url_id, title in entries:
        lis += (
            f'<li>{date_str}&nbsp;<a target=\'_blank\' '
            f'href=\'/corp/view/vCB_AllBulletinDetail.php?stockid=000582&id={url_id}\'>'
            f'{title}</a><br/></li>'
        )
    return f'<div class="datelist"><ul>{lis}</ul></div>'


def test_parse_ir_list_normal():
    html = _make_list_html([
        ("2026-04-16", "12102714", "北部湾港：2026年4月16日投资者关系活动记录表"),
        ("2026-04-11", "12076255", "北部湾港：保荐工作报告"),  # 非 IR，应被过滤
        ("2026-03-10", "11900001", "北部湾港：投资者关系管理信息"),
    ])
    results = _parse_ir_list(html, "000582")
    assert len(results) == 2
    assert results[0]["meeting_date"] == "2026-04-16"
    assert results[0]["meeting_type"] == "research"
    assert "vCB_AllBulletinDetail" in results[0]["source_url"]


def test_parse_ir_list_empty_datelist():
    html = "<html><body><p>No data</p></body></html>"
    results = _parse_ir_list(html, "000582")
    assert results == []


def test_parse_ir_list_all_filtered():
    html = _make_list_html([
        ("2026-04-11", "12076255", "北部湾港：保荐工作报告"),
        ("2026-04-10", "12076000", "北部湾港：年度报告摘要"),
    ])
    results = _parse_ir_list(html, "000582")
    assert results == []


# ─────────────────────────────────────────────────────────
# 5.6 缓存命中
# ─────────────────────────────────────────────────────────

def test_fetch_ir_meeting_list_cache_hit(tmp_path):
    """缓存新鲜时不发出 HTTP 请求。"""
    stockid = "000582"
    cache_entries = [
        {
            "meeting_date": "2026-04-16",
            "title": "北部湾港：投资者关系活动记录表",
            "meeting_type": "research",
            "source_url": "http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=000582&id=12102714",
            "participants": [],
            "summary": "",
        }
    ]
    cache_dir = tmp_path / "ir_meetings"
    cache_dir.mkdir()
    cache_file = cache_dir / f"{stockid}_p1.json"
    cache_file.write_text(
        json.dumps({"entries": cache_entries, "oldest_date": "2026-04-16", "has_next": False}),
        encoding="utf-8",
    )
    with patch("nano_search_mcp.tools.ir_meetings._http_get_gbk") as mock_http:
        results = fetch_ir_meeting_list(stockid, "2026-01-01", "2026-12-31")
    mock_http.assert_not_called()
    assert len(results) == 1
    assert results[0]["meeting_date"] == "2026-04-16"


# ─────────────────────────────────────────────────────────
# 5.7 网络错误处理
# ─────────────────────────────────────────────────────────

def test_fetch_ir_meeting_list_network_error(tmp_path):
    """HTTP 失败时抛出 RuntimeError。"""
    with patch("nano_search_mcp.tools.ir_meetings._http_get_gbk",
              side_effect=RuntimeError("连接超时")):
        with pytest.raises(RuntimeError, match="连接超时"):
            fetch_ir_meeting_list("000582")


# ─────────────────────────────────────────────────────────
# 5.8 非法 ts_code
# ─────────────────────────────────────────────────────────

def test_fetch_ir_meeting_list_invalid_stockid():
    with pytest.raises(ValueError, match="6 位纯数字"):
        fetch_ir_meeting_list("INVALID")


# ─────────────────────────────────────────────────────────
# 5.9 MCP 工具包装
# ─────────────────────────────────────────────────────────

def test_mcp_list_ir_meetings_invalid_ts_code():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP(name="test")
    register_ir_meeting_tools(mcp)
    tool = next(t for t in mcp._tool_manager._tools.values() if t.name == "list_ir_meetings")
    result = tool.fn(ts_code="INVALID")
    assert result["source"] == "unavailable"
    assert "error" in result


def test_mcp_list_ir_meetings_invalid_meeting_type():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP(name="test")
    register_ir_meeting_tools(mcp)
    tool = next(t for t in mcp._tool_manager._tools.values() if t.name == "list_ir_meetings")
    result = tool.fn(ts_code="000001.SZ", meeting_types=["unknown_type"])
    assert result["source"] == "unavailable"
    assert "error" in result


def test_mcp_get_ir_meeting_text_invalid_url():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP(name="test")
    register_ir_meeting_tools(mcp)
    tool = next(t for t in mcp._tool_manager._tools.values() if t.name == "get_ir_meeting_text")
    result = tool.fn(source_url="http://evil.com/malicious")
    assert result["full_text"] == ""
    assert "error" in result
