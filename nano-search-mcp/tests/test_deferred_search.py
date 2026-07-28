"""tests/test_deferred_search.py — search_deferred_topic 工具单元测试"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nano_search_mcp.config import DeferredSearchSettings, Settings
from nano_search_mcp.tools.deferred_search import (
    _search_with_retry,
    load_deferred_topics,
    register_deferred_search_tools,
    render_query_template,
)

# ── 辅助：构造临时 deferred-tasks.md ─────────────────────────

SAMPLE_TASKS_MD = textwrap.dedent(
    """\
    # Deferred Tasks

    ```yaml
    - id: m3b-gov-cn-policy
      milestone: M3b
      reason: Gov.cn 无公开 API
      retry_condition: 发现第三方 API 时重新评估
      search_query_template: "{industry} 产业政策 {date_range} site:gov.cn"
      status: deferred
      created_at: "2026-04-22"
    ```
    """
)


@pytest.fixture()
def tasks_file(tmp_path: Path) -> Path:
    p = tmp_path / "deferred-tasks.md"
    p.write_text(SAMPLE_TASKS_MD, encoding="utf-8")
    return p


# ── load_deferred_topics ───────────────────────────────────


def test_load_deferred_topics_parses_entry(tasks_file: Path) -> None:
    topics = load_deferred_topics(tasks_file)
    assert "m3b-gov-cn-policy" in topics
    t = topics["m3b-gov-cn-policy"]
    assert t["milestone"] == "M3b"
    assert "{industry}" in t["search_query_template"]


def test_load_deferred_topics_missing_file(tmp_path: Path) -> None:
    topics = load_deferred_topics(tmp_path / "nonexistent.md")
    assert topics == {}


def test_load_deferred_topics_skips_resolved(tmp_path: Path) -> None:
    md = textwrap.dedent(
        """\
        ```yaml
        - id: resolved-item
          milestone: M2
          reason: already done
          retry_condition: n/a
          search_query_template: "query"
          status: resolved
          created_at: "2026-01-01"
        ```
        """
    )
    p = tmp_path / "deferred-tasks.md"
    p.write_text(md, encoding="utf-8")
    topics = load_deferred_topics(p)
    assert "resolved-item" not in topics


# ── render_query_template ─────────────────────────────────


def test_render_template_substitutes_variables() -> None:
    template = "{industry} 产业政策 {date_range} site:gov.cn"
    result = render_query_template(template, {"industry": "光伏设备", "date_range": "2025-01-01"})
    assert "光伏设备" in result
    assert "2025-01-01" in result


def test_render_template_leaves_missing_vars() -> None:
    template = "{industry} 未提供的 {unknown}"
    result = render_query_template(template, {"industry": "光伏"})
    assert "{unknown}" in result


# ── search_deferred_topic via register ────────────────────


def _settings_with_tasks_path(tasks_file: Path) -> Settings:
    """创建 deferred_tasks_path 指向 tasks_file 的 Settings。"""
    return Settings(deferred_search=DeferredSearchSettings(deferred_tasks_path=str(tasks_file)))


def _make_mcp_and_get_tool(tasks_file: Path) -> Any:
    """注册工具后从 FastMCP 提取可调用函数（测试用）。"""
    from mcp.server.fastmcp import FastMCP

    settings = _settings_with_tasks_path(tasks_file)
    with patch("nano_search_mcp.tools.deferred_search.get_settings", return_value=settings):
        mcp = FastMCP("test")
        register_deferred_search_tools(mcp)
    # 获取注册的工具函数
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    return tools.get("search_deferred_topic")


# ── 直接测试内部函数 ──────────────────────────────────────


@patch("nano_search_mcp.tools.deferred_search.call_bailian_tool_sync")
@patch("nano_search_mcp.tools.deferred_search.parse_json_text_payload")
def test_search_success(mock_parse: MagicMock, mock_call: MagicMock) -> None:
    mock_call.return_value = {"result": {"content": [{"type": "text", "text": "{}"}]}}
    mock_parse.return_value = {
        "pages": [{"title": "测试标题", "url": "https://example.com", "snippet": "摘要"}]
    }

    results = _search_with_retry("光伏政策", max_results=5, region="cn-zh")
    assert len(results) == 1
    assert results[0]["title"] == "测试标题"
    assert results[0]["url"] == "https://example.com"


@patch("nano_search_mcp.tools.deferred_search.call_bailian_tool_sync")
def test_search_retries_then_raises(mock_call: MagicMock) -> None:
    mock_call.side_effect = RuntimeError("网络错误")

    with patch("nano_search_mcp.tools.deferred_search.time.sleep"):  # 跳过 sleep
        with pytest.raises(RuntimeError, match="连续"):
            _search_with_retry("测试", max_results=5, region="cn-zh")

    assert mock_call.call_count == 3  # _MAX_RETRIES


# ── search_deferred_topic 集成测试（mock WebSearch）────────


@patch("nano_search_mcp.tools.deferred_search._search_with_retry")
def test_search_by_topic_id_success(mock_search: MagicMock, tasks_file: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    mock_search.return_value = [{"title": "gov 政策", "url": "https://gov.cn/a", "snippet": "内容"}]

    settings = _settings_with_tasks_path(tasks_file)
    with patch("nano_search_mcp.tools.deferred_search.get_settings", return_value=settings):
        mcp = FastMCP("test-success")
        register_deferred_search_tools(mcp)
        tool_fn = None
        for t in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]
            if t.name == "search_deferred_topic":
                tool_fn = t.fn
                break
        assert tool_fn is not None

        result = tool_fn(
            topic_id="m3b-gov-cn-policy",
            context={"industry": "光伏设备", "date_range": "2025-01-01"},
        )
        assert result["source"] == "bailian_web_search"
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "gov 政策"
        assert "query" in result


@patch("nano_search_mcp.tools.deferred_search.call_bailian_tool_sync")
@patch("nano_search_mcp.tools.deferred_search.parse_json_text_payload")
def test_search_with_query_override(mock_parse: MagicMock, mock_call: MagicMock, tasks_file: Path) -> None:
    mock_call.return_value = {"result": {"content": [{"type": "text", "text": "{}"}]}}
    mock_parse.return_value = {"pages": []}

    # 模拟 search_deferred_topic 内部逻辑
    query = "直接搜索词"
    results = _search_with_retry(query, max_results=10, region="cn-zh")
    assert results == []
    called_args = mock_call.call_args
    assert called_args is not None
    sent_query = called_args.args[2]["query"]
    assert "直接搜索词" in sent_query


def test_unknown_topic_id_returns_unavailable(tasks_file: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    settings = _settings_with_tasks_path(tasks_file)
    with patch("nano_search_mcp.tools.deferred_search.get_settings", return_value=settings):
        mcp = FastMCP("test-unknown")
        register_deferred_search_tools(mcp)
        tool_fn = None
        for t in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]
            if t.name == "search_deferred_topic":
                tool_fn = t.fn
                break
        assert tool_fn is not None
        result = tool_fn(topic_id="nonexistent-id")
        assert result["source"] == "unavailable"
        assert "unknown topic_id" in result["error"]


@patch("nano_search_mcp.tools.deferred_search.call_bailian_tool_sync")
def test_search_failure_returns_unavailable(mock_call: MagicMock, tasks_file: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    mock_call.side_effect = ConnectionError("断网")

    settings = _settings_with_tasks_path(tasks_file)
    with patch("nano_search_mcp.tools.deferred_search.get_settings", return_value=settings):
        mcp = FastMCP("test-failure")
        register_deferred_search_tools(mcp)
        tool_fn = None
        for t in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]
            if t.name == "search_deferred_topic":
                tool_fn = t.fn
                break
        assert tool_fn is not None
        with patch("nano_search_mcp.tools.deferred_search.time.sleep"):
            result = tool_fn(
                topic_id="m3b-gov-cn-policy",
                context={"industry": "光伏", "date_range": "2025"},
            )
        assert result["source"] == "unavailable"
        assert "error" in result


def test_max_results_clamping(tasks_file: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    settings = _settings_with_tasks_path(tasks_file)
    with patch("nano_search_mcp.tools.deferred_search.get_settings", return_value=settings):
        mcp = FastMCP("test-clamp")
        register_deferred_search_tools(mcp)
        tool_fn = None
        for t in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]
            if t.name == "search_deferred_topic":
                tool_fn = t.fn
                break
        assert tool_fn is not None

        with patch("nano_search_mcp.tools.deferred_search._search_with_retry") as mock_search:
            mock_search.return_value = []
            # max_results=0 应裁剪到 1
            tool_fn(topic_id="", query_override="test", max_results=0)
            actual = mock_search.call_args[1]["max_results"]
            assert actual == 1

            # max_results=999 应裁剪到 30
            tool_fn(topic_id="", query_override="test", max_results=999)
            actual = mock_search.call_args[1]["max_results"]
            assert actual == 30
