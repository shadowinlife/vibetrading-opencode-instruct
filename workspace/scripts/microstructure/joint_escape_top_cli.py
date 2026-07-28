#!/usr/bin/env python3
"""CLI entry point for the joint multi-condition escape-top warning."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .base import write_json
from .ensemble import EnsembleConfig, EnsembleMode
from .joint_escape_top import compute_joint_warning
from .metadata import DEFAULT_DUCKDB_PATH, DEFAULT_OUTPUT_DIR

_VALID_MODES: tuple[str, ...] = ("AND", "VOTE_K_OF_M", "WEIGHTED_SCORE")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.microstructure.joint_escape_top_cli",
        description=(
            "Compute joint multi-condition escape-top warning via ensemble resolution."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python -m scripts.microstructure.joint_escape_top_cli
  python -m scripts.microstructure.joint_escape_top_cli --mode vote --k 2
  python -m scripts.microstructure.joint_escape_top_cli --mode and
  python -m scripts.microstructure.joint_escape_top_cli --mode weighted --weights margin_divergence=0.6,volatility_atr_expansion=0.4
  python -m scripts.microstructure.joint_escape_top_cli --start-date 2024-01-01
  python -m scripts.microstructure.joint_escape_top_cli --include-rejected""",
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
        "--manifest-path",
        default=None,
        metavar="PATH",
        help="path to condition_manifest.json",
    )
    group = parser.add_argument_group("ensemble mode")
    group.add_argument(
        "--mode",
        choices=_VALID_MODES,
        default="VOTE_K_OF_M",
        help="ensemble resolution mode (default: VOTE_K_OF_M)",
    )
    group.add_argument(
        "--k",
        type=int,
        default=None,
        metavar="N",
        help="how many conditions must fire for RED in VOTE_K_OF_M mode",
    )
    group.add_argument(
        "--k-yellow",
        type=int,
        default=None,
        metavar="N",
        help="minimum firing conditions for YELLOW in VOTE_K_OF_M mode",
    )
    group.add_argument(
        "--k-red",
        type=int,
        default=None,
        metavar="N",
        help="minimum firing conditions for RED in VOTE_K_OF_M mode",
    )
    group.add_argument(
        "--weights",
        default=None,
        metavar="KEY=VALUE,...",
        help="comma-separated weights for WEIGHTED_SCORE mode (e.g. a=0.6,b=0.4)",
    )
    group.add_argument(
        "--red-threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help="weighted-score threshold for RED (default: 0.7)",
    )
    group.add_argument(
        "--yellow-threshold",
        type=float,
        default=None,
        metavar="FLOAT",
        help="weighted-score threshold for YELLOW (default: 0.3)",
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
        "--include-rejected",
        action="store_true",
        default=False,
        help="include rejected/research-only conditions as active contributors",
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


def _build_ensemble_config(args: argparse.Namespace) -> EnsembleConfig:
    mode: EnsembleMode = args.mode  # type: ignore[assignment]

    kwargs: dict[str, Any] = {}

    if mode == "VOTE_K_OF_M":
        if args.k is not None:
            kwargs["k_yellow"] = 1
            kwargs["k_red"] = args.k
        if args.k_yellow is not None:
            kwargs["k_yellow"] = args.k_yellow
        if args.k_red is not None:
            kwargs["k_red"] = args.k_red

    if mode == "WEIGHTED_SCORE":
        if args.weights is not None:
            weights = {}
            for pair in args.weights.split(","):
                k, v = pair.split("=", 1)
                weights[k.strip()] = float(v.strip())
            kwargs["weights"] = weights
        if args.red_threshold is not None:
            kwargs["red_threshold"] = args.red_threshold
        if args.yellow_threshold is not None:
            kwargs["yellow_threshold"] = args.yellow_threshold

    return EnsembleConfig(mode=mode, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = _build_ensemble_config(args)
    except (ValueError, KeyError) as exc:
        print(f"[joint-escape-top] CONFIG ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        result = compute_joint_warning(
            duckdb_path=args.duckdb_path,
            config=config,
            manifest_path=args.manifest_path,
            start_date=args.start_date,
            end_date=args.end_date,
            include_rejected=args.include_rejected,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[joint-escape-top] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[joint-escape-top] ERROR: {exc}", file=sys.stderr)
        return 2

    json_str = json.dumps(result, ensure_ascii=False, indent=2)
    print(json_str)

    # Print board concentration anchor summary on RED
    bc = result.get("board_concentration")
    if bc and "error" not in bc:
        anchor = bc.get("anchor_index", {})
        boards = bc.get("boards", [])
        board_summary = ", ".join(
            f"{b['board']}({b['penetration_pct']:.1f}%)"
            for b in boards[:3]
        )
        print(
            f"\n[joint-escape-top] 🔴 RED — 集中度最富集板块: {board_summary}",
            file=sys.stderr,
        )
        print(
            f"[joint-escape-top] 推荐跟踪锚点: "
            f"{anchor.get('index_name', '?')}({anchor.get('index_code', '?')})",
            file=sys.stderr,
        )

    output_path = _resolve_output_path(args.output_path)
    if output_path is not None:
        write_json(result, output_path)
        print(f"\n[joint-escape-top] Wrote {output_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())