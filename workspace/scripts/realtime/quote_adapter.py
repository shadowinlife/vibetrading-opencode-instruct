"""Normalized quote adapter with fallback chains, retry, and freshness.

Provides a unified ``get_quote()`` interface across A-shares, ETFs, and HK
stocks with ordered fallback chains, exponential-backoff retry, rate-limit
awareness, and data-freshness validation.

Output contract — always a DataFrame with these 10 columns::

    symbol, market, open, high, low, close, volume, amount, timestamp, source

Usage::

    from scripts.realtime.quote_adapter import get_quote

    # Simple (backward-compatible — single provider, no fallback):
    df = get_quote("000001", market="A")

    # With fallback chain + freshness:
    df = get_quote("000001", market="A",
                   fallback_chain=[("duckdb", duckdb_fn, "duckdb"),
                                   ("akshare", akshare_fn, "akshare")],
                   freshness_minutes=15)

    # Rich result with metadata:
    from scripts.realtime.quote_adapter import get_quote_with_meta
    df, meta = get_quote_with_meta("000001", market="A",
                                    fallback_chain=chain,
                                    freshness_minutes=15)
    # meta = {"provider": "akshare", "attempts": 2, "fresh": True, ...}

    # For testing — inject mock providers:
    df = get_quote("000001", market="A", providers={"A": my_mock_fn})
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level config (tests can override _sleep_fn to avoid real delays)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1  # exponential: 1s, 2s, 4s
_sleep_fn: Callable[[float], None] = time.sleep  # replaceable for tests

# ---------------------------------------------------------------------------
# Column name mappings (Chinese → English)
# ---------------------------------------------------------------------------

# akshare A-share spot columns (ak.stock_zh_a_spot_em)
_A_SHARE_COL_MAP = {
    "代码": "symbol",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "最新价": "close",
    "成交量": "volume",
    "成交额": "amount",
}

# akshare ETF spot columns (ak.fund_etf_spot_em)
_ETF_COL_MAP = {
    "代码": "symbol",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "最新价": "close",
    "成交量": "volume",
    "成交额": "amount",
}

# akshare HK spot columns (ak.stock_hk_spot_em)
_HK_AKSHARE_COL_MAP = {
    "代码": "symbol",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "最新价": "close",
    "成交量": "volume",
    "成交额": "amount",
}

# DuckDB snapshot columns
_DUCKDB_COL_MAP = {
    "ts_code": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "vol": "volume",
    "amount": "amount",
}

# Tushare daily columns
_TUSHARE_COL_MAP = {
    "ts_code": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "vol": "volume",
    "amount": "amount",
}

# ---------------------------------------------------------------------------
# Normalized output columns
# ---------------------------------------------------------------------------

_OUTPUT_COLUMNS = [
    "symbol", "market", "open", "high", "low", "close",
    "volume", "amount", "timestamp", "source",
]

# ---------------------------------------------------------------------------
# Freshness result keys
# ---------------------------------------------------------------------------

_FRESH_KEY = "_quote_fresh"
_STALE_REASON_KEY = "_quote_stale_reason"


# ===================================================================
# Default provider implementations (lazy imports — no module-level deps)
# ===================================================================

# ---- DuckDB providers (local snapshot cache) ----

def _get_duckdb_path() -> str:
    """Resolve the DuckDB database path."""
    return str(Path(__file__).resolve().parents[2] / "duckdb" / "ashare.duckdb")


def _duckdb_a_share_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch A-share quote from local DuckDB snapshot table."""
    try:
        import duckdb
        db_path = _get_duckdb_path()
        if not Path(db_path).exists():
            return pd.DataFrame()
        con = duckdb.connect(db_path, read_only=True)
        try:
            df = con.execute(
                "SELECT ts_code, open, high, low, close, vol, amount, trade_date "
                "FROM stk_snapshot "
                "WHERE ts_code LIKE ? "
                "ORDER BY trade_date DESC LIMIT 1",
                [f"{symbol}%"],
            ).fetchdf()
        finally:
            con.close()
        if df.empty:
            return pd.DataFrame()
        row = df.iloc[0]
        return pd.DataFrame([{
            "symbol": str(row.get("ts_code", symbol)),
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "volume": float(row.get("vol", 0)),
            "amount": float(row.get("amount", 0)),
            "timestamp": pd.Timestamp(row.get("trade_date", pd.Timestamp.now())),
        }])
    except Exception as e:
        logger.debug("DuckDB A-share provider error: %s", e)
        return pd.DataFrame()


def _duckdb_etf_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch ETF quote from local DuckDB snapshot/fund_daily table."""
    try:
        import duckdb
        db_path = _get_duckdb_path()
        if not Path(db_path).exists():
            return pd.DataFrame()
        con = duckdb.connect(db_path, read_only=True)
        try:
            df = con.execute(
                "SELECT ts_code, open, high, low, close, vol, amount, trade_date "
                "FROM fund_daily "
                "WHERE ts_code LIKE ? "
                "ORDER BY trade_date DESC LIMIT 1",
                [f"{symbol}%"],
            ).fetchdf()
        finally:
            con.close()
        if df.empty:
            return pd.DataFrame()
        row = df.iloc[0]
        return pd.DataFrame([{
            "symbol": str(row.get("ts_code", symbol)),
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "volume": float(row.get("vol", 0)),
            "amount": float(row.get("amount", 0)),
            "timestamp": pd.Timestamp(row.get("trade_date", pd.Timestamp.now())),
        }])
    except Exception as e:
        logger.debug("DuckDB ETF provider error: %s", e)
        return pd.DataFrame()


# ---- akshare providers ----

def _default_a_share_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch A-share quote via akshare (lazy import).

    Calls ``ak.stock_zh_a_spot_em()`` and filters by symbol code.
    Returns raw DataFrame with Chinese column names.
    """
    import akshare as ak  # noqa: WPS433 — lazy import for testability

    df = ak.stock_zh_a_spot_em()
    filtered = df[df["代码"] == symbol]
    return filtered


def _default_etf_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch ETF quote via akshare (lazy import).

    Calls ``ak.fund_etf_spot_em()`` and filters by symbol code.
    Returns raw DataFrame with Chinese column names.
    """
    import akshare as ak  # noqa: WPS433 — lazy import for testability

    df = ak.fund_etf_spot_em()
    filtered = df[df["代码"] == symbol]
    return filtered


def _hk_akshare_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch HK stock quote via akshare (lazy import).

    Calls ``ak.stock_hk_spot_em()`` and filters by symbol code.
    Returns raw DataFrame with Chinese column names.
    """
    try:
        import akshare as ak  # noqa: WPS433

        df = ak.stock_hk_spot_em()
        code = symbol.replace(".HK", "")
        filtered = df[df["代码"] == code]
        return filtered
    except Exception as e:
        logger.debug("akshare HK provider error: %s", e)
        return pd.DataFrame()


# ---- Tushare provider (A-share fallback, requires token) ----

def _tushare_a_share_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch A-share quote via Tushare (lazy import, requires TUSHARE_TOKEN)."""
    try:
        import tushare as ts  # noqa: WPS433

        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            logger.debug("TUSHARE_TOKEN not set, skipping Tushare provider")
            return pd.DataFrame()
        pro = ts.pro_api(token)
        df = pro.daily(ts_code=symbol, limit=1)
        if df.empty:
            return pd.DataFrame()
        row = df.iloc[0]
        return pd.DataFrame([{
            "symbol": str(row.get("ts_code", symbol)),
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "volume": float(row.get("vol", 0)),
            "amount": float(row.get("amount", 0)),
            "timestamp": pd.Timestamp(row.get("trade_date", pd.Timestamp.now())),
        }])
    except Exception as e:
        logger.debug("Tushare provider error: %s", e)
        return pd.DataFrame()


# ---- yfinance providers ----

def _default_hk_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch HK stock quote via yfinance (lazy import).

    Expects symbol in yfinance format (e.g., ``0700.HK``).
    Returns DataFrame with English column names.
    """
    import yfinance as yf  # noqa: WPS433 — lazy import for testability

    ticker = yf.Ticker(symbol)
    info = ticker.fast_info
    hist = ticker.history(period="1d")

    if hist.empty:
        return pd.DataFrame()

    row = hist.iloc[-1]
    return pd.DataFrame([{
        "symbol": symbol,
        "open": float(row.get("Open", 0)),
        "high": float(row.get("High", 0)),
        "low": float(row.get("Low", 0)),
        "close": float(row.get("Close", 0)),
        "volume": int(row.get("Volume", 0)),
        "amount": float(row.get("Volume", 0)) * float(row.get("Close", 0)),  # approx
        "timestamp": pd.Timestamp(row.name) if hasattr(row, "name") else pd.Timestamp.now(),
    }])


def _etf_yfinance_provider(symbol: str, **kwargs) -> pd.DataFrame:
    """Fetch ETF quote via yfinance (lazy import, fallback for ETF)."""
    try:
        import yfinance as yf  # noqa: WPS433

        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d")
        if hist.empty:
            return pd.DataFrame()
        row = hist.iloc[-1]
        return pd.DataFrame([{
            "symbol": symbol,
            "open": float(row.get("Open", 0)),
            "high": float(row.get("High", 0)),
            "low": float(row.get("Low", 0)),
            "close": float(row.get("Close", 0)),
            "volume": int(row.get("Volume", 0)),
            "amount": float(row.get("Volume", 0)) * float(row.get("Close", 0)),
            "timestamp": pd.Timestamp(row.name) if hasattr(row, "name") else pd.Timestamp.now(),
        }])
    except Exception as e:
        logger.debug("yfinance ETF provider error: %s", e)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Default provider registry (backward-compatible single-provider map)
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDERS: Dict[str, Callable] = {
    "A": _default_a_share_provider,
    "ETF": _default_etf_provider,
    "HK": _default_hk_provider,
}

# Source labels per market (used in backward-compatible path)
_SOURCE_LABELS: Dict[str, str] = {
    "A": "akshare",
    "ETF": "akshare",
    "HK": "yfinance",
}

# ---------------------------------------------------------------------------
# Default fallback chains per market
# Each entry: (provider_name, provider_fn, source_label)
# ---------------------------------------------------------------------------

_DEFAULT_FALLBACK_CHAINS: Dict[str, List[Tuple[str, Callable, str]]] = {
    "A": [
        ("duckdb", _duckdb_a_share_provider, "duckdb"),
        ("akshare", _default_a_share_provider, "akshare"),
        ("tushare", _tushare_a_share_provider, "tushare"),
    ],
    "ETF": [
        ("duckdb", _duckdb_etf_provider, "duckdb"),
        ("akshare", _default_etf_provider, "akshare"),
        ("yfinance", _etf_yfinance_provider, "yfinance"),
    ],
    "HK": [
        ("yfinance", _default_hk_provider, "yfinance"),
        ("akshare", _hk_akshare_provider, "akshare"),
    ],
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _normalize_akshare_row(
    raw_df: pd.DataFrame,
    col_map: dict,
    symbol: str,
    market: str,
    source: str,
) -> Optional[pd.DataFrame]:
    """Normalize a single-row akshare DataFrame to the output contract."""
    if raw_df.empty:
        return None

    row = raw_df.iloc[0]
    normalized = {}

    for cn_col, en_col in col_map.items():
        if cn_col in row.index:
            val = row[cn_col]
            normalized[en_col] = float(val) if en_col != "symbol" else str(val)

    # Fill required columns
    normalized["symbol"] = symbol
    normalized["market"] = market
    normalized["source"] = source
    normalized["timestamp"] = pd.Timestamp.now()

    # Ensure all output columns present
    for col in _OUTPUT_COLUMNS:
        if col not in normalized:
            normalized[col] = None

    return pd.DataFrame([normalized])[_OUTPUT_COLUMNS]


def _normalize_hk_row(
    raw_df: pd.DataFrame,
    symbol: str,
    market: str,
    source: str,
) -> Optional[pd.DataFrame]:
    """Normalize a yfinance HK DataFrame to the output contract."""
    if raw_df.empty:
        return None

    row = raw_df.iloc[0]
    normalized = {
        "symbol": symbol,
        "market": market,
        "open": float(row.get("open", 0)),
        "high": float(row.get("high", 0)),
        "low": float(row.get("low", 0)),
        "close": float(row.get("close", 0)),
        "volume": float(row.get("volume", 0)),
        "amount": float(row.get("amount", 0)),
        "timestamp": row.get("timestamp", pd.Timestamp.now()),
        "source": source,
    }

    return pd.DataFrame([normalized])[_OUTPUT_COLUMNS]


def _normalize_generic_row(
    raw_df: pd.DataFrame,
    symbol: str,
    market: str,
    source: str,
) -> Optional[pd.DataFrame]:
    """Normalize a pre-normalized DataFrame (DuckDB, Tushare, yfinance-ETF).

    These providers already return English column names.  This helper ensures
    all 10 output columns are present and correctly typed.
    """
    if raw_df.empty:
        return None

    row = raw_df.iloc[0]
    normalized = {
        "symbol": str(row.get("symbol", symbol)),
        "market": market,
        "open": float(row.get("open", 0)),
        "high": float(row.get("high", 0)),
        "low": float(row.get("low", 0)),
        "close": float(row.get("close", 0)),
        "volume": float(row.get("volume", 0)),
        "amount": float(row.get("amount", 0)),
        "timestamp": row.get("timestamp", pd.Timestamp.now()),
        "source": source,
    }

    return pd.DataFrame([normalized])[_OUTPUT_COLUMNS]


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


def _try_provider_with_retry(
    provider_fn: Callable,
    symbol: str,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> Tuple[Optional[pd.DataFrame], int, Optional[str]]:
    """Call *provider_fn* with retry + exponential backoff.

    Returns:
        (result_df, total_attempts, last_error_message_or_None)
    """
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            result = provider_fn(symbol)
            if result is not None and not result.empty:
                return result, attempt, None
            last_error = "empty_result"
        except Exception as e:
            last_error = str(e)
            logger.debug(
                "Provider attempt %d/%d failed for %s: %s",
                attempt, max_retries, symbol, e,
            )

        if attempt < max_retries:
            backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            _sleep_fn(backoff)

    return None, max_retries, last_error


# ---------------------------------------------------------------------------
# Freshness validation
# ---------------------------------------------------------------------------


def check_freshness(
    quote_df: pd.DataFrame,
    freshness_minutes: int,
    now: Optional[pd.Timestamp] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate quote timestamp against a freshness threshold.

    Args:
        quote_df: Normalized quote DataFrame (must have ``timestamp`` column).
        freshness_minutes: Maximum allowed age in minutes.
        now: Reference time (defaults to ``pd.Timestamp.now()``).

    Returns:
        ``(is_fresh, stale_reason_or_None)``
    """
    if quote_df is None or quote_df.empty:
        return False, "no_data"

    if "timestamp" not in quote_df.columns:
        return False, "missing_timestamp_column"

    ts = quote_df.iloc[0]["timestamp"]
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return False, "no_timestamp"

    if now is None:
        now = pd.Timestamp.now()

    try:
        quote_time = pd.Timestamp(ts)
        if quote_time.tzinfo is not None and now.tzinfo is None:
            quote_time = quote_time.tz_localize(None)
        elif quote_time.tzinfo is None and now.tzinfo is not None:
            quote_time = quote_time.tz_localize(now.tzinfo)
    except Exception:
        return False, f"invalid_timestamp:{ts}"

    age_minutes = (now - quote_time).total_seconds() / 60.0

    if age_minutes > freshness_minutes:
        return False, f"quote_age={age_minutes:.1f}min>threshold={freshness_minutes}min"

    return True, None


# ---------------------------------------------------------------------------
# Fallback chain execution (internal)
# ---------------------------------------------------------------------------


def _get_quote_with_fallback(
    symbol: str,
    market_upper: str,
    chain: List[Tuple[str, Callable, str]],
    max_retries: int,
    freshness_minutes: Optional[int],
    freshness_now: Optional[pd.Timestamp] = None,
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """Walk the fallback chain and return (quote_df, meta_dict)."""
    errors: Dict[str, Any] = {}
    total_attempts = 0

    for provider_name, provider_fn, source_label in chain:
        result, attempts, error = _try_provider_with_retry(
            provider_fn, symbol, max_retries,
        )
        total_attempts += attempts

        if result is not None and not result.empty:
            # --- Normalize ---
            try:
                if source_label in ("akshare",):
                    if market_upper == "A":
                        normalized = _normalize_akshare_row(
                            result, _A_SHARE_COL_MAP, symbol, market_upper, source_label,
                        )
                    elif market_upper == "ETF":
                        normalized = _normalize_akshare_row(
                            result, _ETF_COL_MAP, symbol, market_upper, source_label,
                        )
                    elif market_upper == "HK":
                        normalized = _normalize_akshare_row(
                            result, _HK_AKSHARE_COL_MAP, symbol, market_upper, source_label,
                        )
                    else:
                        normalized = _normalize_generic_row(
                            result, symbol, market_upper, source_label,
                        )
                elif market_upper == "HK" and source_label == "yfinance":
                    normalized = _normalize_hk_row(
                        result, symbol, market_upper, source_label,
                    )
                else:
                    # DuckDB, Tushare, yfinance-ETF — already English columns
                    normalized = _normalize_generic_row(
                        result, symbol, market_upper, source_label,
                    )
            except Exception as e:
                logger.warning(
                    "Normalization error for %s/%s via %s: %s",
                    market_upper, symbol, provider_name, e,
                )
                errors[provider_name] = {
                    "error": f"normalization:{e}",
                    "provider": provider_name,
                    "attempts": attempts,
                }
                continue

            if normalized is None or normalized.empty:
                errors[provider_name] = {
                    "error": "normalization_returned_none",
                    "provider": provider_name,
                    "attempts": attempts,
                }
                continue

            # --- Freshness ---
            fresh: Optional[bool] = None
            stale_reason: Optional[str] = None
            if freshness_minutes is not None:
                fresh, stale_reason = check_freshness(
                    normalized, freshness_minutes, now=freshness_now,
                )

            meta: Dict[str, Any] = {
                "provider": provider_name,
                "source": source_label,
                "attempts": total_attempts,
                "errors": errors if errors else None,
                "fresh": fresh,
                "stale_reason": stale_reason,
            }

            # Embed freshness in DataFrame for backward-compatible access
            if fresh is not None:
                normalized[_FRESH_KEY] = fresh
                if stale_reason:
                    normalized[_STALE_REASON_KEY] = stale_reason

            return normalized, meta

        # Provider failed — record error and continue
        errors[provider_name] = {
            "error": error or "unknown",
            "provider": provider_name,
            "attempts": attempts,
        }

    # All providers exhausted
    meta = {
        "provider": None,
        "source": None,
        "attempts": total_attempts,
        "errors": errors,
        "fresh": None,
        "stale_reason": None,
    }
    return None, meta


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_quote(
    symbol: str,
    market: str,
    providers: Optional[Dict[str, Callable]] = None,
    fallback_chain: Optional[List[Tuple[str, Callable, str]]] = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    freshness_minutes: Optional[int] = None,
    freshness_now: Optional[pd.Timestamp] = None,
) -> Optional[pd.DataFrame]:
    """Fetch a normalized quote for the given symbol and market.

    **Backward-compatible**: when called with only *symbol*, *market*, and
    optionally *providers*, behaves exactly like the Task 5 implementation.

    Args:
        symbol: Stock/ETF code (e.g., ``"000001"``, ``"588000"``, ``"0700.HK"``).
        market: Market identifier — ``"A"``, ``"ETF"``, or ``"HK"``
                (case-insensitive).
        providers: Optional dict of ``{market: callable}`` for dependency
                   injection (backward-compatible single-provider path).
        fallback_chain: Optional ordered list of
                        ``(name, callable, source_label)`` tuples.
                        When provided, overrides the single-provider path
                        and enables retry + freshness.
        max_retries: Max retry attempts per provider (default 3).
        freshness_minutes: If set, validates quote age and embeds
                          ``_quote_fresh`` / ``_quote_stale_reason`` columns.
        freshness_now: Override reference time for freshness check (for tests).

    Returns:
        DataFrame with 10 normalized columns, or ``None`` if all providers
        fail or the market is unsupported.
    """
    market_upper = market.upper()

    # --- Fallback chain path ---
    if fallback_chain is not None:
        if not fallback_chain:
            return None
        result, _meta = _get_quote_with_fallback(
            symbol, market_upper, fallback_chain, max_retries,
            freshness_minutes, freshness_now,
        )
        return result

    # --- Backward-compatible single-provider path ---
    active_providers = providers if providers is not None else _DEFAULT_PROVIDERS

    if market_upper not in active_providers:
        logger.info("Unsupported market: %s (symbol=%s)", market, symbol)
        return None

    provider_fn = active_providers[market_upper]
    source = _SOURCE_LABELS.get(market_upper, "unknown")

    try:
        raw_df = provider_fn(symbol)
    except Exception as e:
        logger.warning("Provider error for %s/%s: %s", market, symbol, e)
        return None

    if raw_df is None or raw_df.empty:
        logger.info("Empty result for %s/%s", market, symbol)
        return None

    try:
        if market_upper == "A":
            return _normalize_akshare_row(
                raw_df, _A_SHARE_COL_MAP, symbol, market_upper, source,
            )
        elif market_upper == "ETF":
            return _normalize_akshare_row(
                raw_df, _ETF_COL_MAP, symbol, market_upper, source,
            )
        elif market_upper == "HK":
            return _normalize_hk_row(raw_df, symbol, market_upper, source)
        else:
            return None
    except Exception as e:
        logger.warning("Normalization error for %s/%s: %s", market, symbol, e)
        return None


def get_quote_with_meta(
    symbol: str,
    market: str,
    fallback_chain: Optional[List[Tuple[str, Callable, str]]] = None,
    providers: Optional[Dict[str, Callable]] = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    freshness_minutes: Optional[int] = None,
    freshness_now: Optional[pd.Timestamp] = None,
) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """Fetch quote **and** return metadata about the fetch attempt.

    Args:
        symbol: Stock/ETF code.
        market: Market identifier (case-insensitive).
        fallback_chain: Ordered list of ``(name, callable, source_label)``.
        providers: Backward-compatible single-provider dict.
        max_retries: Max retries per provider (default 3).
        freshness_minutes: Freshness threshold in minutes.
        freshness_now: Override reference time for freshness check (for tests).

    Returns:
        ``(quote_df_or_None, meta_dict)`` where *meta_dict* contains::

            {
                "provider": str | None,     # name of successful provider
                "source": str | None,       # source label
                "attempts": int,            # total attempts across chain
                "errors": dict | None,      # per-provider error metadata
                "fresh": bool | None,       # freshness result
                "stale_reason": str | None, # why stale (if applicable)
            }
    """
    market_upper = market.upper()

    if fallback_chain is not None:
        if not fallback_chain:
            return None, {
                "provider": None, "source": None, "attempts": 0,
                "errors": None, "fresh": None, "stale_reason": None,
            }
        return _get_quote_with_fallback(
            symbol, market_upper, fallback_chain, max_retries,
            freshness_minutes, freshness_now,
        )

    # Backward-compatible: wrap single provider as a one-entry chain
    active_providers = providers if providers is not None else _DEFAULT_PROVIDERS

    if market_upper not in active_providers:
        return None, {
            "provider": None, "source": None, "attempts": 0,
            "errors": {"unsupported_market": {
                "error": f"unsupported_market:{market}",
                "provider": None, "attempts": 0,
            }},
            "fresh": None, "stale_reason": None,
        }

    source = _SOURCE_LABELS.get(market_upper, "unknown")
    chain = [(market_upper, active_providers[market_upper], source)]
    return _get_quote_with_fallback(
        symbol, market_upper, chain, max_retries,
        freshness_minutes, freshness_now,
    )


# ---------------------------------------------------------------------------
# Intraday adjustment — reuses logic from scripts/backtest/intraday_adjust.py
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

_MORNING_START = 9 * 60 + 30   # 9:30 = 570
_MORNING_END = 11 * 60 + 30    # 11:30 = 690
_AFTERNOON_START = 13 * 60     # 13:00 = 780
_AFTERNOON_END = 15 * 60       # 15:00 = 900
_TOTAL_TRADING_MIN = 240


def _validate_time_format(time_str: str) -> bool:
    match = _TIME_RE.match(time_str.strip())
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    return 0 <= hour <= 23 and 0 <= minute <= 59


def adjust_intraday_data(
    quote_df: pd.DataFrame,
    current_time: Optional[str] = None,
) -> Union[pd.DataFrame, dict]:
    """Adjust intraday volume/amount by extrapolating to full-day estimates.

    Uses A-share trading hours (9:30-11:30, 13:00-15:00, total 240 min).
    Returns DataFrame with ``adjusted``/``original_timestamp``/``adjustment_ratio``
    attrs, or error dict with ``adjusted=False`` for malformed timestamps.
    """
    if current_time is None:
        now = datetime.now()
        current_time = now.strftime("%H:%M")

    if not isinstance(current_time, str) or not _validate_time_format(current_time):
        return {
            "adjusted": False,
            "error": f"Invalid timestamp format: {current_time!r}",
            "error_type": "invalid_timestamp",
            "original_timestamp": (
                quote_df.iloc[0]["timestamp"]
                if not quote_df.empty and "timestamp" in quote_df.columns
                else None
            ),
        }

    parts = current_time.strip().split(":")
    hour, minute = int(parts[0]), int(parts[1])
    total_min = hour * 60 + minute

    result = quote_df.copy()
    original_ts = (
        result.iloc[0]["timestamp"]
        if "timestamp" in result.columns
        else None
    )

    if total_min >= _AFTERNOON_END:
        result.attrs["adjusted"] = False
        result.attrs["original_timestamp"] = original_ts
        result.attrs["adjustment_ratio"] = 1.0
        return result

    if total_min < _MORNING_START:
        result.attrs["adjusted"] = False
        result.attrs["original_timestamp"] = original_ts
        result.attrs["adjustment_ratio"] = 0.0
        return result

    if total_min <= _MORNING_END:
        elapsed = total_min - _MORNING_START
    elif total_min <= _AFTERNOON_START:
        elapsed = 120
    else:
        elapsed = 120 + (total_min - _AFTERNOON_START)

    ratio = elapsed / _TOTAL_TRADING_MIN

    if ratio >= 0.95:
        result.attrs["adjusted"] = False
        result.attrs["original_timestamp"] = original_ts
        result.attrs["adjustment_ratio"] = ratio
        return result

    for col in ["volume", "amount"]:
        if col in result.columns:
            result[col] = result[col].astype(float)
            result.iloc[0, result.columns.get_loc(col)] = (
                result.iloc[0][col] / ratio
            )

    result.attrs["adjusted"] = True
    result.attrs["original_timestamp"] = original_ts
    result.attrs["adjustment_ratio"] = ratio
    return result
