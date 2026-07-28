#!/usr/bin/env python3
"""Portfolio prerequisite validator and end-to-end portfolio runner.

Two modes:

1. **validate** — Check prerequisites for candidate A-share stocks.
   Verifies ``stk_alpha158`` and HFQ ``stk_factor_pro`` coverage for each
   ``ts_code`` across a date window.  Emits stock-level diagnostics and
   exits non-zero on any failure.

2. **run** — Orchestrate the full v1 portfolio backtest workflow:
   resolve universe → validate → load data → bridge signal → selection →
   portfolio simulation → metrics → emit summary outputs.

Usage::

    # Validate mode
    python3 scripts/backtest/verify_portfolio.py --mode validate \\
        --codes-file tmp/sample_codes.txt \\
        --start-date 2024-01-01 --end-date 2025-12-31

    # Run mode (with explicit universe + signal builder)
    python3 scripts/backtest/verify_portfolio.py --mode run \\
        --universe explicit:000001.SZ,600519.SH \\
        --start-date 2024-01-01 --end-date 2025-12-31 \\
        --max-positions 5 --rebalance-freq 20 \\
        --signal-builder-ref policy.601777.signal_builders.momentum.build_roc_signal \\
        --signal-col ROC_COMPOSITE_Z \\
        --output-dir tmp/portfolio_run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_DUCKDB_PATH = "./duckdb/ashare.duckdb"


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/backtest/verify_portfolio.py",
        description="Validate A-share stock prerequisites for portfolio backtesting.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python3 scripts/backtest/verify_portfolio.py --mode validate \\
      --codes-file tmp/sample_codes.txt --start-date 2024-01-01 --end-date 2025-12-31
  python3 scripts/backtest/verify_portfolio.py --mode validate \\
      --codes tmp/000001.SZ,688693.SH --start-date 2022-01-01 --end-date 2023-12-31 --json""",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["validate", "run"],
        help="operation mode: 'validate' (check prerequisites) or "
             "'run' (full portfolio backtest)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--codes-file",
        default=None,
        metavar="PATH",
        help="file with one ts_code per line (blank lines and #-comments ignored)",
    )
    group.add_argument(
        "--codes",
        default=None,
        metavar="CODES",
        help="comma-separated ts_code list, e.g. 000001.SZ,688693.SH",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="backtest window start (inclusive)",
    )
    parser.add_argument(
        "--end-date",
        required=True,
        metavar="YYYY-MM-DD",
        help="backtest window end (inclusive)",
    )
    parser.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB_PATH,
        metavar="PATH",
        help=f"path to DuckDB (default: {DEFAULT_DUCKDB_PATH})",
    )
    parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="optional path to write JSON success/diagnostic summary",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the summary as JSON to stdout",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress human-readable output (verbose diagnostics to stderr only on failure)",
    )

    # --- Run-mode arguments ---
    parser.add_argument(
        "--universe",
        default=None,
        metavar="SPEC",
        help="universe spec for run mode: 'explicit:<codes>', "
             "'index:<code>' (e.g. index:csi300), or 'sw:<l2_name>'",
    )
    parser.add_argument(
        "--signal-builder-ref",
        default=None,
        metavar="REF",
        help="dotted import path to a signal-builder function, e.g. "
             "'policy.601777.signal_builders.momentum.build_roc_signal'",
    )
    parser.add_argument(
        "--signal-col",
        default="ROC_COMPOSITE_Z",
        metavar="COL",
        help="signal column name for ranking (default: ROC_COMPOSITE_Z)",
    )
    parser.add_argument(
        "--max-positions",
        default=10,
        type=int,
        metavar="N",
        help="maximum positions in portfolio (default: 10)",
    )
    parser.add_argument(
        "--rebalance-freq",
        default=20,
        type=int,
        metavar="DAYS",
        help="rebalance interval in calendar days (default: 20)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="directory for output artifacts (NAV, rebalance log, metrics). "
             "Default: tmp/portfolio_run_[name]",
    )
    parser.add_argument(
        "--name",
        default="v1_portfolio",
        metavar="NAME",
        help="portfolio experiment name (default: v1_portfolio)",
    )
    return parser


# ---------------------------------------------------------------------------
# Code list parsing
# ---------------------------------------------------------------------------

def _parse_codes_file(path: str) -> list[str]:
    """Read a codes file, returning non-empty, non-comment lines."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    codes: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        codes.append(stripped)
    return codes


def _parse_codes_list(raw: str) -> list[str]:
    """Parse a comma-separated codes string."""
    return [c.strip() for c in raw.split(",") if c.strip()]


def _resolve_codes(args: argparse.Namespace) -> list[str]:
    if args.codes_file:
        codes = _parse_codes_file(args.codes_file)
    else:
        codes = _parse_codes_list(args.codes)
    if not codes:
        print(
            "[verify] ERROR: no codes provided (empty file or empty --codes list)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return codes


# ---------------------------------------------------------------------------
# Validation queries
# ---------------------------------------------------------------------------

def _check_alpha158(
    con: duckdb.DuckDBPyConnection,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    """Validate stk_alpha158 coverage for one stock in the requested window."""
    row = con.execute(
        "SELECT COUNT(*) FROM stk_alpha158 WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?",
        [ts_code, start_date, end_date],
    ).fetchone()
    row_count: int = int(row[0]) if row else 0

    # Also get the actual date range for diagnostics
    date_row = con.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM stk_alpha158 WHERE ts_code = ?",
        [ts_code],
    ).fetchone()
    date_min: str = str(date_row[0]) if date_row and date_row[0] else ""
    date_max: str = str(date_row[1]) if date_row and date_row[1] else ""

    return {
        "ts_code": ts_code,
        "alpha158_ok": row_count > 0,
        "alpha158_rows_in_window": row_count,
        "alpha158_date_min": date_min,
        "alpha158_date_max": date_max,
    }


def _check_hfq(
    con: duckdb.DuckDBPyConnection,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    """Validate HFQ trading coverage in stk_factor_pro."""
    row = con.execute(
        "SELECT COUNT(*) FROM stk_factor_pro WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ? AND close_hfq IS NOT NULL",
        [ts_code, start_date, end_date],
    ).fetchone()
    row_count: int = int(row[0]) if row else 0

    date_row = con.execute(
        "SELECT MIN(trade_date), MAX(trade_date) FROM stk_factor_pro WHERE ts_code = ? AND close_hfq IS NOT NULL",
        [ts_code],
    ).fetchone()
    date_min: str = str(date_row[0]) if date_row and date_row[0] else ""
    date_max: str = str(date_row[1]) if date_row and date_row[1] else ""

    return {
        "hfq_ok": row_count > 0,
        "hfq_rows_in_window": row_count,
        "hfq_date_min": date_min,
        "hfq_date_max": date_max,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_validate(args: argparse.Namespace) -> int:
    """Execute the validate mode: check every ts_code against both sources."""
    duckdb_path = args.duckdb_path
    start_date = args.start_date
    end_date = args.end_date

    codes = _resolve_codes(args)

    if not Path(duckdb_path).exists():
        print(f"[verify] ERROR: DuckDB file not found: {duckdb_path}", file=sys.stderr)
        return 1

    con = duckdb.connect(duckdb_path, read_only=True)
    try:
        results: list[dict[str, object]] = []
        for ts_code in codes:
            alpha = _check_alpha158(con, ts_code, start_date, end_date)
            hfq = _check_hfq(con, ts_code, start_date, end_date)
            entry: dict[str, object] = {**alpha, **hfq}
            results.append(entry)

    finally:
        con.close()

    failed_codes = [
        r["ts_code"] for r in results
        if not bool(r["alpha158_ok"]) or not bool(r["hfq_ok"])
    ]

    summary: dict[str, object] = {
        "mode": "validate",
        "duckdb_path": str(duckdb_path),
        "start_date": start_date,
        "end_date": end_date,
        "total_codes": len(codes),
        "passed": len(codes) - len(failed_codes),
        "failed": len(failed_codes),
        "failed_codes": failed_codes,
        "results": results,
        "verification_result": "FAILED" if failed_codes else "OK",
    }

    # Write output file if requested
    if args.output:
        Path(args.output).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # JSON mode
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 1 if failed_codes else 0

    # Human-readable mode
    if not args.quiet:
        print(f"DuckDB: {duckdb_path}")
        print(f"Window: {start_date} → {end_date}")
        print(f"Codes:  {len(codes)} total")
        print(f"Passed: {len(codes) - len(failed_codes)}")
        print(f"Failed: {len(failed_codes)}")
        print()

        if failed_codes:
            print("--- FAILURES (stock-level diagnostics) ---")
            for r in results:
                if not bool(r["alpha158_ok"]) or not bool(r["hfq_ok"]):
                    reasons: list[str] = []
                    if not bool(r["alpha158_ok"]):
                        reasons.append(
                            f"missing-alpha158 (rows_in_window={r['alpha158_rows_in_window']}, "
                            f"available_date_range={r['alpha158_date_min']}→{r['alpha158_date_max']})"
                        )
                    if not bool(r["hfq_ok"]):
                        reasons.append(
                            f"missing-hfq (rows_in_window={r['hfq_rows_in_window']}, "
                            f"available_date_range={r['hfq_date_min']}→{r['hfq_date_max']})"
                        )
                    print(f"  {r['ts_code']}: {'; '.join(reasons)}")
            print()
        else:
            print("--- All codes passed ---")
            for r in results:
                print(
                    f"  {r['ts_code']}: "
                    f"alpha158={r['alpha158_rows_in_window']} rows, "
                    f"hfq={r['hfq_rows_in_window']} rows"
                )
            print()

    if failed_codes:
        print("verification_result: FAILED")
        return 1

    print("verification_result: OK")
    return 0


# ---------------------------------------------------------------------------
# Portfolio run mode (full v1 workflow)
# ---------------------------------------------------------------------------


def run_portfolio(args: argparse.Namespace) -> int:
    """Execute the 'run' mode: full v1 portfolio backtest workflow.

    Orchestrates: resolve universe → validate → load data → bridge signal →
    selection → portfolio simulation → metrics → emit outputs.
    """
    from scripts.backtest.portfolio_types import PortfolioConfig, PortfolioResult
    from scripts.backtest.universe import UniverseConfig, resolve_universe, parse_universe_name
    from scripts.backtest.data import load_alpha158_batch, load_prices_batch, check_alignment
    from scripts.backtest.selection import generate_rebalance_dates, select_top_n
    from scripts.backtest.portfolio import simulate_portfolio
    from scripts.backtest.portfolio_metrics import calc_portfolio_metrics, calc_turnover_metrics, rebalance_turnover_series

    duckdb_path = args.duckdb_path
    start_date = args.start_date
    end_date = args.end_date

    if not Path(duckdb_path).exists():
        print(f"[run] ERROR: DuckDB file not found: {duckdb_path}", file=sys.stderr)
        return 1

    # ----- 1. Resolve universe -----
    universe_name = args.universe
    if not universe_name:
        if args.codes:
            universe_name = f"explicit:{args.codes}"
        elif args.codes_file:
            codes = _parse_codes_file(args.codes_file)
            universe_name = "explicit:" + ",".join(codes)
        else:
            print(
                "[run] ERROR: --universe, --codes, or --codes-file is required for run mode",
                file=sys.stderr,
            )
            return 2

    print(f"[run] Resolving universe: {universe_name}")
    source_type, source_spec = parse_universe_name(universe_name)

    universe_cfg = UniverseConfig(
        universe_name=universe_name,
        start_date=start_date,
        end_date=end_date,
        max_positions=args.max_positions,
        db_path=duckdb_path,
    )
    universe_result = resolve_universe(universe_cfg)

    if not args.quiet:
        print(f"  Candidates resolved: {len(universe_result.codes)}")
        if universe_result.warnings:
            for w in universe_result.warnings:
                print(f"  WARNING: {w}")
        if universe_result.rejected:
            print(f"  Rejected: {len(universe_result.rejected)}")
            for code, reason in universe_result.rejected.items():
                print(f"    {code}: {reason}")

    if not universe_result.codes:
        print("[run] ERROR: no valid stocks in universe after filtering", file=sys.stderr)
        return 1

    codes = universe_result.codes

    # ----- 2. Load data -----
    print(f"[run] Loading Alpha158 factor data for {len(codes)} stocks...")
    factor_result = load_alpha158_batch(codes, start_date, end_date, db_path=duckdb_path)
    if not args.quiet:
        print(f"  Factor rows loaded: {factor_result.n_rows_total}")
        if factor_result.missing_codes:
            for code, reason in factor_result.missing_codes.items():
                print(f"  MISSING factor data: {code}: {reason}")

    if factor_result.n_rows_total == 0:
        print("[run] ERROR: no factor data loaded for any stock", file=sys.stderr)
        return 1

    print(f"[run] Loading HFQ price data for {len(codes)} stocks...")
    price_result = load_prices_batch(codes, start_date, end_date, db_path=duckdb_path)
    if not args.quiet:
        print(f"  Price rows loaded: {price_result.n_rows_total}")
        if price_result.missing_codes:
            for code, reason in price_result.missing_codes.items():
                print(f"  MISSING price data: {code}: {reason}")

    if price_result.n_rows_total == 0:
        print("[run] ERROR: no price data loaded for any stock", file=sys.stderr)
        return 1

    # Check alignment
    alignment = check_alignment(factor_result.df, price_result.df)
    if not args.quiet:
        print(f"  Alignment: {alignment.n_common} common keys "
              f"(factor_only={len(alignment.factor_only_keys)}, "
              f"price_only={len(alignment.price_only_keys)})")
        if not alignment.all_aligned:
            print("  WARNING: factor and price data are not perfectly aligned")

    # ----- 3. Bridge strategy signal -----
    factor_df = factor_result.df
    signal_col = args.signal_col

    if args.signal_builder_ref:
        from scripts.backtest.strategy_bridge import execute_signal_builder

        print(f"[run] Computing signals via {args.signal_builder_ref}...")
        signal_df = execute_signal_builder(args.signal_builder_ref, factor_df)
        if signal_col not in signal_df.columns:
            print(
                f"[run] ERROR: signal column '{signal_col}' not found in "
                f"builder output. Available: {list(signal_df.columns)}",
                file=sys.stderr,
            )
            return 1
    else:
        # No signal builder — use factor DataFrame directly; signal column
        # must be a factor column
        print(f"[run] Using factor column '{signal_col}' directly as signal...")
        signal_df = factor_df
        if signal_col not in signal_df.columns:
            print(
                f"[run] ERROR: signal column '{signal_col}' not found in factor data. "
                f"Available factor cols (first 20): {list(factor_df.columns[2:22])}",
                file=sys.stderr,
            )
            return 1

    if not args.quiet:
        print(f"  Signal DataFrame: {len(signal_df)} rows, "
              f"{len(signal_df['ts_code'].unique())} stocks")

    # ----- 4. Build selection map -----
    rebalance_dates = generate_rebalance_dates(start_date, end_date, args.rebalance_freq)
    print(f"[run] Rebalance dates: {len(rebalance_dates)} "
          f"(freq={args.rebalance_freq} days)")

    selection_map: dict[str, list[str]] = {}
    selection_log_entries: list[dict] = []

    for date_str in rebalance_dates:
        selected, excluded = select_top_n(
            signal_df, date_str, signal_col,
            max_positions=args.max_positions,
            higher_better=True,
        )
        selection_map[date_str] = selected
        excluded_reasons_str = "; ".join(
            f"{code}: {reason}" for code, reason in sorted(excluded.items())
        ) if excluded else ""
        selection_log_entries.append({
            "rebalance_date": date_str,
            "selected_codes": ",".join(selected),
            "n_selected": len(selected),
            "n_excluded": len(excluded),
            "excluded_reasons": excluded_reasons_str,
        })

    if not args.quiet:
        total_selections = sum(len(v) for v in selection_map.values())
        print(f"  Total selections across {len(rebalance_dates)} rebalance dates: "
              f"{total_selections}")

    # ----- 5. Portfolio simulation -----
    config = PortfolioConfig(
        name=args.name,
        universe_name=universe_name,
        start_date=start_date,
        end_date=end_date,
        rebalance_freq=args.rebalance_freq,
        max_positions=args.max_positions,
        signal_builder_ref=args.signal_builder_ref,
        signal_col=signal_col,
    )

    price_df = price_result.df
    print("[run] Running portfolio simulation...")
    nav_df, rebalance_log, nav_summary = simulate_portfolio(
        price_df, selection_map, config,
    )

    # ----- 6. Compute metrics -----
    daily_rets = nav_df["daily_ret"].to_numpy()
    turnover_vals = rebalance_turnover_series(rebalance_log)

    metrics = calc_portfolio_metrics(daily_rets, turnover_series=turnover_vals)
    turnover_summary = calc_turnover_metrics(turnover_vals)

    # ----- 7. Emit outputs -----
    output_dir = _resolve_output_dir(args)
    _write_outputs(output_dir, nav_df, rebalance_log, nav_summary, metrics,
                   turnover_summary, selection_log_entries, universe_result,
                   config)

    # Summary to stdout
    print()
    print("=" * 60)
    print(f"PORTFOLIO RESULTS: {config.name}")
    print("=" * 60)
    print(f"  Universe:  {universe_name} ({len(codes)} stocks)")
    print(f"  Period:    {start_date} → {end_date}")
    print(f"  Positions: max {config.max_positions}, rebalance every "
          f"{config.rebalance_freq} days")
    print(f"  Signal:    {signal_col}")
    print(f"  Costs:     {config.one_way_cost:.2%} one-way")
    print()
    print(f"  Total Return:  {metrics['total_return']:+.2%}")
    print(f"  Annual Return: {metrics['annual_return']:+.2%}")
    print(f"  Sharpe Ratio:  {metrics['sharpe_ratio']:.2f}")
    print(f"  Max Drawdown:  {metrics['max_drawdown']:+.2%}")
    print(f"  Calmar Ratio:  {metrics['calmar_ratio']:.2f}")
    print(f"  Volatility:    {metrics['volatility']:.2%}")
    if "annual_turnover" in metrics:
        print(f"  Avg Turnover:  {metrics.get('avg_turnover', 0):.4f} "
              f"(annualised: {metrics.get('annual_turnover', 0):.2f})")
    print()
    print(f"  Start NAV:     {metrics['start_nav']:.4f}")
    print(f"  End NAV:       {metrics['end_nav']:.4f}")
    print(f"  Rebalance Events: {len(rebalance_log)}")
    print(f"  Output dir:    {output_dir}")
    print("=" * 60)

    return 0


def _resolve_output_dir(args: argparse.Namespace) -> Path:
    """Resolve output directory for run-mode artifacts."""
    if args.output_dir:
        out = Path(args.output_dir)
    else:
        out = Path("tmp") / f"portfolio_run_{args.name}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_outputs(
    output_dir: Path,
    nav_df: pd.DataFrame,
    rebalance_log: list,
    nav_summary,
    metrics: dict,
    turnover_summary: dict,
    selection_log_entries: list[dict],
    universe_result,
    config,
) -> None:
    """Write all portfolio run artifacts to output_dir."""
    # NAV CSV
    nav_path = output_dir / "nav.csv"
    nav_df.to_csv(nav_path, index=False)
    print(f"[run] NAV saved to: {nav_path}")

    # Rebalance log CSV
    rebalance_path = output_dir / "rebalance_log.csv"
    if rebalance_log:
        rebalance_df = pd.DataFrame([e.to_dict() for e in rebalance_log])
        # Normalize list columns to comma-separated strings for clean CSV
        if "selected_codes" in rebalance_df.columns:
            rebalance_df["selected_codes"] = rebalance_df["selected_codes"].apply(
                lambda x: ",".join(x) if isinstance(x, list) else str(x)
            )
        if "weights" in rebalance_df.columns:
            rebalance_df["weights"] = rebalance_df["weights"].apply(
                lambda x: ",".join(f"{w:.6f}" for w in x) if isinstance(x, list) else str(x)
            )
        rebalance_df.to_csv(rebalance_path, index=False)
        print(f"[run] Rebalance log saved to: {rebalance_path}")

    # Selection log CSV
    selection_path = output_dir / "selection_log.csv"
    pd.DataFrame(selection_log_entries).to_csv(selection_path, index=False)
    print(f"[run] Selection log saved to: {selection_path}")

    # Summary JSON
    summary = {
        "config": config.to_dict(),
        "nav_summary": nav_summary.to_dict() if hasattr(nav_summary, 'to_dict') else {},
        "metrics": _json_safe_dict(metrics),
        "turnover": _json_safe_dict(turnover_summary),
        "universe": {
            "source": config.universe_name,
            "n_codes": len(universe_result.codes) if hasattr(universe_result, 'codes') else 0,
            "codes": universe_result.codes if hasattr(universe_result, 'codes') else [],
            "n_rejected": len(universe_result.rejected) if hasattr(universe_result, 'rejected') else 0,
        },
        "rebalance_events": len(rebalance_log),
        "selection_events": len(selection_log_entries),
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"[run] Summary saved to: {summary_path}")


def _json_safe_dict(d: dict) -> dict:
    """Convert numpy types to native Python for JSON serialization."""
    import numpy as np
    result = {}
    for k, v in d.items():
        if isinstance(v, (np.integer,)):
            result[k] = int(v)
        elif isinstance(v, (np.floating,)):
            result[k] = float(v)
        elif isinstance(v, np.ndarray):
            result[k] = v.tolist()
        else:
            result[k] = v
    return result

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.mode == "run":
        return run_portfolio(args)

    if args.mode == "validate":
        return run_validate(args)

    # Should be unreachable; argparse validates mode
    return 0


if __name__ == "__main__":
    sys.exit(main())