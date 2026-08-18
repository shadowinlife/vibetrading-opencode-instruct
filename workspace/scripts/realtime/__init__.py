"""Realtime quote adapter package backed by the Vibe-Trading data federation.

Provides normalized quote data through Vibe-Trading's market-data
federation (``src.market_data.fetch_market_data``) with retry, freshness
validation, and dependency injection for testability.

Usage::

    from scripts.realtime.quote_adapter import get_quote

    df = get_quote("000001.SZ", market="A")
    df = get_quote("0700.HK", market="HK")
    df = get_quote("588000", market="ETF")
"""

from scripts.realtime.quote_adapter import get_quote

__all__ = ["get_quote"]
