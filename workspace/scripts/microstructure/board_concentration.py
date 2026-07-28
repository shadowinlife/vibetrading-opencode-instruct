"""
Board-level concentration breakdown for post-RED index anchor detection.

When the joint escape-top system fires a RED signal, this module identifies
which broad-based index (科创50, 创业板指, 上证50, 深证成指, etc.) has the
highest concentration of top-turnover stocks, and recommends it as the
primary tracking anchor for post-RED drawdown analysis.

**Design rationale** (from 2026-07-06 analysis):

* The global top-5% concentration metric aggregates across all boards,
  masking where the heat actually lives.
* Per-board **penetration rate** (fraction of board stocks in the global
  top-5%) is the most informative signal: a 7% penetration on 科创板
  vs 5% on 沪主板 means 科创板 is disproportionately overheated.
* The board with the highest penetration rate maps to a canonical
  broad-based index that best reflects the concentration risk.

Current board → index mapping (see ``metadata.BOARD_INDEX_MAP``):

* 科创板 → 科创50 (000688.SH)
* 创业板 → 创业板指 (399006.SZ)
* 沪主板 → 上证50 (000016.SH)
* 深主板 → 深证成指 (399001.SZ)
"""

from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd

from .base import format_date, get_connection
from .metadata import (
    BOARD_CLASSIFY_RULES,
    BOARD_INDEX_MAP,
    CONCENTRATION_TOP_PCT,
    DEFAULT_DUCKDB_PATH,
)

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

BoardConcentrationResult = dict[str, Any]


# ── Private helpers ──────────────────────────────────────────────────────────


def _classify_board(ts_code: str) -> str:
    """Classify a Tushare-style ts_code into a board label.

    Uses prefix matching against ``BOARD_CLASSIFY_RULES``.
    Returns ``"其他"`` for unrecognised codes.
    """
    code_prefix = ts_code.split(".")[0] if "." in ts_code else ts_code
    for board, prefixes in BOARD_CLASSIFY_RULES:
        if code_prefix.startswith(prefixes):
            return board
    return "其他"


def _build_board_query(trade_date: str, top_pct: float) -> str:
    """Build DuckDB query for board-level concentration on a single date."""
    pct = top_pct / 100.0
    return f"""
    WITH base AS (
        SELECT
            ts_code,
            amount,
            ROW_NUMBER() OVER (ORDER BY amount DESC, ts_code) AS rn,
            COUNT(*) OVER () AS stock_count,
            SUM(amount) OVER () AS total_amount
        FROM stk_factor_pro
        WHERE trade_date = '{trade_date}'
          AND amount IS NOT NULL
          AND amount > 0
          AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ' OR ts_code LIKE '%.BJ')
    ),
    top_stocks AS (
        SELECT
            ts_code,
            amount,
            rn,
            stock_count,
            total_amount,
            CASE WHEN rn <= CEIL(stock_count * {pct}) THEN 1 ELSE 0 END AS is_top
        FROM base
    )
    SELECT
        ts_code,
        amount,
        is_top,
        stock_count,
        total_amount
    FROM top_stocks
    ORDER BY amount DESC
    """


# ── Public API ───────────────────────────────────────────────────────────────


def compute_board_concentration(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    trade_date: str,
    top_pct: float = CONCENTRATION_TOP_PCT,
) -> BoardConcentrationResult:
    """Compute per-board concentration breakdown for a single trade date.

    Parameters
    ----------
    con_or_path : duckdb.DuckDBPyConnection or str
        DuckDB connection or path.
    trade_date : str
        Target trade date in ``YYYY-MM-DD`` format.
    top_pct : float
        Percentage of stocks in the global top group (default 5.0).

    Returns
    -------
    BoardConcentrationResult
        Dictionary with keys:

        * ``trade_date`` — the evaluated date
        * ``total_stocks`` — total stocks with turnover
        * ``top_n`` — number of stocks in the global top group
        * ``boards`` — list of per-board breakdown dicts, each with:
          ``board``, ``total_stocks``, ``top_stocks``, ``penetration_pct``,
          ``board_amount``, ``top_amount``, ``top_amount_share_pct``
        * ``richest_board`` — board label with highest penetration rate
        * ``anchor_index`` — dict with ``index_code``, ``index_name`` for
          the recommended tracking index
        * ``anchor_rationale`` — human-readable explanation
    """
    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = _build_board_query(trade_date, top_pct)
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df.empty:
        raise ValueError(
            f"No data for trade_date={trade_date}. "
            "Ensure stk_factor_pro contains valid turnover data."
        )

    return _build_result(df, trade_date, top_pct)


def _build_result(
    df: pd.DataFrame,
    trade_date: str,
    top_pct: float,
) -> BoardConcentrationResult:
    """Construct the structured result from the raw query DataFrame."""
    # Classify each stock
    df = df.copy()
    df["board"] = df["ts_code"].apply(_classify_board)

    total_stocks = int(df["stock_count"].iloc[0])
    top_n = int((total_stocks * top_pct / 100.0 + 0.999))  # ceil
    total_amount = float(df["total_amount"].iloc[0])

    # Per-board aggregation
    board_stats: list[dict[str, Any]] = []
    for board, grp in df.groupby("board"):
        if board == "其他":
            continue
        board_total = len(grp)
        board_top = int(grp["is_top"].sum())
        board_amount = float(grp["amount"].sum())
        top_amount = float(grp.loc[grp["is_top"] == 1, "amount"].sum())

        penetration = (board_top / board_total * 100.0) if board_total > 0 else 0.0
        top_share = (top_amount / board_amount * 100.0) if board_amount > 0 else 0.0

        board_stats.append({
            "board": board,
            "total_stocks": board_total,
            "top_stocks": board_top,
            "penetration_pct": round(penetration, 2),
            "board_amount_billion_cny": round(board_amount / 1e8, 2),
            "top_amount_billion_cny": round(top_amount / 1e8, 2),
            "top_amount_share_pct": round(top_share, 2),
        })

    # Sort by penetration rate descending
    board_stats.sort(key=lambda x: x["penetration_pct"], reverse=True)

    # Identify richest board and anchor index
    richest_board = board_stats[0]["board"] if board_stats else "未知"
    anchor_info = BOARD_INDEX_MAP.get(richest_board, {})
    anchor_index = {
        "index_code": anchor_info.get("index_code", ""),
        "index_name": anchor_info.get("index_name", ""),
    }

    # Build rationale
    top_boards = [
        f"{b['board']}({b['penetration_pct']:.1f}%)"
        for b in board_stats[:3]
    ]
    rationale = (
        f"Top-{top_pct:.0f}% 成交额标的渗透率最高板块: "
        f"{', '.join(top_boards)}。"
        f"推荐跟踪 {anchor_index['index_name']}({anchor_index['index_code']}) "
        f"作为 RED 信号后回调观测锚点。"
    )

    return {
        "trade_date": trade_date,
        "total_stocks": total_stocks,
        "top_n": top_n,
        "top_pct": top_pct,
        "boards": board_stats,
        "richest_board": richest_board,
        "anchor_index": anchor_index,
        "anchor_rationale": rationale,
    }
