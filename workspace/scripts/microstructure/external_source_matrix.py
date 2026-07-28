"""
External source availability matrix for escape-top P1 conditions (#10-16).

Each SourceEntry classifies the access path, data quality, automation feasibility,
and fallback options for a specific data source backing a P1 escape-top condition.

Classification legend:
  automatable-now     : API available, no additional permission/payment needed
  permission-needed   : API exists but requires elevated Tushare points/permissions
  scraping-needed     : No structured API; must web-scrape from official/government source
  paid-provider-needed: Only available via commercial data vendors (Wind/Bloomberg/etc.)
  defer               : No viable source identified in current scope; defer to future

Rules (MUST NOT):
  - Web articles / forums / blogs are NOT accredited sources.
  - Investor account data MUST NOT be classified automatable-now without verified API.
  - Claims about data quality require explicit evidence or caveats.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Feasibility(str, Enum):
    AUTOMATABLE_NOW = "automatable-now"
    PERMISSION_NEEDED = "permission-needed"
    SCRAPING_NEEDED = "scraping-needed"
    PAID_PROVIDER_NEEDED = "paid-provider-needed"
    DEFER = "defer"


class SourceTier(str, Enum):
    """Ordered hierarchy: prefer lower enum values."""
    LOCAL_DUCKDB = "local-duckdb"           # already in DuckDB, highest trust
    OFFICIAL_API = "official-api"           # Tushare or other structured API
    OFFICIAL_PUBLICATION = "official-pub"   # gov/regulator published data (scrape-able)
    PAID_VENDOR = "paid-vendor"             # Wind/Bloomberg/Choice/CSMAR
    WEB_SCRAPE = "web-scrape"               # official site scraping (NO media articles)
    FALLBACK_PROXY = "fallback-proxy"       # indirect/proxy metric as last resort


class Frequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    TICK = "tick"


class DataLag(str, Enum):
    T0 = "T+0"             # same-day (intraday snapshots)
    T1 = "T+1"             # next trading day
    T2 = "T+2"             # two days
    T_1M = "T+~1 month"   # ~1 month after period-end
    VARIABLE = "variable"  # release calendar dependent


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FieldSpec:
    """A key data field from this source."""
    name: str
    dtype: str              # Python/pandas type hint: float64, int64, str
    description: str
    unit: Optional[str] = None  # e.g. "元", "亿元", "万手", "%" (None for identifier/date fields)


@dataclass
class AccessHint:
    """How to access this source in this project."""
    tushare_endpoint: Optional[str] = None   # e.g. "moneyflow_hsgt"
    tushare_method: str = "query"            # "query" or named method
    duckdb_table: Optional[str] = None       # e.g. "stk_moneyflow"
    doc_url: Optional[str] = None            # Tushare doc URL or official ref
    env_requirements: list[str] = field(default_factory=list)  # ["TUSHARE_TOKEN=120+"]
    special_notes: list[str] = field(default_factory=list)


@dataclass
class Blockers:
    """Explicit data-procurement blockers."""
    permission_required: bool = False
    permission_detail: str = ""
    paid_license_required: bool = False
    paid_license_detail: str = ""
    no_structured_api: bool = False
    no_structured_api_detail: str = ""
    historical_gap: bool = False           # sparse/unreliable history
    historical_gap_detail: str = ""
    release_lag_concern: bool = False      # data released too late to be useful
    release_lag_detail: str = ""


@dataclass
class FallbackSource:
    """Next-best source if primary is unavailable."""
    name: str
    access_method: str
    feasibility: Feasibility
    tier: SourceTier
    caveats: str = ""


@dataclass
class SourceEntry:
    """A single data source backing a P1 escape-top condition."""
    condition_id: int                          # 10-16
    condition_name: str                        # e.g. "Northbound flow divergence"
    source_name: str                           # e.g. "Tushare moneyflow_hsgt"
    access_method: str                         # e.g. "Tushare pro.query('moneyflow_hsgt')"
    feasibility: Feasibility
    tier: SourceTier
    frequency: Frequency
    data_lag: DataLag
    key_fields: list[FieldSpec] = field(default_factory=list)
    access_hint: AccessHint = field(default_factory=AccessHint)
    blockers: Blockers = field(default_factory=Blockers)
    fallbacks: list[FallbackSource] = field(default_factory=list)
    rationale: str = ""                        # why this classification
    data_start_estimate: str = ""              # approximate earliest available date
    human_action_required: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Matrix builder
# ---------------------------------------------------------------------------

def build_source_matrix() -> list[SourceEntry]:
    """Return the finite external source availability matrix for P1 conditions."""

    matrix: list[SourceEntry] = []

    # ---- Condition 10: Northbound flow divergence ----
    matrix.append(SourceEntry(
        condition_id=10,
        condition_name="Northbound flow divergence",
        source_name="Tushare moneyflow_hsgt (沪深港通资金流向)",
        access_method="Tushare pro.query('moneyflow_hsgt')",
        feasibility=Feasibility.PERMISSION_NEEDED,
        tier=SourceTier.OFFICIAL_API,
        frequency=Frequency.DAILY,
        data_lag=DataLag.T1,
        key_fields=[
            FieldSpec("trade_date", "date", "交易日", unit=None),
            FieldSpec("ggt_ss", "float64", "亿元", "港股通(沪)流入"),
            FieldSpec("ggt_sz", "float64", "亿元", "港股通(深)流入"),
            FieldSpec("hgt", "float64", "亿元", "沪股通流入"),
            FieldSpec("sgt", "float64", "亿元", "深股通流入"),
            FieldSpec("north_money", "float64", "亿元", "北向资金净流入(合计)"),
            FieldSpec("south_money", "float64", "亿元", "南向资金净流入(合计)"),
        ],
        access_hint=AccessHint(
            tushare_endpoint="moneyflow_hsgt",
            tushare_method="query",
            duckdb_table=None,  # not yet synced
            doc_url="https://tushare.pro/document/2?doc_id=47",
            env_requirements=["TUSHARE_TOKEN"],
            special_notes=[
                "Requires Tushare 120+ points (users below threshold get empty data)",
                "Field names differ by Tushare version; confirm: north_money, south_money",
            ],
        ),
        blockers=Blockers(
            permission_required=True,
            permission_detail=(
                "Tushare moneyflow_hsgt requires 120+ points. "
                "If current account < 120, data returns empty. "
                "Need human to verify point balance and upgrade if needed."
            ),
        ),
        fallbacks=[
            FallbackSource(
                name="东方财富/同花顺沪深港通网页",
                access_method="Web scrape from eastmoney.com northbound flow page",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.WEB_SCRAPE,
                caveats=(
                    "Exact field names and historical availability inconsistent. "
                    "Not an accredited source; use only as backup if Tushare fails. "
                    "Scraper would need maintenance against site changes."
                ),
            ),
            FallbackSource(
                name="Wind/Choice data terminal",
                access_method="Commercial data terminal API",
                feasibility=Feasibility.PAID_PROVIDER_NEEDED,
                tier=SourceTier.PAID_VENDOR,
                caveats=(
                    "Wind API requires per-seat license (~¥20k+/yr). "
                    "Only viable if team already has terminal access."
                ),
            ),
        ],
        rationale=(
            "moneyflow_hsgt is the canonical Tushare endpoint for northbound/southbound "
            "aggregate flows in CNY billions. Available daily with T+1 lag. "
            "Classified permission-needed because the endpoint requires ≥120 Tushare points; "
            "a free-tier token (100 points) cannot access it. "
            "If permission is granted, this becomes automatable-now via the existing sync pipeline. "
            "Fallback: web scrape from eastmoney (scraping-needed, fragile) or Wind (paid)."
        ),
        data_start_estimate="2017-11 (沪港通 opened 2014, 深港通 2016, daily aggregate ~2017)",
        human_action_required=[
            "Check Tushare account point balance ($TOKEN points query)",
            "If < 120 points: purchase/earn points or seek approval for paid tier",
        ],
    ))

    # ---- Condition 11: ETF inflow heat ----
    matrix.append(SourceEntry(
        condition_id=11,
        condition_name="ETF inflow heat",
        source_name="Tushare fund_daily (ETF日线行情) — already partially synced to DuckDB",
        access_method="Local DuckDB: fund_daily table, or Tushare pro.query('fund_daily')",
        feasibility=Feasibility.AUTOMATABLE_NOW,
        tier=SourceTier.LOCAL_DUCKDB,
        frequency=Frequency.DAILY,
        data_lag=DataLag.T1,
        key_fields=[
            FieldSpec("ts_code", "str", "ETF代码, e.g. 510050.SH", unit=None),
            FieldSpec("trade_date", "date", "交易日", unit=None),
            FieldSpec("close", "float64", "元", "收盘价"),
            FieldSpec("vol", "float64", "手", "成交量"),
            FieldSpec("amount", "float64", "千元", "成交额"),
            FieldSpec("pct_chg", "float64", "%", "涨跌幅"),
        ],
        access_hint=AccessHint(
            tushare_endpoint="fund_daily",
            tushare_method="query",
            duckdb_table="fund_daily",
            doc_url="https://tushare.pro/document/2?doc_id=127",
            env_requirements=["TUSHARE_TOKEN"],
            special_notes=[
                "fund_daily IS in the local DuckDB index (_index.md lists it under fund module)",
                "Current fund_daily table may need sync to catch up to latest data",
                "No pre-computed technical indicators; compute locally if needed",
            ],
        ),
        blockers=Blockers(
            historical_gap=True,
            historical_gap_detail=(
                "ETFs proliferated rapidly after 2019, especially sector/thematic ETFs. "
                "Pre-2019 history may be sparse for specific fund codes. "
                "Use broad-market ETFs (510050, 510300, 510500, 159915) as minimum baseline."
            ),
        ),
        fallbacks=[
            FallbackSource(
                name="Tushare etf_share_size",
                access_method="Tushare pro.query('etf_share_size')",
                feasibility=Feasibility.AUTOMATABLE_NOW,
                tier=SourceTier.OFFICIAL_API,
                caveats=(
                    "Provides ETF share count and NAV; useful for computing aggregate "
                    "inflow/redemption in shares. Requires Tushare basic-tier permissions. "
                    "Not yet synced to local DuckDB."
                ),
            ),
            FallbackSource(
                name="东方财富ETF数据中心",
                access_method="Web scrape from eastmoney ETF data pages",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.WEB_SCRAPE,
                caveats=(
                    "Provides daily net-inflow/redemption and fund size. "
                    "Scraper brittleness: page structure changes may break extraction."
                ),
            ),
        ],
        rationale=(
            "fund_daily is already listed in the project DuckDB index. "
            "Its OHLCV fields (close, vol, amount) can directly support ETF turnover heat "
            "metrics. Classified automatable-now because the table exists locally, "
            "needs only an incremental sync. For share-size/size-related heat, "
            "etf_share_size is the complementary Tushare endpoint."
        ),
        data_start_estimate="2015 (major broad-market ETFs), 2019+ (sector ETFs widely available)",
        human_action_required=[
            "Run incremental sync for fund_daily if data is stale",
            "Optionally sync etf_share_size for share-size inflow dimension",
        ],
    ))

    # ---- Condition 12: Fund issuance heat ----
    matrix.append(SourceEntry(
        condition_id=12,
        condition_name="Fund issuance heat (retail-entry contrarian proxy)",
        source_name="Tushare fund_basic + fund_nav (基金基本信息 + 净值)",
        access_method="Tushare pro.query('fund_basic') + pro.query('fund_nav')",
        feasibility=Feasibility.PERMISSION_NEEDED,
        tier=SourceTier.OFFICIAL_API,
        frequency=Frequency.MONTHLY,
        data_lag=DataLag.T_1M,
        key_fields=[
            FieldSpec("ts_code", "str", "基金代码", unit=None),
            FieldSpec("name", "str", "基金简称", unit=None),
            FieldSpec("fund_type", "str", "基金类型 (股票型/混合型等)", unit=None),
            FieldSpec("found_date", "date", "成立日期", unit=None),
            FieldSpec("issue_amount", "float64", "亿份", "募集份额"),
            FieldSpec("nav_date", "date", "净值日期", unit=None),
            FieldSpec("unit_nav", "float64", "元", "单位净值"),
        ],
        access_hint=AccessHint(
            tushare_endpoint="fund_nav",
            tushare_method="query",
            duckdb_table=None,
            doc_url="https://tushare.pro/document/2?doc_id=19",
            env_requirements=["TUSHARE_TOKEN"],
            special_notes=[
                "fund_basic is a full snapshot (no historical dimension) — captures current fund universe",
                "fund_nav returns daily NAV; needs cross-walk with fund_basic to compute issuance volume",
                "Tushare fund_nav may require VIP endpoint for full historical access",
            ],
        ),
        blockers=Blockers(
            permission_required=True,
            permission_detail=(
                "fund_nav may have point-tier restrictions for extended history. "
                "fund_basic is accessible at basic tier but is a current-snapshot only. "
                "To compute historical issuance by month, we need found_date + issue_amount "
                "history, which Tushare fund_basic may not provide natively."
            ),
            historical_gap=True,
            historical_gap_detail=(
                "fund_basic is a 'none' dimension snapshot — only shows current fund universe, "
                "not historical fund listings. Cannot backfill issuance activity before the "
                "snapshot date without alternative sources."
            ),
        ),
        fallbacks=[
            FallbackSource(
                name="中基协 AMAC 月度公募基金数据",
                access_method="Scrape from AMAC (amac.org.cn) monthly public reports",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.OFFICIAL_PUBLICATION,
                caveats=(
                    "AMAC publishes monthly fund industry stats including new fund launches. "
                    "PDF/Excel format; requires structured extraction. "
                    "Release lag typically mid-month for prior month data."
                ),
            ),
            FallbackSource(
                name="Wind/Choice 基金发行统计",
                access_method="Commercial terminal API",
                feasibility=Feasibility.PAID_PROVIDER_NEEDED,
                tier=SourceTier.PAID_VENDOR,
                caveats="Most reliable for historical fund issuance time series. Requires terminal license.",
            ),
            FallbackSource(
                name="东方财富基金发行频道",
                access_method="Web scrape from eastmoney fund IPO calendar",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.WEB_SCRAPE,
                caveats="Scraper maintenance burden. Coverage completeness uncertain.",
            ),
        ],
        rationale=(
            "Fund issuance data (especially historical) has a structural gap in Tushare: "
            "fund_basic is a snapshot of currently-listed funds, not a time-series of "
            "what was issued when. To construct a monthly issuance volume indicator, "
            "we would need either: (a) historical fund_basic snapshots (not available), "
            "or (b) alternative sources like AMAC or Wind. "
            "Classified permission-needed because base Tushare may not suffice; "
            "scraping of AMAC official data is the most viable fallback."
        ),
        data_start_estimate="2008 (AMAC data); Tushare snapshot is current only",
        human_action_required=[
            "Check Tushare fund_nav point requirements for extended history",
            "Evaluate AMAC scraping feasibility: PDF parsing + historical archive",
            "Consider Wind/Choice if existing terminal access exists",
        ],
    ))

    # ---- Condition 13: Liquidity tightening ----
    matrix.append(SourceEntry(
        condition_id=13,
        condition_name="Liquidity tightening (Shibor/LPR/DR007/MLF)",
        source_name="Tushare shibor (Shibor利率) + shibor_lpr (LPR报价)",
        access_method="Tushare pro.query('shibor') + pro.query('shibor_lpr')",
        feasibility=Feasibility.AUTOMATABLE_NOW,
        tier=SourceTier.OFFICIAL_API,
        frequency=Frequency.DAILY,
        data_lag=DataLag.T0,
        key_fields=[
            FieldSpec("date", "date", "日期", unit=None),
            FieldSpec("on", "float64", "%", "隔夜利率"),
            FieldSpec("1w", "float64", "%", "1周利率"),
            FieldSpec("2w", "float64", "%", "2周利率"),
            FieldSpec("1m", "float64", "%", "1月利率"),
            FieldSpec("3m", "float64", "%", "3月利率"),
            FieldSpec("6m", "float64", "%", "6月利率"),
            FieldSpec("9m", "float64", "%", "9月利率"),
            FieldSpec("1y", "float64", "%", "1年利率"),
        ],
        access_hint=AccessHint(
            tushare_endpoint="shibor",
            tushare_method="query",
            duckdb_table=None,
            doc_url="https://tushare.pro/document/2?doc_id=305",
            env_requirements=["TUSHARE_TOKEN"],
            special_notes=[
                "shibor is freely available at basic Tushare tier",
                "Separate endpoint for shibor_lpr (loan prime rate, monthly)",
                "Shibor quote is T+0 (same day release before 11:30 AM)",
            ],
        ),
        blockers=Blockers(),
        fallbacks=[
            FallbackSource(
                name="中国货币网 (Chinamoney.com.cn)",
                access_method="Web scrape from chinamoney.com.cn Shibor/LPR pages",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.OFFICIAL_PUBLICATION,
                caveats=(
                    "Shibor is officially published on chinamoney.com.cn by CFETS. "
                    "HTML table extraction is straightforward but requires scraper maintenance."
                ),
            ),
            FallbackSource(
                name="PBOC 官网 (pbc.gov.cn)",
                access_method="Scrape from PBOC '货币政策' → '公开市场操作'",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.OFFICIAL_PUBLICATION,
                caveats=(
                    "MLF/OMO/DR007 published by PBOC. Data in news-release format; "
                    "structured extraction is challenging. PBOC site has anti-scraping measures."
                ),
            ),
        ],
        rationale=(
            "shibor endpoint is free-tier on Tushare and provides daily interbank rates "
            "(ON, 1W, 2W, 1M, 3M, 6M, 9M, 1Y) from 2006 onward. This covers the "
            "core liquidity-tightening signal well. LPR is monthly via shibor_lpr. "
            "For MLF and DR007, Tushare does NOT provide direct endpoints — these would "
            "require scraping from PBOC/chinamoney or paid terminal data. "
            "The Shibor spread (3M-ON) and absolute levels provide sufficient "
            "liquidity-tightening proxies for the initial condition."
        ),
        data_start_estimate="2006-10 (Shibor launch)",
        human_action_required=[
            "Initiate sync of shibor to local DuckDB",
            "For MLF/DR007: decide if Shibor proxy is sufficient before investing in scraping",
        ],
    ))

    # ---- Condition 14: Macro credit impulse weakening ----
    matrix.append(SourceEntry(
        condition_id=14,
        condition_name="Macro credit impulse weakening (M2/social financing)",
        source_name="Tushare cn_m (货币供应量) + cn_social_financing (社融)",
        access_method="Tushare pro.query('cn_m') + pro.query('cn_sf') or pro.sf()",
        feasibility=Feasibility.PERMISSION_NEEDED,
        tier=SourceTier.OFFICIAL_API,
        frequency=Frequency.MONTHLY,
        data_lag=DataLag.T_1M,
        key_fields=[
            FieldSpec("month", "date", "月度", unit=None),
            FieldSpec("m0", "float64", "亿元", "M0 (流通中现金)"),
            FieldSpec("m1", "float64", "亿元", "M1 (狭义货币)"),
            FieldSpec("m2", "float64", "亿元", "M2 (广义货币)"),
            FieldSpec("m0_yoy", "float64", "%", "M0同比增速"),
            FieldSpec("m1_yoy", "float64", "%", "M1同比增速"),
            FieldSpec("m2_yoy", "float64", "%", "M2同比增速"),
        ],
        access_hint=AccessHint(
            tushare_endpoint="cn_m",
            tushare_method="query",
            duckdb_table=None,
            doc_url="https://tushare.pro/document/2?doc_id=159",
            env_requirements=["TUSHARE_TOKEN", "Possibly 120+ points"],
            special_notes=[
                "cn_m returns M0/M1/M2 and YoY growth rates at monthly frequency",
                "cn_social_financing endpoint may be named 'sf' — check Tushare docs",
                "Both require medium-tier Tushare permissions (likely 120+ points)",
            ],
        ),
        blockers=Blockers(
            permission_required=True,
            permission_detail=(
                "cn_m and cn_social_financing likely require ≥120 Tushare points. "
                "If current account is below threshold, data returns empty. "
                "Need human to verify point balance."
            ),
            release_lag_concern=True,
            release_lag_detail=(
                "M2 and social financing data are released ~10-15 days after month-end "
                "by PBOC. This means the signal will ALWAYS lag by several weeks — "
                "not a same-week indicator. The forward-drawdown validation must "
                "account for this lag by using effective_date, not period_date."
            ),
        ),
        fallbacks=[
            FallbackSource(
                name="PBOC 官网金融统计数据",
                access_method="Scrape from pbc.gov.cn '统计数据' → '金融统计数据'",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.OFFICIAL_PUBLICATION,
                caveats=(
                    "PBOC publishes M2/social-financing press releases and CSV/Excel files. "
                    "Table extraction is doable but needs anti-scraping handling. "
                    "Official source is definitive — same data as Wind/Bloomberg."
                ),
            ),
            FallbackSource(
                name="Wind/Choice 宏观数据库",
                access_method="Commercial terminal macro API",
                feasibility=Feasibility.PAID_PROVIDER_NEEDED,
                tier=SourceTier.PAID_VENDOR,
                caveats="Most convenient; requires terminal license.",
            ),
        ],
        rationale=(
            "M2 and social financing are fundamental macro credit indicators. "
            "Tushare provides cn_m and cn_sf endpoints but at elevated point tiers. "
            "The critical data-sourcing challenge is the release lag: data for month M "
            "is released ~M+10-15 days. Any validation must use effective_date "
            "(release date), NOT the month-end period date, to avoid look-ahead bias. "
            "If Tushare permission fails, PBOC official website is the definitive "
            "free source (scraping-needed)."
        ),
        data_start_estimate="2000 (M2 data); 2016 (social financing detailed categories)",
        human_action_required=[
            "Check Tushare point balance for cn_m and sf endpoints",
            "If scraping PBOC: assess anti-scraping difficulty",
        ],
    ))

    # ---- Condition 15: Options implied volatility / fear gauge ----
    matrix.append(SourceEntry(
        condition_id=15,
        condition_name="Options implied volatility / fear gauge",
        source_name="Tushare opt_daily (期权日线行情) — raw prices for IV computation",
        access_method="Tushare pro.query('opt_daily')",
        feasibility=Feasibility.PERMISSION_NEEDED,
        tier=SourceTier.OFFICIAL_API,
        frequency=Frequency.DAILY,
        data_lag=DataLag.T1,
        key_fields=[
            FieldSpec("ts_code", "str", "期权代码, e.g. 510050C2305M03500", unit=None),
            FieldSpec("trade_date", "date", "交易日", unit=None),
            FieldSpec("exchange", "str", "交易所 (SSE/SZSE/CFFEX)", unit=None),
            FieldSpec("close", "float64", "元", "收盘价"),
            FieldSpec("settle", "float64", "元", "结算价"),
            FieldSpec("open_interest", "float64", "张", "持仓量"),
            FieldSpec("volume", "float64", "张", "成交量"),
            FieldSpec("strike_price", "float64", "元", "行权价"),
        ],
        access_hint=AccessHint(
            tushare_endpoint="opt_daily",
            tushare_method="query",
            duckdb_table=None,
            doc_url="https://tushare.pro/document/2?doc_id=156",
            env_requirements=["TUSHARE_TOKEN", "Possibly 2000+ points"],
            special_notes=[
                "opt_daily returns raw option prices only — NO implied volatility or Greeks",
                "IV must be COMPUTED locally (Black-Scholes inversion on raw prices)",
                "This is a substantial compute task: need underlying price, risk-free rate, time-to-expiry",
                "ETF options (50ETF, 300ETF) most liquid; index options via CFFEX also available",
            ],
        ),
        blockers=Blockers(
            permission_required=True,
            permission_detail=(
                "opt_daily is a high-point Tushare endpoint (likely 2000+ points). "
                "If permission fails, alternative sources for raw option data are scarce. "
                "Note: opt_daily provides raw PRICES, not IV. IV computation is a separate "
                "engine task requiring: underlying close, risk-free rate (interpolated from shibor), "
                "days-to-expiry, and Black-Scholes inverse solver."
            ),
            no_structured_api=True,
            no_structured_api_detail=(
                "No Tushare endpoint provides pre-computed IV for Chinese options. "
                "上证50ETF期权 VIX (iVIX) was published by SSE until ~2018 but discontinued. "
                "There is currently NO official Chinese VIX equivalent that is actively published. "
                "Any IV-based signal requires self-computation from raw option prices."
            ),
        ),
        fallbacks=[
            FallbackSource(
                name="CFFEX/SSE原始期权数据",
                access_method="Web scrape from SSE (sse.com.cn) options data section",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.WEB_SCRAPE,
                caveats=(
                    "SSE provides delayed options data. CFFEX has limited web access. "
                    "Scraping for full historical option chains is highly impractical."
                ),
            ),
            FallbackSource(
                name="Wind/Choice期权数据库",
                access_method="Commercial terminal options analytics API",
                feasibility=Feasibility.PAID_PROVIDER_NEEDED,
                tier=SourceTier.PAID_VENDOR,
                caveats=(
                    "Wind provides pre-computed IV surfaces for Chinese options. "
                    "This is the gold standard but requires license. "
                    "Choice (东方财富) lower-cost alternative."
                ),
            ),
            FallbackSource(
                name="VXY (全球波动率) as proxy",
                access_method="Tushare index_global or web fetch VIX/VXY",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.FALLBACK_PROXY,
                caveats=(
                    "Weak proxy: US VIX doesn't directly map to A-share fear. "
                    "Use only as global risk-off indicator, not specific A-share signal. "
                    "Correlation with A-share tops is inconsistent."
                ),
            ),
        ],
        rationale=(
            "Chinese options market launched in 2015 (50ETF options). opt_daily provides "
            "raw daily price/volume/OI data but at a high Tushare point tier. "
            "CRITICALLY: there is NO pre-computed IV field in Tushare. "
            "Any IV-based signal requires: (1) raw opt_daily data, (2) local IV computation, "
            "(3) aggregation into a fear gauge. This is a high-effort, high-permission "
            "condition. The official iVIX (SSE 50ETF VIX) was discontinued. "
            "If permission is unavailable AND IV computation is infeasible, this condition "
            "should be classified as 'defer' to Wave 2+."
        ),
        data_start_estimate="2015-02 (50ETF options launch)",
        human_action_required=[
            "Verify Tushare point tier for opt_daily (likely 2000+ points)",
            "If insufficient: decide defer vs. purchase",
            "If data obtained: invest in IV computation engine (1-3 days of work)",
            "Fallback decision: use foreign VXY proxy or defer",
        ],
    ))

    # ---- Condition 16: Investor account opening heat ----
    matrix.append(SourceEntry(
        condition_id=16,
        condition_name="Investor account opening heat (new A-share accounts, monthly/lagged)",
        source_name="CSDC (中国结算) monthly account statistics 投资者新增账户数",
        access_method="Web scrape from CSDC (chinaclear.cn) monthly statistical reports",
        feasibility=Feasibility.SCRAPING_NEEDED,
        tier=SourceTier.OFFICIAL_PUBLICATION,
        frequency=Frequency.MONTHLY,
        data_lag=DataLag.T_1M,
        key_fields=[
            FieldSpec("month", "date", "统计月份", unit=None),
            FieldSpec("new_investors", "float64", "万户", "新增投资者数量(万)"),
            FieldSpec("total_investors", "float64", "亿户", "期末投资者总数"),
            FieldSpec("natural_investors", "float64", "万户", "新增自然人投资者"),
            FieldSpec("non_natural_investors", "float64", "户", "新增非自然人投资者"),
        ],
        access_hint=AccessHint(
            tushare_endpoint=None,  # Tushare does NOT have this endpoint
            tushare_method="query",
            duckdb_table=None,
            doc_url="http://www.chinaclear.cn/zdjs/tjyb/center_tjbg.shtml",
            env_requirements=[],
            special_notes=[
                "Tushare has NO endpoint for investor account data",
                "CSDC publishes monthly report ~10th-15th of following month",
                "Data available as PDF/Excel from chinaclear.cn under '统计月报'",
                "CSRC also publishes quarterly market statistics (fewer details)",
                "NO verified structured API exists for this data — scraping is the only free path",
            ],
        ),
        blockers=Blockers(
            no_structured_api=True,
            no_structured_api_detail=(
                "NO structured API exists for investor account data in this project. "
                "Tushare does not provide this endpoint. "
                "CSDC official website (chinaclear.cn) publishes monthly PDF/Excel reports. "
                "These must be scraped/extracted — no REST API, no WebSocket, no CSV download. "
                "Extraction requires: PDF parsing (tabula/camelot) or Excel scraping with "
                "authentication/captcha handling."
            ),
            release_lag_concern=True,
            release_lag_detail=(
                "Monthly data released ~10-15 days after month-end. "
                "Signal inherently lags by 2-6 weeks. Validation MUST use effective_date "
                "(CSDC publication date) NOT the month-end period_date."
            ),
        ),
        fallbacks=[
            FallbackSource(
                name="Wind/Choice 投资者数据",
                access_method="Commercial terminal API",
                feasibility=Feasibility.PAID_PROVIDER_NEEDED,
                tier=SourceTier.PAID_VENDOR,
                caveats=(
                    "Wind provides CSDC investor account data in structured format. "
                    "Most reliable but requires terminal license (~¥20k+/yr)."
                ),
            ),
            FallbackSource(
                name="券商月度数据汇总 (indirect proxy)",
                access_method="Web scrape aggregate broker account-opening data",
                feasibility=Feasibility.SCRAPING_NEEDED,
                tier=SourceTier.FALLBACK_PROXY,
                caveats=(
                    "Proxy, not CSDC official data. Reliability varies by broker aggregation. "
                    "Suitable only for rough directional check, not quantitative validation."
                ),
            ),
            FallbackSource(
                name="中证协/中基协季度数据",
                access_method="Web scrape SAC (sac.net.cn) or AMAC quarterly reports",
                feasibility=Feasibility.DEFER,
                tier=SourceTier.FALLBACK_PROXY,
                caveats=(
                    "Lower frequency (quarterly). SAC and AMAC provide market participation "
                    "data but at coarser granularity than CSDC. Defer to future extraction phase."
                ),
            ),
        ],
        rationale=(
            "This is the MOST DIFFICULT P1 signal to source automatically. "
            "There is NO Tushare endpoint for investor account data. "
            "CSDC is the only official source; it publishes monthly PDF/Excel files "
            "with no structured API. The data procurement pipeline requires: "
            "(1) developing a chinaclear.cn scraper (PDF/Excel extraction), "
            "(2) maintaining it against site changes, "
            "(3) handling the ~2-week release lag for effective-date compliance. "
            "Classified scraping-needed (NOT automatable-now per guardrail: "
            "'Do NOT approve investor-account source as automatable-now unless a "
            "verified API exists'). Wind/Choice is the most reliable paid fallback."
        ),
        data_start_estimate="2015-04 (CSDC started detailed monthly reporting)",
        human_action_required=[
            "CSDC scraping: develop PDF/Excel parser + monthly cron",
            "Consider Wind/Choice API if team has existing license",
            "Validate effective_date vs period_date for look-ahead bias",
            "If scraping proves infeasible in Wave 2, reclassify as defer/paid-provider-needed",
        ],
    ))

    return matrix


# ---------------------------------------------------------------------------
# Matrix metadata
# ---------------------------------------------------------------------------

@dataclass
class MatrixSummary:
    """Aggregate stats for the source matrix."""
    total_conditions: int
    automatable_count: int
    permission_needed_count: int
    scraping_needed_count: int
    paid_provider_needed_count: int
    defer_count: int
    blocked_count: int              # non-automatable conditions (permission+scraping+paid+defer)
    needs_human_count: int          # conditions requiring at least one human action
    conditions_with_fallback: int   # conditions with >=1 fallback documented


def compute_summary(matrix: list[SourceEntry]) -> MatrixSummary:
    """Compute aggregate classification counts from the source matrix."""
    feasibility_counts = {f: 0 for f in Feasibility}
    total = len(matrix)
    blocked = 0
    needs_human = 0
    with_fallback = 0

    for entry in matrix:
        feasibility_counts[entry.feasibility] += 1
        if entry.feasibility != Feasibility.AUTOMATABLE_NOW:
            blocked += 1
        if entry.human_action_required:
            needs_human += 1
        if entry.fallbacks:
            with_fallback += 1

    return MatrixSummary(
        total_conditions=total,
        automatable_count=feasibility_counts[Feasibility.AUTOMATABLE_NOW],
        permission_needed_count=feasibility_counts[Feasibility.PERMISSION_NEEDED],
        scraping_needed_count=feasibility_counts[Feasibility.SCRAPING_NEEDED],
        paid_provider_needed_count=feasibility_counts[Feasibility.PAID_PROVIDER_NEEDED],
        defer_count=feasibility_counts[Feasibility.DEFER],
        blocked_count=blocked,
        needs_human_count=needs_human,
        conditions_with_fallback=with_fallback,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    matrix = build_source_matrix()
    summary = compute_summary(matrix)

    # JSON output
    output = {
        "summary": {
            "total_conditions": summary.total_conditions,
            "automatable_now": summary.automatable_count,
            "permission_needed": summary.permission_needed_count,
            "scraping_needed": summary.scraping_needed_count,
            "paid_provider_needed": summary.paid_provider_needed_count,
            "defer": summary.defer_count,
            "blocked_total": summary.blocked_count,
            "needs_human_action": summary.needs_human_count,
            "conditions_with_fallback": summary.conditions_with_fallback,
        },
        "entries": [
            {
                "condition_id": e.condition_id,
                "condition_name": e.condition_name,
                "source_name": e.source_name,
                "access_method": e.access_method,
                "feasibility": e.feasibility.value,
                "tier": e.tier.value,
                "frequency": e.frequency.value,
                "data_lag": e.data_lag.value,
                "key_fields": [
                    {"name": f.name, "dtype": f.dtype, "unit": f.unit, "description": f.description}
                    for f in e.key_fields
                ],
                "access_hint": {
                    "tushare_endpoint": e.access_hint.tushare_endpoint,
                    "tushare_method": e.access_hint.tushare_method,
                    "duckdb_table": e.access_hint.duckdb_table,
                    "doc_url": e.access_hint.doc_url,
                    "env_requirements": e.access_hint.env_requirements,
                    "special_notes": e.access_hint.special_notes,
                },
                "blockers": {
                    "permission_required": e.blockers.permission_required,
                    "permission_detail": e.blockers.permission_detail,
                    "paid_license_required": e.blockers.paid_license_required,
                    "paid_license_detail": e.blockers.paid_license_detail,
                    "no_structured_api": e.blockers.no_structured_api,
                    "no_structured_api_detail": e.blockers.no_structured_api_detail,
                    "historical_gap": e.blockers.historical_gap,
                    "historical_gap_detail": e.blockers.historical_gap_detail,
                    "release_lag_concern": e.blockers.release_lag_concern,
                    "release_lag_detail": e.blockers.release_lag_detail,
                },
                "fallbacks": [
                    {
                        "name": fb.name,
                        "access_method": fb.access_method,
                        "feasibility": fb.feasibility.value,
                        "tier": fb.tier.value,
                        "caveats": fb.caveats,
                    }
                    for fb in e.fallbacks
                ],
                "rationale": e.rationale,
                "data_start_estimate": e.data_start_estimate,
                "human_action_required": e.human_action_required,
            }
            for e in matrix
        ],
    }

    # Determine output path
    output_dir = "tmp/microstructure"
    json_path = f"{output_dir}/external_source_matrix.json"
    md_path = f"{output_dir}/external_source_matrix.md"

    import os
    os.makedirs(output_dir, exist_ok=True)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"JSON matrix written to {json_path}")

    # Generate Markdown table
    md_lines = []
    md_lines.append("# External Source Availability Matrix — P1 Escape-Top Conditions\n")
    md_lines.append("## Summary\n")
    md_lines.append(f"| Metric | Count |")
    md_lines.append(f"|---|---|")
    md_lines.append(f"| Total P1 conditions | {summary.total_conditions} |")
    md_lines.append(f"| automatable-now | {summary.automatable_count} |")
    md_lines.append(f"| permission-needed | {summary.permission_needed_count} |")
    md_lines.append(f"| scraping-needed | {summary.scraping_needed_count} |")
    md_lines.append(f"| paid-provider-needed | {summary.paid_provider_needed_count} |")
    md_lines.append(f"| defer | {summary.defer_count} |")
    md_lines.append(f"| Blocked (non-automatable) | {summary.blocked_count} |")
    md_lines.append(f"| Needs human action | {summary.needs_human_count} |")
    md_lines.append(f"| Has fallback documented | {summary.conditions_with_fallback} |")
    md_lines.append("")

    md_lines.append("## Source Hierarchy Convention\n")
    md_lines.append("Preferred order: **Tushare → official source → reliable provider → web scrape**.\n")
    md_lines.append("Web articles, forums, and blogs are **NOT** accredited sources.\n")

    md_lines.append("## Per-Condition Matrix\n")

    for e in matrix:
        md_lines.append(f"### Condition {e.condition_id}: {e.condition_name}\n")
        md_lines.append(f"| Attribute | Value |")
        md_lines.append(f"|---|---|")
        md_lines.append(f"| **Feasibility** | `{e.feasibility.value}` |")
        md_lines.append(f"| **Source Tier** | `{e.tier.value}` |")
        md_lines.append(f"| **Primary Source** | {e.source_name} |")
        md_lines.append(f"| **Access Method** | {e.access_method} |")
        md_lines.append(f"| **Frequency** | {e.frequency.value} |")
        md_lines.append(f"| **Data Lag** | {e.data_lag.value} |")
        md_lines.append(f"| **Tushare Endpoint** | `{e.access_hint.tushare_endpoint or 'N/A'}` |")
        md_lines.append(f"| **Local DuckDB** | `{e.access_hint.duckdb_table or 'Not synced'}` |")
        md_lines.append(f"| **Est. Data Start** | {e.data_start_estimate} |")
        md_lines.append("")

        md_lines.append("**Key Fields:**\n")
        md_lines.append("| Field | Type | Unit | Description |")
        md_lines.append("|---|---|---|---|")
        for f in e.key_fields:
            md_lines.append(f"| `{f.name}` | `{f.dtype}` | {f.unit or '—'} | {f.description} |")
        md_lines.append("")

        md_lines.append("**Blockers:**\n")
        blockers_present = []
        if e.blockers.permission_required:
            blockers_present.append(f"- **Permission**: {e.blockers.permission_detail}")
        if e.blockers.paid_license_required:
            blockers_present.append(f"- **Paid License**: {e.blockers.paid_license_detail}")
        if e.blockers.no_structured_api:
            blockers_present.append(f"- **No Structured API**: {e.blockers.no_structured_api_detail}")
        if e.blockers.historical_gap:
            blockers_present.append(f"- **Historical Gap**: {e.blockers.historical_gap_detail}")
        if e.blockers.release_lag_concern:
            blockers_present.append(f"- **Release Lag**: {e.blockers.release_lag_detail}")
        if blockers_present:
            for b in blockers_present:
                md_lines.append(b)
        else:
            md_lines.append("_No blockers identified._")
        md_lines.append("")

        md_lines.append("**Rationale:**\n")
        md_lines.append(f"{e.rationale}\n")

        if e.fallbacks:
            md_lines.append("**Fallback Sources (ordered by preference):**\n")
            md_lines.append("| # | Source | Access | Feasibility | Caveats |")
            md_lines.append("|---|---|---|---|---|")
            for i, fb in enumerate(e.fallbacks, 1):
                md_lines.append(
                    f"| {i} | {fb.name} | {fb.access_method} | `{fb.feasibility.value}` "
                    f"| {fb.caveats[:120]}{'...' if len(fb.caveats) > 120 else ''} |"
                )
            md_lines.append("")

        if e.human_action_required:
            md_lines.append("**Human Actions Required:**\n")
            for ha in e.human_action_required:
                md_lines.append(f"- [ ] {ha}")
            md_lines.append("")

        if e.access_hint.special_notes:
            md_lines.append("**Special Notes:**\n")
            for sn in e.access_hint.special_notes:
                md_lines.append(f"- {sn}")
            md_lines.append("")

    md_lines.append("## Data Procurement Blockers Summary\n")
    md_lines.append("Below are the explicit blockers that prevent fully-automated data access:\n")
    for e in matrix:
        blocker_items = []
        if e.blockers.permission_required:
            blocker_items.append("Tushare permission (elevated point tier)")
        if e.blockers.no_structured_api:
            blocker_items.append("No structured API — scraping required")
        if e.blockers.paid_license_required:
            blocker_items.append("Paid vendor license required")
        if e.blockers.historical_gap:
            blocker_items.append("Historical data gap")
        if e.blockers.release_lag_concern:
            blocker_items.append("Release lag — effective_date needed")
        blocker_str = "; ".join(blocker_items) if blocker_items else "_None_"
        md_lines.append(f"| Condition {e.condition_id} ({e.condition_name}) | {blocker_str} |")

    md_lines.append(f"\n---\n")
    md_lines.append(f"_Generated from `scripts/microstructure/external_source_matrix.py`._\n")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"Markdown matrix written to {md_path}")
    print(f"\nSummary: {summary.automatable_count} automatable, "
          f"{summary.blocked_count} blocked, "
          f"{summary.needs_human_count} need human action")