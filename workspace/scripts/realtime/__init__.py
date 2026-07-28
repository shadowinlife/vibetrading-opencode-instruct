"""Realtime quote adapter package for multi-market data retrieval.

Provides normalized quote data from various providers (akshare, yfinance, etc.)
with dependency injection for testability.

Usage::

    from scripts.realtime.quote_adapter import get_quote

    df = get_quote("000001.SZ", market="A")
    df = get_quote("0700.HK", market="HK")
    df = get_quote("588000", market="ETF")
"""

from scripts.realtime.quote_adapter import get_quote

__all__ = ["get_quote"]
