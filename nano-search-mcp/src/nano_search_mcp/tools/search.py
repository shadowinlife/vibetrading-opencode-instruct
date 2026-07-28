"""搜索工具 - 基于阿里云百炼 WebSearch MCP。"""

import logging
from typing import Any, TypedDict

from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings
from nano_search_mcp.tools.bailian_client import (
    BailianMCPError,
    call_bailian_tool_sync,
    parse_json_text_payload,
)

logger = logging.getLogger(__name__)

def _normalize_search_query(
    query: str,
    region: str,
    timelimit: str | None,
) -> str:
    """在服务内做轻量预处理，提升检索稳定性与可控性。"""
    parts = [query.strip()]

    if timelimit:
        label = {
            "d": "过去1天",
            "w": "过去1周",
            "m": "过去1个月",
            "y": "过去1年",
        }.get(timelimit)
        if label:
            parts.append(label)

    if region and region.lower() not in {"zh-cn", "cn-zh"}:
        parts.append(f"region:{region}")

    return " ".join(p for p in parts if p)


def _search_via_bailian(
    query: str,
    max_results: int,
    region: str,
    timelimit: str | None,
) -> list[dict[str, Any]]:
    normalized = _normalize_search_query(query=query, region=region, timelimit=timelimit)
    try:
        response = call_bailian_tool_sync(
            get_settings().api.bailian_websearch_endpoint,
            "bailian_web_search",
            {"query": normalized, "count": max_results},
        )
        payload = parse_json_text_payload(response)
    except BailianMCPError as exc:
        raise RuntimeError(f"百炼 WebSearch 调用失败: {exc}") from exc

    pages = payload.get("pages") or []
    results: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        results.append(
            {
                "title": str(page.get("title") or ""),
                "url": str(page.get("url") or ""),
                "snippet": str(page.get("snippet") or ""),
            }
        )
    return results


class SearchItem(TypedDict):
    title: str
    url: str
    snippet: str


def register_search_tools(mcp: FastMCP) -> None:
    """注册搜索相关的 MCP Tools"""

    @mcp.tool()
    def search(
        query: str,
        max_results: int = 5,
        region: str = "zh-cn",
        timelimit: str | None = None,
    ) -> list[SearchItem]:
        """使用百炼 WebSearch MCP 搜索网页，返回标题、URL 和摘要。

        Args:
            query: 搜索关键词（必填，非空字符串）
            max_results: 最大返回结果数，取值范围 [1, 30]，默认 5；
                超出范围会被截断到边界。
            region: 搜索区域代码，常用值 ``"zh-cn"``（中文）、``"us-en"``、
                ``"uk-en"``、``"wt-wt"``（全球）；默认 ``"zh-cn"``
            timelimit: 时间范围过滤，可选 ``"d"``（近 1 天）/ ``"w"``（近 1 周）/
                ``"m"``（近 1 月）/ ``"y"``（近 1 年）；``None`` 表示不限。

        Returns:
            list[SearchItem]: 每项含 ``title`` / ``url`` / ``snippet`` 三个字段
            的字符串。无结果时返回空列表。

        Raises:
            RuntimeError: 百炼 MCP 调用失败。

        Notes:
            百炼 WebSearch 原生不支持 DDG 风格的 ``region`` / ``timelimit`` 过滤参数，
            本工具将其降级为查询提示词附加到 ``query``（例如 ``region:zh-cn 过去1个月``），
            精确度取决于上游模型理解；如需严格过滤请在 ``query`` 中显式表达。
        """
        max_results = max(1, min(int(max_results), 30))
        return _search_via_bailian(
            query,
            max_results=max_results,
            region=region,
            timelimit=timelimit,
        )
