"""百炼 MCP 客户端封装。"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from nano_search_mcp.config import get_settings

# 错误消息中原始响应片段的截断长度
_ERR_SNIPPET = 1000


class BailianMCPError(RuntimeError):
    """百炼 MCP 请求失败。"""


def _auth_headers() -> dict[str, str]:
    api_key = get_settings().api.dashscope_api_key
    if not api_key:
        raise BailianMCPError("缺少 DASHSCOPE_API_KEY（可通过环境变量或配置文件设置）")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _extract_result_text(response_body: dict[str, Any]) -> str:
    result = response_body.get("result")
    if not isinstance(result, dict):
        raise BailianMCPError(f"MCP 返回缺少 result: {response_body}")

    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise BailianMCPError(f"MCP 返回缺少 content: {response_body}")

    first = content[0]
    if not isinstance(first, dict) or "text" not in first:
        raise BailianMCPError(f"MCP content 非 text: {response_body}")
    return str(first["text"])


def parse_json_text_payload(response_body: dict[str, Any]) -> dict[str, Any]:
    """将 MCP tools/call 的 text 字段解析为 JSON 对象。"""
    text = _extract_result_text(response_body)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BailianMCPError(f"MCP text 非法 JSON: {text[:_ERR_SNIPPET]}") from exc


def call_bailian_tool_sync(
    endpoint: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    effective_timeout = timeout if timeout is not None else get_settings().api.bailian_mcp_timeout
    with httpx.Client(timeout=effective_timeout) as client:
        resp = client.post(endpoint, json=payload, headers=_auth_headers())
    if resp.status_code >= 400:
        raise BailianMCPError(f"MCP HTTP {resp.status_code}: {resp.text[:_ERR_SNIPPET]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise BailianMCPError(f"MCP 返回非 JSON: {resp.text[:_ERR_SNIPPET]}") from exc

    if "error" in body:
        raise BailianMCPError(f"MCP error: {body['error']}")
    return body
