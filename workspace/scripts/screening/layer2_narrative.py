"""Layer 2: Narrative momentum screening via DuckDB.

Screens stocks by:
- Sector momentum (Shenwan L2 industry performance ranking)
- Volume/amount momentum (recent vs historical trading activity)
- Turnover rate change

Usage:
    from scripts.screening.layer2_narrative import screen_narrative
    df = screen_narrative()
"""
from __future__ import annotations

import duckdb
import pandas as pd
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "duckdb" / "ashare.duckdb"

# ── SQL: Sector Momentum ──
# Ranks Shenwan L2 industries by average pct_chg over recent N days,
# then returns stocks in top-performing industries
SECTOR_MOMENTUM_SQL = """
WITH industry_perf AS (
    SELECT
        sw.l2_name AS industry,
        AVG(p.pct_chg) AS avg_pct_chg,
        SUM(p.amount) AS total_amount,
        COUNT(DISTINCT p.ts_code) AS stock_count
    FROM stk_factor_pro p
    JOIN idx_sw_member_all sw ON p.ts_code = sw.ts_code
    LEFT JOIN stk_st_daily st ON p.ts_code = st.ts_code AND p.trade_date = st.trade_date
    WHERE p.trade_date >= '{start_date}'
      AND p.trade_date <= '{trade_date}'
      AND st.ts_code IS NULL
    GROUP BY sw.l2_name
    ORDER BY avg_pct_chg DESC
),
top_industries AS (
    SELECT industry, avg_pct_chg, total_amount, stock_count
    FROM industry_perf
    LIMIT {top_n_industries}
)
SELECT
    p.ts_code,
    si.name,
    sw.l2_name AS industry,
    ti.avg_pct_chg AS industry_avg_pct_chg,
    ROUND(p.pct_chg, 2) AS pct_chg,
    ROUND(p.amount / 10000, 2) AS amount_wan,
    ROUND(p.turnover_rate, 2) AS turnover_rate,
    ROUND(p.total_mv / 10000, 2) AS total_mv_yi
FROM stk_factor_pro p
JOIN stk_info si ON p.ts_code = si.ts_code
JOIN idx_sw_member_all sw ON p.ts_code = sw.ts_code
JOIN top_industries ti ON sw.l2_name = ti.industry
LEFT JOIN stk_st_daily st ON p.ts_code = st.ts_code AND p.trade_date = st.trade_date
WHERE p.trade_date = '{trade_date}'
  AND st.ts_code IS NULL
ORDER BY ti.avg_pct_chg DESC, p.amount DESC
"""

# ── SQL: Volume/Amount Momentum ──
# Compares recent 20-day average amount/turnover vs 60-day average
VOLUME_MOMENTUM_SQL = """
WITH recent AS (
    SELECT
        ts_code,
        AVG(amount) AS avg_amount_20d,
        AVG(turnover_rate) AS avg_turnover_20d,
        AVG(pct_chg) AS avg_pct_chg_20d
    FROM stk_factor_pro
    WHERE trade_date >= '{start_20d}'
      AND trade_date <= '{trade_date}'
    GROUP BY ts_code
),
historical AS (
    SELECT
        ts_code,
        AVG(amount) AS avg_amount_60d,
        AVG(turnover_rate) AS avg_turnover_60d
    FROM stk_factor_pro
    WHERE trade_date >= '{start_60d}'
      AND trade_date <= '{trade_date}'
    GROUP BY ts_code
)
SELECT
    r.ts_code,
    si.name,
    si.industry,
    ROUND(r.avg_amount_20d / 10000, 2) AS avg_amount_20d_wan,
    ROUND(h.avg_amount_60d / 10000, 2) AS avg_amount_60d_wan,
    ROUND(r.avg_amount_20d / NULLIF(h.avg_amount_60d, 0), 2) AS amount_ratio,
    ROUND(r.avg_turnover_20d, 2) AS avg_turnover_20d,
    ROUND(h.avg_turnover_60d, 2) AS avg_turnover_60d,
    ROUND(r.avg_turnover_20d / NULLIF(h.avg_turnover_60d, 0), 2) AS turnover_ratio,
    ROUND(r.avg_pct_chg_20d, 2) AS avg_pct_chg_20d
FROM recent r
JOIN historical h ON r.ts_code = h.ts_code
JOIN stk_info si ON r.ts_code = si.ts_code
LEFT JOIN stk_st_daily st ON r.ts_code = st.ts_code AND st.trade_date = '{trade_date}'
WHERE st.ts_code IS NULL
  AND h.avg_amount_60d > 0
  AND r.avg_amount_20d / h.avg_amount_60d > {amount_ratio_min}
  AND r.avg_turnover_20d / h.avg_turnover_60d > {turnover_ratio_min}
ORDER BY r.avg_amount_20d / h.avg_amount_60d DESC
"""

DEFAULT_PARAMS = {
    "trade_date": "2026-06-26",
    "start_date": "2026-06-06",     # 20 trading days back
    "start_20d": "2026-05-26",      # 20 trading days
    "start_60d": "2026-03-26",      # 60 trading days
    "top_n_industries": 10,
    "amount_ratio_min": 1.2,        # 20d amount > 1.2x 60d amount
    "turnover_ratio_min": 1.1,      # 20d turnover > 1.1x 60d turnover
}


def screen_sector_momentum(
    trade_date: str | None = None,
    start_date: str | None = None,
    top_n_industries: int | None = None,
) -> pd.DataFrame:
    """Screen stocks in top-performing Shenwan L2 industries."""
    params = dict(DEFAULT_PARAMS)
    if trade_date is not None: params["trade_date"] = trade_date
    if start_date is not None: params["start_date"] = start_date
    if top_n_industries is not None: params["top_n_industries"] = top_n_industries

    sql = SECTOR_MOMENTUM_SQL.format(**params)
    db = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return db.execute(sql).fetchdf()
    finally:
        db.close()


def screen_volume_momentum(
    trade_date: str | None = None,
    start_20d: str | None = None,
    start_60d: str | None = None,
    amount_ratio_min: float | None = None,
    turnover_ratio_min: float | None = None,
) -> pd.DataFrame:
    """Screen stocks with rising volume/amount momentum."""
    params = dict(DEFAULT_PARAMS)
    if trade_date is not None: params["trade_date"] = trade_date
    if start_20d is not None: params["start_20d"] = start_20d
    if start_60d is not None: params["start_60d"] = start_60d
    if amount_ratio_min is not None: params["amount_ratio_min"] = amount_ratio_min
    if turnover_ratio_min is not None: params["turnover_ratio_min"] = turnover_ratio_min

    sql = VOLUME_MOMENTUM_SQL.format(**params)
    db = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return db.execute(sql).fetchdf()
    finally:
        db.close()


def screen_narrative(
    trade_date: str | None = None,
    top_n_industries: int = 10,
    amount_ratio_min: float = 1.2,
    turnover_ratio_min: float = 1.1,
) -> dict:
    """Run both sector momentum and volume momentum screens.

    Returns:
        Dict with keys:
        - sector_df: DataFrame of stocks in top industries
        - volume_df: DataFrame of stocks with volume momentum
        - sector_summary: dict with top industry names and avg pct_chg
        - volume_summary: dict with count and median ratios
    """
    sector_df = screen_sector_momentum(
        trade_date=trade_date,
        top_n_industries=top_n_industries,
    )
    volume_df = screen_volume_momentum(
        trade_date=trade_date,
        amount_ratio_min=amount_ratio_min,
        turnover_ratio_min=turnover_ratio_min,
    )

    sector_summary = {}
    if not sector_df.empty:
        sector_summary = {
            "stock_count": len(sector_df),
            "industry_count": sector_df["industry"].nunique(),
            "top_industries": sector_df.groupby("industry")["industry_avg_pct_chg"].first()
                .sort_values(ascending=False).head(5).to_dict(),
        }

    volume_summary = {}
    if not volume_df.empty:
        volume_summary = {
            "count": len(volume_df),
            "amount_ratio_median": round(volume_df["amount_ratio"].median(), 2),
            "turnover_ratio_median": round(volume_df["turnover_ratio"].median(), 2),
        }

    return {
        "sector_df": sector_df,
        "volume_df": volume_df,
        "sector_summary": sector_summary,
        "volume_summary": volume_summary,
    }


if __name__ == "__main__":
    result = screen_narrative()
    print("=== Layer 2: 叙事动量筛选 ===\n")

    ss = result["sector_summary"]
    print(f"板块动量: {ss.get('stock_count', 0)} 只股票, {ss.get('industry_count', 0)} 个行业")
    if "top_industries" in ss:
        print("  Top 5 行业:")
        for ind, pct in ss["top_industries"].items():
            print(f"    {ind}: {pct:.2f}%")

    vs = result["volume_summary"]
    print(f"\n量能动量: {vs.get('count', 0)} 只通过")
    if vs:
        print(f"  成交额比中位数: {vs.get('amount_ratio_median', 'N/A')}")
        print(f"  换手率比中位数: {vs.get('turnover_ratio_median', 'N/A')}")

    # Show overlap
    if not result["sector_df"].empty and not result["volume_df"].empty:
        overlap = set(result["sector_df"]["ts_code"]) & set(result["volume_df"]["ts_code"])
        print(f"\n板块动量 ∩ 量能动量: {len(overlap)} 只")
