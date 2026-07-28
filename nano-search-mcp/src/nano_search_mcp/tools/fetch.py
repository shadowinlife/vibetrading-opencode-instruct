"""页面抓取工具 - 基于 Playwright 的全 async 实现。"""

import asyncio
import ipaddress
import logging
import socket
from typing import Any, TypedDict
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify
from mcp.server.fastmcp import FastMCP

from nano_search_mcp.config import get_settings

logger = logging.getLogger(__name__)

# ── SSRF 防护 ────────────────────────────────────────────────
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeURLError(ValueError):
    """SSRF 安全检查失败时抛出。"""


def _ensure_safe_url(url: str) -> str:
    """验证 URL 为 http/https 且目标主机不是本机或私网地址。

    防御向量：
    - file:// / ftp:// / gopher:// 等非 HTTP 协议
    - localhost / 127.0.0.0/8 / ::1
    - RFC1918 私网（10/8、172.16/12、192.168/16）
    - 链路本地（169.254/16，包括云服务商元数据端点 169.254.169.254）
    - 回环 / 多播 / 保留段
    """
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("url 必须是非空字符串")

    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"禁止的协议: {parsed.scheme!r}，仅允许 http/https")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError(f"URL 缺少 host: {url!r}")

    # 若 host 是字面 IP，直接校验；否则解析 DNS。
    ips: list[str] = []
    try:
        ipaddress.ip_address(hostname)
        ips = [hostname]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None)
            ips = [info[4][0] for info in infos]
        except socket.gaierror as exc:
            raise UnsafeURLError(f"无法解析主机 {hostname!r}: {exc}") from exc

    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise UnsafeURLError(
                f"禁止访问内网/本地/保留地址: {hostname} -> {ip}"
            )

    return url

# 精确标签级噪声
_JUNK_TAGS = ["header", "footer", "nav", "aside"]

# 更严格的 class/id 匹配：使用 [attr~=token]（按空格分隔的精确 token 匹配），
# 避免 [class*='ad'] 误删 reader/loader/download/roadmap 等含 "ad" 子串的正文容器。
_JUNK_TOKEN_SELECTORS = [
    # 广告（token 精确匹配）
    "[class~='ad']", "[class~='ads']",
    "[class*='advertisement']", "[class*='advert']",
    # 侧边栏
    "[class*='sidebar']", "[id*='sidebar']",
    # 横幅、弹窗、Cookie
    "[class*='banner']", "[class*='popup']", "[class*='cookie']",
    # 评论
    "[class*='comment']",
    # 页头/页脚/导航（id 用 token 形式避免误伤）
    "[id~='footer']", "[id~='header']", "[id~='nav']",
]


def _clean_html(html: str) -> str:
    """删除 header/footer/广告/侧边栏，返回正文 Markdown。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in _JUNK_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for sel in _JUNK_TOKEN_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.find("body") or soup
    return markdownify(str(body), heading_style="ATX", strip=["script", "style"]).strip()


def _truncate(text: str) -> tuple[str, bool]:
    """若超过最大长度则截断，返回 (text, truncated)。"""
    max_len = get_settings().fetch.max_content_length
    if len(text) <= max_len:
        return text, False
    return text[:max_len], True


# ── Playwright 浏览器复用 ────────────────────────────────────
_playwright_ctx: Any = None
_browser: Any = None
_browser_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


async def _get_browser() -> Any:
    """惰性创建并复用 Chromium 实例，降低冷启开销。"""
    global _playwright_ctx, _browser
    async with _get_lock():
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            _playwright_ctx = await async_playwright().start()
            _browser = await _playwright_ctx.chromium.launch(headless=True)
        return _browser


async def shutdown_browser() -> None:
    """关闭复用的 Playwright 资源（供测试或程序退出时调用）。"""
    global _playwright_ctx, _browser
    async with _get_lock():
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:  # noqa: BLE001
                pass
            _browser = None
        if _playwright_ctx is not None:
            try:
                await _playwright_ctx.stop()
            except Exception:  # noqa: BLE001
                pass
            _playwright_ctx = None


async def _fetch_with_playwright(url: str) -> str:
    """使用 Playwright async 渲染页面并提取正文。"""
    browser = await _get_browser()
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(get_settings().fetch.playwright_wait_ms)
        html = await page.content()
    finally:
        await context.close()

    return _clean_html(html)


class PageResult(TypedDict, total=False):
    url: str
    content: str
    method: str
    truncated: bool
    error: str


async def fetch_page_async(url: str) -> PageResult:
    """异步抓取入口：Playwright 渲染后提取正文。"""
    # 0. SSRF 校验
    try:
        _ensure_safe_url(url)
    except UnsafeURLError as exc:
        logger.warning("[fetch] 拒绝不安全 URL: %s — %s", url, exc)
        return {
            "url": url,
            "content": "",
            "method": "blocked",
            "truncated": False,
            "error": f"unsafe_url: {exc}",
        }

    logger.info("[fetch] Playwright 抓取: %s", url)
    try:
        content = await _fetch_with_playwright(url)
        content, truncated = _truncate(content)
        logger.info(
            "[fetch] Playwright 成功: %s (%d 字符, truncated=%s)", url, len(content), truncated
        )
        return {"url": url, "content": content, "method": "playwright", "truncated": truncated}
    except Exception as exc:  # noqa: BLE001
        logger.error("[fetch] Playwright 失败: %s — %s", url, exc)
        return {
            "url": url,
            "content": "",
            "method": "playwright",
            "truncated": False,
            "error": str(exc),
        }


def register_fetch_tools(mcp: FastMCP) -> None:
    """注册页面抓取相关的 MCP Tools"""

    @mcp.tool()
    async def fetch_page(url: str) -> PageResult:
        """抓取任意 HTTP/HTTPS 页面正文，自动清理导航/页脚/广告/侧边栏等噪声。

        使用 Playwright 无头浏览器渲染页面后提取正文。

        **安全性**：目标 URL 必须为 ``http://`` 或 ``https://``；拒绝 file://、
        loopback、RFC1918 私网、链路本地（含云元数据端点 169.254.169.254）等
        潜在 SSRF 向量。

        Args:
            url: 需要抓取的绝对 URL。

        Returns:
            dict:
              - ``url``: 实际抓取的 URL
              - ``content``: 正文 Markdown；失败时为空字符串
              - ``method``: ``"playwright"`` | ``"blocked"``
              - ``truncated``: 是否因超长（>50 万字符）被截断
              - ``error``: 失败时的错误信息（仅失败场景出现）
        """
        return await fetch_page_async(url)
