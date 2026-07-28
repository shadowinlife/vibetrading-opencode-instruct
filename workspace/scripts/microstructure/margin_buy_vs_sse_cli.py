#!/usr/bin/env python3
"""CLI entry point for the margin-buy ratio / SSE divergence indicator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .base import write_json
from .margin_buy_vs_sse import compute_margin_buy_vs_sse
from .metadata import DEFAULT_DUCKDB_PATH, DEFAULT_OUTPUT_DIR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.microstructure.margin_buy_vs_sse_cli",
        description="Compute all-A-share margin-buy ratio vs SSE divergence indicator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python -m scripts.microstructure.margin_buy_vs_sse_cli
  python -m scripts.microstructure.margin_buy_vs_sse_cli --output ./tmp/margin_summary.json
  python -m scripts.microstructure.margin_buy_vs_sse_cli --start-date 2024-01-01 --end-date 2026-05-26""",
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

    try:
        result = compute_margin_buy_vs_sse(
            duckdb_path=args.duckdb_path,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[margin-buy-vs-sse] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[margin-buy-vs-sse] ERROR: {exc}", file=sys.stderr)
        return 2

    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    print(json_str)

    output_path = _resolve_output_path(args.output_path)
    if output_path is not None:
        write_json(result, output_path)
        print(f"\n[margin-buy-vs-sse] Wrote {output_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
