"""
Shared utilities for microstructure indicator modules.

All functions are stateless and can be reused by any indicator script
without side effects.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


# ── DuckDB connection helper ────────────────────────────────────────────────


def get_connection(duckdb_path: str, *, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Open (and return) a DuckDB connection.

    Parameters
    ----------
    duckdb_path : str
        Path to the ``.duckdb`` file.
    read_only : bool
        If ``True`` (default), opens the database in read-only mode.

    Returns
    -------
    duckdb.DuckDBPyConnection
        An open connection.  The caller is responsible for closing it.
    """
    return duckdb.connect(duckdb_path, read_only=read_only)


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