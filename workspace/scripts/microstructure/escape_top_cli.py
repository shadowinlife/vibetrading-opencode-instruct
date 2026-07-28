#!/usr/bin/env python3
"""CLI entry point for the unified escape-top warning."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .base import write_json
from .escape_top import compute_escape_warning
from .metadata import (
    DEFAULT_DUCKDB_PATH,
    DEFAULT_OUTPUT_DIR,
    ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD,
    ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS,
    ESCAPE_TOP_PRESETS,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.microstructure.escape_top_cli",
        description="Compute joint escape-top warning (concentration + margin-buy/SSE divergence).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python -m scripts.microstructure.escape_top_cli
  python -m scripts.microstructure.escape_top_cli --preset strong
  python -m scripts.microstructure.escape_top_cli --concentration-threshold 0.48 --lookback 10
  python -m scripts.microstructure.escape_top_cli --output ./tmp/escape_top.json
  python -m scripts.microstructure.escape_top_cli --start-date 2024-01-01 --end-date 2025-12-31""",
    )
    parser.add_argument(
        "--duckdb-path",
        default=DEFAULT_DUCKDB_PATH,
        metavar="PATH",
        help=f"path to DuckDB file (default: {DEFAULT_DUCKDB_PATH})",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        default=None,
        metavar="PATH",
        help="write JSON to this file (default: print to stdout only)",
    )
    parser.add_argument(
        "--concentration-threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help=(
            "top-N%% turnover share threshold for concentration hit "
            f"(default: {ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--lookback",
        dest="divergence_lookback_days",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "trading-day lookback for margin-buy / SSE divergence "
            f"(default: {ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS})"
        ),
    )
    parser.add_argument(
        "--preset",
        choices=sorted(ESCAPE_TOP_PRESETS),
        default=None,
        help=(
            "named parameter preset: strong=0.50/40, balanced=0.48/40, "
            "early=0.52/60, extended=0.50/40±5d. "
            "Preset overrides explicit threshold/lookback."
        ),
    )
    parser.add_argument(
        "--joint-mode",
        default="AND",
        choices=["AND"],
        help="joint warning mode (default: AND)",
    )
    parser.add_argument(
        "--top-pct",
        type=float,
        default=5.0,
        metavar="PCT",
        help="top percentage of stocks for concentration (default: 5.0)",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        metavar="DATE",
        help="start of date window (YYYY-MM-DD, inclusive)",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        metavar="DATE",
        help="end of date window (YYYY-MM-DD, inclusive)",
    )
    return parser


def _resolve_output_path(output_path: str | None) -> Path | None:
    if output_path is None:
        return None
    dest = Path(output_path)
    if dest.parent == Path("."):
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        return DEFAULT_OUTPUT_DIR / dest.name
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    concentration_threshold = (
        args.concentration_threshold
        if args.concentration_threshold is not None
        else ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD
    )
    divergence_lookback_days = (
        args.divergence_lookback_days
        if args.divergence_lookback_days is not None
        else ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS
    )
    temporal_window_days = 0

    if args.preset is not None:
        preset = ESCAPE_TOP_PRESETS[args.preset]
        if args.concentration_threshold is not None or args.divergence_lookback_days is not None:
            print(
                "[escape-top] WARNING: --preset overrides explicit "
                "--concentration-threshold/--lookback values",
                file=sys.stderr,
            )
        concentration_threshold = float(preset["concentration_threshold"])
        divergence_lookback_days = int(preset["divergence_lookback_days"])
        temporal_window_days = int(preset.get("temporal_window_days", 0))

    try:
        result = compute_escape_warning(
            duckdb_path=args.duckdb_path,
            concentration_threshold=concentration_threshold,
            divergence_lookback_days=divergence_lookback_days,
            temporal_window_days=temporal_window_days,
            joint_mode=args.joint_mode,
            concentration_top_pct=args.top_pct,
            preset=args.preset,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[escape-top] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[escape-top] ERROR: {exc}", file=sys.stderr)
        return 2

    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    print(json_str)

    output_path = _resolve_output_path(args.output_path)
    if output_path is not None:
        write_json(result, output_path)
        print(f"\n[escape-top] Wrote {output_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
