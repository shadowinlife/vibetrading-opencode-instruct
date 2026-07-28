#!/usr/bin/env python3
"""CLI for ensemble escape-top hyperparameter tuning.

Usage::

    conda activate legonanobot
    python -m scripts.microstructure.tune_joint_escape_top_cli --mode coarse

This runs a walk-forward grid search over ensemble parameters (VOTE_K_OF_M
K thresholds, WEIGHTED_SCORE weights and score thresholds), evaluates OOS
against forward SSE drawdowns, and writes JSON + Markdown +
CSV to ``--output-dir``.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import write_json
from .metadata import DEFAULT_DUCKDB_PATH, DEFAULT_OUTPUT_DIR
from .tune_joint_escape_top import (
    _DEFAULT_HORIZONS,
    _DEFAULT_DD_THRESHOLDS,
    _MIN_SIGNALS,
    _TEST_END,
    _TEST_START,
    _TRAIN_END,
    _TRAIN_START,
    tune_joint_ensemble,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.microstructure.tune_joint_escape_top_cli",
        description="Tune ensemble escape-top parameters with walk-forward OOS.",
    )
    parser.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB_PATH,
        metavar="PATH",
        help=f"path to DuckDB file (default: {DEFAULT_DUCKDB_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR / "tuning" / "joint"),
        metavar="DIR",
        help="output directory (default: tmp/microstructure/tuning/joint/)",
    )
    parser.add_argument(
        "--mode",
        default="coarse",
        choices=["coarse", "fine"],
        help="grid resolution: coarse (default) or fine (around best coarse region)",
    )
    parser.add_argument(
        "--train-start",
        default=_TRAIN_START,
        metavar="DATE",
        help=f"train start date (default: {_TRAIN_START})",
    )
    parser.add_argument(
        "--train-end",
        default=_TRAIN_END,
        metavar="DATE",
        help=f"train end date (default: {_TRAIN_END})",
    )
    parser.add_argument(
        "--test-start",
        default=_TEST_START,
        metavar="DATE",
        help=f"test start date (default: {_TEST_START})",
    )
    parser.add_argument(
        "--test-end",
        default=_TEST_END,
        metavar="DATE",
        help=f"test end date (default: {_TEST_END})",
    )
    parser.add_argument(
        "--min-signals",
        type=int,
        default=_MIN_SIGNALS,
        metavar="INT",
        help=f"minimum signals for robustness (default: {_MIN_SIGNALS})",
    )
    return parser


def _fmt(value: object, *, digits: int = 4) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _render_report(
    result: dict[str, Any],
    *,
    mode: str,
    output_dir: Path,
) -> Path:
    best = result.get("best_oos")
    baseline = result.get("baseline", {})
    comparison = result.get("comparison", {})
    sensitivity = result.get("sensitivity")
    oos_results = result.get("oos_results", [])
    dw = result.get("data_window", {})
    sdd = result.get("sse_trading_days", {})

    lines: list[str] = [
        "# Ensemble Escape-Top Tuning Report",
        "",
        f"- **Generated**: {datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode**: `{mode}` grid",
        f"- **Train**: {dw.get('train_start')} – {dw.get('train_end')} "
        f"({sdd.get('train', '?')} trading days)",
        f"- **Test (OOS)**: {dw.get('test_start')} – {dw.get('test_end')} "
        f"({sdd.get('test', '?')} trading days)",
        f"- **Horizons**: {dw.get('horizons')}",
        f"- **Min signals**: {dw.get('min_signals')}",
        "",
        "## Best OOS Ensemble Configuration",
        "",
    ]

    if best:
        lines.extend([
            f"- **Mode**: {best['mode']}",
            f"- **Composite DD**: {_fmt(best['composite_dd'])}",
            f"- **Signals**: {best['n_signals']}",
            f"- **Signal %**: {_fmt(best.get('signal_pct'), digits=2)}%",
            f"- **Precision @ 60d**: {_fmt(best.get('precision_60d'))}",
            "",
            "### Horizon Detail",
            "",
            "| Horizon | mean_fwd_dd (sig) | mean_fwd_dd (no sig) | precision |",
            "|---:|---:|---:|---:|",
        ])
        hm = best.get("horizon_metrics", {})
        for h in dw.get("horizons", []):
            hm_h = hm.get(f"horizon_{h}d", {})
            lines.append(
                f"| {h}d "
                f"| {_fmt(hm_h.get('mean_fwd_dd_signal'))} "
                f"| {_fmt(hm_h.get('mean_fwd_dd_no_signal'))} "
                f"| {_fmt(hm_h.get('precision'))} |"
            )
    else:
        lines.append("*No ensemble config passed OOS min_signals filter.*")

    lines.extend([
        "",
        "## Baseline (strong: conc=0.50 + margin 40d, AND)",
        "",
        f"- Composite DD: {_fmt(baseline.get('composite_dd'))}",
        f"- Signals: {baseline.get('n_signals')}",
        "",
        "## Comparison",
        "",
        f"- **Recommendation**: `{comparison.get('recommendation', '?')}`",
        f"- **Reason**: {comparison.get('reason', '?')}",
        f"- **Δ DD**: {_fmt(comparison.get('delta_pct'), digits=2)}%",
        f"- **Signal ratio**: {_fmt(comparison.get('signal_ratio'), digits=2)}x",
        f"- Ensemble DD: {_fmt(comparison.get('ensemble_composite_dd'))} vs "
        f"Baseline DD: {_fmt(comparison.get('baseline_composite_dd'))}",
        "",
    ])

    if sensitivity:
        lines.extend([
            "## Sensitivity Analysis (±10% threshold perturbation)",
            "",
            f"- Base DD: {_fmt(sensitivity.get('base', {}).get('composite_dd'))} "
            f"({sensitivity.get('base', {}).get('n_signals', '?')} signals)",
            f"- Degradation (up): {_fmt(sensitivity.get('degradation_up'))}",
            f"- Degradation (down): {_fmt(sensitivity.get('degradation_down'))}",
            f"- Max degradation: {_fmt(sensitivity.get('max_degradation'))}",
            "",
        ])
        note = sensitivity.get("note")
        if note:
            lines.append(f"*{note}*")
            lines.append("")

    # Top 5 OOS results.
    if oos_results:
        lines.extend([
            "## Top 5 OOS Configurations",
            "",
            "| Rank | Mode | Composite DD | Signals | Precision@60d |",
            "|---:|---:|---:|---:|---:|",
        ])
        for i, r in enumerate(oos_results[:5], 1):
            lines.append(
                f"| {i} "
                f"| {r.get('mode', '?')[:40]} "
                f"| {_fmt(r.get('composite_dd'))} "
                f"| {r.get('n_signals')} "
                f"| {_fmt(r.get('precision_60d'))} |"
            )
        lines.append("")

    lines.extend([
        "## Interpretation",
        "",
        "- `composite_dd` = mean of forward 20d/60d/120d drawdowns on signal days.",
        "  More negative = signal fires before deeper drawdowns.",
        "- OOS = out-of-sample: parameters chosen on train evaluated on held-out test.",
        "- If recommendation is `fallback_to_baseline`, the ensemble does not",
        "  meaningfully improve over the existing strong preset.",
        "",
    ])

    report_path = output_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _write_csv(
    oos_results: list[dict[str, Any]],
    output_dir: Path,
) -> Path:
    csv_path = output_dir / "oos_grid.csv"
    if not oos_results:
        csv_path.write_text("mode,params,n_signals,composite_dd,precision_60d\n", encoding="utf-8")
        return csv_path

    fieldnames = ["mode", "params", "n_signals", "composite_dd", "signal_pct", "precision_60d"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in oos_results:
            writer.writerow({
                "mode": r.get("mode", ""),
                "params": str(r.get("params", {})),
                "n_signals": r.get("n_signals"),
                "composite_dd": r.get("composite_dd"),
                "signal_pct": r.get("signal_pct"),
                "precision_60d": r.get("precision_60d"),
            })
    return csv_path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    coarse_grid = args.mode == "coarse"

    print(f"[tune_joint] mode={args.mode}", file=sys.stderr)
    print(f"[tune_joint] train: {args.train_start} – {args.train_end}", file=sys.stderr)
    print(f"[tune_joint] test:  {args.test_start} – {args.test_end}", file=sys.stderr)
    print(f"[tune_joint] min_signals: {args.min_signals}", file=sys.stderr)
    print(f"[tune_joint] output dir: {output_dir}", file=sys.stderr)
    print(file=sys.stderr)

    try:
        result = tune_joint_ensemble(
            duckdb_path=args.duckdb_path,
            min_signals=args.min_signals,
            train_start=args.train_start,
            train_end=args.train_end,
            test_start=args.test_start,
            test_end=args.test_end,
            coarse_grid=coarse_grid,
            verbose=True,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[tune_joint] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[tune_joint] ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 2

    # ── Write JSON ──────────────────────────────────────────────────────
    json_path = output_dir / "tune_results.json"
    write_json(result, json_path)
    print(f"[tune_joint] wrote {json_path}", file=sys.stderr)

    # ── Write CSV ──────────────────────────────────────────────────────
    csv_path = _write_csv(result.get("oos_results", []), output_dir)
    print(f"[tune_joint] wrote {csv_path}", file=sys.stderr)

    # ── Write Markdown report ──────────────────────────────────────────
    report_path = _render_report(result, mode=args.mode, output_dir=output_dir)
    print(f"[tune_joint] wrote {report_path}", file=sys.stderr)

    # ── Summary to stderr ───────────────────────────────────────────────
    best = result.get("best_oos")
    baseline = result.get("baseline", {})
    comparison = result.get("comparison", {})

    print(file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print("ENSEMBLE TUNING RESULTS", file=sys.stderr)
    if best:
        print(f"  Best OOS: {best['mode']}", file=sys.stderr)
        print(f"    composite_dd = {_fmt(best['composite_dd'])}", file=sys.stderr)
        print(f"    n_signals    = {best['n_signals']}", file=sys.stderr)
        print(f"    precision@60 = {_fmt(best.get('precision_60d'))}", file=sys.stderr)
    print(f"  Baseline: composite_dd = {_fmt(baseline.get('composite_dd'))}, "
          f"n_signals = {baseline.get('n_signals')}", file=sys.stderr)
    print(f"  Recommendation: {comparison.get('recommendation', '?')}", file=sys.stderr)
    print(f"    {comparison.get('reason', '?')}", file=sys.stderr)
    print(file=sys.stderr)

    top5 = result.get("oos_results", [])[:5]
    if top5:
        print("TOP 5 OOS CONFIGS", file=sys.stderr)
        print(f"  {'rank':<5} {'mode':<25} {'comp_dd':>10} {'n_sig':>7} {'prec@60':>10}", file=sys.stderr)
        for i, r in enumerate(top5, 1):
            mode_short = r.get("mode", "")[:25]
            print(
                f"  {i:<5} {mode_short:<25} "
                f"{_fmt(r.get('composite_dd')):>10} "
                f"{r.get('n_signals', 0):>7} "
                f"{_fmt(r.get('precision_60d')):>10}",
                file=sys.stderr,
            )
    print("=" * 70, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())