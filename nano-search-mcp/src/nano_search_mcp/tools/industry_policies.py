"""行业政策搜索工具 — 百炼 WebSearch gov.cn 检索实现（M3b）。

搜索策略：
1. 以申万二级行业名 + 产业政策关键词构造两类 site:gov.cn 查询
2. 百炼 WebSearch 检索近 1 年政策相关页面
3. 去重、合并，返回最新 5 条
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings
from nano_search_mcp.tools.bailian_client import (
    call_bailian_tool_sync,
    parse_json_text_payload,
)

logger = logging.getLogger(__name__)

# 常见 gov.cn 子域 → (机构名, level)
_DOMAIN_MAP: dict[str, tuple[str, str]] = {
    "ndrc.gov.cn": ("国家发展改革委", "ministry"),
    "miit.gov.cn": ("工业和信息化部", "ministry"),
    "mof.gov.cn": ("财政部", "ministry"),
    "mee.gov.cn": ("生态环境部", "ministry"),
    "samr.gov.cn": ("国家市场监督管理总局", "ministry"),
    "csrc.gov.cn": ("中国证监会", "ministry"),
    "nfra.gov.cn": ("国家金融监督管理总局", "ministry"),
    "cbirc.gov.cn": ("国家金融监督管理总局", "ministry"),  # 旧域名，已合并到 nfra
    "pboc.gov.cn": ("中国人民银行", "central"),
    "safe.gov.cn": ("国家外汇管理局", "ministry"),
    "stats.gov.cn": ("国家统计局", "ministry"),
    "customs.gov.cn": ("海关总署", "ministry"),
    "moa.gov.cn": ("农业农村部", "ministry"),
    "mot.gov.cn": ("交通运输部", "ministry"),
    "mohurd.gov.cn": ("住房和城乡建设部", "ministry"),
    "nea.gov.cn": ("国家能源局", "ministry"),
    "nrb.gov.cn": ("国家铁路局", "ministry"),
}


def _infer_issuer_level(url: str) -> tuple[str, str]:
    """从 URL 域名推断发文机构和层级。

    返回 ``(issuer, level)``，level 取 ``central|ministry|local``。
    """
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    if not m:
        return ("未知机构", "local")
    domain = m.group(1).lower()
    for suffix, (name, level) in _DOMAIN_MAP.items():
        if domain.endswith(suffix) and suffix != "gov.cn":
            return (name, level)
    if ".gov.cn" in domain:
        return ("地方政府", "local")
    return ("未知机构", "local")


def _build_policy_queries(
    industry_sw_l2: str,
    keywords: list[str] | None,
) -> list[str]:
    """构造 site:gov.cn 政策搜索词列表。"""
    queries: list[str] = []
    kws = [k.strip() for k in (keywords or []) if k.strip()]

    if industry_sw_l2:
        queries.append(f'"{industry_sw_l2}" 产业政策 site:gov.cn')
        queries.append(f'"{industry_sw_l2}" 行业规范 site:gov.cn')

    for kw in kws:
        queries.append(f'"{kw}" 政策 site:gov.cn')

    # 如果两者都没有，用通用政策搜索（caller 应至少提供一个）
    if not queries:
        queries.append("产业政策 通知 site:gov.cn")

    return queries


def _search_gov_cn(
    queries: list[str],
    max_per_query: int | None = None,
    region: str = "cn-zh",
) -> list[dict[str, Any]]:
    """对每条 query 调用百炼 WebSearch，去重后返回。

    当所有 query 的所有重试均失败（且未得到任何结果）时抛出 ``RuntimeError``，
    以便上层区分「百炼服务不可用」与「无匹配结果」两种语义。
    """
    cfg = get_settings()
    if max_per_query is None:
        max_per_query = cfg.industry_policies.max_per_query
    seen_urls: set[str] = set()
    results: list[dict[str, Any]] = []
    failed_queries = 0

    for query in queries:
        last_err: Exception | None = None
        for attempt in range(cfg.http.max_retries):
            if attempt > 0:
                backoff = cfg.http.backoff_base**attempt + random.uniform(0.5, 1.5)
                logger.warning(
                    "[industry_policies] WebSearch 失败，第 %d 次重试，退避 %.1fs: %s",
                    attempt,
                    backoff,
                    query,
                )
                time.sleep(backoff)
            try:
                merged_query = f"{query} 近一年 region:{region}"
                response = call_bailian_tool_sync(
                    cfg.api.bailian_websearch_endpoint,
                    "bailian_web_search",
                    {"query": merged_query, "count": max_per_query},
                )
                payload = parse_json_text_payload(response)
                for item in payload.get("pages") or []:
                    url = item.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        issuer, level = _infer_issuer_level(url)
                        results.append(
                            {
                                "pub_date": "",
                                "issuer": issuer,
                                "title": item.get("title", ""),
                                "level": level,
                                "source_url": url,
                                "summary": item.get("snippet", ""),
                            }
                        )
                break  # success
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning(
                    "[industry_policies] WebSearch 查询异常（第 %d 次）：%s — %s",
                    attempt + 1,
                    query,
                    exc,
                )
        else:
            # for-else: 仅当内层 for 未执行 break（即每次 attempt 均捕获异常）时进入。
            failed_queries += 1
            logger.error(
                "[industry_policies] WebSearch 查询连续 %d 次失败：%s，最后错误：%s",
                cfg.http.max_retries,
                query,
                last_err,
            )

    if queries and failed_queries == len(queries) and not results:
        raise RuntimeError(
            f"百炼 WebSearch 对全部 {len(queries)} 条查询均重试失败，服务不可用"
        )

    return results


def fetch_industry_policy_list(
    industry_sw_l2: str = "",
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """搜索行业政策文件（gov.cn），返回最新 `industry_policies.top_n` 条。

    使用百炼 WebSearch 检索政策相关页面并去重，返回前 `industry_policies.top_n` 条。
    """
    queries = _build_policy_queries(industry_sw_l2, keywords)
    results = _search_gov_cn(queries)

    top_n = get_settings().industry_policies.top_n
    return results[:top_n]


def register_industry_policy_tools(mcp: FastMCP) -> None:
    """注册 list_industry_policies 工具到 MCP 服务。"""

    @mcp.tool()
    def list_industry_policies(
        industry_sw_l2: str = "",
        keywords: list[str] | None = None,
    ) -> dict:
        r"""检索中国政府机构（\*.gov.cn）发布的行业政策文件。

        根据申万二级行业名和业务关键词，通过百炼 WebSearch 检索
        政府网站发布的产业政策、行业规范等文件。

        Args:
            industry_sw_l2: 申万二级行业名，如 ``"汽车零部件"``、``"光伏设备"``
            keywords: 主营业务关键词列表，如 ``["锂电池", "新能源"]``

        Returns:
            dict：
              成功：{
                "industry_sw_l2": str,
                                "source": "bailian_web_search_gov_cn",
                "policies": [
                  {"pub_date": str, "issuer": str, "title": str,
                   "level": "central"|"ministry"|"local",
                   "source_url": str, "summary": str}
                ],
                "fetch_time": ISO8601
              }
              未命中：同上但 policies 为空列表，并附 coverage_note
              异常：{"industry_sw_l2", "source": "unavailable", "error", "fetch_time"}

        Notes:
            - 无结果时不触发第二数据源，交由调用方处理无数据场景
        """
        fetch_time = datetime.now(tz=timezone.utc).isoformat()
        try:
            policies = fetch_industry_policy_list(
                industry_sw_l2=industry_sw_l2,
                keywords=keywords,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[industry_policies] 工具执行异常：%s", exc)
            return {
                "industry_sw_l2": industry_sw_l2,
                "source": "unavailable",
                "error": str(exc),
                "fetch_time": fetch_time,
            }

        result: dict[str, Any] = {
            "industry_sw_l2": industry_sw_l2,
            "source": "bailian_web_search_gov_cn",
            "policies": policies,
            "fetch_time": fetch_time,
        }
        if not policies:
            result["coverage_note"] = (
                "gov.cn 近 1 年内未检索到相关政策文件，调用方可选择人工补充或放宽关键词。"
            )
        return result
