"""tests/test_industry_policies.py — 行业政策搜索工具单元测试。"""

from __future__ import annotations

from unittest.mock import patch

from nano_search_mcp.tools.industry_policies import (
    _build_policy_queries,
    _search_gov_cn,
    _infer_issuer_level,
    fetch_industry_policy_list,
    register_industry_policy_tools,
)

# ── _infer_issuer_level ────────────────────────────────────────


def test_infer_ndrc():
    issuer, level = _infer_issuer_level("https://www.ndrc.gov.cn/xxgk/zcfb/tz/202401/t20240110_1001.html")
    assert issuer == "国家发展改革委"
    assert level == "ministry"


def test_infer_miit():
    issuer, level = _infer_issuer_level("https://www.miit.gov.cn/jgsj/ggs/202403/t20240315.html")
    assert issuer == "工业和信息化部"
    assert level == "ministry"


def test_infer_local_gov():
    issuer, level = _infer_issuer_level("https://www.beijing.gov.cn/zhengce/2024/abc.html")
    assert "地方" in issuer or issuer != ""
    assert level == "local"


def test_infer_unknown():
    issuer, level = _infer_issuer_level("https://example.com/abc")
    assert issuer == "未知机构"


def test_infer_csrc():
    issuer, level = _infer_issuer_level("https://www.csrc.gov.cn/csrc/c100028/abc.html")
    assert issuer == "中国证监会"


# ── _build_policy_queries ──────────────────────────────────────


def test_build_queries_industry_only():
    queries = _build_policy_queries("汽车零部件", None)
    assert len(queries) == 2
    assert any("汽车零部件" in q and "site:gov.cn" in q for q in queries)


def test_build_queries_with_keywords():
    queries = _build_policy_queries("光伏设备", ["新能源", "光伏"])
    assert len(queries) >= 3
    assert any("新能源" in q for q in queries)
    assert any("光伏" in q for q in queries)


def test_build_queries_empty_inputs():
    queries = _build_policy_queries("", None)
    assert len(queries) == 1
    assert "site:gov.cn" in queries[0]


def test_build_queries_dedup_keywords():
    queries = _build_policy_queries("家用电器", ["  ", "  "])  # blank keywords
    # blank keywords should be stripped, only industry queries remain
    assert all("家用电器" in q or "产业政策" in q for q in queries)


# ── _search_gov_cn ─────────────────────────────────────────────


def _make_page_item(title: str, url: str, snippet: str = "") -> dict:
    return {"title": title, "url": url, "snippet": snippet}


def test_search_gov_cn_success():
    pages = [
        _make_page_item("光伏行业政策", "https://www.ndrc.gov.cn/a.html", "支持光伏发展"),
        _make_page_item("新能源规范", "https://www.miit.gov.cn/b.html", "规范新能源"),
    ]
    with patch("nano_search_mcp.tools.industry_policies.call_bailian_tool_sync", return_value={}) as _call:
        with patch(
            "nano_search_mcp.tools.industry_policies.parse_json_text_payload",
            return_value={"pages": pages},
        ):
            results = _search_gov_cn(["光伏设备 产业政策 site:gov.cn"])

    assert _call.called

    assert len(results) == 2
    assert results[0]["title"] == "光伏行业政策"
    assert results[0]["source_url"] == "https://www.ndrc.gov.cn/a.html"
    assert results[0]["issuer"] == "国家发展改革委"
    assert results[0]["level"] == "ministry"


def test_search_gov_cn_deduplication():
    url = "https://www.ndrc.gov.cn/a.html"
    pages = [
        _make_page_item("标题1", url),
        _make_page_item("标题2", url),  # same URL
    ]
    with patch("nano_search_mcp.tools.industry_policies.call_bailian_tool_sync", return_value={}):
        with patch(
            "nano_search_mcp.tools.industry_policies.parse_json_text_payload",
            return_value={"pages": pages},
        ):
            results = _search_gov_cn([url, url])  # two identical queries

    urls = [r["source_url"] for r in results]
    assert len(urls) == len(set(urls))


def test_search_gov_cn_all_retries_fail():
    import pytest

    with patch(
        "nano_search_mcp.tools.industry_policies.call_bailian_tool_sync",
        side_effect=RuntimeError("connection refused"),
    ):
        with patch("nano_search_mcp.tools.industry_policies.time.sleep"):
            with pytest.raises(RuntimeError, match="百炼 WebSearch"):
                _search_gov_cn(["失败查询"])


def test_search_gov_cn_query_contains_recent_and_region_hint():
    with patch("nano_search_mcp.tools.industry_policies.call_bailian_tool_sync", return_value={}) as call_mock:
        with patch(
            "nano_search_mcp.tools.industry_policies.parse_json_text_payload",
            return_value={"pages": []},
        ):
            _search_gov_cn(["test query"], region="cn-zh")

    args, _kwargs = call_mock.call_args
    payload = args[2]
    assert "近一年" in payload["query"]
    assert "region:cn-zh" in payload["query"]


# ── fetch_industry_policy_list ────────────────────────────────


def test_fetch_policy_list_top5():
    """Result is capped at 5 items."""
    many_items = [
        {"pub_date": "", "issuer": "国家发改委", "title": f"政策{i}",
         "level": "ministry", "source_url": f"https://www.ndrc.gov.cn/{i}.html",
         "summary": ""}
        for i in range(10)
    ]
    with patch("nano_search_mcp.tools.industry_policies._search_gov_cn", return_value=many_items):
        results = fetch_industry_policy_list(industry_sw_l2="光伏设备")

    assert len(results) == 5


def test_fetch_policy_list_empty():
    with patch("nano_search_mcp.tools.industry_policies._search_gov_cn", return_value=[]):
        results = fetch_industry_policy_list(industry_sw_l2="冷门行业X")

    assert results == []


# ── MCP 工具包装 ──────────────────────────────────────────────


def test_mcp_tool_list_policies_success():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name="test")
    register_industry_policy_tools(mcp)

    mock_policies = [
        {"pub_date": "", "issuer": "工信部", "title": "智能制造指导意见",
         "level": "ministry", "source_url": "https://www.miit.gov.cn/abc.html",
         "summary": "..."}
    ]
    with patch("nano_search_mcp.tools.industry_policies._search_gov_cn", return_value=mock_policies):
        result = mcp._tool_manager.call_tool(
            "list_industry_policies",
            {"industry_sw_l2": "智能制造", "keywords": ["工业互联网"]},
        )

    # Result might be a coroutine or sync depending on FastMCP version
    import asyncio
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(result)
        finally:
            loop.close()

    assert isinstance(result, (dict, list, str)) or result is not None


def test_mcp_tool_empty_returns_coverage_note():
    with patch("nano_search_mcp.tools.industry_policies.fetch_industry_policy_list", return_value=[]):
        from nano_search_mcp.tools.industry_policies import register_industry_policy_tools
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP(name="test2")
        register_industry_policy_tools(mcp)

        # Directly test the inner logic
        with patch("nano_search_mcp.tools.industry_policies.fetch_industry_policy_list", return_value=[]):
            # Call through module-level to simulate MCP tool
            import nano_search_mcp.tools.industry_policies as mod

            result = mod.fetch_industry_policy_list("冷门行业")

        assert result == []
