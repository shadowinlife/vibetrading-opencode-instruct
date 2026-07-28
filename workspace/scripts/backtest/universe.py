"""Universe resolver for A-share portfolio backtesting.

Resolves a practical v1 stock pool (10-30 stocks) from local DuckDB data tables
via three resolution modes and applies filtering with explicit diagnostics.

Resolution modes (encoded in ``universe_name``):
  - **index membership**: ``index:<code>`` — e.g. ``index:000300.SH`` for CSI 300.
    Resolves constituents from ``idx_weight``, date-parameterized over the
    request window.  Friendly aliases ``csi300``, ``sz50``, ``csi500``,
    ``csi1000`` are also accepted.
  - **Shenwan industry**: ``sw:<l2_name>`` — e.g. ``sw:汽车零部件``.  Resolves
    current members from ``idx_sw_member_all WHERE out_date IS NULL``.
  - **explicit list**: ``explicit:<codes>`` or bare comma-separated list —
    e.g. ``explicit:000001.SZ,600519.SH`` or ``000001.SZ,600519.SH``.

V1 filters applied in order:
  1. **ST exclusion** — any ``ts_code`` that appears in ``stk_st_daily``
     during the request window is rejected.
  2. **Minimum listing age** — ``stk_info.list_date`` must precede
     ``start_date`` by at least ``min_listing_days`` calendar days.
  3. **Coverage check** — each candidate must have at least one row in
     ``stk_alpha158`` and at least one row with non-null ``close_hfq`` in
     ``stk_factor_pro`` within the window.

Every rejection is recorded in the ``UniverseResult.diagnostics`` dict with
a per-code reason string.  No stock is ever silently dropped.

Usage::

    from scripts.backtest.universe import UniverseConfig, resolve_universe

    cfg = UniverseConfig(
        universe_name="index:000300.SH",
        start_date="2024-01-01",
        end_date="2025-12-31",
    )
    result = resolve_universe(cfg)
    print(f"Validated: {len(result.codes)} stocks")
    for code, reason in result.rejected.items():
        print(f"  REJECTED {code}: {reason}")

This module is additive — it does not modify any existing backtest modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Source type constants for parse_universe_name()
_SOURCE_INDEX = "index"
_SOURCE_SW = "sw"
_SOURCE_EXPLICIT = "explicit"

# Friendly aliases for common index codes
_INDEX_CODE_ALIASES: dict[str, str] = {
    "csi300": "000300.SH",
    "sz50": "000016.SH",
    "csi500": "000905.SH",
    "csi1000": "000852.SH",
}

# Default DuckDB path relative to project root
_DEFAULT_DB_PATH = "./duckdb/ashare.duckdb"

# Default minimum listing days (~1 calendar year)
_DEFAULT_MIN_LISTING_DAYS = 252

# Default maximum positions (v1 guard: 10-30)
_DEFAULT_MAX_POSITIONS = 30


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class UniverseConfig:
    """Configuration for universe resolution and filtering.

    Encodes the source spec via ``universe_name``, the backtest date window,
    and filter thresholds.  Designed to align with
    ``PortfolioConfig.universe_name`` so downstream runners can pass the
    same string value directly.

    Attributes:
        universe_name: Source spec string.  Formats:
            ``index:<code>``, ``sw:<l2_name>``, ``explicit:<codes>``,
            or bare comma-separated list.
        start_date: Backtest window start, inclusive (YYYY-MM-DD).
        end_date: Backtest window end, inclusive (YYYY-MM-DD).
        min_listing_days: Minimum calendar days between ``list_date`` and
            ``start_date``.  Default 252 (~1 year).
        max_positions: Warn-only ceiling.  Resolution does NOT truncate;
            truncation is the selection module's responsibility.
        db_path: Path to the DuckDB file.
    """

    universe_name: str
    start_date: str
    end_date: str
    min_listing_days: int = _DEFAULT_MIN_LISTING_DAYS
    max_positions: int = _DEFAULT_MAX_POSITIONS
    db_path: str = _DEFAULT_DB_PATH


@dataclass
class UniverseResult:
    """Result of universe resolution with full diagnostics.

    Attributes:
        codes: Validated ``ts_code`` list that passed all filters.
        rejected: Map of ``ts_code → reason`` for every rejected stock.
            Reasons are human-readable strings naming the failed filter.
        warnings: Non-fatal warnings (e.g. pool exceeds ``max_positions``).
        diagnostics: Detailed per-code information including:
            ``source``: resolution source type,
            ``st_status``: whether ST was detected in window,
            ``listing_days``: computed listing age at ``start_date``,
            ``alpha158_rows``: row count in ``stk_alpha158`` within window,
            ``hfq_rows``: row count with non-null ``close_hfq`` in
            ``stk_factor_pro`` within window.
    """

    codes: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Universe name parsing
# ---------------------------------------------------------------------------


def parse_universe_name(universe_name: str) -> tuple[str, str]:
    """Parse a ``universe_name`` string into ``(source_type, source_spec)``.

    ``source_type`` is one of ``"index"``, ``"sw"``, or ``"explicit"``.
    ``source_spec`` is the index code, industry name, or comma-separated
    code list respectively.

    Args:
        universe_name: The encoded universe spec string.

    Returns:
        A ``(source_type, source_spec)`` tuple.
    """
    name = universe_name.strip()
    # Check prefix-based modes
    if name.startswith("index:"):
        spec = name[len("index:"):]
        # Resolve friendly aliases like "csi300" → "000300.SH"
        spec = _INDEX_CODE_ALIASES.get(spec.lower(), spec)
        return (_SOURCE_INDEX, spec)
    if name.startswith("sw:"):
        return (_SOURCE_SW, name[len("sw:"):])
    if name.startswith("explicit:"):
        return (_SOURCE_EXPLICIT, name[len("explicit:"):])
    # Fallback: treat bare string as comma-separated ts_codes
    return (_SOURCE_EXPLICIT, name)


def _parse_explicit_codes(spec: str) -> list[str]:
    """Parse a comma-separated ts_code string into a deduplicated list."""
    return list(dict.fromkeys(
        c.strip() for c in spec.split(",") if c.strip()
    ))


# ---------------------------------------------------------------------------
# Candidate resolution per source type
# ---------------------------------------------------------------------------


def _resolve_index_members(
    con: duckdb.DuckDBPyConnection,
    index_code: str,
    start_date: str,
    end_date: str,
) -> list[str]:
    """Resolve index constituents from ``idx_weight`` within the date window.

    ``idx_weight.trade_date`` is stored as VARCHAR in YYYYMMDD format, so
    we convert the YYYY-MM-DD input parameters to YYYYMMDD for comparison.

    Args:
        con: Open DuckDB connection (read_only).
        index_code: Target index code, e.g. ``"000300.SH"``.
        start_date: Window start (YYYY-MM-DD).
        end_date: Window end (YYYY-MM-DD).

    Returns:
        Deduplicated, sorted list of constituent ``ts_code`` strings.
    """
    # Convert YYYY-MM-DD to YYYYMMDD for idx_weight string comparison
    start_yyyymmdd = start_date.replace("-", "")
    end_yyyymmdd = end_date.replace("-", "")

    # Fetch distinct constituent codes that appear at any point in the window.
    # We use trade_date as string (YYYYMMDD) comparison — no CAST needed.
    rows = con.execute(
        """
        SELECT DISTINCT con_code
        FROM idx_weight
        WHERE index_code = ?
          AND trade_date >= ?
          AND trade_date <= ?
        ORDER BY con_code
        """,
        [index_code, start_yyyymmdd, end_yyyymmdd],
    ).fetchall()

    return [str(r[0]) for r in rows]


def _resolve_sw_members(
    con: duckdb.DuckDBPyConnection,
    l2_name: str,
) -> list[str]:
    """Resolve current Shenwan L2 industry members.

    Queries ``idx_sw_member_all`` for rows where ``l2_name`` matches and
    ``out_date IS NULL`` (current membership snapshot).

    Args:
        con: Open DuckDB connection (read_only).
        l2_name: Shenwan L2 industry name, e.g. ``"汽车零部件"``.

    Returns:
        Deduplicated, sorted list of constituent ``ts_code`` strings.
    """
    rows = con.execute(
        """
        SELECT DISTINCT ts_code
        FROM idx_sw_member_all
        WHERE l2_name = ?
          AND out_date IS NULL
        ORDER BY ts_code
        """,
        [l2_name],
    ).fetchall()

    return [str(r[0]) for r in rows]


def _resolve_explicit(spec: str) -> list[str]:
    """Parse explicit comma-separated code list."""
    return _parse_explicit_codes(spec)


# ---------------------------------------------------------------------------
# V1 filters
# ---------------------------------------------------------------------------


def _filter_st(
    con: duckdb.DuckDBPyConnection,
    codes: list[str],
    start_date: str,
    end_date: str,
) -> tuple[list[str], dict[str, str]]:
    """Exclude any ``ts_code`` that appears in ``stk_st_daily`` during the window.

    ST-like status (ST, *ST, 退市风险警示, etc.) at any point in the backtest
    window makes the stock ineligible for the universe.

    Args:
        con: Open DuckDB connection (read_only).
        codes: Candidate ``ts_code`` list.
        start_date: Window start (YYYY-MM-DD).
        end_date: Window end (YYYY-MM-DD).

    Returns:
        ``(passed_codes, rejected_map)`` — ``passed_codes`` are those NOT
        found in ``stk_st_daily``; ``rejected_map`` maps each rejected code
        to a reason string.
    """
    if not codes:
        return ([], {})

    # Use IN clause with parameterized query — DuckDB supports list params
    placeholders = ",".join(["?"] * len(codes))
    # Find all codes that have ANY ST record in the window
    rows = con.execute(
        f"""
        SELECT DISTINCT ts_code
        FROM stk_st_daily
        WHERE ts_code IN ({placeholders})
          AND trade_date >= ?
          AND trade_date <= ?
        """,
        [*codes, start_date, end_date],
    ).fetchall()

    st_codes = {str(r[0]) for r in rows}

    passed: list[str] = []
    rejected: dict[str, str] = {}
    for code in codes:
        if code in st_codes:
            # ST status detected during backtest window — exclude
            rejected[code] = "ST status detected in backtest window"
        else:
            passed.append(code)

    return (passed, rejected)


def _filter_listing_age(
    con: duckdb.DuckDBPyConnection,
    codes: list[str],
    start_date: str,
    min_listing_days: int,
) -> tuple[list[str], dict[str, str]]:
    """Exclude stocks whose listing date is too close to ``start_date``.

    Uses ``DATEDIFF('day', list_date, start_date)`` to compute the listing age
    in calendar days.  ``stk_info.list_date`` is DATE type and accepts
    YYYY-MM-DD string comparison.

    Stocks NOT found in ``stk_info`` are also rejected (unknown listing date).

    Args:
        con: Open DuckDB connection (read_only).
        codes: Candidate ``ts_code`` list.
        start_date: Backtest start date (YYYY-MM-DD).
        min_listing_days: Minimum calendar days listed before start_date.

    Returns:
        ``(passed_codes, rejected_map)``.
    """
    if not codes:
        return ([], {})

    placeholders = ",".join(["?"] * len(codes))
    rows = con.execute(
        f"""
        SELECT ts_code,
               DATEDIFF(
                   'day',
                   strptime(list_date, '%Y%m%d'),
                   CAST(? AS DATE)
               ) AS listing_days
        FROM stk_info
        WHERE ts_code IN ({placeholders})
        """,
        [start_date, *codes],
    ).fetchall()

    # Build lookup: ts_code → listing_days
    info_map: dict[str, int | None] = {str(r[0]): r[1] for r in rows}

    passed: list[str] = []
    rejected: dict[str, str] = {}
    for code in codes:
        listing_days = info_map.get(code)
        if listing_days is None:
            # Not found in stk_info — reject as unknown listing info
            rejected[code] = "not found in stk_info (unknown listing date)"
        elif listing_days < min_listing_days:
            # Listed too recently — reject
            rejected[code] = (
                f"listing age {listing_days} days < required {min_listing_days} days"
            )
        else:
            passed.append(code)

    return (passed, rejected)


def _check_coverage(
    con: duckdb.DuckDBPyConnection,
    codes: list[str],
    start_date: str,
    end_date: str,
) -> tuple[list[str], dict[str, str], dict[str, dict[str, Any]]]:
    """Verify each code has rows in ``stk_alpha158`` and non-null ``close_hfq``
    rows in ``stk_factor_pro`` within the date window.

    Coverage is checked per-table:
      - ``alpha158_ok``: at least one row in ``stk_alpha158`` in window.
      - ``hfq_ok``: at least one row with non-null ``close_hfq`` in
        ``stk_factor_pro`` in window.

    Missing coverage produces a rejection with a diagnostic reason;
    additionally, a per-code dict of row counts is returned for inspection.

    Args:
        con: Open DuckDB connection (read_only).
        codes: Candidate ``ts_code`` list (already filtered by ST/listing-age).
        start_date: Window start (YYYY-MM-DD).
        end_date: Window end (YYYY-MM-DD).

    Returns:
        ``(passed_codes, rejected_map, coverage_map)`` where ``coverage_map``
        is ``{ts_code: {alpha158_rows, hfq_rows}}``.
    """
    if not codes:
        return ([], {}, {})

    placeholders = ",".join(["?"] * len(codes))

    # Query alpha158 coverage in one batch
    alpha_rows = con.execute(
        f"""
        SELECT ts_code, COUNT(*) AS n
        FROM stk_alpha158
        WHERE ts_code IN ({placeholders})
          AND trade_date >= ?
          AND trade_date <= ?
        GROUP BY ts_code
        """,
        [*codes, start_date, end_date],
    ).fetchall()
    alpha_map: dict[str, int] = {str(r[0]): int(r[1]) for r in alpha_rows}

    # Query HFQ coverage in one batch (non-null close_hfq only)
    hfq_rows = con.execute(
        f"""
        SELECT ts_code, COUNT(*) AS n
        FROM stk_factor_pro
        WHERE ts_code IN ({placeholders})
          AND trade_date >= ?
          AND trade_date <= ?
          AND close_hfq IS NOT NULL
        GROUP BY ts_code
        """,
        [*codes, start_date, end_date],
    ).fetchall()
    hfq_map: dict[str, int] = {str(r[0]): int(r[1]) for r in hfq_rows}

    passed: list[str] = []
    rejected: dict[str, str] = {}
    coverage_map: dict[str, dict[str, Any]] = {}

    for code in codes:
        a158_rows = alpha_map.get(code, 0)
        hfq_rows_n = hfq_map.get(code, 0)
        coverage_map[code] = {"alpha158_rows": a158_rows, "hfq_rows": hfq_rows_n}

        # Collect failure reasons for this code
        reasons: list[str] = []
        if a158_rows == 0:
            # No Alpha158 factor data in the window — reject
            reasons.append("no stk_alpha158 rows in window")
        if hfq_rows_n == 0:
            # No HFQ trading data in the window — reject
            reasons.append("no stk_factor_pro (HFQ) rows in window")

        if reasons:
            rejected[code] = "; ".join(reasons)
        else:
            passed.append(code)

    return (passed, rejected, coverage_map)


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


def resolve_universe(cfg: UniverseConfig) -> UniverseResult:
    """Resolve and filter a v1 A-share stock universe.

    Full pipeline:
      1. Parse ``cfg.universe_name`` → source type and spec.
      2. Resolve initial candidate list from the source table.
      3. Exclude ST stocks (check ``stk_st_daily`` in window).
      4. Exclude under-aged listings (check ``stk_info.list_date``).
      5. Verify coverage (check ``stk_alpha158`` and ``stk_factor_pro``).
      6. Assemble ``UniverseResult`` with codes, rejections, warnings, and
         per-code diagnostics.

    Args:
        cfg: Universe resolution configuration.

    Returns:
        ``UniverseResult`` with validated codes, rejection reasons, warnings,
        and detailed per-code diagnostics.
    """
    # Step 1: parse universe name
    source_type, source_spec = parse_universe_name(cfg.universe_name)

    # Step 2: connect to DuckDB (read_only for safety)
    con = duckdb.connect(cfg.db_path, read_only=True)
    try:
        # Step 3: resolve initial candidate list
        if source_type == _SOURCE_INDEX:
            candidates = _resolve_index_members(
                con, source_spec, cfg.start_date, cfg.end_date
            )
            source_label = f"index:{source_spec}"
        elif source_type == _SOURCE_SW:
            candidates = _resolve_sw_members(con, source_spec)
            source_label = f"sw:{source_spec}"
        else:
            candidates = _resolve_explicit(source_spec)
            source_label = "explicit"

        # Record initial count for diagnostics
        total_candidates = len(candidates)

        # Step 4: apply ST filter
        passed_st, rejected_st = _filter_st(
            con, candidates, cfg.start_date, cfg.end_date
        )

        # Step 5: apply listing-age filter
        passed_age, rejected_age = _filter_listing_age(
            con, passed_st, cfg.start_date, cfg.min_listing_days
        )

        # Step 6: apply coverage check
        passed_cov, rejected_cov, coverage_map = _check_coverage(
            con, passed_age, cfg.start_date, cfg.end_date
        )

    finally:
        # Always close the connection even if an exception occurs
        con.close()

    # Merge all rejections into a single map
    all_rejected: dict[str, str] = {}
    all_rejected.update(rejected_st)
    all_rejected.update(rejected_age)
    all_rejected.update(rejected_cov)

    # Assemble per-code diagnostics
    diagnostics: dict[str, dict[str, Any]] = {}
    for code in candidates:
        diag: dict[str, Any] = {
            "source": source_label,
            "passed": code not in all_rejected,
            "st_rejected": code in rejected_st,
            "age_rejected": code in rejected_age,
            "coverage_rejected": code in rejected_cov,
            "reject_reason": all_rejected.get(code, ""),
            "alpha158_rows": coverage_map.get(code, {}).get("alpha158_rows", -1),
            "hfq_rows": coverage_map.get(code, {}).get("hfq_rows", -1),
        }
        diagnostics[code] = diag

    # Warnings
    warnings: list[str] = []
    if len(passed_cov) > cfg.max_positions:
        # v1 does NOT truncate — warn that the pool exceeds the configured max
        warnings.append(
            f"resolved pool size ({len(passed_cov)}) exceeds max_positions "
            f"({cfg.max_positions}); truncation is the selection module's responsibility"
        )
    if total_candidates == 0:
        warnings.append("universe resolution produced zero candidates")
    if len(passed_cov) == 0 and total_candidates > 0:
        warnings.append(
            f"all {total_candidates} candidates were rejected by filters"
        )

    return UniverseResult(
        codes=passed_cov,
        rejected=all_rejected,
        warnings=warnings,
        diagnostics=diagnostics,
    )