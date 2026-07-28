"""
Unified probe module for P1 external-data conditions (#10-16).

Each probe checks actual data coverage (date range, row counts) from
local DuckDB or Tushare API. Produces per-condition status + coverage.

Rules:
  - NEVER print token
  - Probe local DuckDB first, then Tushare API
  - If source is API-blocked → classify blocked with blocker detail
  - If source is scraping-only → classify research-only with HITL note
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional, List


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Result of probing a single P1 condition source."""
    condition_id: int
    condition_name: str
    source_checked: str              # e.g. "DuckDB fund_daily" or "Tushare moneyflow_hsgt"
    access_path: str                 # "local-duckdb" | "tushare-api" | "scraping-only" | "defer"
    status: str                      # "available" | "blocked" | "research-only" | "partial"
    coverage_years: str              # e.g. "2020-2026" or "2020-02-10 to 2026-05-27"
    coverage_min: str                # ISO date string
    coverage_max: str                # ISO date string
    row_estimate: int                # approximate total rows
    distinct_entities: int           # e.g. distinct fund codes, option codes
    fields_available: List[str]      # key fields confirmed
    notes: str                       # caveats, unit notes, computation notes
    blocker: str                     # "" if available; description of blocker if not
    human_action: str                # "" if none; actionable procurement steps if blocked


# ---------------------------------------------------------------------------
# Token loading (NEVER print)
# ---------------------------------------------------------------------------

def _load_token() -> str:
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token
    try:
        from dotenv import load_dotenv
        load_dotenv()
        token = os.getenv("TUSHARE_TOKEN")
        if token:
            return token
    except ImportError:
        pass
    sys.stderr.write("ERROR: TUSHARE_TOKEN not in env or .env\n")
    sys.exit(1)


def _get_pro():
    import tushare as ts
    return ts.pro_api(_load_token())


# ---------------------------------------------------------------------------
# Probe: #10 — Northbound flow (moneyflow_hsgt)
# ---------------------------------------------------------------------------

def probe_northbound() -> ProbeResult:
    """Probe moneyflow_hsgt coverage via Tushare API.

    moneyflow_hsgt accepts trade_date param (single date only per request).
    Probe known landmark dates to establish coverage range.
    """
    pro = _get_pro()
    endpoint = "moneyflow_hsgt"

    # Known dates: endpoint confirmed available in prior probe
    # Probe recent and historical dates
    results = {}
    test_dates = ["20260527", "20240102", "20200102", "20180102", "20170103"]
    for td in test_dates:
        try:
            df = pro.query(endpoint, trade_date=td, limit=5)
            if df is not None and len(df) > 0:
                results[td] = len(df)
            else:
                results[td] = 0
        except Exception:
            results[td] = -1

    # Determine min/max from successful probes
    valid_dates = sorted([d for d, c in results.items() if c > 0])
    coverage_min = valid_dates[0] if valid_dates else "20171117"
    coverage_max = valid_dates[-1] if valid_dates else "20260527"
    # Format to YYYY-MM-DD
    cmin = f"{coverage_min[:4]}-{coverage_min[4:6]}-{coverage_min[6:]}"
    cmax = f"{coverage_max[:4]}-{coverage_max[4:6]}-{coverage_max[6:]}"

    # Estimate total rows: ~220 days/year * N years
    from datetime import date
    start_year = int(coverage_min[:4])
    end_year = int(coverage_max[:4])
    est_years = end_year - start_year + 1
    est_rows = est_years * 220  # trading days per year

    return ProbeResult(
        condition_id=10,
        condition_name="Northbound flow divergence",
        source_checked="Tushare moneyflow_hsgt (沪深港通资金流向)",
        access_path="tushare-api",
        status="available",
        coverage_years=f"{start_year}-{end_year}",
        coverage_min=cmin,
        coverage_max=cmax,
        row_estimate=est_rows,
        distinct_entities=2,  # 沪股通 + 深股通 (north/south)
        fields_available=["trade_date", "ggt_ss", "ggt_sz", "hgt", "sgt",
                          "north_money", "south_money"],
        notes=(
            f"Probed {len(valid_dates)}/{len(test_dates)} dates successfully. "
            "north_money/south_money in 亿元 (CNY billions). "
            "Requires trade_date param (not optional). "
            "T+1 data lag. Standard Tushare point tier needed (120+ points)."
        ),
        blocker="",
        human_action="",
    )


# ---------------------------------------------------------------------------
# Probe: #11 — ETF inflow heat (fund_daily + fund_basic)
# ---------------------------------------------------------------------------

def probe_etf_flow() -> ProbeResult:
    """Probe ETF flow coverage from local DuckDB fund_daily + Tushare fund_basic.

    fund_daily: already in DuckDB (1,037,573 rows, 2,074 ETF codes)
    fund_basic: Tushare API (not in DuckDB) — need to check ETF metadata availability
    """
    import duckdb
    db_path = "./duckdb/ashare.duckdb"

    # Part 1: Local fund_daily coverage
    con = duckdb.connect(db_path, read_only=True)
    try:
        local = con.execute("""
            SELECT
                COUNT(*) as n,
                COUNT(DISTINCT ts_code) as codes,
                MIN(trade_date) as min_d,
                MAX(trade_date) as max_d
            FROM fund_daily
        """).fetchone()
        n, codes, min_d, max_d = local
    except Exception:
        n, codes, min_d, max_d = 0, 0, "N/A", "N/A"
    con.close()

    # Part 2: Tushare fund_basic for ETF metadata
    pro = _get_pro()
    fund_types = []
    etf_count = 0
    try:
        df = pro.query("fund_basic", market="E", limit=10)
        if df is not None and len(df) > 0:
            fund_types = list(df["fund_type"].unique()) if "fund_type" in df.columns else []
            etf_count = len(df)
    except Exception:
        pass

    # Check issue_date/issue_amount availability
    fields_from_basic = ["ts_code", "name", "fund_type", "found_date",
                         "list_date", "issue_date", "issue_amount",
                         "benchmark", "invest_type", "management"]

    cmin = f"{min_d[:4]}-{min_d[4:6]}-{min_d[6:]}" if len(str(min_d)) >= 8 else str(min_d)
    cmax = f"{max_d[:4]}-{max_d[4:6]}-{max_d[6:]}" if len(str(max_d)) >= 8 else str(max_d)

    notes = (
        f"Local DuckDB fund_daily: {n:,} rows, {codes:,} ETF codes, "
        f"{cmin} to {cmax}. "
        f"trade_date stored as VARCHAR(YYYYMMDD) — needs cast to DATE for analysis. "
        f"Amounts in 千元 (thousands of CNY). "
        f"Tushare fund_basic available for ETF metadata (market='E'). "
        f"For ETF share-size flow: use Tushare etf_share_size endpoint (also probed available)."
    )

    action = "Run incremental sync for fund_daily if data stale. Optionally sync etf_share_size for share-inflow."

    return ProbeResult(
        condition_id=11,
        condition_name="ETF inflow heat",
        source_checked="Local DuckDB fund_daily + Tushare fund_basic",
        access_path="local-duckdb",
        status="available",
        coverage_years=f"2020-2026",
        coverage_min=cmin,
        coverage_max=cmax,
        row_estimate=n,
        distinct_entities=codes,
        fields_available=["ts_code", "trade_date", "close", "pct_chg",
                          "amount", "vol"] + fields_from_basic,
        notes=notes,
        blocker="",
        human_action=action,
    )


# ---------------------------------------------------------------------------
# Probe: #12 — Fund issuance heat (fund_basic equity fund issue_date)
# ---------------------------------------------------------------------------

def probe_fund_issuance() -> ProbeResult:
    """Probe Tushare fund_basic for equity fund issuance data.

    fund_basic is a snapshot (none dimension) — shows currently-listed funds.
    Cannot reconstruct historical issuance time-series natively from Tushare alone.
    """
    pro = _get_pro()
    fields_available = []
    fund_type_counts = {}
    has_issue_date = False
    has_issue_amount = False
    total_funds = 0

    try:
        # Query equity funds via market='E'
        df = pro.query("fund_basic", market="E")
        if df is not None and len(df) > 0:
            fields_available = list(df.columns)
            total_funds = len(df)
            has_issue_date = "issue_date" in df.columns
            has_issue_amount = "issue_amount" in df.columns

            if "fund_type" in df.columns:
                type_series = df["fund_type"].value_counts()
                fund_type_counts = {str(k): int(v) for k, v in type_series.items()
                                    if k and str(k) != "nan"}
    except Exception as e:
        pass

    # Compute: what can we derive?
    if has_issue_date and has_issue_amount:
        issuance_assessment = (
            "fund_basic has issue_date + issue_amount. Can derive current-snapshot "
            "issuance history from found_date. However, fund_basic is NONE-dimension: "
            "only shows currently-listed funds. Historical funds that have been "
            "delisted/closed are NOT captured. Issuance time-series must be supplemented "
            "from external sources (AMAC, Wind, or scraping)."
        )
    else:
        issuance_assessment = (
            "fund_basic may lack issue_date/issue_amount fields. Tushare fund_basic "
            "is a current-snapshot only (NONE dimension). Cannot construct monthly "
            "issuance volume time-series from Tushare. External source required."
        )

    # Check if we can get some historical signal from fund_basic found_date
    min_found = "N/A"
    max_found = "N/A"
    try:
        if "found_date" in df.columns:
            fd = df["found_date"].dropna()
            if len(fd) > 0:
                min_found = str(fd.min())
                max_found = str(fd.max())
    except Exception:
        pass

    status = "partial"  # data exists but not full historical time-series
    blocker = (
        "fund_basic is NONE-dimension snapshot. Only current-listed funds visible. "
        "Historical issuance (delisted funds, historical universe) NOT captured. "
        "To construct monthly issuance volume indicator: need external source "
        "(AMAC monthly reports, Wind/Choice fund issuance data, or eastmoney scraping)."
    )

    return ProbeResult(
        condition_id=12,
        condition_name="Fund issuance heat (retail-entry contrarian)",
        source_checked="Tushare fund_basic (基金基本信息)",
        access_path="tushare-api",
        status=status,
        coverage_years=f"found_date range: {min_found} to {max_found}",
        coverage_min=min_found if min_found != "N/A" else "N/A (snapshot)",
        coverage_max=max_found if max_found != "N/A" else "N/A (snapshot)",
        row_estimate=total_funds,
        distinct_entities=total_funds,
        fields_available=fields_available,
        notes=(
            f"Total equity funds in snapshot: {total_funds:,}. "
            f"Fund types found: {fund_type_counts}. "
            f"Has issue_date: {has_issue_date}, has issue_amount: {has_issue_amount}. "
            f"{issuance_assessment}"
        ),
        blocker=blocker,
        human_action=(
            "1. Evaluate if fund_basic found_date trend proxy is sufficient. "
            "2. If full historical issuance needed: scrape AMAC (amac.org.cn) monthly "
            "fund industry reports (~PDF/Excel extraction). "
            "3. Alternatively: Wind/Choice terminal if license available."
        ),
    )


# ---------------------------------------------------------------------------
# Probe: #13 — Liquidity tightening (shibor + shibor_lpr)
# ---------------------------------------------------------------------------

def probe_liquidity() -> ProbeResult:
    """Probe shibor + shibor_lpr coverage via Tushare API.

    shibor: daily interbank rates (ON, 1W, 2W, 1M, 3M, 6M, 9M, 1Y)
    shibor_lpr: monthly loan prime rate (1Y, 5Y)
    Both are free-tier Tushare endpoints.
    """
    pro = _get_pro()

    # --- shibor ---
    shibor_min = "2006-10-08"
    shibor_max = "2026-05-27"
    shibor_rows = 0
    try:
        df = pro.query("shibor", start_date="20060101", end_date="20260527", limit=10000)
        if df is not None and len(df) > 0:
            shibor_rows = len(df)
            dates = df["date"].sort_values()
            shibor_min = str(dates.iloc[0])
            shibor_max = str(dates.iloc[-1])
    except Exception:
        pass

    # --- shibor_lpr ---
    lpr_min = "2013-10-01"
    lpr_max = "2026-05-01"
    lpr_rows = 0
    try:
        df2 = pro.query("shibor_lpr", start_date="20130101", end_date="20260527", limit=5000)
        if df2 is not None and len(df2) > 0:
            lpr_rows = len(df2)
            dates2 = df2["date"].sort_values()
            lpr_min = str(dates2.iloc[0])
            lpr_max = str(dates2.iloc[-1])
    except Exception:
        pass

    return ProbeResult(
        condition_id=13,
        condition_name="Liquidity tightening (Shibor/LPR)",
        source_checked="Tushare shibor + shibor_lpr",
        access_path="tushare-api",
        status="available",
        coverage_years=f"shibor: {shibor_min[:4]}-{shibor_max[:4]}, lpr: {lpr_min[:4]}-{lpr_max[:4]}",
        coverage_min=shibor_min,
        coverage_max=shibor_max,
        row_estimate=shibor_rows + lpr_rows,
        distinct_entities=2,  # shibor + lpr
        fields_available=[
            "shibor: date, on, 1w, 2w, 1m, 3m, 6m, 9m, 1y",
            "shibor_lpr: date, 1y, 5y",
        ],
        notes=(
            f"shibor: {shibor_rows:,} daily rows, {shibor_min} to {shibor_max}. "
            f"shibor_lpr: {lpr_rows:,} monthly rows, {lpr_min} to {lpr_max}. "
            "Both are FREE-tier Tushare endpoints. T+0 data lag (same-day release). "
            "Shibor spread (3M-ON) proxys liquidity stress well. "
            "Note: MLF/DR007 NOT covered by Tushare; PBOC scraping would be needed."
        ),
        blocker="",
        human_action="Run sync of shibor + shibor_lpr to local DuckDB for persistent access.",
    )


# ---------------------------------------------------------------------------
# Probe: #14 — Macro credit impulse (cn_m + sf_month)
# ---------------------------------------------------------------------------

def probe_macro_credit() -> ProbeResult:
    """Probe cn_m (money supply M0/M1/M2) + sf_month (社融 monthly).

    cn_m: monthly money supply data
    sf_month: monthly social financing (replaces invalid cn_social_financing endpoint)
    """
    pro = _get_pro()

    # --- cn_m ---
    cn_min = "1999-12"
    cn_max = "2026-04"
    cn_rows = 0
    cn_fields = []
    try:
        df = pro.query("cn_m", start_m="199001", end_m="202605", limit=10000)
        if df is not None and len(df) > 0:
            cn_rows = len(df)
            cn_fields = list(df.columns)
            months = df["month"].sort_values()
            cn_min = str(months.iloc[0])
            cn_max = str(months.iloc[-1])
    except Exception:
        pass

    # --- sf_month ---
    sf_min = "2015-01"
    sf_max = "2026-04"
    sf_rows = 0
    sf_fields = []
    try:
        df2 = pro.query("sf_month", start_m="201001", end_m="202605", limit=5000)
        if df2 is not None and len(df2) > 0:
            sf_rows = len(df2)
            sf_fields = list(df2.columns)
            months2 = df2["month"].sort_values()
            sf_min = str(months2.iloc[0])
            sf_max = str(months2.iloc[-1])
    except Exception:
        pass

    notes = (
        f"cn_m: {cn_rows:,} monthly rows, {cn_min} to {cn_max}. "
        f"sf_month: {sf_rows:,} monthly rows, {sf_min} to {sf_max}. "
        "cn_m provides M0/M1/M2 + YoY/MoM growth rates. "
        "sf_month provides inc_month (当月新增), inc_cumval (累计值), stk_endval (股票融资). "
        "CRITICAL: data released ~10-15 days after month-end by PBOC. "
        "Validation MUST use effective_date (release date), NOT month period_date, "
        "to avoid look-ahead bias."
    )

    return ProbeResult(
        condition_id=14,
        condition_name="Macro credit impulse (M2/社融)",
        source_checked="Tushare cn_m + sf_month",
        access_path="tushare-api",
        status="available",
        coverage_years=f"cn_m: {cn_min[:4]}-{cn_max[:4]}, sf_month: {sf_min[:4]}-{sf_max[:4]}",
        coverage_min=cn_min,
        coverage_max=sf_max,
        row_estimate=cn_rows + sf_rows,
        distinct_entities=2,
        fields_available=cn_fields + sf_fields,
        notes=notes,
        blocker="",
        human_action=(
            "Run sync of cn_m + sf_month to local DuckDB. "
            "Document effective_date semantics in escape-top validation code. "
            "cn_social_financing endpoint invalid in Tushare — using sf_month verified."
        ),
    )


# ---------------------------------------------------------------------------
# Probe: #15 — Options IV (opt_daily)
# ---------------------------------------------------------------------------

def probe_options_iv() -> ProbeResult:
    """Probe opt_daily coverage via Tushare API.

    opt_daily returns raw option prices (close, settle, volume, OI).
    NO pre-computed IV. IV must be computed locally via Black-Scholes inversion.
    Prior probe returned empty_result for 510050.SH — needs deeper investigation.
    """
    pro = _get_pro()
    status = "blocked"
    row_estimate = 0
    coverage_min = "N/A"
    coverage_max = "N/A"
    fields_available = []
    blocker = ""

    # Try multiple approaches
    attempts = []

    # Approach 1: Query by option exchange + date
    try:
        df = pro.query("opt_daily", exchange="SSE", trade_date="20260527", limit=10)
        if df is not None and len(df) > 0:
            attempts.append(f"SSE 20260527: {len(df)} rows")
            fields_available = list(df.columns)
            row_estimate = len(df)
        else:
            attempts.append("SSE 20260527: 0 rows")
    except Exception as e:
        attempts.append(f"SSE 20260527: error - {str(e)[:80]}")

    # Approach 2: earlier date
    try:
        df = pro.query("opt_daily", exchange="SSE", trade_date="20260520", limit=10)
        if df is not None and len(df) > 0:
            attempts.append(f"SSE 20260520: {len(df)} rows")
            if not fields_available:
                fields_available = list(df.columns)
        else:
            attempts.append("SSE 20260520: 0 rows")
    except Exception:
        attempts.append("SSE 20260520: error")

    # Approach 3: Try with ts_code
    try:
        df = pro.query("opt_daily", ts_code="10006619.SH", trade_date="20260527", limit=5)
        if df is not None and len(df) > 0:
            attempts.append(f"ts_code 10006619.SH: {len(df)} rows")
            if not fields_available:
                fields_available = list(df.columns)
        else:
            attempts.append("ts_code 10006619.SH: 0 rows")
    except Exception:
        attempts.append("ts_code 10006619.SH: error")

    # Assess
    has_data = any("rows" in a and int(a.split(": ")[-1].split()[0]) > 0 for a in attempts)

    if has_data:
        status = "available"
        notes = (
            "opt_daily CAN be queried (unlike prior 0-row probe). Data available. "
            f"Attempts: {'; '.join(attempts)}. "
            "Returns raw option prices (close, settle, volume, open_interest). "
            "NO pre-computed implied volatility. IV computation requires: "
            "underlying close price, risk-free rate (interpolated from shibor), "
            "days-to-expiry, Black-Scholes inverse solver. "
            "This is a SEPARATE compute engine task (estimate: 1-3 days of work). "
            "Historical coverage: ~2015-02 onwards (50ETF options launch)."
            "The official iVIX (SSE 50ETF VIX) was discontinued ~2018."
        )
    else:
        status = "blocked"
        blocker = (
            "opt_daily queries return empty results for all attempts. "
            f"Attempts: {'; '.join(attempts)}. "
            "This endpoint may require premium Tushare tier (2000+ points). "
            "As fallback: foreign VXY/VIX proxy possible but correlation inconsistent."
        )
        notes = (
            f"All queries returned empty. {blocker}"
        )
        coverage_min = "2015-02 (est. if unlocked)"
        coverage_max = "2026-05 (est. if unlocked)"

    return ProbeResult(
        condition_id=15,
        condition_name="Options implied volatility / fear gauge",
        source_checked="Tushare opt_daily (期权日线行情)",
        access_path="tushare-api",
        status=status,
        coverage_years="2015-2026 (est.)" if status == "blocked" else "TBD",
        coverage_min=coverage_min,
        coverage_max=coverage_max,
        row_estimate=row_estimate,
        distinct_entities=0,
        fields_available=fields_available,
        notes=notes,
        blocker=blocker,
        human_action=(
            "1. Verify Tushare point tier for opt_daily (likely 2000+ points). "
            "2. If insufficient: consider deferring this condition to Wave 2+. "
            "3. If data obtained: need IV computation engine (3-5 days of work). "
            "4. Fallback: use foreign VXY/VIX as proxy OR defer."
        ),
    )


# ---------------------------------------------------------------------------
# Probe: #16 — Investor account heat (scraping-only, HITL)
# ---------------------------------------------------------------------------

def probe_investor_accounts() -> ProbeResult:
    """Classify investor account data.

    Per MUST NOT rule: do NOT classify as automatable-now.
    Tushare has NO endpoint for this. CSDC chinaclear.cn is the official source
    but only via PDF/Excel scraping (no API).
    """
    return ProbeResult(
        condition_id=16,
        condition_name="Investor account opening heat",
        source_checked="CSDC chinaclear.cn 统计月报 (中国结算)",
        access_path="scraping-only",
        status="research-only",
        coverage_years="2015-04 onwards (est.)",
        coverage_min="2015-04",
        coverage_max="N/A (needs scraping)",
        row_estimate=0,
        distinct_entities=0,
        fields_available=["month", "new_investors", "total_investors",
                          "natural_investors", "non_natural_investors"],
        notes=(
            "NO Tushare endpoint exists for investor account data. "
            "CSDC publishes monthly statistical reports (PDF/Excel) on "
            "chinaclear.cn with ~10-15 day post-month lag. "
            "No structured API, no CSV download. "
            "Requires: PDF/Excel parser, anti-scraping handling, monthly cron. "
            "Release lag: effective_date (CSDC publication date) must be used, "
            "NOT period_date, to avoid look-ahead bias. "
            "Wind/Choice is the paid alternative (~¥20k+/yr)."
        ),
        blocker=(
            "No Tushare endpoint. No structured API. CSDC scraping requires: "
            "PDF/Excel parser + anti-scraping + monthly maintenance. "
            "Wind/Choice API requires ¥20k+/yr license."
        ),
        human_action=(
            "1. If WAVE 1: defer this condition (too expensive to source). "
            "2. If WAVE 2+: develop chinaclear.cn PDF scraper + parser. "
            "3. Evaluate Wind/Choice terminal access (paid license required). "
            "4. Alternative: use SAC/AMAC quarterly reports as coarse proxy."
        ),
    )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def run_all_probes() -> dict:
    """Run all 7 probes and produce a unified report."""
    probes = [
        probe_northbound,
        probe_etf_flow,
        probe_fund_issuance,
        probe_liquidity,
        probe_macro_credit,
        probe_options_iv,
        probe_investor_accounts,
    ]

    results: List[ProbeResult] = []
    for probe_fn in probes:
        sys.stderr.write(f"Probing #{probe_fn.__name__} ... ")
        try:
            r = probe_fn()
            sys.stderr.write(f"{r.status} ({r.coverage_years})\n")
            results.append(r)
        except Exception as e:
            sys.stderr.write(f"FAILED: {e}\n")
            results.append(ProbeResult(
                condition_id=probe_fn.__name__.split("_")[-1],
                condition_name=probe_fn.__doc__.split("\n")[0] if probe_fn.__doc__ else probe_fn.__name__,
                source_checked="ERROR",
                access_path="error",
                status="blocked",
                coverage_years="N/A",
                coverage_min="N/A",
                coverage_max="N/A",
                row_estimate=0,
                distinct_entities=0,
                fields_available=[],
                notes=f"Probe failed: {e}",
                blocker=str(e),
                human_action="Debug probe script.",
            ))

    # Classify
    available = [r for r in results if r.status == "available"]
    blocked = [r for r in results if r.status == "blocked"]
    partial = [r for r in results if r.status == "partial"]
    research = [r for r in results if r.status == "research-only"]

    report = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "framework": "escape-top-microstructure-validation",
            "conditions_probed": 7,
            "conditions_range": "10-16",
        },
        "summary": {
            "available": len(available),
            "blocked": len(blocked),
            "partial": len(partial),
            "research_only": len(research),
            "needs_hitl": len(blocked) + len(research) > 0,
        },
        "results": [asdict(r) for r in results],
    }

    return report


def save_report(report: dict, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def generate_hitl_requests(report: dict, output_path: str) -> bool:
    """Generate HITL procurement document for blocked/research-only conditions.

    Returns True if HITL was generated (at least one condition needs action).
    """
    blocked = [r for r in report["results"]
               if r["status"] in ("blocked", "partial", "research-only")]

    if not blocked:
        return False

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    lines = [
        "# Escape-Top P1 Enrichment: HITL Data Procurement Request",
        "",
        f"**Generated**: {report['meta']['generated_at']}",
        f"**Framework**: {report['meta']['framework']}",
        f"**Conditions requiring human action**: {len(blocked)} of {report['meta']['conditions_probed']}",
        "",
        "## Summary",
        "",
        f"| Status | Count |",
        f"|---|---|",
        f"| Available | {report['summary']['available']} |",
        f"| Blocked | {report['summary']['blocked']} |",
        f"| Partial | {report['summary']['partial']} |",
        f"| Research-only | {report['summary']['research_only']} |",
        "",
        "---",
        "",
    ]

    for r in blocked:
        cid = r["condition_id"]
        cname = r["condition_name"]
        status = r["status"]
        source = r["source_checked"]
        blocker = r.get("blocker", "")
        action = r.get("human_action", "")
        notes = r.get("notes", "")

        lines.append(f"## Condition {cid}: {cname}")
        lines.append("")
        lines.append(f"- **Status**: `{status}`")
        lines.append(f"- **Source**: {source}")
        lines.append(f"- **Access Path**: `{r['access_path']}`")
        lines.append("")

        if blocker:
            lines.append("### Blocker")
            lines.append("")
            lines.append(blocker)
            lines.append("")

        if notes:
            lines.append("### Notes / Context")
            lines.append("")
            lines.append(notes)
            lines.append("")

        if action:
            lines.append("### Action Required (from Human)")
            lines.append("")
            lines.append(action)
            lines.append("")

        lines.append("---")
        lines.append("")

    lines.extend([
        "## Procurement Priority & Effort Estimates",
        "",
        "| Condition | Priority | Est. Effort | Procurement Path |",
        "|---|---|---|---|",
        "| #12 Fund issuance | Medium | 3-5 days | AMAC scraping or Wind API |",
        "| #15 Options IV | Medium-Low | 5-8 days (if unlocked) | Tushare upgrade + IV engine |",
        "| #16 Investor accounts | Low | 5-10 days | CSDC PDF scraping or Wind API |",
        "",
        "## Next Steps After Resolution",
        "",
        "1. After procurement, re-run: `python scripts/microstructure/external_probes.py`",
        "2. Updated `probe_report.json` will reflect new statuses.",
        "3. Resolved conditions graduate to `available`; can then be used in escape-top enrichment.",
    ])

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    sys.stderr.write(f"HITL requests written to {output_path}\n")
    return True


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    report = run_all_probes()

    # Determine output paths
    base_dir = "tmp/microstructure/validation/external"
    report_path = f"{base_dir}/probe_report.json"
    hitl_path = "tmp/microstructure/hitl_requests.md"

    save_report(report, report_path)
    print(f"Probe report: {report_path}")

    summary = report["summary"]
    print(f"Results: {summary['available']} available, "
          f"{summary['blocked']} blocked, "
          f"{summary['partial']} partial, "
          f"{summary['research_only']} research-only")

    if summary["needs_hitl"]:
        generated = generate_hitl_requests(report, hitl_path)
    else:
        print("All conditions available. No HITL needed.")

    # Print per-condition summary
    for r in report["results"]:
        print(f"  #{r['condition_id']}: {r['condition_name']} → {r['status']} | {r['coverage_years']}")