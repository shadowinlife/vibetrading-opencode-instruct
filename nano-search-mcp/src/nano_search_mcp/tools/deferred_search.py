"""deferred_search — 百炼 WebSearch 兜底搜索工具

功能：
- 按 topic_id 从 deferred-tasks.md 读取查询模板并搜索
- 支持 query_override 直接搜索
- 指数退避重试；失败时返回 source: "unavailable"
- max_results 裁剪到 [1, 30]
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings
from nano_search_mcp.tools.bailian_client import (
    call_bailian_tool_sync,
    parse_json_text_payload,
)

logger = logging.getLogger(__name__)

_MAX_RESULTS_MIN = 1
_MAX_RESULTS_MAX = 30


# ── 解析器 ──────────────────────────────────────────────────


def load_deferred_topics(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """解析 deferred-tasks.md 中的 YAML 代码块条目。

    返回 {id: {id, milestone, reason, retry_condition, search_query_template, ...}} 字典。
    重复 id 后者覆盖前者（与既有行为保持一致）。
    """
    tasks_path = path or Path(get_settings().deferred_search.deferred_tasks_path)
    if not tasks_path.exists():
        logger.warning("deferred-tasks.md 不存在：%s", tasks_path)
        return {}

    content = tasks_path.read_text(encoding="utf-8")
    yaml_blocks = re.findall(r"```yaml\n(.*?)```", content, re.DOTALL)

    topics: dict[str, dict[str, Any]] = {}
    for block in yaml_blocks:
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            logger.warning("deferred-tasks.md YAML 块解析失败，已跳过：%s", exc)
            continue

        # Schema 示例块可能全部是注释，safe_load 返回 None；也可能是 dict（单条，无 `-`）。
        if parsed is None:
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            topic_id = entry.get("id")
            if not isinstance(topic_id, str) or not topic_id:
                continue
            # 跳过示例占位符（例如 "<milestone>-<source>-<topic>"）
            if topic_id.startswith("<"):
                continue
            if entry.get("status") == "resolved":
                continue
            topics[topic_id] = entry

    return topics


# ── Query 模板替换 ──────────────────────────────────────────


def render_query_template(template: str, context: dict[str, str]) -> str:
    """将模板字符串中的 {variable} 替换为 context 中的值。"""
    result = template
    for key, value in context.items():
        result = result.replace(f"{{{key}}}", value)
    return result.strip()


# ── 搜索执行 ────────────────────────────────────────────────


def _search_with_retry(
    query: str,
    max_results: int,
    region: str,
) -> list[dict[str, str]]:
    """带指数退避的百炼 WebSearch，最多重试 `http.max_retries 次。"""
    cfg = get_settings()
    max_retries = cfg.http.max_retries
    backoff_base = cfg.http.backoff_base
    last_err: Exception | None = None

    for attempt in range(max_retries):
        if attempt > 0:
            backoff = backoff_base**attempt + random.uniform(0.5, 1.5)
            logger.warning("WebSearch 失败，第 %d 次重试，退避 %.1fs", attempt, backoff)
            time.sleep(backoff)

        try:
            # 轻量预处理：将 region 作为提示词附加到 query。
            merged_query = query if not region else f"{query} region:{region}"
            response = call_bailian_tool_sync(
                cfg.api.bailian_websearch_endpoint,
                "bailian_web_search",
                {"query": merged_query, "count": max_results},
            )
            payload = parse_json_text_payload(response)
            results = []
            for item in payload.get("pages") or []:
                results.append(
                    {
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("snippet", ""),
                    }
                )
            return results
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("WebSearch 异常（第 %d 次）：%s", attempt + 1, exc)

    raise RuntimeError(f"WebSearch 连续 {max_retries} 次失败：{last_err}") from last_err


# ── MCP 工具注册 ─────────────────────────────────────────────


def register_deferred_search_tools(mcp: FastMCP) -> None:
    """注册 search_deferred_topic 工具到 MCP 服务。"""

    @mcp.tool()
    def search_deferred_topic(
        topic_id: str,
        query_override: str = "",
        max_results: int = 10,
        region: str = "cn-zh",
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """通用 WebSearch 网页搜索工具，支持预置主题模板与自由查询两种模式。

        **模式一：预置主题搜索** —— 提供 ``topic_id`` 且不设 ``query_override`` 时，
        工具加载对应主题的查询模板，结合 ``context`` 变量填充后执行搜索。
        适用于需要标准化查询语义的场景（例如行业政策调研、监管动态跟踪等）。

        **模式二：自由查询** —— 提供 ``query_override`` 时直接以该字符串作为搜索词，
        忽略主题模板；``topic_id`` 此时仍作为结果标识字段返回，可传任意字符串
        （如 ``"adhoc"``）。

        Args:
            topic_id: 主题标识符；自由查询模式下可传任意字符串作为结果标签。
            query_override: 非空时覆盖主题模板，直接作为搜索词使用。
            max_results: 返回结果上限，取值范围 [1, 30]，默认 10；越界自动截断。
            region: 地区提示，默认 ``"cn-zh"``（中文简体）；
                其它常用值 ``"wt-wt"``（全球）、``"us-en"``、``"uk-en"``。
            context: 模板变量字典，用于填充主题查询模板中的占位符，
                例如 ``{"industry": "光伏设备", "ts_code": "600660.SH"}``。

        Returns:
            dict:
              成功：{
                "topic_id":   str,
                "query":      str,            # 最终执行的搜索词
                                "source":     "bailian_web_search",
                "results":    list[{"title", "url", "snippet"}],
                "fetch_time": str             # ISO8601 UTC 时间戳
              }
              失败：{"topic_id", "source": "unavailable", "error", "fetch_time"}

        Notes:
                        本工具不抛异常；未知 topic_id、模板缺失、WebSearch 重试耗尽等错误均经由
            返回字典的 ``error`` 字段传递。
        """
        fetch_time = datetime.now(timezone.utc).isoformat()

        # 约束 max_results
        max_results = max(_MAX_RESULTS_MIN, min(_MAX_RESULTS_MAX, max_results))

        # 解析 deferred-tasks.md
        topics = load_deferred_topics()

        # 确定搜索词
        if query_override:
            query = query_override
        else:
            topic = topics.get(topic_id)
            if not topic:
                return {
                    "topic_id": topic_id,
                    "source": "unavailable",
                    "error": f"unknown topic_id '{topic_id}' and no query_override provided",
                    "fetch_time": fetch_time,
                }
            template = topic.get("search_query_template", "")
            if not template:
                return {
                    "topic_id": topic_id,
                    "source": "unavailable",
                    "error": f"topic '{topic_id}' has no search_query_template",
                    "fetch_time": fetch_time,
                }
            query = render_query_template(template, context or {})

        # 执行搜索
        try:
            results = _search_with_retry(query, max_results=max_results, region=region)
        except RuntimeError as exc:
            return {
                "topic_id": topic_id,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": fetch_time,
            }

        return {
            "topic_id": topic_id,
            "query": query,
            "source": "bailian_web_search",
            "results": results,
            "fetch_time": fetch_time,
        }
