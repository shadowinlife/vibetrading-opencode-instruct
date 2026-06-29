"""Layer 3: Capital flow screening via DuckDB.

Screens stocks by:
- Main force net inflow (large + extra-large orders) from stk_moneyflow
- 5-day main force direction from stk_moneyflow_ths
- Margin balance (融资余额) growth from stk_margin

Usage:
    from scripts.screening.layer3_flow import screen_flow
    result = screen_flow()
"""
from __future__ import annotations

import duckdb
import pandas as pd
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "duckdb" / "ashare.duckdb"

# ── SQL: Main force net inflow ──
# Stocks where main force (large + extra-large) is net buying today
# AND 5-day main force direction is also positive
MAIN_FORCE_SQL = """
WITH mf_today AS (
    SELECT
        m.ts_code,
        m.net_mf_amount,
        m.buy_elg_amount,
        m.sell_elg_amount,
        m.buy_lg_amount,
        m.sell_lg_amount,
        (m.buy_elg_amount - m.sell_elg_amount) AS net_elg,
        (m.buy_lg_amount - m.sell_lg_amount)   AS net_lg
    FROM stk_moneyflow m
    WHERE m.trade_date = '{trade_date}'
      AND m.net_mf_amount > 0
),
ths_5d AS (
    SELECT
        t.ts_code,
        t.net_d5_amount,
        t.net_amount,
        t.buy_lg_amount_rate
    FROM stk_moneyflow_ths t
    WHERE t.trade_date = '{trade_date}'
      AND t.net_d5_amount > 0
)
SELECT
    mf.ts_code,
    si.name,
    si.industry,
    ROUND(mf.net_mf_amount / 10000, 4)  AS net_mf_yi,
    ROUND(mf.net_elg / 10000, 4)        AS net_elg_yi,
    ROUND(mf.net_lg / 10000, 4)         AS net_lg_yi,
    ROUND(ths.net_d5_amount / 10000, 4) AS net_d5_yi,
    ROUND(ths.net_amount / 10000, 4)    AS net_1d_yi,
    ROUND(ths.buy_lg_amount_rate, 2)    AS buy_lg_rate
FROM mf_today mf
JOIN ths_5d ths ON mf.ts_code = ths.ts_code
JOIN stk_info si ON mf.ts_code = si.ts_code
LEFT JOIN stk_st_daily st ON mf.ts_code = st.ts_code AND st.trade_date = '{trade_date}'
WHERE st.ts_code IS NULL
ORDER BY mf.net_mf_amount DESC
"""

# ── SQL: Margin balance growth ──
# Stocks where 融资余额 grew vs 5 trading days ago
MARGIN_GROWTH_SQL = """
WITH margin_now AS (
    SELECT ts_code, rzye, rzrqye, rzmre
    FROM stk_margin
    WHERE trade_date = '{trade_date}'
      AND rzye > 0
),
margin_prev AS (
    SELECT ts_code, rzye AS rzye_prev
    FROM stk_margin
    WHERE trade_date = '{trade_date_prev}'
      AND rzye > 0
)
SELECT
    mn.ts_code,
    si.name,
    si.industry,
    ROUND(mn.rzye / 10000, 4)           AS rzye_yi,
    ROUND(mn.rzrqye / 10000, 4)         AS rzrqye_yi,
    ROUND(mn.rzmre / 10000, 4)          AS rzmre_yi,
    ROUND(mp.rzye_prev / 10000, 4)      AS rzye_prev_yi,
    ROUND(mn.rzye - mp.rzye_prev, 4)    AS rzye_delta,
    ROUND((mn.rzye - mp.rzye_prev) / NULLIF(mp.rzye_prev, 0) * 100, 2)
                                         AS rzye_growth_pct
FROM margin_now mn
JOIN margin_prev mp ON mn.ts_code = mp.ts_code
JOIN stk_info si ON mn.ts_code = si.ts_code
LEFT JOIN stk_st_daily st ON mn.ts_code = st.ts_code AND st.trade_date = '{trade_date}'
WHERE st.ts_code IS NULL
  AND mn.rzye > mp.rzye_prev
ORDER BY (mn.rzye - mp.rzye_prev) / NULLIF(mp.rzye_prev, 0) DESC
"""

# ── SQL: Combined flow screen (main force + margin) ──
COMBINED_FLOW_SQL = """
WITH mf_today AS (
    SELECT
        m.ts_code,
        m.net_mf_amount,
        (m.buy_elg_amount - m.sell_elg_amount) AS net_elg,
        (m.buy_lg_amount - m.sell_lg_amount)   AS net_lg
    FROM stk_moneyflow m
    WHERE m.trade_date = '{trade_date}'
      AND m.net_mf_amount > 0
),
ths_5d AS (
    SELECT
        t.ts_code,
        t.net_d5_amount,
        t.buy_lg_amount_rate
    FROM stk_moneyflow_ths t
    WHERE t.trade_date = '{trade_date}'
      AND t.net_d5_amount > 0
),
margin_now AS (
    SELECT ts_code, rzye, rzrqye
    FROM stk_margin
    WHERE trade_date = '{trade_date}'
      AND rzye > 0
),
margin_prev AS (
    SELECT ts_code, rzye AS rzye_prev
    FROM stk_margin
    WHERE trade_date = '{trade_date_prev}'
      AND rzye > 0
),
margin_growth AS (
    SELECT
        mn.ts_code,
        mn.rzye,
        mn.rzrqye,
        mp.rzye_prev,
        (mn.rzye - mp.rzye_prev) / NULLIF(mp.rzye_prev, 0) * 100 AS rzye_growth_pct
    FROM margin_now mn
    JOIN margin_prev mp ON mn.ts_code = mp.ts_code
    WHERE mn.rzye > mp.rzye_prev
)
SELECT
    mf.ts_code,
    si.name,
    si.industry,
    ROUND(mf.net_mf_amount / 10000, 4)  AS net_mf_yi,
    ROUND(mf.net_elg / 10000, 4)        AS net_elg_yi,
    ROUND(ths.net_d5_amount / 10000, 4) AS net_d5_yi,
    ROUND(ths.buy_lg_amount_rate, 2)    AS buy_lg_rate,
    ROUND(mg.rzye / 10000, 4)           AS rzye_yi,
    ROUND(mg.rzye_growth_pct, 2)        AS rzye_growth_pct,
    CASE
        WHEN mg.ts_code IS NOT NULL THEN 3
        ELSE 2
    END AS signal_count
FROM mf_today mf
JOIN ths_5d ths ON mf.ts_code = ths.ts_code
JOIN stk_info si ON mf.ts_code = si.ts_code
LEFT JOIN margin_growth mg ON mf.ts_code = mg.ts_code
LEFT JOIN stk_st_daily st ON mf.ts_code = st.ts_code AND st.trade_date = '{trade_date}'
WHERE st.ts_code IS NULL
ORDER BY signal_count DESC, mf.net_mf_amount DESC
"""

DEFAULT_PARAMS = {
    "trade_date": "2026-06-26",
    "trade_date_prev": "2026-06-18",  # ~5 trading days prior (skip weekends)
}


def _run_sql(sql: str, params: dict) -> pd.DataFrame:
    """Execute parameterized SQL against DuckDB and return DataFrame."""
    db = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return db.execute(sql.format(**params)).fetchdf()
    finally:
        db.close()


def screen_main_force(
    trade_date: str | None = None,
) -> pd.DataFrame:
    """Screen stocks with positive main force inflow today AND 5-day direction.

    Returns DataFrame with: ts_code, name, industry, net_mf_yi, net_elg_yi,
    net_lg_yi, net_d5_yi, net_1d_yi, buy_lg_rate.
    """
    params = dict(DEFAULT_PARAMS)
    if trade_date is not None:
        params["trade_date"] = trade_date
    return _run_sql(MAIN_FORCE_SQL, params)


def screen_margin_growth(
    trade_date: str | None = None,
    trade_date_prev: str | None = None,
) -> pd.DataFrame:
    """Screen stocks with growing margin balance (融资余额) vs prior period.

    Returns DataFrame with: ts_code, name, industry, rzye_yi, rzrqye_yi,
    rzmre_yi, rzye_prev_yi, rzye_delta, rzye_growth_pct.
    """
    params = dict(DEFAULT_PARAMS)
    if trade_date is not None:
        params["trade_date"] = trade_date
    if trade_date_prev is not None:
        params["trade_date_prev"] = trade_date_prev
    return _run_sql(MARGIN_GROWTH_SQL, params)


def screen_flow(
    trade_date: str | None = None,
    trade_date_prev: str | None = None,
) -> dict:
    """Run Layer 3 capital flow screening (combined main force + margin).

    Returns:
        Dict with keys:
        - flow_df: DataFrame of stocks with positive flow signals
        - main_force_df: DataFrame from main force screen only
        - margin_df: DataFrame from margin growth screen only
        - flow_summary: dict with count and key statistics
    """
    params = dict(DEFAULT_PARAMS)
    if trade_date is not None:
        params["trade_date"] = trade_date
    if trade_date_prev is not None:
        params["trade_date_prev"] = trade_date_prev

    db = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        main_force_df = db.execute(MAIN_FORCE_SQL.format(**params)).fetchdf()
        margin_df = db.execute(MARGIN_GROWTH_SQL.format(**params)).fetchdf()
        flow_df = db.execute(COMBINED_FLOW_SQL.format(**params)).fetchdf()
    finally:
        db.close()

    summary: dict = {"count": len(flow_df)}
    if not flow_df.empty:
        summary["main_force_count"] = len(main_force_df)
        summary["margin_growth_count"] = len(margin_df)
        summary["triple_signal_count"] = int((flow_df["signal_count"] >= 3).sum())
        summary["net_mf_median_yi"] = round(flow_df["net_mf_yi"].median(), 4)
        summary["net_d5_median_yi"] = round(flow_df["net_d5_yi"].median(), 4)
        if "rzye_growth_pct" in flow_df.columns:
            has_margin = flow_df.dropna(subset=["rzye_growth_pct"])
            if not has_margin.empty:
                summary["rzye_growth_median_pct"] = round(
                    has_margin["rzye_growth_pct"].median(), 2
                )

    return {
        "flow_df": flow_df,
        "main_force_df": main_force_df,
        "margin_df": margin_df,
        "flow_summary": summary,
    }


if __name__ == "__main__":
    result = screen_flow()
    s = result["flow_summary"]
    print("=== Layer 3: 资金流筛选 ===\n")
    print(f"综合通过: {s['count']} 只")
    print(f"  主力净流入: {s.get('main_force_count', 0)} 只")
    print(f"  融资余额增长: {s.get('margin_growth_count', 0)} 只")
    print(f"  三重信号 (主力+5日+融资): {s.get('triple_signal_count', 0)} 只")
    if s["count"] > 0:
        print(f"  主力净流入中位数: {s.get('net_mf_median_yi', 'N/A')} 亿")
        print(f"  5日主力净流入中位数: {s.get('net_d5_median_yi', 'N/A')} 亿")
        if "rzye_growth_median_pct" in s:
            print(f"  融资余额增长中位数: {s['rzye_growth_median_pct']}%")
        print()
        print("Top 20 (按信号数 + 主力净流入排序):")
        print(result["flow_df"].head(20).to_string(index=False))
    else:
        print("  无符合条件的股票，请检查参数或数据日期。")
