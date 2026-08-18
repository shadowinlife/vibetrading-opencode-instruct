"""Shared ClickHouse access helpers for the screening package.

Data layer: the screening pipeline talks to the Vibe-Trading ClickHouse
warehouse through ``src.clickhouse_connector.ClickHouseConnector``.  All
connection parameters (host, port, user, password, database) are resolved
from the ``CLICKHOUSE_*`` environment variables inside the connector —
nothing is hardcoded here.

The ``src`` package is imported lazily so the screening modules stay
import-safe (and the CLI keeps answering ``--help``) even on machines where
the Vibe-Trading agent package is not installed.  An unavailable connector
surfaces as :class:`ClickHouseUnavailableError`; callers degrade gracefully
(the CLI prints ``{"available": false, "reason": "ClickHouse unreachable"}``
and exits 0).
"""
from __future__ import annotations

import pandas as pd

__all__ = [
    "ClickHouseUnavailableError",
    "get_connector",
    "run_query",
    "coerce_numeric_columns",
]

# Columns that must stay strings regardless of what the JSON transport
# returns (the ClickHouse HTTP interface serializes every value as a string).
_STRING_COLUMNS = {"ts_code", "name", "industry", "symbol"}


class ClickHouseUnavailableError(RuntimeError):
    """Raised when the ClickHouse connector cannot be constructed or queried."""


def get_connector():
    """Construct an env-configured ``ClickHouseConnector``.

    Returns:
        src.clickhouse_connector.ClickHouseConnector instance.

    Raises:
        ClickHouseUnavailableError: if the Vibe-Trading ``src`` package is
            not importable in this environment.
    """
    try:
        from src.clickhouse_connector import ClickHouseConnector
    except Exception as exc:  # noqa: BLE001 — import failure == unavailable
        raise ClickHouseUnavailableError(
            f"Vibe-Trading ClickHouse connector not importable: {exc}"
        ) from exc
    return ClickHouseConnector()


def coerce_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce numeric-looking columns into real numbers.

    The ClickHouse HTTP connector returns JSONCompact payloads where every
    value arrives as a string.  Screening math (medians, Z-scores, sorting)
    needs real numbers, so every column outside :data:`_STRING_COLUMNS` is
    converted with ``pd.to_numeric`` (non-parseable values become NaN).
    """
    if df.empty:
        return df
    for col in df.columns:
        if col in _STRING_COLUMNS:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def run_query(sql: str) -> pd.DataFrame:
    """Execute *sql* against ClickHouse and return a numeric-coerced DataFrame.

    Raises:
        ClickHouseUnavailableError: when the connector package is missing or
            the server is unreachable / returns an error.
    """
    conn = get_connector()
    try:
        df = conn.query(sql)
    except ConnectionError as exc:
        raise ClickHouseUnavailableError(str(exc)) from exc
    return coerce_numeric_columns(df)
