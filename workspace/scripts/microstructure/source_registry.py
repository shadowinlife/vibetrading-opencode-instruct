"""
Data source registry for the escape-top microstructure validation framework.

Lists every candidate source (local DuckDB + external Tushare) with:
  - table/API endpoint
  - key fields for escape-top signals
  - units (critical for cross-source calculations)
  - frequency (daily / period / snapshot)
  - date semantics (trade_date vs ann_date vs end_date)
  - max_data_lag_days (how old data can be before it's stale)
  - coverage range (min/max dates)
  - row count
  - access method (DuckDB query vs Tushare API)

Sources are organized in three tiers:
  CORE   — required for escape-top signal computation
  BROAD  — enriching signals with breadth, liquidity, macro context
  PROBE  — external Tushare endpoints to probe for availability
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import List, Optional

# ── Dataclass definitions ──────────────────────────────────────────────


@dataclass
class SourceField:
    """A single field in a source, with units and escape-top relevance."""

    name: str
    dtype: str  # DuckDB type or Tushare type
    description: str
    units: str  # e.g. "元", "千元", "万元", "手", "股", "点", ""
    purpose: str  # "identifier", "dimension", "amount", "volume", "price", "ratio", "aux"


@dataclass
class LocalSource:
    """A local DuckDB table used in escape-top validation."""

    table_name: str
    tier: str  # "core" | "broad"
    description: str
    frequency: str  # "daily" | "period" | "snapshot"
    date_semantics: str  # "trade_date" | "end_date" | "ann_date"
    max_data_lag_days: int  # expected max lag in calendar days
    row_count: int
    coverage_min: str
    coverage_max: str
    pk: List[str]
    key_fields: List[SourceField]
    notes: str = ""


@dataclass
class ExternalSource:
    """A Tushare API endpoint to probe for escape-top enrichment."""

    endpoint: str
    tier: str  # "broad" | "probe"
    description: str
    frequency: str
    date_semantics: str  # semantics of the date axis after fetch
    max_data_lag_days: int
    required_fields: List[str]
    point_cost: str  # "free" | "standard" | "premium"
    doc_reference: str  # URL or doc_id
    permission_status: str = "unprobed"  # populated by tushare_probe.py


# ── Registry builder ──────────────────────────────────────────────────


def build_local_registry() -> List[LocalSource]:
    """Return all local DuckDB sources relevant to escape-top validation.

    Based on actual schemas probed from ./duckdb/ashare.duckdb on 2026-05-28.
    """

    return [
        # ── CORE tier: required for escape-top signal computation ──
        LocalSource(
            table_name="stk_factor_pro",
            tier="core",
            description="Individual stock daily OHLCV + amount + valuation + 190+ technical indicators",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=14_212_441,
            coverage_min="2010-01-04",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code (Tushare format)", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("open", "DOUBLE", "Opening price (unadjusted)", "元", "price"),
                SourceField("high", "DOUBLE", "High price (unadjusted)", "元", "price"),
                SourceField("low", "DOUBLE", "Low price (unadjusted)", "元", "price"),
                SourceField("close", "DOUBLE", "Close price (unadjusted)", "元", "price"),
                SourceField("close_hfq", "DOUBLE", "Close price (HFQ adjusted)", "元", "price"),
                SourceField("amount", "DOUBLE", "Turnover value", "千元", "amount"),
                SourceField("vol", "DOUBLE", "Turnover volume", "手", "volume"),
                SourceField("total_mv", "DOUBLE", "Total market cap", "万元", "amount"),
                SourceField("circ_mv", "DOUBLE", "Circulating market cap", "万元", "amount"),
                SourceField("pe", "DOUBLE", "PE (trailing)", "", "ratio"),
                SourceField("pe_ttm", "DOUBLE", "PE (TTM)", "", "ratio"),
                SourceField("pb", "DOUBLE", "PB", "", "ratio"),
                SourceField("turnover_rate", "DOUBLE", "Turnover rate", "%", "ratio"),
                SourceField("turnover_rate_f", "DOUBLE", "Free-float turnover rate", "%", "ratio"),
                SourceField("pct_chg", "DOUBLE", "Daily return", "%", "ratio"),
            ],
            notes=(
                "amount is in 千元 (thousands of CNY). "
                "stk_margin.rzmre is in 元, so cross-table calculations require "
                "unit alignment: stk_margin.rzmre / (stk_factor_pro.amount * 1000). "
                "Has 199 columns total; key_fields lists only escape-top relevant columns."
            ),
        ),
        LocalSource(
            table_name="stk_margin",
            tier="core",
            description="Margin trading detail: rzye (financing balance), rzmre (financing buy), rqyl (short volume)",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=6_486_384,
            coverage_min="2010-03-31",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("rzye", "DOUBLE", "Financing balance (margin loan outstanding)", "元", "amount"),
                SourceField("rzmre", "DOUBLE", "Financing buy amount (new margin loans)", "元", "amount"),
                SourceField("rqye", "DOUBLE", "Short-selling balance", "元", "amount"),
                SourceField("rzche", "DOUBLE", "Financing repayment amount", "元", "amount"),
                SourceField("rqyl", "DOUBLE", "Short-selling volume (shares)", "股", "volume"),
                SourceField("rzrqye", "DOUBLE", "Total margin balance (rzye + rqye)", "元", "amount"),
            ],
            notes=(
                "rzmre is in 元, NOT 千元. Critical: must align with stk_factor_pro.amount "
                "when computing margin_buy_ratio. Escape-top divergence formula: "
                "margin_buy_ratio = SUM(rzmre) / (SUM(stk_factor_pro.amount) * 1000)."
            ),
        ),
        LocalSource(
            table_name="idx_factor_pro",
            tier="core",
            description="Index daily prices + 80+ technical indicators (multiple indices)",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=8_990_894,
            coverage_min="2010-01-04",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Index code (e.g. 000001.SH for SSE)", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("close", "DOUBLE", "Close price", "点", "price"),
                SourceField("open", "DOUBLE", "Open price", "点", "price"),
                SourceField("high", "DOUBLE", "High price", "点", "price"),
                SourceField("low", "DOUBLE", "Low price", "点", "price"),
                SourceField("vol", "DOUBLE", "Volume", "手", "volume"),
                SourceField("amount", "DOUBLE", "Turnover value", "千元", "amount"),
                SourceField("pct_change", "DOUBLE", "Daily return", "%", "ratio"),
            ],
            notes=(
                "Default target index for escape-top: 000001.SH (SSE Composite). "
                "close is the primary field for forward drawdown calculation."
            ),
        ),
        # ── BROAD tier: enrichment sources for market breadth, liquidity, classification ──
        LocalSource(
            table_name="stk_moneyflow",
            tier="broad",
            description="Individual stock money flow by order size (small/medium/large/extra-large)",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=13_617_739,
            coverage_min="2010-01-04",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("buy_lg_amount", "DOUBLE", "Large-order buy amount", "万元", "amount"),
                SourceField("sell_lg_amount", "DOUBLE", "Large-order sell amount", "万元", "amount"),
                SourceField("buy_elg_amount", "DOUBLE", "Extra-large buy amount", "万元", "amount"),
                SourceField("sell_elg_amount", "DOUBLE", "Extra-large sell amount", "万元", "amount"),
                SourceField("net_mf_amount", "DOUBLE", "Net capital flow", "万元", "amount"),
            ],
            notes=(
                "Can enrich escape-top with institutional vs retail flow divergence. "
                "Amounts are in 万元 (ten-thousands), NOT 千元 or 元."
            ),
        ),
        LocalSource(
            table_name="stk_info",
            tier="broad",
            description="A-share basic info: symbol, name, industry, listing date, market",
            frequency="snapshot",
            date_semantics="list_date",
            max_data_lag_days=360,  # basic info rarely changes
            row_count=5_524,
            coverage_min="N/A (snapshot)",
            coverage_max="N/A (snapshot)",
            pk=["ts_code"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code", "", "identifier"),
                SourceField("symbol", "VARCHAR", "Ticker symbol", "", "identifier"),
                SourceField("name", "VARCHAR", "Stock name", "", "aux"),
                SourceField("industry", "VARCHAR", "SW industry sector", "", "aux"),
                SourceField("market", "VARCHAR", "Market (主板/创业板/科创板)", "", "aux"),
                SourceField("list_date", "VARCHAR", "Listing date", "", "dimension"),
                SourceField("act_name", "VARCHAR", "Actual controller name", "", "aux"),
            ],
            notes="Used to filter stocks by market/industry for segment-specific concentration analysis.",
        ),
        LocalSource(
            table_name="trade_calendar",
            tier="broad",
            description="SSE trading calendar 2000-2030, with is_open flag and pretrade_date",
            frequency="snapshot",
            date_semantics="cal_date",
            max_data_lag_days=0,  # static, pre-generated
            row_count=9_862,
            coverage_min="20000101",
            coverage_max="20261231",
            pk=["cal_date"],
            key_fields=[
                SourceField("exchange", "VARCHAR", "Exchange (SSE)", "", "aux"),
                SourceField("cal_date", "VARCHAR", "Calendar date (YYYYMMDD)", "", "dimension"),
                SourceField("is_open", "BIGINT", "1=trading day, 0=closed", "", "aux"),
                SourceField("pretrade_date", "VARCHAR", "Previous trading day", "", "aux"),
            ],
            notes="Used for forward drawdown alignment and trading-day offset calculations.",
        ),
        LocalSource(
            table_name="idx_daily_dc",
            tier="broad",
            description="Index (sector/board/concept) daily OHLCV bars",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=1_374_031,
            coverage_min="2020-01-02",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Sector/board code", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("close", "DOUBLE", "Close", "点", "price"),
                SourceField("pct_change", "DOUBLE", "Daily return", "%", "ratio"),
                SourceField("amount", "DOUBLE", "Turnover value", "元", "amount"),
                SourceField("turnover_rate", "DOUBLE", "Turnover rate", "%", "ratio"),
            ],
            notes="Covers ~2020 onwards only; prefer idx_factor_pro for longer history.",
        ),
        LocalSource(
            table_name="idx_quote_dc",
            tier="broad",
            description="Index market breadth snapshot: up/down counts, total market cap, leading stock",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=206_973,
            coverage_min="2024-12-20",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Concept/board code", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("up_num", "BIGINT", "Number of advancers", "支", "amount"),
                SourceField("down_num", "BIGINT", "Number of decliners", "支", "amount"),
                SourceField("total_mv", "DOUBLE", "Total market value", "万元", "amount"),
                SourceField("pct_change", "DOUBLE", "Index daily return", "%", "ratio"),
                SourceField("turnover_rate", "DOUBLE", "Turnover rate", "%", "ratio"),
                SourceField("leading", "VARCHAR", "Leading stock name", "", "aux"),
                SourceField("leading_pct", "DOUBLE", "Leading stock return", "%", "ratio"),
            ],
            notes=(
                "up_num/down_num provide market breadth — useful for confirming "
                "concentration signals (high concentration + narrow breadth = stronger signal). "
                "Coverage starts late 2024-12."
            ),
        ),
        LocalSource(
            table_name="fund_daily",
            tier="broad",
            description="ETF/LOF on-exchange fund daily bars",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=1_037_573,
            coverage_min="2020-02-10",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Fund code", "", "identifier"),
                SourceField("trade_date", "VARCHAR", "Trading date (YYYYMMDD string)", "", "dimension"),
                SourceField("close", "DOUBLE", "Close price", "元", "price"),
                SourceField("pct_chg", "DOUBLE", "Daily return", "%", "ratio"),
                SourceField("amount", "DOUBLE", "Turnover value", "千元", "amount"),
                SourceField("vol", "DOUBLE", "Volume", "手", "volume"),
            ],
            notes=(
                "trade_date is VARCHAR (YYYYMMDD), not DATE. "
                "Can be used for ETF flow analysis to complement escape-top. "
                "Coverage from 2020-02-10."
            ),
        ),
        LocalSource(
            table_name="stk_ah_comparison",
            tier="broad",
            description="A/H share price comparison and premium/discount",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=30_200,
            coverage_min="2025-08-12",
            coverage_max="2026-05-27",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "A-share code", "", "identifier"),
                SourceField("hk_code", "VARCHAR", "H-share code", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
                SourceField("ah_premium", "DOUBLE", "A/H premium ratio", "", "ratio"),
                SourceField("ah_comparison", "DOUBLE", "A/H price ratio", "", "ratio"),
            ],
            notes="Limited to ~10 months. Useful for cross-market sentiment overlay.",
        ),
        LocalSource(
            table_name="stk_suspend",
            tier="broad",
            description="Stock suspension/resumption calendar",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=-1,  # not probed in detail
            coverage_min="N/A",
            coverage_max="N/A",
            pk=["ts_code", "trade_date", "suspend_type"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code", "", "identifier"),
                SourceField("trade_date", "DATE", "Suspension/resumption date", "", "dimension"),
                SourceField("suspend_type", "VARCHAR", "S=停牌, R=复牌", "", "aux"),
            ],
            notes="Use to identify trading gaps that affect concentration calculations.",
        ),
        LocalSource(
            table_name="stk_cyq_perf",
            tier="broad",
            description="Chip distribution: cost percentiles and profit/loss ratio",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            row_count=-1,  # not probed
            coverage_min="N/A",
            coverage_max="N/A",
            pk=["ts_code", "trade_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code", "", "identifier"),
                SourceField("trade_date", "DATE", "Trading date", "", "dimension"),
            ],
            notes="No longer in incremental sync. Only use historical data already in DB.",
        ),
        LocalSource(
            table_name="fin_indicator",
            tier="broad",
            description="Financial indicators: ROE, ROA, EPS, gross margin, leverage, etc.",
            frequency="period",
            date_semantics="end_date",
            max_data_lag_days=120,  # quarterly reporting lag
            row_count=-1,
            coverage_min="N/A",
            coverage_max="N/A",
            pk=["ts_code", "end_date"],
            key_fields=[
                SourceField("ts_code", "VARCHAR", "Stock code", "", "identifier"),
                SourceField("end_date", "DATE", "Report period end date", "", "dimension"),
                SourceField("roe", "DOUBLE", "Return on equity", "%", "ratio"),
                SourceField("roa", "DOUBLE", "Return on assets", "%", "ratio"),
                SourceField("grossprofit_margin", "DOUBLE", "Gross profit margin", "%", "ratio"),
                SourceField("debt_to_assets", "DOUBLE", "Debt-to-assets ratio", "%", "ratio"),
                SourceField("ocf_to_revenue", "DOUBLE", "Operating CF / Revenue", "", "ratio"),
            ],
            notes="For fundamental overlay validation (e.g. do signals work better on high-ROE stocks?).",
        ),
    ]


def build_external_registry() -> List[ExternalSource]:
    """Return Tushare endpoints that could enrich escape-top but need permission probing.

    These are external data sources NOT yet available locally.
    """

    return [
        ExternalSource(
            endpoint="moneyflow_hsgt",
            tier="broad",
            description="Northbound (沪港通/深港通) daily money flow by market",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            required_fields=["trade_date", "ggt_ss", "ggt_sz", "hgt", "sgt", "north_money", "south_money"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=47",
        ),
        ExternalSource(
            endpoint="index_dailybasic",
            tier="broad",
            description="Index daily valuation: PE, PB, dividend yield, turnover volume at index level",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            required_fields=["ts_code", "trade_date", "pe", "pe_ttm", "pb", "total_mv", "turnover_rate"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=149",
        ),
        ExternalSource(
            endpoint="daily_info",
            tier="broad",
            description="Individual stock daily trading indicators beyond OHLCV (volatility, liquidity metrics)",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            required_fields=["ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe", "pe_ttm", "pb"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=32",
        ),
        ExternalSource(
            endpoint="etf_share_size",
            tier="broad",
            description="ETF share size changes (creation/redemption) — proxy for smart-money flows",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            required_fields=["ts_code", "trade_date", "fund_size", "share_size", "pe"],
            point_cost="standard",
            doc_reference="N/A (Tushare ETF module)",
        ),
        ExternalSource(
            endpoint="fund_basic",
            tier="probe",
            description="Fund basic information (type, manager, benchmark, inception date)",
            frequency="snapshot",
            date_semantics="list_date",
            max_data_lag_days=90,
            required_fields=["ts_code", "name", "management", "found_date", "fund_type", "benchmark"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=19",
        ),
        ExternalSource(
            endpoint="fund_nav",
            tier="probe",
            description="Fund NAV history (for tracking real vs. benchmark performance)",
            frequency="daily",
            date_semantics="nav_date",
            max_data_lag_days=1,
            required_fields=["ts_code", "nav_date", "unit_nav", "accum_nav", "adj_nav"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=19",
        ),
        ExternalSource(
            endpoint="shibor",
            tier="probe",
            description="Shanghai Interbank Offered Rate (Shibor) — key liquidity/credit spread indicator",
            frequency="daily",
            date_semantics="date",
            max_data_lag_days=1,
            required_fields=["date", "on", "1w", "2w", "1m", "3m", "6m", "9m", "1y"],
            point_cost="free",
            doc_reference="https://tushare.pro/document/2?doc_id=107",
        ),
        ExternalSource(
            endpoint="shibor_lpr",
            tier="probe",
            description="Loan Prime Rate (LPR) — benchmark lending rate, monthly",
            frequency="monthly",
            date_semantics="date",
            max_data_lag_days=30,
            required_fields=["date", "1y", "5y"],
            point_cost="free",
            doc_reference="https://tushare.pro/document/2?doc_id=116",
        ),
        ExternalSource(
            endpoint="cn_m",
            tier="probe",
            description="China money supply: M0/M1/M2 monthly aggregates — macro liquidity",
            frequency="monthly",
            date_semantics="month",
            max_data_lag_days=30,
            required_fields=["month", "m0", "m1", "m2", "m1_yoy", "m2_yoy"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=108",
        ),
        ExternalSource(
            endpoint="cn_social_financing",
            tier="probe",
            description="Aggregate Social Financing (社融) — broad credit impulse indicator",
            frequency="monthly",
            date_semantics="month",
            max_data_lag_days=30,
            required_fields=["month", "afre", "rmb_loan"],
            point_cost="standard",
            doc_reference="https://tushare.pro/document/2?doc_id=110",
        ),
        ExternalSource(
            endpoint="opt_daily",
            tier="probe",
            description="SSE/CSI option daily data — implied volatility surface for risk sentiment",
            frequency="daily",
            date_semantics="trade_date",
            max_data_lag_days=1,
            required_fields=["ts_code", "trade_date", "exchange", "close", "settle", "volume"],
            point_cost="premium",  # likely requires higher tier
            doc_reference="https://tushare.pro/document/2?doc_id=159",
        ),
    ]


# ── Registry materialization ──────────────────────────────────────────


def materialize_registry(output_path: str):
    """Write full registry to JSON for machine consumption."""
    local = build_local_registry()
    external = build_external_registry()

    registry = {
        "meta": {
            "generated_at": "2026-05-28",
            "framework": "escape-top-microstructure-validation",
            "duckdb_path": "./duckdb/ashare.duckdb",
        },
        "local_sources": [
            {
                **asdict(s),
                "key_fields": [asdict(f) for f in s.key_fields],
            }
            for s in local
        ],
        "external_sources": [asdict(s) for s in external],
        "summary": {
            "local_core_count": sum(1 for s in local if s.tier == "core"),
            "local_broad_count": sum(1 for s in local if s.tier == "broad"),
            "external_broad_count": sum(1 for s in external if s.tier == "broad"),
            "external_probe_count": sum(1 for s in external if s.tier == "probe"),
            "total_local": len(local),
            "total_external": len(external),
        },
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    return registry


# ── CLI entry ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    output = sys.argv[1] if len(sys.argv) > 1 else "tmp/microstructure/source_registry.json"
    registry = materialize_registry(output)
    print(f"Source registry written to {output}")
    print(f"  Local: {registry['summary']['total_local']} sources "
          f"({registry['summary']['local_core_count']} core, "
          f"{registry['summary']['local_broad_count']} broad)")
    print(f"  External: {registry['summary']['total_external']} sources "
          f"({registry['summary']['external_broad_count']} broad, "
          f"{registry['summary']['external_probe_count']} probe)")