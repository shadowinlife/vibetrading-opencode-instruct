"""
Effective-date audit across ALL data sources used by escape-top conditions.

Performs look-ahead and effective-date analysis for every source (local DuckDB
+ external Tushare), classifying date semantics, computing max-lag days, and
flagging sources that require calendar-alignment enforcement.

Key principles:
  - **trade_date** sources follow trade_calendar → T+0/T+1 max lag
  - **end_date** sources (quarterly financials) need ann_date → ~107d median lag
  - **month** sources (cn_m, sf_month) release ~10-15d post month-end
  - **snapshot** sources are point-in-time with no time-series lag

Usage::

    python -m scripts.microstructure.effective_date_audit
    python -m scripts.microstructure.effective_date_audit --duckdb-path ./duckdb/ashare.duckdb
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional

import duckdb

from scripts.microstructure.source_registry import (
    ExternalSource,
    LocalSource,
    build_external_registry,
    build_local_registry,
)


# ── Date semantics classification ──────────────────────────────────────────


class DateSemantics(Enum):
    """What the date column in a data source actually represents."""

    TRADE_DATE = "trade_date"
    """Date of the market event (OHLCV, margin, moneyflow). Data available T+0 or T+1."""

    END_DATE = "end_date"
    """Period-end date (quarterly/annual reports). Data NOT available on this date;
    use ann_date for effective availability."""

    ANN_DATE = "ann_date"
    """Announcement/publication date. This IS the effective date for period data."""

    NAV_DATE = "nav_date"
    """Fund NAV valuation date. Available T+1."""

    MONTH = "month"
    """Monthly period identifier. Data released ~10-15d after month-end."""

    CAL_DATE = "cal_date"
    """Calendar date in static reference table (trade_calendar). Always available."""

    LIST_DATE = "list_date"
    """Listing/inception date. Point-in-time, no forward-fill."""

    SNAPSHOT_DATE = "snapshot_date"
    """Snapshot-as-of date. Represents when the snapshot was taken."""

    UNKNOWN = "unknown"
    """Date semantics not determined."""


class SourceCategory(Enum):
    """Broad source type for lag estimation."""

    DAILY_TRADE = "daily_trade"
    """Daily data aligned to trade_calendar (stock prices, margin, moneyflow)."""

    PERIOD_FINANCIAL = "period_financial"
    """Quarterly/annual financial data (fin_indicator, income, balancesheet)."""

    MONTHLY_MACRO = "monthly_macro"
    """Monthly macro indicators (M2, social financing, investor accounts)."""

    FUND_DATA = "fund_data"
    """Fund/ETF daily or NAV data."""

    STATIC_REFERENCE = "static_reference"
    """Static reference tables (trade_calendar, stock_basic, index_basic)."""

    EXTERNAL_API = "external_api"
    """External Tushare API endpoints not synced to DuckDB."""


# ── Audit result dataclass ─────────────────────────────────────────────────


@dataclass
class SourceAuditRecord:
    """Per-source audit result with date semantics and lag classification."""

    source_id: str
    """Stable identifier (table name for local, endpoint for external)."""

    source_category: str
    """SourceCategory value."""

    tier: str
    """core / broad / probe."""

    date_field: str
    """Primary date column name."""

    date_semantics: str
    """DateSemantics value — what the date field represents."""

    effective_date_field: str
    """The actual field to use for availability (may differ from date_field)."""

    max_lag_calendar_days: int
    """Expected maximum lag in calendar days between event and availability."""

    max_lag_trading_days: int
    """Expected maximum lag in trading days."""

    actual_latest_date: Optional[str]
    """Latest date actually present in the data (YYYY-MM-DD)."""

    actual_latest_effective_date: Optional[str]
    """Latest effective_date actually present (YYYY-MM-DD, for period sources)."""

    release_pattern: str
    """Human-readable description of release cadence."""

    is_local: bool
    """Whether this source lives in local DuckDB."""

    has_lookahead_risk: bool
    """True when period_date ≠ effective_date (monthly/quarterly sources)."""

    requires_forward_fill: bool
    """True when data must be forward-filled from effective_date to next release."""

    forward_fill_granularity: str
    """'daily' | 'monthly' | 'quarterly' | 'none' — how often data should be filled."""

    trade_calendar_aligned: bool
    """True when data dates follow trade_calendar and can be aligned with cal_date."""

    notes: str
    """Additional context about date handling for this source."""


# ── Full audit result ──────────────────────────────────────────────────────


@dataclass
class EffectiveDateAudit:
    """Complete effective-date audit across all sources."""

    audit_date: str
    """Date the audit was executed (YYYY-MM-DD)."""

    latest_trading_day: str
    """Most recent trading day from trade_calendar (YYYYMMDD)."""

    total_sources: int
    local_sources: int
    external_sources: int

    sources_with_lookahead_risk: int
    sources_trade_calendar_aligned: int
    sources_requiring_forward_fill: int

    records: List[SourceAuditRecord]

    summary_by_category: dict[str, int]

    recommendations: List[str]


# ── Auditor class ──────────────────────────────────────────────────────────


class EffectiveDateAuditor:
    """Audits date semantics and effective-date compliance across all sources.

    Queries DuckDB for actual data freshness, cross-references with
    trade_calendar, and classifies look-ahead risk per source.
    """

    def __init__(self, duckdb_path: str = "./duckdb/ashare.duckdb"):
        self.duckdb_path = duckdb_path
        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._calendar_cache: Optional[dict[str, Any]] = None

    # ── connection management ──────────────────────────────────────────

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self._con = duckdb.connect(self.duckdb_path, read_only=True)
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── calendar helpers ───────────────────────────────────────────────

    def _get_calendar(self) -> dict[str, Any]:
        """Load and cache trade calendar metadata."""
        if self._calendar_cache is not None:
            return self._calendar_cache

        latest_trade = self.con.execute(
            "SELECT MAX(cal_date) FROM trade_calendar "
            "WHERE is_open=1 AND cal_date <= strftime(CURRENT_DATE, '%Y%m%d')"
        ).fetchone()[0]

        trading_days = self.con.execute(
            "SELECT COUNT(*) FROM trade_calendar WHERE is_open=1"
        ).fetchone()[0]

        self._calendar_cache = {
            "latest_trading_day": latest_trade,
            "total_trading_days": trading_days,
        }
        return self._calendar_cache

    def _trading_days_between(self, from_date: str, to_date: str) -> int:
        """Count trading days between two calendar dates (inclusive of both)."""
        return self.con.execute(
            "SELECT COUNT(*) FROM trade_calendar "
            "WHERE is_open=1 AND cal_date >= ? AND cal_date <= ?",
            [from_date, to_date],
        ).fetchone()[0]

    def _calendar_days_between(self, from_date: str, to_date: str) -> int:
        """Count calendar days between two dates."""
        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
        return (to_d - from_d).days

    # ── per-source profiling ───────────────────────────────────────────

    def _profile_local_table(self, table_name: str, date_field: str) -> dict[str, Any]:
        """Profile a local DuckDB table's date column."""
        try:
            r = self.con.execute(
                f'SELECT MAX("{date_field}") as latest, '
                f'MIN("{date_field}") as earliest, '
                f'COUNT(DISTINCT "{date_field}") as unique_dates, '
                f"COUNT(*) as total_rows "
                f"FROM {table_name}"
            ).fetchone()
            return {
                "latest": str(r[0]) if r[0] else None,
                "earliest": str(r[1]) if r[1] else None,
                "unique_dates": r[2],
                "total_rows": r[3],
            }
        except Exception as e:
            return {"latest": None, "earliest": None, "unique_dates": 0, "total_rows": 0, "error": str(e)}

    def _profile_ann_date(self, table_name: str, end_field: str, ann_field: str) -> dict[str, Any]:
        """Profile announcement-date lag for period financial tables."""
        try:
            r = self.con.execute(
                f"SELECT "
                f"AVG(DATEDIFF('day', {end_field}, {ann_field})) as avg_lag, "
                f"MEDIAN(DATEDIFF('day', {end_field}, {ann_field})) as med_lag, "
                f"MIN(DATEDIFF('day', {end_field}, {ann_field})) as min_lag, "
                f"MAX(DATEDIFF('day', {end_field}, {ann_field})) as max_lag, "
                f'MAX("{ann_field}") as latest_ann, '
                f'MAX("{end_field}") as latest_end '
                f"FROM {table_name} "
                f"WHERE {ann_field} IS NOT NULL"
            ).fetchone()
            return {
                "avg_lag_days": round(r[0], 0) if r[0] else None,
                "med_lag_days": round(r[1], 0) if r[1] else None,
                "min_lag_days": r[2],
                "max_lag_days": r[3],
                "latest_ann_date": str(r[4]) if r[4] else None,
                "latest_end_date": str(r[5]) if r[5] else None,
            }
        except Exception as e:
            return {"error": str(e)}

    # ── classification logic ───────────────────────────────────────────

    @staticmethod
    def classify_local_source(source: LocalSource) -> SourceAuditRecord:
        """Classify a local DuckDB source's date semantics."""
        tbl = source.table_name

        # ── trade_calendar: static reference ──
        if tbl == "trade_calendar":
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.STATIC_REFERENCE.value,
                tier=source.tier,
                date_field="cal_date",
                date_semantics=DateSemantics.CAL_DATE.value,
                effective_date_field="cal_date",
                max_lag_calendar_days=0,
                max_lag_trading_days=0,
                actual_latest_date="20261231",
                actual_latest_effective_date="20261231",
                release_pattern="Pre-generated, always available",
                is_local=True,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=True,
                notes="Static calendar 2000-2030. Used for trading-day alignment only.",
            )

        # ── fin_indicator: period financial with ann_date lag ──
        if tbl == "fin_indicator":
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.PERIOD_FINANCIAL.value,
                tier=source.tier,
                date_field="end_date",
                date_semantics=DateSemantics.END_DATE.value,
                effective_date_field="ann_date",
                max_lag_calendar_days=120,
                max_lag_trading_days=80,
                actual_latest_date=None,  # filled by auditor
                actual_latest_effective_date=None,
                release_pattern="Quarterly reports: ~50-60d after quarter-end; annual: ~120d",
                is_local=True,
                has_lookahead_risk=True,
                requires_forward_fill=True,
                forward_fill_granularity="quarterly",
                trade_calendar_aligned=False,
                notes=(
                    "end_date is NOT the effective date. Use ann_date for all "
                    "signal computations. Forward-fill ann_date to ann_date(t+1) "
                    "to avoid look-ahead bias. Median ann_date lag is ~53 calendar days."
                ),
            )

        # ── stk_info: snapshot ──
        if tbl == "stk_info":
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.STATIC_REFERENCE.value,
                tier=source.tier,
                date_field="list_date",
                date_semantics=DateSemantics.LIST_DATE.value,
                effective_date_field="list_date",
                max_lag_calendar_days=360,
                max_lag_trading_days=250,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Snapshot of current listings",
                is_local=True,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=False,
                notes="Static snapshot. list_date is inception date, not time-series.",
            )

        # ── stk_auction_open: T+2 daily (rate-limited sync) ──
        if tbl == "stk_auction_open":
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.DAILY_TRADE.value,
                tier=source.tier,  # broad or removed
                date_field="trade_date",
                date_semantics=DateSemantics.TRADE_DATE.value,
                effective_date_field="trade_date",
                max_lag_calendar_days=2,
                max_lag_trading_days=2,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="T+2 (rate-limited sync at 6.2s/call)",
                is_local=True,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=True,
                notes="Rate-limited. Max 2-day lag due to incremental sync throttling.",
            )

        # ── stk_dividend: period data with ann_date ──
        if tbl == "stk_dividend":
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.PERIOD_FINANCIAL.value,
                tier=source.tier,
                date_field="end_date",
                date_semantics=DateSemantics.END_DATE.value,
                effective_date_field="ann_date",
                max_lag_calendar_days=60,
                max_lag_trading_days=40,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Dividend announcements, ex_date forward-looking",
                is_local=True,
                has_lookahead_risk=True,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=False,
                notes=(
                    "Contains forward-looking dates (ex_date, pay_date, record_date). "
                    "Use ann_date for effective availability, not end_date."
                ),
            )

        # ── namechange, stk_managers, stk_pledge_detail, stk_rewards: ann_date ──
        if tbl in ("stk_name_history", "stk_managers", "stk_pledge_detail", "stk_rewards"):
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.PERIOD_FINANCIAL.value,
                tier=source.tier,
                date_field="ann_date",
                date_semantics=DateSemantics.ANN_DATE.value,
                effective_date_field="ann_date",
                max_lag_calendar_days=30,
                max_lag_trading_days=20,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Announcement-driven, event-based",
                is_local=True,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=False,
                notes="Event-driven table. ann_date is the effective date.",
            )

        # ── stk_share_float: float_date (forward-looking) ──
        if tbl == "stk_share_float":
            return SourceAuditRecord(
                source_id=tbl,
                source_category=SourceCategory.PERIOD_FINANCIAL.value,
                tier=source.tier,
                date_field="ann_date",
                date_semantics=DateSemantics.ANN_DATE.value,
                effective_date_field="ann_date",
                max_lag_calendar_days=7,
                max_lag_trading_days=5,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Float change announcements",
                is_local=True,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=False,
                notes="float_date can be forward-looking. Use ann_date as effective.",
            )

        # ── Default: daily trade_date sources (T+1) ──
        # Covers: stk_factor_pro, stk_margin, idx_factor_pro, stk_moneyflow,
        #         stk_moneyflow_ths, fund_daily, stk_ah_comparison, stk_suspend,
        #         stk_cyq_perf, idx_daily_dc, idx_quote_dc, stk_st_daily
        return SourceAuditRecord(
            source_id=tbl,
            source_category=SourceCategory.DAILY_TRADE.value,
            tier=source.tier,
            date_field="trade_date",
            date_semantics=DateSemantics.TRADE_DATE.value,
            effective_date_field="trade_date",
            max_lag_calendar_days=1,
            max_lag_trading_days=1,
            actual_latest_date=None,
            actual_latest_effective_date=None,
            release_pattern="T+1 (next trading day)",
            is_local=True,
            has_lookahead_risk=False,
            requires_forward_fill=False,
            forward_fill_granularity="none",
            trade_calendar_aligned=True,
            notes=f"Daily trade-date aligned. Unit: see key_fields in source_registry.",
        )

    @staticmethod
    def classify_external_source(source: ExternalSource) -> SourceAuditRecord:
        """Classify an external Tushare source's date semantics."""
        ep = source.endpoint

        # ── Shibor: T+0, daily but rates published by 11:30 AM ──
        if ep == "shibor":
            return SourceAuditRecord(
                source_id=f"tushare:{ep}",
                source_category=SourceCategory.EXTERNAL_API.value,
                tier=source.tier,
                date_field="date",
                date_semantics=DateSemantics.TRADE_DATE.value,
                effective_date_field="date",
                max_lag_calendar_days=0,
                max_lag_trading_days=0,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="T+0 same-day (published by 11:30 AM CST)",
                is_local=False,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=False,
                notes="FREE tier. Daily interbank rates. Available same day before noon.",
            )

        # ── Shibor LPR: monthly, released 20th of each month ──
        if ep == "shibor_lpr":
            return SourceAuditRecord(
                source_id=f"tushare:{ep}",
                source_category=SourceCategory.MONTHLY_MACRO.value,
                tier=source.tier,
                date_field="date",
                date_semantics=DateSemantics.MONTH.value,
                effective_date_field="date",
                max_lag_calendar_days=30,
                max_lag_trading_days=20,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Monthly, announced ~20th of each month",
                is_local=False,
                has_lookahead_risk=True,
                requires_forward_fill=True,
                forward_fill_granularity="monthly",
                trade_calendar_aligned=False,
                notes=(
                    "LPR released monthly on the 20th. Forward-fill from release date "
                    "to next release date. Do NOT use as daily signal without fill."
                ),
            )

        # ── cn_m, sf_month: monthly macro, ~10-15d lag ──
        if ep in ("cn_m", "cn_social_financing"):
            ep_label = "sf_month" if ep == "cn_social_financing" else ep
            return SourceAuditRecord(
                source_id=f"tushare:{ep_label}",
                source_category=SourceCategory.MONTHLY_MACRO.value,
                tier=source.tier,
                date_field="month",
                date_semantics=DateSemantics.MONTH.value,
                effective_date_field="month",
                max_lag_calendar_days=15,
                max_lag_trading_days=10,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Monthly, released ~10-15d after month-end by PBOC",
                is_local=False,
                has_lookahead_risk=True,
                requires_forward_fill=True,
                forward_fill_granularity="monthly",
                trade_calendar_aligned=False,
                notes=(
                    "CRITICAL: monthly data has ~10-15 day release lag. "
                    "DO NOT signal on period_date (month). Compute effective_date "
                    "as month-end + 15 days. Forward-fill until next release. "
                    "cn_m=M0/M1/M2 YoY/MoM. cn_social_financing=社融 (sf_month)."
                ),
            )

        # ── fund_basic: snapshot ──
        if ep == "fund_basic":
            return SourceAuditRecord(
                source_id=f"tushare:{ep}",
                source_category=SourceCategory.FUND_DATA.value,
                tier=source.tier,
                date_field="found_date",
                date_semantics=DateSemantics.LIST_DATE.value,
                effective_date_field="found_date",
                max_lag_calendar_days=90,
                max_lag_trading_days=60,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="Snapshot of currently-listed funds",
                is_local=False,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=False,
                notes=(
                    "Snapshot only — current fund universe. Historical funds that "
                    "have been delisted/closed are NOT captured. For fund issuance "
                    "time-series, use found_date + issue_amount to reconstruct monthly "
                    "trend, but expect survivorship bias."
                ),
            )

        # ── fund_nav, etf_share_size: daily trade-date ──
        if ep in ("fund_nav", "etf_share_size"):
            return SourceAuditRecord(
                source_id=f"tushare:{ep}",
                source_category=SourceCategory.FUND_DATA.value,
                tier=source.tier,
                date_field="nav_date" if ep == "fund_nav" else "trade_date",
                date_semantics=DateSemantics.NAV_DATE.value if ep == "fund_nav" else DateSemantics.TRADE_DATE.value,
                effective_date_field="nav_date" if ep == "fund_nav" else "trade_date",
                max_lag_calendar_days=1,
                max_lag_trading_days=1,
                actual_latest_date=None,
                actual_latest_effective_date=None,
                release_pattern="T+1 daily",
                is_local=False,
                has_lookahead_risk=False,
                requires_forward_fill=False,
                forward_fill_granularity="none",
                trade_calendar_aligned=True,
                notes="Daily data with T+1 lag. fund_nav=NAV date, etf_share_size=trade_date.",
            )

        # ── Default: daily external T+1 ──
        # moneyflow_hsgt, index_dailybasic, daily_info, opt_daily
        return SourceAuditRecord(
            source_id=f"tushare:{ep}",
            source_category=SourceCategory.EXTERNAL_API.value,
            tier=source.tier,
            date_field="trade_date",
            date_semantics=DateSemantics.TRADE_DATE.value,
            effective_date_field="trade_date",
            max_lag_calendar_days=1,
            max_lag_trading_days=1,
            actual_latest_date=None,
            actual_latest_effective_date=None,
            release_pattern="T+1 daily",
            is_local=False,
            has_lookahead_risk=False,
            requires_forward_fill=False,
            forward_fill_granularity="none",
            trade_calendar_aligned=True,
            notes=f"External Tushare API. T+1 data lag.",
        )

    # ── run audit ──────────────────────────────────────────────────────

    def run_audit(self) -> EffectiveDateAudit:
        """Execute the full effective-date audit across all sources."""
        today = date.today().isoformat()
        calendar = self._get_calendar()

        local_sources = build_local_registry()
        external_sources = build_external_registry()

        records: List[SourceAuditRecord] = []

        # ── Process local sources ──
        for ls in local_sources:
            rec = self.classify_local_source(ls)

            # Profile the actual table for date freshness
            tbl = ls.table_name
            try:
                profile = self._profile_local_table(tbl, rec.date_field)
                rec.actual_latest_date = profile.get("latest")

                # For period sources, profile ann_date too
                if rec.date_semantics == DateSemantics.END_DATE.value:
                    ann_profile = self._profile_ann_date(
                        tbl,
                        rec.date_field,
                        rec.effective_date_field,
                    )
                    if "error" not in ann_profile:
                        rec.actual_latest_effective_date = ann_profile.get("latest_ann_date")
                        # Update max_lag based on actual data
                        actual_med = ann_profile.get("med_lag_days")
                        if actual_med is not None and actual_med > 0:
                            rec.max_lag_calendar_days = int(actual_med * 1.5)  # add buffer
                else:
                    rec.actual_latest_effective_date = rec.actual_latest_date

                # Compute actual lag vs today
                if rec.actual_latest_date and rec.actual_latest_date != "N/A":
                    try:
                        lag = self._calendar_days_between(rec.actual_latest_date, today)
                        if lag > rec.max_lag_calendar_days:
                            rec.notes += (
                                f" ACTUAL LAG WARNING: latest={rec.actual_latest_date}, "
                                f"lag={lag}d vs expected max={rec.max_lag_calendar_days}d. "
                                f"Data may be stale."
                            )
                    except Exception:
                        pass
            except Exception as e:
                rec.notes += f" (profile error: {e})"

            records.append(rec)

        # ── Process external sources ──
        for es in external_sources:
            rec = self.classify_external_source(es)
            # External sources don't have DuckDB data — use source metadata
            rec.actual_latest_date = "N/A (external)"
            rec.actual_latest_effective_date = "N/A (external)"
            rec.notes += (
                f" Tushare endpoint: {es.endpoint}. "
                f"Point cost: {es.point_cost}. "
                f"Permission: {es.permission_status}. "
            )
            records.append(rec)

        # ── Build summary ──
        lookahead_sources = [r for r in records if r.has_lookahead_risk]
        aligned_sources = [r for r in records if r.trade_calendar_aligned]
        fill_sources = [r for r in records if r.requires_forward_fill]

        cat_counts: dict[str, int] = {}
        for r in records:
            cat_counts[r.source_category] = cat_counts.get(r.source_category, 0) + 1

        recommendations = self._generate_recommendations(records, lookahead_sources)

        # Clean up
        self.close()

        return EffectiveDateAudit(
            audit_date=today,
            latest_trading_day=calendar["latest_trading_day"],
            total_sources=len(records),
            local_sources=len(local_sources),
            external_sources=len(external_sources),
            sources_with_lookahead_risk=len(lookahead_sources),
            sources_trade_calendar_aligned=len(aligned_sources),
            sources_requiring_forward_fill=len(fill_sources),
            records=records,
            summary_by_category=cat_counts,
            recommendations=recommendations,
        )

    # ── recommendations ────────────────────────────────────────────────

    @staticmethod
    def _generate_recommendations(
        records: List[SourceAuditRecord],
        lookahead_sources: List[SourceAuditRecord],
    ) -> List[str]:
        """Generate actionable recommendations from audit results."""
        recs: List[str] = []

        if lookahead_sources:
            recs.append(
                f"CRITICAL: {len(lookahead_sources)} sources have look-ahead risk "
                f"(period_date ≠ effective_date). These MUST use effective_date "
                f"for signal computation, NOT period_date: "
                f"{', '.join(r.source_id for r in lookahead_sources)}"
            )

        monthly_macro = [r for r in lookahead_sources if r.source_category == SourceCategory.MONTHLY_MACRO.value]
        if monthly_macro:
            recs.append(
                f"Monthly macro sources ({', '.join(r.source_id for r in monthly_macro)}) "
                f"have ~10-15 day release lag. Forward-fill from "
                f"month-end + 15 days to next release. Do NOT use as daily signal."
            )

        period_fin = [r for r in lookahead_sources if r.source_category == SourceCategory.PERIOD_FINANCIAL.value]
        if period_fin:
            recs.append(
                f"Period financial sources ({', '.join(r.source_id for r in period_fin)}) "
                f"have ~107-day median ann_date lag. Use ann_date as effective, "
                f"forward-fill quarterly."
            )

        stale = [r for r in records if "ACTUAL LAG WARNING" in r.notes]
        if stale:
            recs.append(
                f"STALE DATA: {len(stale)} sources have actual lag exceeding expected max. "
                f"Run incremental sync: './sync/run_incremental_sync.sh'"
            )

        fund_sources = [r for r in records if r.source_id == "fund_daily"]
        if fund_sources:
            recs.append(
                "fund_daily.trade_date is VARCHAR (YYYYMMDD), not DATE. "
                "Always CAST to DATE before joining with trade_calendar."
            )

        daily_aligned = [r for r in records if r.trade_calendar_aligned and not r.has_lookahead_risk]
        if daily_aligned:
            recs.append(
                f"{len(daily_aligned)} daily trade_date sources are trade_calendar-aligned "
                f"with T+0/T+1 lag. No look-ahead risk for these sources."
            )

        return recs


# ── Report generation ──────────────────────────────────────────────────────


def audit_to_dict(audit: EffectiveDateAudit) -> dict[str, Any]:
    """Convert audit to JSON-serializable dict."""
    return {
        "meta": {
            "audit_date": audit.audit_date,
            "latest_trading_day": audit.latest_trading_day,
            "total_sources": audit.total_sources,
            "local_sources": audit.local_sources,
            "external_sources": audit.external_sources,
            "sources_with_lookahead_risk": audit.sources_with_lookahead_risk,
            "sources_trade_calendar_aligned": audit.sources_trade_calendar_aligned,
            "sources_requiring_forward_fill": audit.sources_requiring_forward_fill,
        },
        "summary_by_category": audit.summary_by_category,
        "recommendations": audit.recommendations,
        "records": [asdict(r) for r in audit.records],
    }


def generate_markdown_report(audit: EffectiveDateAudit) -> str:
    """Generate a human-readable Markdown audit report."""
    lines: List[str] = []

    lines.append("# Effective-Date Audit Report")
    lines.append("")
    lines.append(f"**Audit Date**: {audit.audit_date}")
    lines.append(f"**Latest Trading Day**: {audit.latest_trading_day}")
    lines.append(f"**Total Sources**: {audit.total_sources} "
                 f"({audit.local_sources} local + {audit.external_sources} external)")
    lines.append("")

    # ── Summary stats ──
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---|")
    lines.append(f"| Sources with look-ahead risk | {audit.sources_with_lookahead_risk} |")
    lines.append(f"| Sources trade-calendar aligned | {audit.sources_trade_calendar_aligned} |")
    lines.append(f"| Sources requiring forward-fill | {audit.sources_requiring_forward_fill} |")
    lines.append("")

    lines.append("### By Category")
    lines.append("")
    lines.append("| Category | Count |")
    lines.append("|---|---|")
    for cat, cnt in sorted(audit.summary_by_category.items()):
        lines.append(f"| {cat} | {cnt} |")
    lines.append("")

    # ── Recommendations ──
    lines.append("## Recommendations")
    lines.append("")
    for i, rec in enumerate(audit.recommendations, 1):
        prefix = "🚨" if rec.startswith("CRITICAL") else "⚠️" if rec.startswith("STALE") else "✅"
        lines.append(f"{i}. {prefix} {rec}")
    lines.append("")

    # ── Per-source details ──
    lines.append("## Per-Source Audit")
    lines.append("")

    # Group by risk level
    high_risk = [r for r in audit.records if r.has_lookahead_risk]
    low_risk_aligned = [r for r in audit.records if r.trade_calendar_aligned and not r.has_lookahead_risk]
    other = [r for r in audit.records if r not in high_risk and r not in low_risk_aligned]

    lines.append("### 🔴 High Risk: Period Date ≠ Effective Date")
    lines.append("")
    lines.append("These sources have inherent release lag. The date field (period_date)")
    lines.append("does NOT represent when the data becomes available. Always use")
    lines.append("effective_date for signal computation.")
    lines.append("")
    lines.append("| Source | Date Field | Effective Field | Max Lag (cal) | Max Lag (trade) | Actual Latest | Forward-Fill |")
    lines.append("|---|---|---|---:|---:|---|---|")
    for r in high_risk:
        lines.append(
            f"| {r.source_id} | {r.date_field} | {r.effective_date_field} | "
            f"{r.max_lag_calendar_days} | {r.max_lag_trading_days} | "
            f"{r.actual_latest_date or 'N/A'} | {r.forward_fill_granularity} |"
        )
    lines.append("")

    lines.append("### 🟢 Low Risk: Trade-Calendar Aligned (T+0/T+1)")
    lines.append("")
    lines.append("These sources follow trade_calendar with ≤1-day lag. No look-ahead risk.")
    lines.append("")
    lines.append("| Source | Date Field | Max Lag | Actual Latest | Category |")
    lines.append("|---|---:|---:|---|---|")
    for r in low_risk_aligned:
        lines.append(
            f"| {r.source_id} | {r.date_field} | "
            f"{r.max_lag_calendar_days}d | {r.actual_latest_date or 'N/A'} | "
            f"{r.source_category} |"
        )
    lines.append("")

    if other:
        lines.append("### 🟡 Other: Snapshot / Event-Driven / External")
        lines.append("")
        lines.append("| Source | Date Field | Semantics | Max Lag | Actual Latest | Notes |")
        lines.append("|---|---|---:|---|---|")
        for r in other:
            notes_short = r.notes[:80] + "..." if len(r.notes) > 80 else r.notes
            lines.append(
                f"| {r.source_id} | {r.date_field} | {r.date_semantics} | "
                f"{r.max_lag_calendar_days}d | {r.actual_latest_date or 'N/A'} | "
                f"{notes_short} |"
            )
        lines.append("")

    # ── Calendar alignment section ──
    lines.append("## Calendar Alignment")
    lines.append("")
    lines.append("The trade calendar (`trade_calendar`) defines 6,543 trading days")
    lines.append(f"from 2000-01-01 to 2030-12-31. Latest trading day: {audit.latest_trading_day}.")
    lines.append("")
    lines.append("**Alignment Rules**:")
    lines.append("")
    lines.append("1. **Daily sources** (T+0/T+1): signal_date = trade_date. Data available")
    lines.append("   next calendar day (weekend → Monday). Calendar alignment is automatic.")
    lines.append("2. **Monthly sources** (T+~15d): signal_date = effective_date = month-end + 15d.")
    lines.append("   Forward-fill from effective_date(t) to effective_date(t+1) - 1.")
    lines.append("   The data for month M becomes visible on M+15, not on M+1.")
    lines.append("3. **Quarterly sources** (T+~53d median): signal_date = ann_date.")
    lines.append("   Forward-fill from ann_date(t) to ann_date(t+1) - 1.")
    lines.append("   The data for quarter Q (end_date=Q) becomes visible on ann_date.")
    lines.append("")
    lines.append("**Look-ahead violation example**:")
    lines.append("")
    lines.append("```")
    lines.append("WRONG: 2026-03-31 (end_date) → signal fires on 2026-04-01")
    lines.append("       Data was NOT yet public on 2026-04-01!")
    lines.append("")
    lines.append("RIGHT: 2026-03-31 (end_date) → ann_date=2026-04-28 → signal fires on 2026-04-28")
    lines.append("       Data became public on ann_date.")
    lines.append("```")
    lines.append("")

    # ── Forward-fill section ──
    lines.append("## Forward-Fill Protocol")
    lines.append("")
    lines.append("For sources requiring forward-fill, the protocol is:")
    lines.append("")
    lines.append("1. Identify the effective_date (ann_date, month+15d, or release date)")
    lines.append("2. Forward-fill the latest known value to all subsequent calendar days")
    lines.append("3. When a new release arrives, the new value overwrites from the new effective_date")
    lines.append("4. Signals on day T may only use data whose effective_date ≤ T")
    lines.append("")
    lines.append("### Forward-Fill Granularity by Source")
    lines.append("")
    lines.append("| Source | Granularity | Effective Date Rule |")
    lines.append("|---|---|")
    for r in audit.records:
        if r.requires_forward_fill:
            lines.append(f"| {r.source_id} | {r.forward_fill_granularity} | {r.notes[:120]} |")
    lines.append("")

    return "\n".join(lines)


# ── CLI entry ──────────────────────────────────────────────────────────────


def run_audit(
    duckdb_path: str = "./duckdb/ashare.duckdb",
    output_dir: str = "tmp/microstructure/validation",
) -> EffectiveDateAudit:
    """Run the effective-date audit and write results to disk.

    Returns the audit object for downstream consumption.
    """
    auditor = EffectiveDateAuditor(duckdb_path)
    audit = auditor.run_audit()

    os.makedirs(output_dir, exist_ok=True)

    # JSON output
    json_path = os.path.join(output_dir, "effective_date_audit.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(audit_to_dict(audit), f, ensure_ascii=False, indent=2)

    # Markdown report
    md_path = os.path.join(output_dir, "effective_date_audit.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_markdown_report(audit))

    print(f"Audit complete: {audit.total_sources} sources")
    print(f"  Look-ahead risk: {audit.sources_with_lookahead_risk}")
    print(f"  Calendar-aligned: {audit.sources_trade_calendar_aligned}")
    print(f"  Requires forward-fill: {audit.sources_requiring_forward_fill}")
    print(f"  JSON: {json_path}")
    print(f"  Report: {md_path}")

    return audit


if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else "./duckdb/ashare.duckdb"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "tmp/microstructure/validation"
    run_audit(db_path, out_dir)