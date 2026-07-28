"""Deterministic signal scanner — scans a watchlist for triggered entry signals.

Loads ``cron_jobs/watchlist.json``, fetches quotes via the quote adapter,
evaluates entry/exit rules against available quote columns, and optionally
sends notifications through ``cron_jobs.notifier``.

Usage::

    # CLI:
    python -m scripts.realtime.signal_scanner \\
        --watchlist cron_jobs/watchlist.json --dry-run --json

    # API:
    from scripts.realtime.signal_scanner import scan_watchlist
    result = scan_watchlist("cron_jobs/watchlist.json", dry_run=True)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

from scripts.realtime.quote_adapter import get_quote_with_meta, _DEFAULT_FALLBACK_CHAINS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import for notifier (avoid side-effects at import time)
# ---------------------------------------------------------------------------


def _notify(task: Mapping[str, object], signal: Mapping[str, object]) -> None:
    """Thin wrapper around ``cron_jobs.notifier.notify`` for testability."""
    from cron_jobs.notifier import notify
    notify(task, signal)


# ---------------------------------------------------------------------------
# Watchlist loading & validation
# ---------------------------------------------------------------------------


def _load_watchlist(watchlist_path: str) -> Dict[str, Any]:
    """Load and return the watchlist JSON. Raises FileNotFoundError if missing."""
    path = Path(watchlist_path)
    if not path.exists():
        raise FileNotFoundError(f"Watchlist not found: {watchlist_path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _validate_watchlist(data: Dict[str, Any]) -> List[str]:
    """Validate watchlist against signal_rules.schema.json.

    Returns a list of validation error strings (empty = valid).
    Uses jsonschema if available; falls back to basic structural checks.
    """
    errors: List[str] = []

    if "symbols" not in data:
        errors.append("Missing required key: 'symbols'")
        return errors

    if not isinstance(data["symbols"], list):
        errors.append("'symbols' must be an array")
        return errors

    required_keys = {
        "symbol", "market", "data_source", "signal_col",
        "entry_rule", "exit_rule", "freshness_minutes", "notify_channels",
    }

    for i, sym_entry in enumerate(data["symbols"]):
        if not isinstance(sym_entry, dict):
            errors.append(f"symbols[{i}]: not a dict")
            continue
        missing = required_keys - set(sym_entry.keys())
        if missing:
            errors.append(f"symbols[{i}] ({sym_entry.get('symbol', '?')}): missing {missing}")

    # Try jsonschema validation if available
    try:
        import jsonschema
        schema_path = Path(__file__).resolve().parents[2] / "cron_jobs" / "signal_rules.schema.json"
        if schema_path.exists():
            with open(schema_path, encoding="utf-8") as f:
                schema = json.load(f)
            jsonschema.validate(data, schema)
    except ImportError:
        pass  # jsonschema not installed — structural check only
    except Exception as e:
        errors.append(f"Schema validation: {e}")

    return errors


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


def _evaluate_rule(rule_expr: str, quote_row: pd.Series) -> Optional[bool]:
    """Evaluate a boolean rule expression against quote row columns.

    The namespace contains only the quote row's column values (lowercase keys).
    Returns ``True``/``False`` on success, ``None`` on evaluation error.

    Supported expressions reference column names directly::

        "close > 10"
        "close > 10 and volume > 500000"
    """
    namespace: Dict[str, Any] = {}
    for col in quote_row.index:
        val = quote_row[col]
        # Convert numpy types to Python native for eval safety
        if isinstance(val, (pd.Timestamp,)):
            namespace[col] = val
        elif hasattr(val, 'item'):
            namespace[col] = val.item()
        else:
            namespace[col] = val

    try:
        result = eval(rule_expr, {"__builtins__": {}}, namespace)  # noqa: S307
        return bool(result)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------


def scan_watchlist(
    watchlist_path: str,
    dry_run: bool = False,
    output_json: bool = False,
) -> Dict[str, Any]:
    """Scan all symbols in a watchlist and return structured results.

    Args:
        watchlist_path: Path to watchlist JSON file.
        dry_run: If True, suppress notifications.
        output_json: If True, print JSON to stdout (for CLI use).

    Returns:
        Dict with keys:
          - ``scanned_count``: total symbols attempted
          - ``triggered_signals``: list of triggered signal dicts
          - ``errors``: list of per-symbol error dicts
          - ``data_as_of``: latest quote timestamp (ISO string)
          - ``freshness_summary``: ``{fresh, stale, unknown}`` counts
    """
    # --- Load & validate ---
    wl_data = _load_watchlist(watchlist_path)
    validation_errors = _validate_watchlist(wl_data)
    if validation_errors:
        logger.warning("Watchlist validation issues: %s", validation_errors)

    symbols = wl_data.get("symbols", [])

    triggered_signals: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    would_notify: List[Dict[str, Any]] = []
    timestamps: List[pd.Timestamp] = []
    freshness_counts: Dict[str, int] = {"fresh": 0, "stale": 0, "unknown": 0}

    # --- Per-symbol scan ---
    for sym_entry in symbols:
        symbol = sym_entry["symbol"]
        market = sym_entry["market"]
        entry_rule = sym_entry["entry_rule"]
        freshness_min = sym_entry.get("freshness_minutes", 30)

        try:
            market_upper = market.upper()
            fallback_chain = _DEFAULT_FALLBACK_CHAINS.get(market_upper)
            quote_df, meta = get_quote_with_meta(
                symbol, market,
                fallback_chain=fallback_chain,
                freshness_minutes=freshness_min,
            )

            # --- Freshness accounting ---
            if meta.get("fresh") is True:
                freshness_counts["fresh"] += 1
            elif meta.get("fresh") is False:
                freshness_counts["stale"] += 1
            else:
                freshness_counts["unknown"] += 1

            # --- Quote unavailable ---
            if quote_df is None or quote_df.empty:
                err_msg = "no_data"
                if meta.get("errors"):
                    err_msg = str(meta["errors"])
                errors.append({
                    "symbol": symbol,
                    "error": err_msg,
                    "meta": meta,
                })
                continue

            # --- Track timestamp ---
            if "timestamp" in quote_df.columns:
                ts = quote_df.iloc[0]["timestamp"]
                if ts is not None and not (isinstance(ts, float) and pd.isna(ts)):
                    timestamps.append(pd.Timestamp(ts))

            # --- Evaluate entry rule ---
            row = quote_df.iloc[0]
            entry_result = _evaluate_rule(entry_rule, row)

            if entry_result is None:
                # Rule evaluation failed (e.g., unknown column)
                errors.append({
                    "symbol": symbol,
                    "error": f"rule_eval_failed:{entry_rule}",
                    "meta": meta,
                })
                continue

            if entry_result:
                close_val = float(row.get("close", 0))
                signal_info = {
                    "symbol": symbol,
                    "market": market,
                    "close": close_val,
                    "entry_decision": "YES",
                    "entry_rule": entry_rule,
                    "reason": f"Entry rule satisfied: {entry_rule}",
                    "source": meta.get("source", "unknown"),
                    "fresh": meta.get("fresh"),
                }
                triggered_signals.append(signal_info)

                # --- Notify (unless dry_run) ---
                if dry_run:
                    channels = sym_entry.get("notify_channels", [])
                    would_notify.append({
                        "symbol": symbol,
                        "signal": "entry",
                        "channels": channels,
                    })
                    logger.info("would notify: %s entry (channels=%s)", symbol, channels)
                else:
                    task_obj = {
                        "id": symbol,
                        "name": symbol,
                        "notify": {ch: True for ch in sym_entry.get("notify_channels", [])},
                    }
                    signal_obj = {
                        "signal": "entry",
                        "type": "entry_triggered",
                        "reason": signal_info["reason"],
                    }
                    try:
                        _notify(task_obj, signal_obj)
                    except Exception as e:
                        logger.warning("Notification failed for %s: %s", symbol, e)
                        errors.append({
                            "symbol": symbol,
                            "error": f"notification_failed: {e}",
                            "phase": "notify",
                        })

        except Exception as e:
            errors.append({
                "symbol": symbol,
                "error": str(e),
            })
            freshness_counts["unknown"] += 1

    # --- Assemble result ---
    data_as_of = ""
    if timestamps:
        data_as_of = max(timestamps).isoformat()

    result = {
        "scanned_count": len(symbols),
        "triggered_signals": triggered_signals,
        "errors": errors,
        "data_as_of": data_as_of,
        "freshness_summary": freshness_counts,
    }
    if dry_run:
        result["would_notify"] = would_notify

    if output_json:
        print(json.dumps(result, indent=2, default=str))

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: ``python -m scripts.realtime.signal_scanner``."""
    parser = argparse.ArgumentParser(
        description="Deterministic signal scanner for watchlist monitoring",
    )
    parser.add_argument(
        "--watchlist",
        required=True,
        help="Path to watchlist JSON file (e.g. cron_jobs/watchlist.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Scan without sending notifications",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="output_json",
        help="Print results as JSON to stdout",
    )

    args = parser.parse_args()

    result = scan_watchlist(
        watchlist_path=args.watchlist,
        dry_run=args.dry_run,
        output_json=args.output_json,
    )

    # Exit 0 on success regardless of errors (errors are collected, not fatal)
    sys.exit(0)


if __name__ == "__main__":
    main()
