"""NanoSearch MCP Server - 主服务模块"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import generate_sample_config, init_settings

# ── MCP 延迟初始化 ───────────────────────────────────────────

_mcp_instance: FastMCP | None = None

_MCP_INSTRUCTIONS = (
    "NanoSearch 是面向中国 A 股市场的结构化文本检索服务，按能力域提供以下 MCP 工具：\n"
    "\n"
    "【通用检索】\n"
    "- search: 百炼 WebSearch 网页搜索，返回 [{title, url, snippet}] 列表\n"
    "- fetch_page: 抓取任意 URL 正文（Markdown 格式），当前使用 Playwright\n"
    "- search_deferred_topic: 基于命名模板或自由查询的百炼 WebSearch 检索，"
    "支持通过 context 变量填充模板参数\n"
    "\n"
    "【定期报告】\n"
    "- get_company_report: 获取指定 A 股公司指定年份的年报/半年报/一季报/三季报全文正文；"
    "调用方必须显式提供 year 与 report_type（默认 annual）\n"
    "\n"
    "【临时公告】\n"
    "- list_announcements: 列出指定公司临时公告条目（支持 ann_type 过滤）\n"
    "- get_announcement_text: 抓取单条公告正文\n"
    "\n"
    "【行业研报】\n"
    "- list_industry_reports: 列出行业研究报告（可通过 ts_code 自动路由至申万二级行业，"
    "或直接指定 industry_sw_l2 + 关键词；默认返回近 1 年数据）\n"
    "- get_report_text: 抓取单条行业研报正文\n"
    "\n"
    "【监管与处罚】\n"
    "- list_regulatory_penalties: 列出指定公司监管处罚 / 违规处理记录\n"
    "\n"
    "【投资者关系】\n"
    "- list_ir_meetings: 列出机构调研记录、业绩说明会等投资者关系活动\n"
    "- get_ir_meeting_text: 抓取单条 IR 纪要正文及参会机构名单\n"
    "\n"
    "【行业政策】\n"
    "- list_industry_policies: 检索政府机构（*.gov.cn）近 1 年内发布的行业政策文件，"
    "最多返回 5 条\n"
    "\n"
    "错误契约：除 search 与 get_company_report 在参数非法或网络彻底失败时抛出异常外，"
    "其余工具在失败时返回 {source: \"unavailable\", error, fetch_time}，不抛异常。"
)


def _create_mcp() -> FastMCP:
    """创建 FastMCP 实例并注册所有工具。"""
    from nano_search_mcp.tools.announcements import register_announcement_tools
    from nano_search_mcp.tools.deferred_search import register_deferred_search_tools
    from nano_search_mcp.tools.fetch import register_fetch_tools
    from nano_search_mcp.tools.industry_policies import register_industry_policy_tools
    from nano_search_mcp.tools.industry_reports import register_industry_report_tools
    from nano_search_mcp.tools.ir_meetings import register_ir_meeting_tools
    from nano_search_mcp.tools.regulatory_penalties import register_regulatory_penalty_tools
    from nano_search_mcp.tools.search import register_search_tools
    from nano_search_mcp.tools.sina_reports import register_sina_report_tools

    instance = FastMCP(
        name="NanoSearch",
        streamable_http_path="/mcp",
        instructions=_MCP_INSTRUCTIONS,
    )

    register_search_tools(instance)
    register_fetch_tools(instance)
    register_sina_report_tools(instance)
    register_deferred_search_tools(instance)
    register_announcement_tools(instance)
    register_industry_report_tools(instance)
    register_regulatory_penalty_tools(instance)
    register_ir_meeting_tools(instance)
    register_industry_policy_tools(instance)

    return instance


def get_mcp() -> FastMCP:
    """获取 MCP 实例（延迟创建）。"""
    global _mcp_instance
    if _mcp_instance is None:
        _mcp_instance = _create_mcp()
    return _mcp_instance


# 模块级 __getattr__ hook，使 `from nano_search_mcp.server import mcp` 继续工作
def __getattr__(name: str) -> Any:
    if name == "mcp":
        return get_mcp()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── CLI 参数解析 ─────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 NanoSearch MCP server")
    parser.add_argument(
        "--config",
        default=None,
        help="指定 YAML 配置文件路径",
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        default=False,
        help="输出示例配置文件到 stdout 后退出",
    )
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio"),
        default=None,
        help="选择 MCP transport；默认 streamable-http，本地直连可用 stdio",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP 监听地址；默认 0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP 监听端口；默认 8000",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """启动 MCP Server，支持配置文件、环境变量和命令行参数。"""
    args = _build_parser().parse_args(list(argv) if argv is not None else [])

    # 处理 --generate-config
    if args.generate_config:
        sys.stdout.write(generate_sample_config())
        return

    # 初始化配置（四层合并）
    cli_args = {
        "transport": args.transport,
        "host": args.host,
        "port": args.port,
    }
    cfg = init_settings(cli_args=cli_args, config_path=args.config)

    # 获取 MCP 实例并启动
    mcp_instance = get_mcp()
    mcp_instance.run(transport=cfg.server.transport)


if __name__ == "__main__":
    main()
