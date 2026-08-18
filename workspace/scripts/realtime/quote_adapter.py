"""Normalized quote adapter backed by the Vibe-Trading market-data federation.

Data layer
----------
The previous provider chain (akshare spot tables, yfinance, tushare daily,
local DuckDB snapshot) has been replaced by a single source: Vibe-Trading's
``src.market_data.fetch_market_data`` federation.  For each requested symbol
the adapter fetches a short recent daily window through the federation with
``source="auto"`` (which walks the per-market loader chain, e.g. ClickHouse
→ Tencent/mootdx → Eastmoney → … for A-shares) and takes the latest bar as
the quote.  A single retry with backoff covers empty results.

Intraday volume/amount extrapolation has been REMOVED together with the
former dependency on ``scripts.backtest.intraday_adjust``: when the
federation can serve a same-day bar the quote reflects it; otherwise the
latest bar is the last settled daily bar (typically T-1) and the metadata
says so honestly — ``meta["fresh"]`` / ``meta["stale_reason"]`` reflect the
real age of the latest bar, and ``meta["note"]`` states explicitly when the
quote is only a T-N daily close.

Output contract — always a DataFrame with these 10 columns::

    symbol, market, open, high, low, close, volume, amount, timestamp, source

Usage::

    from scripts.realtime.quote_adapter import get_quote, get_quote_with_meta

    df = get_quote("000001.SZ", market="A")
    df, meta = get_quote_with_meta("000001.SZ", market="A", freshness_minutes=30)
    # meta = {"provider": "vibe-trading federation", "attempts": 1,
    #         "fresh": False, "stale_reason": "quote_age=...",
    #         "note": "latest bar is T-1 daily close; ...", ...}

    # For testing — inject mock providers (must return English column names):
    df = get_quote("000001.SZ", market="A", providers={"A": my_mock_fn})
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level config (tests can override _sleep_fn to avoid real delays)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_RETRIES = 2  # initial attempt + one retry on empty result
_BACKOFF_BASE_SECONDS = 1  # exponential: 1s, 2s, 4s
_sleep_fn: Callable[[float], None] = time.sleep  # replaceable for tests

# Calendar days fetched backwards so at least one settled bar is present
# even after long market holidays.
_LOOKBACK_DAYS = 21

_PROVIDER_NAME = "vibe-trading federation"
_SOURCE_LABEL = "vibe-trading federation"

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

# ---------------------------------------------------------------------------
# Symbol normalization into Vibe-Trading federation notation
# ---------------------------------------------------------------------------

_A_SHARE_RE = re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.IGNORECASE)
_BARE_CODE_RE = re.compile(r"^\d{6}$")
_HK_CODE_RE = re.compile(r"^\d{3,5}$")


def _to_vt_symbol(symbol: str, market: str) -> str:
    """Normalize a user-supplied symbol to the notation the federation routes.

    A-share/ETF bare codes get an exchange suffix inferred from their first
    digit; HK codes are zero-padded to 5 digits; US tickers get ``.US``.
    Already-suffixed symbols pass through unchanged.
    """
    s = symbol.strip().upper()
    if market == "A":
        if _A_SHARE_RE.match(s):
            return s
        if _BARE_CODE_RE.match(s):
            if s[0] in "69":
                return f"{s}.SH"
            if s[0] in "48":
                return f"{s}.BJ"
            return f"{s}.SZ"
        return s
    if market == "ETF":
        if _A_SHARE_RE.match(s):
            return s
        if _BARE_CODE_RE.match(s):
            return f"{s}.SH" if s[0] in "56" else f"{s}.SZ"
        return s
    if market == "HK":
        if s.endswith(".HK"):
            code = s.split(".", 1)[0]
            if _HK_CODE_RE.match(code):
                return f"{code.zfill(5)}.HK"
            return s
        if _HK_CODE_RE.match(s):
            return f"{s.zfill(5)}.HK"
        return s
    if market == "US":
        return s if s.endswith(".US") else f"{s}.US"
    return s


# ---------------------------------------------------------------------------
# Federation fetch
# ---------------------------------------------------------------------------

_BAR_DATE_KEYS = ("trade_date", "date", "datetime", "timestamp")


def _num(value: Any) -> float:
    """Coerce a federation field to a finite float (0.0 when missing)."""
    try:
        if value is None:
            return 0.0
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _bar_date_key(record: Dict[str, Any]) -> Optional[str]:
    return next((k for k in _BAR_DATE_KEYS if k in record), None)


def _fetch_federation_quote(code: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Fetch the latest federation bar for *code*.

    Returns ``(record_or_None, provenance_dict)``.  Raises when the ``src``
    package is missing or every loader in the chain failed — the retry
    wrapper records those errors.
    """
    from src.market_data import fetch_market_data

    today = date.today()
    start = (today - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    result = fetch_market_data(
        codes=[code],
        start_date=start,
        end_date=today.isoformat(),
        source="auto",
        interval="1D",
        max_rows=0,
        include_provenance=True,
    )

    rows = result.get(code)
    if isinstance(rows, dict):  # truncation envelope
        rows = rows.get("data", [])
    if not rows:
        return None, {}

    date_key = _bar_date_key(rows[-1])
    if date_key is not None:
        rows = sorted(rows, key=lambda r: str(r.get(date_key) or ""))
    record = rows[-1]
    provenance = (result.get("_provenance") or {}).get(code) or {}
    return record, provenance


def _make_vt_provider(market: str) -> Callable[[str], pd.DataFrame]:
    """Build the federation quote provider for one market."""

    def provider(symbol: str, **_kwargs) -> pd.DataFrame:
        code = _to_vt_symbol(symbol, market)
        record, provenance = _fetch_federation_quote(code)
        if record is None:
            return pd.DataFrame()

        close = _num(record.get("close"))
        volume = _num(record.get("volume"))
        amount_raw = record.get("amount")
        amount = _num(amount_raw) if amount_raw is not None else volume * close

        date_key = _bar_date_key(record)
        ts_raw = record.get(date_key) if date_key else None
        try:
            timestamp: Optional[pd.Timestamp] = (
                pd.Timestamp(ts_raw) if ts_raw is not None else None
            )
        except (TypeError, ValueError):
            timestamp = None

        quote = pd.DataFrame([{
            "symbol": symbol,
            "open": _num(record.get("open")),
            "high": _num(record.get("high")),
            "low": _num(record.get("low")),
            "close": close,
            "volume": volume,
            "amount": amount,
            "timestamp": timestamp,
        }])
        quote.attrs["vt_meta"] = {
            "latest_bar_date": str(ts_raw) if ts_raw is not None else None,
            "volume_unit": provenance.get("volume_unit"),
            "served_by": provenance.get("source"),
            "fallback_used": bool(provenance.get("fallback_used", False)),
        }
        return quote

    provider.__name__ = f"vt_federation_{market.lower()}_provider"
    return provider


# ---------------------------------------------------------------------------
# Default provider registry and fallback chains
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDERS: Dict[str, Callable] = {
    "A": _make_vt_provider("A"),
    "ETF": _make_vt_provider("ETF"),
    "HK": _make_vt_provider("HK"),
    "US": _make_vt_provider("US"),
}

_SOURCE_LABELS: Dict[str, str] = {
    market: _SOURCE_LABEL for market in _DEFAULT_PROVIDERS
}

# One federation entry per market; the federation itself walks its internal
# per-market loader chain, so no outer multi-provider chain is needed.
_DEFAULT_FALLBACK_CHAINS: Dict[str, List[Tuple[str, Callable, str]]] = {
    market: [(_PROVIDER_NAME, fn, _SOURCE_LABEL)]
    for market, fn in _DEFAULT_PROVIDERS.items()
}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _normalize_generic_row(
    raw_df: pd.DataFrame,
    symbol: str,
    market: str,
    source: str,
) -> Optional[pd.DataFrame]:
    """Normalize a provider DataFrame (English column names) to the contract.

    Ensures all 10 output columns are present and correctly typed.  Carries
    ``raw_df.attrs["vt_meta"]`` forward so federation metadata reaches the
    final meta dict.
    """
    if raw_df.empty:
        return None

    row = raw_df.iloc[0]
    normalized = {
        "symbol": str(row.get("symbol", symbol)),
        "market": market,
        "open": _num(row.get("open")),
        "high": _num(row.get("high")),
        "low": _num(row.get("low")),
        "close": _num(row.get("close")),
        "volume": _num(row.get("volume")),
        "amount": _num(row.get("amount")),
        "timestamp": row.get("timestamp"),
        "source": source,
    }

    out = pd.DataFrame([normalized])[_OUTPUT_COLUMNS]
    vt_meta = raw_df.attrs.get("vt_meta")
    if vt_meta:
        out.attrs["vt_meta"] = vt_meta
    return out


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


def _federation_note(
    latest_bar_date: Optional[str],
    now: Optional[pd.Timestamp] = None,
) -> Optional[str]:
    """Honest freshness note derived from the latest federation bar date."""
    if not latest_bar_date:
        return "federation returned no bar date"
    try:
        bar_date = pd.Timestamp(latest_bar_date).date()
    except (TypeError, ValueError):
        return None
    ref_date = (now or pd.Timestamp.now()).date()
    if bar_date >= ref_date:
        return "same-day bar served by federation"
    lag_days = (ref_date - bar_date).days
    return (
        f"latest bar is T-{lag_days} daily close; "
        "intraday quote not available from federation"
    )


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
            try:
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

            vt_meta = normalized.attrs.get("vt_meta")
            if vt_meta:
                meta["latest_bar_date"] = vt_meta.get("latest_bar_date")
                meta["volume_unit"] = vt_meta.get("volume_unit")
                meta["served_by"] = vt_meta.get("served_by")
                meta["fallback_used"] = vt_meta.get("fallback_used")
                note = _federation_note(
                    vt_meta.get("latest_bar_date"), freshness_now,
                )
                if note:
                    meta["note"] = note

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

    All quotes come from the Vibe-Trading market-data federation
    (``src.market_data.fetch_market_data``, ``source="auto"``); the latest
    bar of a short recent window becomes the quote.

    Args:
        symbol: Stock/ETF code (e.g., ``"000001.SZ"``, ``"588000"``,
                ``"0700.HK"``, ``"AAPL"``).
        market: Market identifier — ``"A"``, ``"ETF"``, ``"HK"``, or ``"US"``
                (case-insensitive).
        providers: Optional dict of ``{market: callable}`` for dependency
                   injection (single-provider path; injected providers must
                   return English column names).
        fallback_chain: Optional ordered list of
                        ``(name, callable, source_label)`` tuples.
        max_retries: Max retry attempts per provider (default 2: initial
                     attempt + one retry).
        freshness_minutes: If set, validates quote age and embeds
                           ``_quote_fresh`` / ``_quote_stale_reason`` columns.
        freshness_now: Override reference time for freshness check (for tests).

    Returns:
        DataFrame with 10 normalized columns, or ``None`` if all providers
        fail or the market is unsupported.
    """
    market_upper = market.upper()

    if fallback_chain is not None:
        if not fallback_chain:
            return None
        result, _meta = _get_quote_with_fallback(
            symbol, market_upper, fallback_chain, max_retries,
            freshness_minutes, freshness_now,
        )
        return result

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
        return _normalize_generic_row(raw_df, symbol, market_upper, source)
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
        providers: Single-provider dict for dependency injection.
        max_retries: Max retries per provider (default 2).
        freshness_minutes: Freshness threshold in minutes.
        freshness_now: Override reference time for freshness check (for tests).

    Returns:
        ``(quote_df_or_None, meta_dict)`` where *meta_dict* contains::

            {
                "provider": str | None,     # "vibe-trading federation" on success
                "source": str | None,       # source label
                "attempts": int,            # total attempts across chain
                "errors": dict | None,      # per-provider error metadata
                "fresh": bool | None,       # freshness result
                "stale_reason": str | None, # why stale (if applicable)
                # federation-provided fields (when available):
                "latest_bar_date": str | None,
                "volume_unit": str | None,
                "served_by": str | None,
                "fallback_used": bool,
                "note": str | None,         # honest T-N / same-day statement
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
