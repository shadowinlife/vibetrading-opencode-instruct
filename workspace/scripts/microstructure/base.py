"""
Shared utilities for microstructure indicator modules.

Data layer
----------
All market data is read through the Vibe-Trading ClickHouse connector
(``src.clickhouse_connector.ClickHouseConnector``).  Connection settings
come **exclusively** from environment variables (``CLICKHOUSE_HOST``,
``CLICKHOUSE_PORT``, ``CLICKHOUSE_USER``, ``CLICKHOUSE_PASSWORD``,
``CLICKHOUSE_DATABASE``) via the VT ``DataConfig`` defaults — never
hardcode connection parameters in this package.

All functions are stateless and can be reused by any indicator script
without side effects.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.clickhouse_connector import ClickHouseConnector

__all__ = [
    "ClickHouseConnector",
    "get_connection",
    "connection_available",
    "query_dataframe",
    "unavailable_payload",
    "validate_trade_date",
    "write_json",
    "format_date",
    "pct_rank",
    "top_pct_mask",
    "rolling_zscore",
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── ClickHouse connection helpers ────────────────────────────────────────────


def get_connection() -> ClickHouseConnector:
    """Return a Vibe-Trading ClickHouse connector.

    Connection parameters are resolved from the ``CLICKHOUSE_*``
    environment variables (VT ``DataConfig`` defaults apply); no path
    argument exists because the legacy file-based warehouse path has been retired.

    Returns
    -------
    ClickHouseConnector
        Connector to the configured ClickHouse instance.  The connector
        is a stateless HTTP client — no close is required.
    """
    return ClickHouseConnector()


def connection_available() -> bool:
    """Return ``True`` when the configured ClickHouse instance is reachable.

    Wraps ``ClickHouseConnector.health_check()``; never raises.
    """
    try:
        return get_connection().health_check()
    except Exception:  # noqa: BLE001 - degradation probe must never raise
        return False


def unavailable_payload(reason: str = "ClickHouse unreachable") -> dict[str, Any]:
    """Standard degradation payload emitted by CLIs when ClickHouse is down.

    Mirrors the data-warehouse skill convention: CLIs print this JSON
    object and exit 0 instead of raising a raw traceback.
    """
    return {"available": False, "reason": reason}


def query_dataframe(
    conn: ClickHouseConnector,
    sql: str,
    params: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Execute *sql* via the ClickHouse connector and return a typed frame.

    The ClickHouse HTTP interface (``JSONCompact``) serialises 64-bit
    integers and decimals as strings.  To preserve the typed semantics the
    indicator code relies on, every object column whose non-empty values
    all parse as numbers is coerced to a numeric dtype.  Date-like and
    text columns (e.g. ``trade_date``, ``ts_code``) are left as strings.

    Parameters
    ----------
    conn : ClickHouseConnector
        Connector obtained from :func:`get_connection`.
    sql : str
        ClickHouse SQL statement.
    params : dict[str, str] or None
        Optional ClickHouse parameter bindings (``{"param_name": value}``)
        for ``{name:Type}`` placeholders in *sql*.

    Returns
    -------
    pd.DataFrame
        Query result with numeric columns coerced from string encoding.
    """
    df = conn.query(sql, params=params)
    if df.empty:
        return df
    for col in df.columns:
        series = df[col]
        if series.dtype != object:
            continue
        stripped = series.astype("string").str.strip()
        numeric = pd.to_numeric(stripped, errors="coerce")
        non_empty = stripped.notna() & (stripped != "")
        if non_empty.any() and numeric[non_empty].notna().all():
            df[col] = numeric
    return df


def validate_trade_date(value: str, *, name: str = "date") -> str:
    """Validate a ``YYYY-MM-DD`` date string before SQL interpolation.

    Raises
    ------
    ValueError
        When *value* does not match the strict ``YYYY-MM-DD`` shape.
    """
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
    return value


# ── JSON writer ─────────────────────────────────────────────────────────────


def write_json(data: dict[str, Any] | list[Any], path: str | Path, /) -> Path:
    """Serialize *data* to a UTF-8 JSON file.

    Creates parent directories if they do not exist.

    Parameters
    ----------
    data
        JSON-serialisable object.
    path
        Destination file path (``str`` or ``Path``).

    Returns
    -------
    Path
        The written file path.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return dest


# ── Date helpers ────────────────────────────────────────────────────────────


def format_date(d: date | datetime | pd.Timestamp | str, /) -> str:
    """Normalise a date-like value to ``"YYYY-MM-DD"`` string.

    Parameters
    ----------
    d
        A ``datetime.date``, ``datetime.datetime``, ``pd.Timestamp``,
        or already-formatted ``"YYYY-MM-DD"`` string.

    Returns
    -------
    str
        ISO-8601 date string, e.g. ``"2025-05-27"``.
    """
    if isinstance(d, str):
        return d[:10]  # already YYYY-MM-DD or truncated
    if isinstance(d, (date, pd.Timestamp)):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, datetime):
        return d.date().isoformat()
    raise TypeError(f"Unsupported date type: {type(d)}")


# ── Series utilities ────────────────────────────────────────────────────────


def pct_rank(series: pd.Series) -> pd.Series:
    """Compute percentile rank (0‑100) for each element in *series*.

    Uses ``method='average'`` and scales to ``[0, 100]``.

    Parameters
    ----------
    series : pd.Series
        Numeric series.

    Returns
    -------
    pd.Series
        Same index, values in ``[0, 100]``.
    """
    return series.rank(pct=True) * 100.0


def top_pct_mask(series: pd.Series, pct: float) -> pd.Series:
    """Return a boolean mask for elements whose percentile rank ≥ (100 − *pct*).

    Parameters
    ----------
    series : pd.Series
        Numeric series.
    pct : float
        Percentage threshold, e.g. ``5.0`` for top 5 %.

    Returns
    -------
    pd.Series
        Boolean mask, same index as *series*.
    """
    return pct_rank(series) >= (100.0 - pct)


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Compute rolling Z-score: ``(x − μ) / σ`` over *window* periods.

    Parameters
    ----------
    series : pd.Series
        Numeric series sorted chronologically.
    window : int
        Look-back window in periods (trading days).

    Returns
    -------
    pd.Series
        Z-score series.  Leading ``window-1`` rows contain ``NaN``.
    """
    roll = series.rolling(window, min_periods=window)
    mean = roll.mean()
    std = roll.std(ddof=0)
    return (series - mean) / std.replace({0.0: float("nan")})
