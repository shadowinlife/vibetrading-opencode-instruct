"""Experiment runner: CLI and programmatic interface for backtest experiments.

Validates YAML experiment configs against the JSON schema, prints a resolved
summary in dry-run mode, and routes to the appropriate engine in run mode.

Usage::

    # Dry-run: validate and print summary without executing
    python -m scripts.experiment.runner --config experiments/my_exp.yaml --dry-run

    # Run: validate and execute (placeholder for Task 19)
    python -m scripts.experiment.runner --config experiments/my_exp.yaml

    # Programmatic
    from scripts.experiment.runner import run_experiment
    result = run_experiment("experiments/my_exp.yaml", dry_run=True)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMA_PATH = _PROJECT_ROOT / "experiments" / "schemas" / "experiment.schema.json"
_DEFAULT_RUNS_DIR = _PROJECT_ROOT / "experiments" / "runs"

VALID_ENGINES = ("qdata_local", "vibe_trading", "hybrid")


# ---------------------------------------------------------------------------
# Config loading and validation
# ---------------------------------------------------------------------------


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load a YAML experiment config file.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Parsed config dictionary.

    Raises:
        FileNotFoundError: If config_path does not exist.
        yaml.YAMLError: If the file is not valid YAML.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_schema() -> dict[str, Any]:
    """Load the experiment JSON schema.

    Returns:
        Schema dictionary.

    Raises:
        FileNotFoundError: If schema file is missing.
    """
    if not _SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {_SCHEMA_PATH}")
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_config(config: dict[str, Any], schema: dict[str, Any] | None = None) -> list[str]:
    """Validate a config dict against the experiment schema.

    Args:
        config: Parsed experiment config.
        schema: Optional pre-loaded schema. Loaded from disk if None.

    Returns:
        List of validation error messages. Empty list means valid.
    """
    if schema is None:
        schema = load_schema()

    if jsonschema is None:
        return ["jsonschema library not installed; cannot validate"]

    validator = jsonschema.Draft7Validator(schema)
    errors: list[str] = []
    for error in sorted(validator.iter_errors(config), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.path) if error.path else "(root)"
        errors.append(f"{path}: {error.message}")
    return errors


# ---------------------------------------------------------------------------
# Dry-run summary
# ---------------------------------------------------------------------------


def format_dry_run_summary(config: dict[str, Any], output_dir: str) -> str:
    """Format a human-readable dry-run summary.

    Args:
        config: Validated experiment config.
        output_dir: Resolved output directory path.

    Returns:
        Multi-line summary string.
    """
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("EXPERIMENT DRY-RUN SUMMARY")
    lines.append("=" * 60)
    lines.append(f"  Name:          {config.get('name', '(unnamed)')}")
    lines.append(f"  Engine:        {config.get('engine', '(not set)')}")

    # Universe
    universe = config.get("universe", {})
    uni_type = universe.get("type", "(not set)")
    uni_detail = ""
    if uni_type == "index":
        uni_detail = universe.get("index_code", "")
    elif uni_type == "sw":
        uni_detail = universe.get("sw_code", "")
    elif uni_type == "custom":
        codes = universe.get("codes", [])
        uni_detail = f"{len(codes)} stocks"
    lines.append(f"  Universe:      {uni_type}" + (f" ({uni_detail})" if uni_detail else ""))

    # Date range
    date_range = config.get("date_range", {})
    start = date_range.get("start", "(not set)")
    end = date_range.get("end", "(not set)")
    lines.append(f"  Date Range:    {start} → {end}")
    oos_start = date_range.get("oos_start")
    if oos_start:
        lines.append(f"  OOS Start:     {oos_start}")

    # Data and factor sources
    lines.append(f"  Data Source:   {config.get('data_source', 'auto')}")
    lines.append(f"  Factor Source:  {config.get('factor_source', 'qdata_alpha158')}")

    # Market
    market = config.get("market")
    if market:
        lines.append(f"  Market:        {market}")

    # Strategies
    strategies = config.get("strategies", [])
    if strategies:
        lines.append(f"  Strategies:    {len(strategies)} defined")
        for s in strategies:
            lines.append(f"    - {s.get('name', '(unnamed)')} (signal: {s.get('signal_col', '?')})")
    else:
        lines.append("  Strategies:    (none defined)")

    # Output
    lines.append(f"  Output Dir:    {output_dir}")
    lines.append("=" * 60)
    lines.append("[DRY-RUN] No backtest executed. No artifacts written.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Engine routing
# ---------------------------------------------------------------------------


def _route_engine(
    engine: str,
    config: dict[str, Any],
    output_dir: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Route experiment execution to the appropriate backtest engine.

    Args:
        engine: Engine identifier — one of ``qdata_local``, ``vibe_trading``,
            ``hybrid``.
        config: Validated experiment config dictionary.
        output_dir: Resolved output directory path.
        dry_run: If True, only describe what would happen (no execution).

    Returns:
        Result dictionary with at least ``engine`` and ``routing_status`` keys.

    Raises:
        ValueError: If *engine* is not a recognised engine value.
    """
    if engine not in VALID_ENGINES:
        raise ValueError(
            f"Unknown engine: {engine!r}. "
            f"Valid engines: {', '.join(VALID_ENGINES)}"
        )

    if engine == "qdata_local":
        print("Routing to Qdata local engine")
        print(f"  [qdata_local] Universe: {config.get('universe', {})}")
        print(f"  [qdata_local] Date range: {config.get('date_range', {})}")
        print(f"  [qdata_local] Strategies: {len(config.get('strategies', []))} defined")
        print(f"  [qdata_local] Output: {output_dir}")
        if dry_run:
            print("  [qdata_local] DRY-RUN: would call scripts.backtest.engine.run_fold_evaluation()")
        else:
            print("  [qdata_local] Calling local backtest placeholder (full integration pending)")
        return {
            "engine": "qdata_local",
            "routing_status": "dry_run" if dry_run else "placeholder",
            "detail": "Qdata local Walk-Forward backtest path",
        }

    elif engine == "vibe_trading":
        print("Routing to Vibe-Trading")
        # Prepare run directory structure compatible with Vibe-Trading
        run_dir = Path(output_dir) / "vibe_run"
        if not dry_run:
            os.makedirs(run_dir, exist_ok=True)
            # Write a config.json stub for Vibe-Trading's backtest runner
            vibe_config = {
                "source": config.get("data_source", "auto"),
                "codes": _extract_codes(config),
                "start_date": config.get("date_range", {}).get("start", ""),
                "end_date": config.get("date_range", {}).get("end", ""),
            }
            vibe_config_path = run_dir / "config.json"
            with open(vibe_config_path, "w", encoding="utf-8") as f:
                json.dump(vibe_config, f, indent=2)
            print(f"  [vibe_trading] Run directory prepared: {run_dir}")
            print(f"  [vibe_trading] Config written: {vibe_config_path}")
        else:
            print(f"  [vibe_trading] DRY-RUN: would prepare run directory at {run_dir}")
        print(f"  [vibe_trading] Output: {output_dir}")
        return {
            "engine": "vibe_trading",
            "routing_status": "dry_run" if dry_run else "prepared",
            "detail": "Vibe-Trading execution platform (run directory prepared)",
            "run_dir": str(run_dir),
        }

    else:
        print("Routing hybrid: Qdata factor prep → Vibe execution")
        print("  [hybrid] Sequence:")
        print("    1. Qdata: compute Alpha158 factors from local DuckDB")
        print("    2. Qdata: resolve universe (index/SW/custom + ST/listing filters)")
        print("    3. Bridge: SignalEngineAdapter translates signals to Vibe contract")
        print("    4. Vibe-Trading: execute backtest with prepared factor/signal data")
        print(f"  [hybrid] Output: {output_dir}")
        if dry_run:
            print("  [hybrid] DRY-RUN: no execution performed")
        else:
            print("  [hybrid] Full hybrid pipeline placeholder (integration pending)")
        return {
            "engine": "hybrid",
            "routing_status": "dry_run" if dry_run else "placeholder",
            "detail": "Hybrid: Qdata factor prep → Vibe-Trading execution",
        }


def _extract_codes(config: dict[str, Any]) -> list[str]:
    """Extract stock codes from a config's universe section.

    Returns a list of codes suitable for Vibe-Trading's ``codes`` field.
    """
    universe = config.get("universe", {})
    uni_type = universe.get("type", "")
    if uni_type == "custom":
        return list(universe.get("codes", []))
    elif uni_type == "index":
        return [universe.get("index_code", "")]
    elif uni_type == "sw":
        return [universe.get("sw_code", "")]
    return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_experiment(
    config_path: str | Path,
    dry_run: bool = False,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Run or dry-run an experiment from a YAML config file.

    Args:
        config_path: Path to the YAML experiment config.
        dry_run: If True, validate and print summary without executing.
        output_dir: Override output directory. Defaults to
            ``experiments/runs/<experiment_name>/``.

    Returns:
        Result dictionary with keys:
        - ``status``: "dry_run", "success", or "error"
        - ``config``: parsed config (if valid)
        - ``errors``: validation errors (if any)
        - ``output_dir``: resolved output directory (if applicable)
        - ``summary``: dry-run summary text (if dry_run=True)

    Raises:
        FileNotFoundError: If config_path does not exist.
    """
    # Load config
    config = load_config(config_path)

    # Validate
    errors = validate_config(config)
    if errors:
        error_msg = "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        print(error_msg, file=sys.stderr)
        return {
            "status": "error",
            "config": config,
            "errors": errors,
        }

    # Resolve output directory
    exp_name = config.get("name", "unnamed")
    resolved_output = output_dir or str(_DEFAULT_RUNS_DIR / exp_name)

    if dry_run:
        summary = format_dry_run_summary(config, resolved_output)
        print(summary)
        return {
            "status": "dry_run",
            "config": config,
            "errors": [],
            "output_dir": resolved_output,
            "summary": summary,
        }

    engine = config.get("engine", "qdata_local")

    # Create output directory
    os.makedirs(resolved_output, exist_ok=True)

    # Route to the appropriate engine
    routing_result = _route_engine(engine, config, resolved_output, dry_run=False)

    return {
        "status": "success",
        "config": config,
        "errors": [],
        "output_dir": resolved_output,
        "engine": routing_result["engine"],
        "routing_status": routing_result["routing_status"],
        "routing_detail": routing_result["detail"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="experiment-runner",
        description="Run or dry-run a multi-stock backtest experiment.",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the YAML experiment config file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and print config summary without executing.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory (default: experiments/runs/<name>/).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments. Defaults to sys.argv[1:].

    Returns:
        Exit code: 0 for success/dry-run, 1 for validation error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    result = run_experiment(
        config_path=args.config,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
    )

    if result["status"] == "error":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
