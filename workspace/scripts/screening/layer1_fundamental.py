"""Layer 1: Fundamental screening via DuckDB.

Usage:
    from scripts.screening.layer1_fundamental import screen_fundamental
    df = screen_fundamental()  # uses default params
    df = screen_fundamental(roe_min=15, trade_date="2026-06-27")
"""
from __future__ import annotations

import duckdb
import pandas as pd
from pathlib import Path

from scripts.screening.sql_templates import LAYER1_FUNDAMENTAL_SQL, DEFAULT_PARAMS

DB_PATH = Path(__file__).resolve().parents[2] / "duckdb" / "ashare.duckdb"


def screen_fundamental(
    trade_date: str | None = None,
    end_date: str | None = None,
    roe_min: float | None = None,
    revenue_yoy_min: float | None = None,
    netprofit_yoy_min: float | None = None,
    ocfps_min: float | None = None,
    mv_min: float | None = None,
) -> pd.DataFrame:
    """Run Layer 1 fundamental screening against local DuckDB.

    Args:
        trade_date: Trading date for price/valuation data (YYYY-MM-DD).
        end_date: Financial report end date (YYYY-MM-DD).
        roe_min: Minimum ROE (weighted avg, %). Default 8.
        revenue_yoy_min: Minimum revenue YoY growth (%). Default 0.
        netprofit_yoy_min: Minimum net profit YoY growth (%). Default -20.
        ocfps_min: Minimum operating cash flow per share. Default 0.
        mv_min: Minimum total market value (万元). Default 500000 (50亿).

    Returns:
        DataFrame with columns: ts_code, name, industry, roe_waa, roe,
        revenue_yoy, netprofit_yoy, ocfps, gross_margin, debt_ratio,
        pe_ttm, pb, total_mv_yi, dv_ttm.
    """
    params = dict(DEFAULT_PARAMS)
    if trade_date is not None:
        params["trade_date"] = trade_date
    if end_date is not None:
        params["end_date"] = end_date
    if roe_min is not None:
        params["roe_min"] = roe_min
    if revenue_yoy_min is not None:
        params["revenue_yoy_min"] = revenue_yoy_min
    if netprofit_yoy_min is not None:
        params["netprofit_yoy_min"] = netprofit_yoy_min
    if ocfps_min is not None:
        params["ocfps_min"] = ocfps_min
    if mv_min is not None:
        params["mv_min"] = mv_min

    sql = LAYER1_FUNDAMENTAL_SQL.format(**params)

    db = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = db.execute(sql).fetchdf()
    finally:
        db.close()

    return df


def screen_fundamental_summary(df: pd.DataFrame) -> dict:
    """Compute summary statistics for a Layer 1 screening result.

    Returns:
        Dict with keys: count, roe_median, revenue_yoy_median,
        gross_margin_median, pe_median, pb_median.
    """
    if df.empty:
        return {"count": 0}
    return {
        "count": len(df),
        "roe_median": round(df["roe_waa"].median(), 2),
        "revenue_yoy_median": round(df["revenue_yoy"].median(), 2),
        "gross_margin_median": round(df["gross_margin"].median(), 2),
        "pe_median": round(df["pe_ttm"].median(), 2),
        "pb_median": round(df["pb"].median(), 2),
    }


if __name__ == "__main__":
    df = screen_fundamental()
    summary = screen_fundamental_summary(df)
    print(f"Layer 1 基本面筛选结果: {summary['count']} 只通过")
    if summary["count"] > 0:
        print(f"  ROE 中位数: {summary['roe_median']}%")
        print(f"  营收增长中位数: {summary['revenue_yoy_median']}%")
        print(f"  毛利率中位数: {summary['gross_margin_median']}%")
        print(f"  PE_TTM 中位数: {summary['pe_median']}")
        print(f"  PB 中位数: {summary['pb_median']}")
        print()
        print("Top 20 (按 ROE 排序):")
        print(df.head(20).to_string(index=False))
    else:
        print("  无符合条件的股票，请检查参数或数据日期。")
