"""tests/test_fetch.py — fetch_page SSRF 防护单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from nano_search_mcp.tools.fetch import (
    UnsafeURLError,
    _ensure_safe_url,
    fetch_page_async,
)


# ── _ensure_safe_url: 放行合法 URL ───────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a",
        "https://www.sina.com.cn/news",
        "https://stock.finance.sina.com.cn/stock/go.php/x.phtml",
    ],
)
def test_ensure_safe_url_accepts_public_http(url):
    assert _ensure_safe_url(url) == url


# ── _ensure_safe_url: 拒绝非法协议 ───────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "gopher://evil.com",
        "data:text/html,<script>",
    ],
)
def test_ensure_safe_url_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeURLError, match="禁止的协议"):
        _ensure_safe_url(url)


# ── _ensure_safe_url: 拒绝 loopback / 私网 / 元数据 ───────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
        "http://0.0.0.0/",
    ],
)
def test_ensure_safe_url_rejects_private_and_loopback(url):
    with pytest.raises(UnsafeURLError, match="禁止访问|禁止的协议"):
        _ensure_safe_url(url)


# ── _ensure_safe_url: 其它异常输入 ───────────────────────────


def test_ensure_safe_url_rejects_empty():
    with pytest.raises(UnsafeURLError):
        _ensure_safe_url("")


def test_ensure_safe_url_rejects_missing_host():
    with pytest.raises(UnsafeURLError):
        _ensure_safe_url("http:///no-host")


# ── fetch_page_async: 阻止场景下返回 blocked ─────────────────


def test_fetch_page_async_blocks_unsafe_url():
    result = asyncio.run(fetch_page_async("http://127.0.0.1/secret"))

    assert result["method"] == "blocked"
    assert result["content"] == ""
    assert "unsafe_url" in result.get("error", "")


def test_fetch_page_async_blocks_file_scheme():
    result = asyncio.run(fetch_page_async("file:///etc/hosts"))

    assert result["method"] == "blocked"
    assert "unsafe_url" in result.get("error", "")
