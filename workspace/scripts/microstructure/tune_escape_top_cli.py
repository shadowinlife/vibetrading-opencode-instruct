#!/usr/bin/env python3
"""CLI for escape-top hyperparameter tuning.

Usage::

    conda activate legonanobot
    python -m scripts.microstructure.tune_escape_top_cli --mode quick

This runs a grid search over concentration thresholds and divergence
lookback windows, ranks them by warning quality against forward SSE
drawdowns, and writes JSON + CSV to ``--output-dir``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .base import write_json
from .metadata import DEFAULT_DUCKDB_PATH, DEFAULT_OUTPUT_DIR
from .tune_escape_top import grid_search

# ── Quick/full grids ─────────────────────────────────────────────────────────

_QUICK_CONC_THRESHOLDS = [0.40, 0.43, 0.45, 0.48, 0.50]
_QUICK_DIV_LOOKBACKS = [10, 15, 20, 30, 40]
_QUICK_HORIZONS = [20, 60, 120]
_QUICK_DD_THRESHOLDS = {20: -0.03, 60: -0.05, 120: -0.08}

_FULL_CONC_THRESHOLDS = [0.38, 0.40, 0.42, 0.43, 0.45, 0.48, 0.50, 0.52, 0.55]
_FULL_DIV_LOOKBACKS = [5, 10, 15, 20, 30, 40, 60]
_FULL_HORIZONS = [20, 60, 120]
_FULL_DD_THRESHOLDS = {20: -0.03, 60: -0.05, 120: -0.08}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.microstructure.tune_escape_top_cli",
        description="Grid-search escape-top parameters against forward SSE drawdowns.",
    )
    parser.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB_PATH,
        metavar="PATH",
        help=f"path to DuckDB file (default: {DEFAULT_DUCKDB_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        metavar="DIR",
        help=f"output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--mode",
        default="quick",
        choices=["quick", "full"],
        help="search mode: quick uses 5×5 grid; full uses 9×7 grid (default: quick)",
    )
    parser.add_argument(
        "--conc-thresholds",
        nargs="+",
        type=float,
        default=None,
        metavar="FLOAT",
        help="override concentration thresholds (space-separated floats)",
    )
    parser.add_argument(
        "--div-lookbacks",
        nargs="+",
        type=int,
        default=None,
        metavar="INT",
        help="override divergence lookback windows (space-separated ints)",
    )
    parser.add_argument(
        "--horizons",
        nargs="+",
        type=int,
        default=None,
        metavar="INT",
        help="override forward drawdown horizons (space-separated ints)",
    )
    parser.add_argument(
        "--min-signals",
        type=int,
        default=0,
        metavar="INT",
        help="minimum number of joint-signal days required for robust ranking (default: 0)",
    )
    return parser


def _fmt(value: object, *, digits: int = 4) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _render_markdown_report(
    result: dict[str, Any],
    *,
    mode: str,
    horizons: list[int],
    output_dir: Path,
) -> Path:
    summary = result["sse_summary"]
    best = result["best_params"]
    top_ranked = result["top_ranked"][:10]

    lines: list[str] = [
        "# Escape-Top Parameter Tuning Report",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Mode: `{mode}`",
        f"- SSE range: `{summary['start_date']}` — `{summary['end_date']}`",
        f"- SSE trading days: `{summary['n_days']}`",
        f"- Horizons: `{', '.join(str(h) for h in horizons)}`",
        f"- Minimum signals filter: `{result['min_signals']}`",
        "",
        "## Best Parameters",
        "",
        "| Parameter | Value |",
        "|---|---:|",
        f"| concentration_threshold | {_fmt(best['concentration_threshold'], digits=2)} |",
        f"| divergence_lookback_days | {_fmt(best['divergence_lookback_days'])} |",
        f"| composite_dd_after_signal | {_fmt(best['composite_dd_after_signal'])} |",
        f"| n_signals | {_fmt(best['n_signals'])} |",
        f"| used_robust_filter | {_fmt(best['used_robust_filter'])} |",
        "",
        "## Top Ranked Parameter Combinations",
        "",
        "| Rank | Conc Threshold | Div Lookback | Signals | Composite DD |",
        "|---:|---:|---:|---:|---:|",
    ]

    for entry in top_ranked:
        lines.append(
            "| "
            f"{entry['rank']} | "
            f"{_fmt(entry['concentration_threshold'], digits=2)} | "
            f"{entry['divergence_lookback_days']} | "
            f"{entry['n_signals']} | "
            f"{_fmt(entry['composite_dd_after_signal'])} |"
        )

    lines.extend([
        "",
        "## Horizon Detail (Best Parameters)",
        "",
        "| Horizon | n_signal | mean_fwd_dd_signal | mean_fwd_dd_no_signal | precision | recall | f1 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ])

    metrics = best.get("metrics", {})
    for horizon in horizons:
        h_metrics = metrics.get(f"horizon_{horizon}d", {})
        lines.append(
            "| "
            f"{horizon} | "
            f"{_fmt(h_metrics.get('n_signal'))} | "
            f"{_fmt(h_metrics.get('mean_fwd_dd_signal'))} | "
            f"{_fmt(h_metrics.get('mean_fwd_dd_no_signal'))} | "
            f"{_fmt(h_metrics.get('precision'))} | "
            f"{_fmt(h_metrics.get('recall'))} | "
            f"{_fmt(h_metrics.get('f1'))} |"
        )

    lines.extend([
        "",
        "## Interpretation",
        "",
        "`composite_dd_after_signal` 越负，说明该参数组合越倾向于在更深的未来回撤前触发。",
        "若 full grid 的最优参数信号次数过少，应优先参考加入 `--min-signals` 后的稳健排序。",
        "",
    ])

    report_path = output_dir / "tune_escape_top_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "full":
        default_conc = _FULL_CONC_THRESHOLDS
        default_div = _FULL_DIV_LOOKBACKS
        default_horizons = _FULL_HORIZONS
        default_dd_thresholds = _FULL_DD_THRESHOLDS
    else:
        default_conc = _QUICK_CONC_THRESHOLDS
        default_div = _QUICK_DIV_LOOKBACKS
        default_horizons = _QUICK_HORIZONS
        default_dd_thresholds = _QUICK_DD_THRESHOLDS

    conc_thresh = args.conc_thresholds or default_conc
    div_lb = args.div_lookbacks or default_div
    horizons = args.horizons or default_horizons

    print(f"[tune_escape_top] mode={args.mode}", file=sys.stderr)
    print(
        f"[tune_escape_top] grid: {len(conc_thresh)} conc × "
        f"{len(div_lb)} div = {len(conc_thresh) * len(div_lb)} combos",
        file=sys.stderr,
    )
    print(f"[tune_escape_top] horizons: {horizons}", file=sys.stderr)
    print(f"[tune_escape_top] min_signals: {args.min_signals}", file=sys.stderr)
    print(f"[tune_escape_top] output dir: {output_dir}", file=sys.stderr)
    print(file=sys.stderr)

    try:
        result = grid_search(
            duckdb_path=args.duckdb_path,
            concentration_thresholds=conc_thresh,
            divergence_lookbacks=div_lb,
            horizons=horizons,
            dd_thresholds=default_dd_thresholds,
            min_signals=args.min_signals,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[tune_escape_top] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[tune_escape_top] ERROR: {exc}", file=sys.stderr)
        return 2

    # ── Write JSON ──────────────────────────────────────────────────────

    json_path = output_dir / "tune_escape_top.json"
    write_json(result, json_path)
    print(f"[tune_escape_top] wrote {json_path}", file=sys.stderr)

    # ── Write CSV (flattened grid results) ──────────────────────────────

    csv_rows: list[dict[str, object]] = []
    for entry in result["grid_results"]:
        row: dict[str, object] = {
            "concentration_threshold": entry["concentration_threshold"],
            "divergence_lookback_days": entry["divergence_lookback_days"],
            "n_signals": entry["n_signals"],
            "composite_dd_after_signal": entry["metrics"].get("_composite_dd"),
        }
        for h in horizons:
            h_key = f"horizon_{h}d"
            h_metrics = entry["metrics"].get(h_key, {})
            prefix = f"h{h}d"
            row[f"{prefix}_n_signal"] = h_metrics.get("n_signal")
            row[f"{prefix}_mean_fwd_dd_signal"] = h_metrics.get("mean_fwd_dd_signal")
            row[f"{prefix}_mean_fwd_dd_no_signal"] = h_metrics.get("mean_fwd_dd_no_signal")
            row[f"{prefix}_precision"] = h_metrics.get("precision")
            row[f"{prefix}_recall"] = h_metrics.get("recall")
            row[f"{prefix}_f1"] = h_metrics.get("f1")
        csv_rows.append(row)

    df_csv = pd.DataFrame(csv_rows).sort_values("composite_dd_after_signal", ascending=True)
    csv_path = output_dir / "tune_escape_top_grid.csv"
    df_csv.to_csv(csv_path, index=False)
    print(f"[tune_escape_top] wrote {csv_path}", file=sys.stderr)

    # ── Write Markdown report ──────────────────────────────────────────

    report_path = _render_markdown_report(
        result,
        mode=args.mode,
        horizons=horizons,
        output_dir=output_dir,
    )
    print(f"[tune_escape_top] wrote {report_path}", file=sys.stderr)

    # ── Summary to stderr ───────────────────────────────────────────────

    best = result["best_params"]
    print(file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print("BEST PARAMETERS", file=sys.stderr)
    print(f"  concentration_threshold = {best['concentration_threshold']}", file=sys.stderr)
    print(f"  divergence_lookback_days = {best['divergence_lookback_days']}", file=sys.stderr)
    print(f"  n_signals = {best['n_signals']}", file=sys.stderr)
    print(f"  composite_dd_after_signal = {best['composite_dd_after_signal']:.4f}",
          file=sys.stderr)
    print(file=sys.stderr)
    print("TOP 5 RANKED", file=sys.stderr)
    print(f"  {'rank':<5} {'conc':<8} {'lb':<5} {'composite_dd':>12} {'n_sig':>7}",
          file=sys.stderr)
    for entry in result["top_ranked"]:
        print(
            f"  {entry['rank']:<5} "
            f"{entry['concentration_threshold']:<8.2f} "
            f"{entry['divergence_lookback_days']:<5} "
            f"{entry['composite_dd_after_signal']:>12.4f} "
            f"{entry['n_signals']:>7}",
            file=sys.stderr,
        )
    print("=" * 60, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
