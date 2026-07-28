#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .base import write_json
from .concentration import compute_concentration
from .metadata import DEFAULT_DUCKDB_PATH, DEFAULT_OUTPUT_DIR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.microstructure.concentration_cli",
        description="Compute daily top-5% turnover concentration and emit a JSON summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python -m scripts.microstructure.concentration_cli
  python -m scripts.microstructure.concentration_cli --output ./tmp/my_summary.json
  python -m scripts.microstructure.concentration_cli --start-date 2024-01-02
  python -m scripts.microstructure.concentration_cli --start-date 2024-01-02 --end-date 2025-01-02""",
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
    parser.add_argument(
        "--top-pct",
        type=float,
        default=5.0,
        metavar="PCT",
        help="top percentage of stocks by turnover (default: 5.0)",
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

    start_date_raw: date | None = (
        date.fromisoformat(args.start_date) if args.start_date else None
    )
    end_date_raw: date | None = (
        date.fromisoformat(args.end_date) if args.end_date else None
    )

    try:
        result = compute_concentration(
            args.duckdb_path,
            top_pct=args.top_pct,
            start_date=start_date_raw,
            end_date=end_date_raw,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[concentration] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[concentration] ERROR: {exc}", file=sys.stderr)
        return 2

    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    print(json_str)

    output_path = _resolve_output_path(args.output_path)
    if output_path is not None:
        write_json(result, output_path)
        print(f"\n[concentration] Wrote {output_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
