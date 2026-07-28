"""Parameterized DuckDB SQL templates for stock screening.

All templates use {param} placeholders that are filled by the screening functions.
Standard conditions (from audit 2026-06-28):
  - ROE >= 8% (inclusive, was > 8 in v1)
  - Revenue YoY > 0%
  - Net profit YoY > -20%
  - Operating cash flow per share > 0
  - Exclude ST stocks
  - Market cap > 50 billion (500000 万元)
"""

# ── Layer 1: Fundamental Screening ──
LAYER1_FUNDAMENTAL_SQL = """
SELECT
    f.ts_code,
    si.name,
    si.industry,
    ROUND(f.roe_waa, 2)            AS roe_waa,
    ROUND(f.roe, 2)                AS roe,
    ROUND(f.or_yoy, 2)             AS revenue_yoy,
    ROUND(f.netprofit_yoy, 2)      AS netprofit_yoy,
    ROUND(f.ocfps, 2)              AS ocfps,
    ROUND(f.grossprofit_margin, 2) AS gross_margin,
    ROUND(f.debt_to_assets, 2)     AS debt_ratio,
    ROUND(p.pe_ttm, 2)             AS pe_ttm,
    ROUND(p.pb, 2)                 AS pb,
    ROUND(p.total_mv / 10000, 2)   AS total_mv_yi,
    ROUND(p.dv_ttm, 4)             AS dv_ttm
FROM fin_indicator f
JOIN stk_info si ON f.ts_code = si.ts_code
JOIN stk_factor_pro p ON f.ts_code = p.ts_code AND p.trade_date = '{trade_date}'
LEFT JOIN stk_st_daily st ON f.ts_code = st.ts_code AND st.trade_date = '{trade_date}'
WHERE f.end_date = '{end_date}'
  AND f.roe_waa >= {roe_min}
  AND f.or_yoy > {revenue_yoy_min}
  AND f.netprofit_yoy > {netprofit_yoy_min}
  AND f.ocfps > {ocfps_min}
  AND st.ts_code IS NULL
  AND p.total_mv > {mv_min}
ORDER BY f.roe_waa DESC
"""

# ── Full Market Valuation Snapshot ──
FULL_MARKET_VALUATION_SQL = """
SELECT
    p.ts_code,
    si.name,
    si.industry,
    ROUND(p.pe_ttm, 2)             AS pe_ttm,
    ROUND(p.pb, 2)                 AS pb,
    ROUND(p.ps_ttm, 2)             AS ps_ttm,
    ROUND(p.dv_ttm, 4)             AS dv_ttm,
    ROUND(p.total_mv / 10000, 2)   AS total_mv_yi,
    ROUND(p.turnover_rate, 2)      AS turnover_rate,
    ROUND(p.pct_chg, 2)            AS pct_chg
FROM stk_factor_pro p
JOIN stk_info si ON p.ts_code = si.ts_code
LEFT JOIN stk_st_daily st ON p.ts_code = st.ts_code AND p.trade_date = st.trade_date
WHERE p.trade_date = '{trade_date}'
  AND st.ts_code IS NULL
  AND p.total_mv > {mv_min}
ORDER BY p.total_mv DESC
"""

# ── Default Parameters ──
DEFAULT_PARAMS = {
    "trade_date": "2026-06-26",
    "end_date": "2025-12-31",
    "roe_min": 8.0,
    "revenue_yoy_min": 0.0,
    "netprofit_yoy_min": -20.0,
    "ocfps_min": 0.0,
    "mv_min": 500000,  # 50 billion yuan in 万元
}
