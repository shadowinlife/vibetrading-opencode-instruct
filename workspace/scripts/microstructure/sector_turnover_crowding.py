"""
Sector turnover crowding indicator (candidate #7).

Joins ``stk_factor_pro`` turnover with ``idx_sw_member_all`` current industry
membership, computes per-sector daily turnover totals, then derives:

  - **HHI** (Herfindahl-Hirschman Index) across sectors — higher values mean
    market turnover is increasingly concentrated in fewer sectors.
  - **top_sector_share** — turnover share of the largest sector.
  - **top3_sector_share** — combined share of the three largest sectors.

Operates at L1 (broad), L2 (medium), and L3 (fine-grain) sector levels.

.. warning::
   ``idx_sw_member_all`` is a **current-snapshot only** table (3 000 stocks,
   all with ``out_date IS NULL``).  ~49 % of trading stocks lack industry
   membership.  The membership gap is reported explicitly in every output.

"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

import duckdb
import pandas as pd

from .base import format_date, get_connection, write_json
from .metadata import DEFAULT_DUCKDB_PATH

# ── Type aliases ──────────────────────────────────────────────────────────────

SectorLevel = Literal["L1", "L2", "L3"]

SectorCrowdingSummary = dict[str, Any]


# ── Constants ─────────────────────────────────────────────────────────────────

_VALID_LEVELS: tuple[SectorLevel, ...] = ("L1", "L2", "L3")

_SECTOR_QUERY_TEMPLATE = """
WITH raw AS (
    SELECT
        f.trade_date,
        f.ts_code,
        f.amount,
        m.{code_col} AS sector_code,
        m.{name_col} AS sector_name
    FROM stk_factor_pro f
    LEFT JOIN (
        SELECT DISTINCT ts_code, {code_col}, {name_col}
        FROM idx_sw_member_all
        WHERE out_date IS NULL AND {code_col} IS NOT NULL
    ) m ON f.ts_code = m.ts_code
    WHERE f.amount IS NOT NULL
      AND f.amount > 0
      AND (f.ts_code LIKE '%.SH' OR f.ts_code LIKE '%.SZ' OR f.ts_code LIKE '%.BJ')
      {where_clause}
),
daily_sector AS (
    SELECT
        trade_date,
        sector_code,
        sector_name,
        SUM(amount) AS sector_amount,
        COUNT(DISTINCT ts_code) AS sector_stock_count
    FROM raw
    WHERE sector_code IS NOT NULL
    GROUP BY trade_date, sector_code, sector_name
),
daily_totals AS (
    SELECT
        trade_date,
        SUM(sector_amount) AS matched_total_amount,
        SUM(sector_stock_count) AS matched_stock_count,
        COUNT(*) AS n_sectors
    FROM daily_sector
    GROUP BY trade_date
),
-- HHI = sum( (sector_i / total)^2 ) scaled to [0, 1]
daily_hhi AS (
    SELECT
        s.trade_date,
        SUM(POWER(s.sector_amount / NULLIF(t.matched_total_amount, 0), 2)) AS hhi
    FROM daily_sector s
    JOIN daily_totals t ON s.trade_date = t.trade_date
    GROUP BY s.trade_date
),
daily_ranked AS (
    SELECT
        s.*,
        ROW_NUMBER() OVER (PARTITION BY s.trade_date ORDER BY s.sector_amount DESC) AS sector_rank
    FROM daily_sector s
),
top_shares AS (
    SELECT
        r.trade_date,
        MAX(CASE WHEN r.sector_rank = 1 THEN r.sector_amount / NULLIF(t.matched_total_amount, 0) END) AS top_sector_share,
        MAX(CASE WHEN r.sector_rank = 1 THEN r.sector_code END) AS top_sector_code,
        MAX(CASE WHEN r.sector_rank = 1 THEN r.sector_name END) AS top_sector_name,
        SUM(CASE WHEN r.sector_rank <= 3 THEN r.sector_amount END)
            / NULLIF(MAX(t.matched_total_amount), 0) AS top3_sector_share
    FROM daily_ranked r
    JOIN daily_totals t ON r.trade_date = t.trade_date
    GROUP BY r.trade_date
),
gap AS (
    SELECT
        r.trade_date,
        SUM(r.amount) AS raw_total_amount,
        COUNT(DISTINCT r.ts_code) AS raw_stock_count,
        COUNT(DISTINCT CASE WHEN r.sector_code IS NULL THEN r.ts_code END) AS unmatched_stock_count,
        SUM(CASE WHEN r.sector_code IS NULL THEN r.amount ELSE 0 END) AS unmatched_amount
    FROM raw r
    GROUP BY r.trade_date
)
SELECT
    t.trade_date,
    t.n_sectors,
    t.matched_total_amount,
    t.matched_stock_count,
    h.hhi,
    ts.top_sector_share,
    ts.top_sector_code,
    ts.top_sector_name,
    ts.top3_sector_share,
    g.raw_total_amount,
    g.raw_stock_count,
    g.unmatched_stock_count,
    g.unmatched_amount,
    -- industry membership coverage ratios
    g.raw_stock_count - g.unmatched_stock_count AS matched_stock_count_v2,
    CASE WHEN g.raw_total_amount > 0
         THEN (g.raw_total_amount - g.unmatched_amount) / g.raw_total_amount
         ELSE 1.0 END AS matched_turnover_share,
    CASE WHEN g.raw_stock_count > 0
         THEN (g.raw_stock_count - g.unmatched_stock_count) * 1.0 / g.raw_stock_count
         ELSE 1.0 END AS matched_stock_share
FROM daily_totals t
JOIN daily_hhi h ON t.trade_date = h.trade_date
JOIN top_shares ts ON t.trade_date = ts.trade_date
JOIN gap g ON t.trade_date = g.trade_date
ORDER BY t.trade_date
"""


# ── Private helpers ───────────────────────────────────────────────────────────


def _validate_level(level: str) -> SectorLevel:
    """Normalise and validate the sector level."""
    level_up = level.upper().strip()
    if level_up not in _VALID_LEVELS:
        raise ValueError(
            f"Invalid sector level '{level}'. Must be one of {_VALID_LEVELS}."
        )
    return level_up  # type: ignore[return-value]


def _validate_date_window(
    start_date: str | date | None,
    end_date: str | date | None,
) -> tuple[str | None, str | None]:
    """Normalise and validate an optional date window."""
    if start_date is None and end_date is not None:
        raise ValueError("--start-date is required when --end-date is specified")
    if end_date is not None and start_date > end_date:  # type: ignore[operator]
        raise ValueError(f"start_date ({start_date}) must be <= end_date ({end_date})")

    start_str = format_date(start_date) if start_date is not None else None
    end_str = format_date(end_date) if end_date is not None else None
    return start_str, end_str


def _build_query(
    sector_level: SectorLevel,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Build the parameterised DuckDB query for sector turnover crowding."""
    code_col = f"{sector_level.lower()}_code"
    name_col = f"{sector_level.lower()}_name"

    where_clauses: list[str] = []
    if start_date is not None:
        where_clauses.append(f"f.trade_date >= '{start_date}'")
    if end_date is not None:
        where_clauses.append(f"f.trade_date <= '{end_date}'")
    where_line = f"AND {' AND '.join(where_clauses)}" if where_clauses else ""

    return _SECTOR_QUERY_TEMPLATE.format(
        code_col=code_col,
        name_col=name_col,
        where_clause=where_line,
    )


def _build_summary(
    df: pd.DataFrame,
    sector_level: SectorLevel,
) -> SectorCrowdingSummary:
    """Construct the summary dict from the daily-aggregated DataFrame."""
    latest: Any = df.iloc[-1]
    latest_trade_date: pd.Timestamp = pd.Timestamp(latest["trade_date"])  # type: ignore[arg-type, assignment]

    # -- historical extremes --
    max_hhi_idx: int = int(df["hhi"].idxmax())  # type: ignore[arg-type]
    max_hhi_row: Any = df.loc[max_hhi_idx]
    max_share_idx: int = int(df["top_sector_share"].idxmax())  # type: ignore[arg-type]
    max_share_row: Any = df.loc[max_share_idx]

    # -- HHI rank/percentile --
    df_hhi_sorted = df.sort_values(
        ["hhi", "trade_date"], ascending=[False, True]
    ).reset_index(drop=True)
    hhi_rank_mask = df_hhi_sorted["trade_date"] == latest_trade_date
    latest_hhi_rank: int = int(hhi_rank_mask.idxmax()) + 1  # type: ignore[arg-type]
    latest_hhi_percentile: float = float(
        (df["hhi"] <= latest["hhi"]).mean()
    )

    # -- threshold-hit stats for top-sector share --
    thresholds = [0.15, 0.20, 0.25, 0.30]
    threshold_stats: dict[str, dict[str, Any]] = {}
    for t in thresholds:
        hits = df[df["top_sector_share"] >= t]
        threshold_stats[str(round(t, 2))] = {
            "count": int(len(hits)),
            "pct_of_days": round(len(hits) / len(df) * 100, 2) if len(df) > 0 else 0.0,
            "dates": [
                format_date(d)
                for d in pd.to_datetime(hits["trade_date"]).tolist()[:20]
            ],
        }

    # -- gap report (use latest day) --
    gap_report: dict[str, Any] = {
        "latest_matched_stock_count": int(latest["matched_stock_count_v2"]),
        "latest_raw_stock_count": int(latest["raw_stock_count"]),
        "latest_matched_stock_share": float(latest["matched_stock_share"]),
        "latest_matched_turnover_share": float(latest["matched_turnover_share"]),
        "latest_unmatched_stock_count": int(latest["unmatched_stock_count"]),
        "latest_unmatched_amount_kcny": float(latest["unmatched_amount"]),
        "note": (
            "idx_sw_member_all is a current-snapshot table (3 000 members). "
            "~49 % of trading stocks lack industry membership. "
            "Historical analysis uses current membership as proxy — "
            "this introduces look-ahead bias for historical dates."
        ),
    }

    # -- historical gap stats --
    gap_report["historical_mean_matched_turnover_share"] = float(
        df["matched_turnover_share"].mean()
    )
    gap_report["historical_min_matched_turnover_share"] = float(
        df["matched_turnover_share"].min()
    )
    gap_report["historical_max_matched_turnover_share"] = float(
        df["matched_turnover_share"].max()
    )

    # -- daily series --
    daily_series: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        row_date = pd.Timestamp(row["trade_date"])  # type: ignore[arg-type]
        daily_series.append({
            "trade_date": format_date(row_date),
            "n_sectors": int(row["n_sectors"]),
            "hhi": float(row["hhi"]),
            "hhi_pct": float(row["hhi"] * 100),
            "top_sector_share": float(row["top_sector_share"]),
            "top_sector_share_pct": float(row["top_sector_share"] * 100),
            "top_sector_code": str(row["top_sector_code"]),
            "top_sector_name": str(row["top_sector_name"]),
            "top3_sector_share": float(row["top3_sector_share"]),
            "top3_sector_share_pct": float(row["top3_sector_share"] * 100),
            "matched_total_amount_billion_cny": float(
                row["matched_total_amount"] / 1_000_000
            ),
            "matched_stock_count": int(row["matched_stock_count"]),
            "n_sectors_effective": int(row["n_sectors"]),
            "matched_turnover_share": float(row["matched_turnover_share"]),
            "matched_stock_share": float(row["matched_stock_share"]),
        })

    # -- top-10 HHI dates --
    top10_hhi: list[dict[str, Any]] = []
    for _, row in df_hhi_sorted.head(10).iterrows():
        row_date = pd.Timestamp(row["trade_date"])  # type: ignore[arg-type]
        top10_hhi.append({
            "trade_date": format_date(row_date),
            "hhi_pct": float(row["hhi"] * 100),
            "top_sector_share_pct": float(row["top_sector_share"] * 100),
        })

    return {
        "sector_level": sector_level,
        "latest_trade_date": format_date(latest_trade_date),  # type: ignore[arg-type]
        "latest_hhi": float(latest["hhi"]),
        "latest_hhi_pct": float(latest["hhi"] * 100),
        "latest_n_sectors": int(latest["n_sectors"]),
        "latest_top_sector_share": float(latest["top_sector_share"]),
        "latest_top_sector_share_pct": float(latest["top_sector_share"] * 100),
        "latest_top_sector_code": str(latest["top_sector_code"]),
        "latest_top_sector_name": str(latest["top_sector_name"]),
        "latest_top3_sector_share": float(latest["top3_sector_share"]),
        "latest_top3_sector_share_pct": float(latest["top3_sector_share"] * 100),
        "latest_matched_total_amount_billion_cny": float(
            latest["matched_total_amount"] / 1_000_000
        ),
        "historical_max_hhi": float(max_hhi_row["hhi"]),
        "historical_max_hhi_pct": float(max_hhi_row["hhi"] * 100),
        "historical_max_hhi_date": format_date(
            pd.Timestamp(max_hhi_row["trade_date"])  # type: ignore[arg-type]
        ),
        "historical_max_top_sector_share": float(max_share_row["top_sector_share"]),
        "historical_max_top_sector_share_pct": float(
            max_share_row["top_sector_share"] * 100
        ),
        "historical_max_top_share_date": format_date(
            pd.Timestamp(max_share_row["trade_date"])  # type: ignore[arg-type]
        ),
        "hhi_rank_of_latest": latest_hhi_rank,
        "hhi_percentile_of_latest": float(latest_hhi_percentile),
        "top10_hhi_dates": top10_hhi,
        "top_sector_share_threshold_stats": threshold_stats,
        "membership_gap_report": gap_report,
        "daily_series": daily_series,
    }


# ── Public API ────────────────────────────────────────────────────────────────


def compute_sector_crowding(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    sector_level: str = "L1",
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> SectorCrowdingSummary:
    """Compute sector turnover crowding (HHI, top-sector share, top-3 share).

    Parameters
    ----------
    con_or_path : duckdb.DuckDBPyConnection or str
        An open DuckDB connection *or* a path to a ``.duckdb`` file.
    sector_level : str
        Sector granularity: ``"L1"`` (broad, 31 sectors), ``"L2"`` (medium,
        129 sectors), or ``"L3"`` (fine-grain, 314 sectors).
    start_date : str or date, optional
        Earliest trade date (inclusive).
    end_date : str or date, optional
        Latest trade date (inclusive).

    Returns
    -------
    SectorCrowdingSummary
        Dictionary with keys:

        * ``sector_level``
        * ``latest_trade_date``
        * ``latest_hhi`` / ``latest_hhi_pct``
        * ``latest_n_sectors``
        * ``latest_top_sector_share`` / ``latest_top_sector_share_pct``
        * ``latest_top_sector_code`` / ``latest_top_sector_name``
        * ``latest_top3_sector_share`` / ``latest_top3_sector_share_pct``
        * ``latest_matched_total_amount_billion_cny``
        * ``historical_max_hhi`` / ``historical_max_hhi_pct`` / ``historical_max_hhi_date``
        * ``historical_max_top_sector_share`` / ``*_pct`` / ``*_date``
        * ``hhi_rank_of_latest`` / ``hhi_percentile_of_latest``
        * ``top10_hhi_dates``
        * ``top_sector_share_threshold_stats``
        * ``membership_gap_report``
        * ``daily_series``
    """
    level = _validate_level(sector_level)
    start_str, end_str = _validate_date_window(start_date, end_date)

    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    try:
        query = _build_query(level, start_date=start_str, end_date=end_str)
        df = con.execute(query).fetchdf()
    finally:
        if own_connection:
            con.close()

    if df.empty:
        raise ValueError(
            "No data returned. Check the date window and ensure "
            "stk_factor_pro and idx_sw_member_all contain valid data."
        )

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return _build_summary(df, level)


def compute_all_levels(
    con_or_path: duckdb.DuckDBPyConnection | str = DEFAULT_DUCKDB_PATH,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> dict[str, SectorCrowdingSummary]:
    """Compute sector crowding for all three levels (L1, L2, L3).

    Returns a dict mapping level name to its ``SectorCrowdingSummary``.
    """
    own_connection = isinstance(con_or_path, str)
    if own_connection:
        con = get_connection(con_or_path, read_only=True)
    else:
        con = con_or_path

    results: dict[str, SectorCrowdingSummary] = {}
    try:
        for level in _VALID_LEVELS:
            results[level] = compute_sector_crowding(
                con,
                sector_level=level,
                start_date=start_date,
                end_date=end_date,
            )
    finally:
        if own_connection:
            con.close()

    return results


# ── CLI convenience ───────────────────────────────────────────────────────────


def _main() -> None:
    """Simple CLI for quick inspection."""
    import argparse

    parser = argparse.ArgumentParser(description="Sector turnover crowding")
    parser.add_argument("--duckdb-path", default=DEFAULT_DUCKDB_PATH)
    parser.add_argument("--level", choices=["L1", "L2", "L3"], default="L1")
    parser.add_argument("--all-levels", action="store_true",
                        help="Compute for L1, L2, and L3")
    parser.add_argument("--output", default=None, help="Path to write JSON")
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    args = parser.parse_args()

    if args.all_levels:
        results = compute_all_levels(
            args.duckdb_path, start_date=args.start_date, end_date=args.end_date,
        )
        if args.output:
            write_json(results, args.output)
        for level, summary in results.items():
            print(f"\n=== {level} ===")
            print(f"  Latest HHI: {summary['latest_hhi_pct']:.2f}%")
            print(f"  Top sector: {summary['latest_top_sector_name']} "
                  f"({summary['latest_top_sector_share_pct']:.1f}%)")
            print(f"  Membership coverage: "
                  f"{summary['membership_gap_report']['latest_matched_turnover_share']*100:.1f}% "
                  f"of turnover")
    else:
        result = compute_sector_crowding(
            args.duckdb_path, sector_level=args.level,
            start_date=args.start_date, end_date=args.end_date,
        )
        if args.output:
            write_json(result, args.output)
        print(f"Level: {result['sector_level']}")
        print(f"Latest HHI: {result['latest_hhi_pct']:.2f}%  "
              f"(rank {result['hhi_rank_of_latest']}/{len(result['daily_series'])}, "
              f"pct {result['hhi_percentile_of_latest']*100:.1f})")
        print(f"Top sector: {result['latest_top_sector_name']} "
              f"({result['latest_top_sector_share_pct']:.1f}%)")
        print(f"Top-3 share: {result['latest_top3_sector_share_pct']:.1f}%")
        print(f"Membership coverage: "
              f"{result['membership_gap_report']['latest_matched_turnover_share']*100:.1f}% "
              f"of turnover")

if __name__ == "__main__":
    _main()