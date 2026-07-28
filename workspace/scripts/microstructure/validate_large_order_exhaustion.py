"""Validation runner for large_order_exhaustion condition.

Runs compute_large_order_exhaustion against actual DuckDB data with both
Tushare (stk_moneyflow) and THS (stk_moneyflow_ths) data sources, and
produces report.json + report.md.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.microstructure.base import write_json
from scripts.microstructure.large_order_exhaustion import (
    LargeOrderSummary,
    compute_large_order_exhaustion,
)
from scripts.microstructure.metadata import DEFAULT_DUCKDB_PATH

REPORT_DIR = (
    Path(__file__).resolve().parents[2] / "tmp" / "microstructure"
    / "validation" / "large_order_exhaustion"
)


def _run_source(source: str) -> LargeOrderSummary:
    print(f"  Querying {source} …", flush=True)
    return compute_large_order_exhaustion(
        DEFAULT_DUCKDB_PATH,
        data_source=source,  # type: ignore[arg-type]
        sse_high_pct=90.0,
        ratio_ma_window=20,
        rolling_sum_window=5,
        rolling_z_threshold=-1.5,
    )


def _build_markdown(
    ts: LargeOrderSummary | None,
    ths: LargeOrderSummary | None,
) -> str:
    lines: list[str] = []
    lines.append("# Large-Order Exhaustion Validation Report")
    lines.append("")
    lines.append(f"**Generated**: {date.today().isoformat()}")
    lines.append("")

    # ── Tushare ──
    lines.append("## 1. Tushare Data Source (stk_moneyflow)")
    lines.append("")
    if ts is None:
        lines.append("**FAILED**: could not run query.  See `report.json` for error details.")
    else:
        _append_source_section(lines, ts)

    # ── THS ──
    lines.append("## 2. THS Data Source (stk_moneyflow_ths)")
    lines.append("")
    if ths is None:
        lines.append("**FAILED**: could not run query.  See `report.json` for error details.")
    else:
        _append_source_section(lines, ths)

    # ── Comparison ──
    lines.append("## 3. Cross-Source Comparison")
    lines.append("")
    if ts and ths:
        ts_latest = ts["latest_snapshot"]["trade_date"]
        ths_latest = ths["latest_snapshot"]["trade_date"]
        lines.append(f"| | Tushare | THS |")
        lines.append(f"| --- | --- | --- |")
        lines.append(f"| Latest date | {ts_latest} | {ths_latest} |")
        lines.append(
            f"| Net flow (亿元) | {ts['latest_snapshot']['net_flow_billion_cny']:.2f} | "
            f"{ths['latest_snapshot']['net_flow_billion_cny']:.2f} |"
        )
        lines.append(
            f"| Flow ratio % | {ts['latest_snapshot']['flow_ratio_pct']:.2f} | "
            f"{ths['latest_snapshot']['flow_ratio_pct']:.2f} |"
        )
        lines.append(
            f"| Signal A (flow_deterioration) | {ts['latest_snapshot']['signal_flow_deterioration']} | "
            f"{ths['latest_snapshot']['signal_flow_deterioration']} |"
        )
        lines.append(
            f"| Signal B (ratio_declining) | {ts['latest_snapshot']['signal_ratio_declining']} | "
            f"{ths['latest_snapshot']['signal_ratio_declining']} |"
        )
        lines.append(
            f"| Signal C (rolling_deterioration) | {ts['latest_snapshot']['signal_rolling_deterioration']} | "
            f"{ths['latest_snapshot']['signal_rolling_deterioration']} |"
        )
        lines.append(
            f"| Any signal | {ts['latest_snapshot']['signal_any']} | "
            f"{ths['latest_snapshot']['signal_any']} |"
        )

    # ── Unit documentation ──
    lines.append("")
    lines.append("## 4. Unit Documentation")
    lines.append("")
    lines.append("- **stk_moneyflow `*_amount`**: 万元 (ten-thousand CNY)")
    lines.append("- **stk_moneyflow_ths *.amount**: 万元 (ten-thousand CNY)")
    lines.append("- **stk_factor_pro.amount**: 千元 (thousand CNY)")
    lines.append("- **flow_ratio formula**: `net_flow_wan / (total_amount_kcy / 10.0)` — dimensionless")
    lines.append("- **万元 → 亿元**: divide by 100,000")

    return "\n".join(lines)


def _append_source_section(lines: list[str], result: LargeOrderSummary) -> None:
    snap = result["latest_snapshot"]
    hist = result["historical_stats"]
    src = result["source_info"]
    params = result["parameters"]
    variants = result["variants_summary"]

    lines.append(f"**Source table**: {src['source_table']}")
    lines.append(f"**Amount unit**: {src['amount_unit']}")
    lines.append(f"**Date range**: {hist['date_range_start']} to {hist['date_range_end']}")
    lines.append(f"**Total trading days**: {hist['total_days']}")
    lines.append("")

    lines.append("### Latest Snapshot")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"| --- | --- |")
    lines.append(f"| Trade date | {snap['trade_date']} |")
    lines.append(f"| Net flow (亿元) | {snap['net_flow_billion_cny']:.2f} |")
    lines.append(f"| Flow ratio | {snap['flow_ratio_pct']:.2f}% |")
    lines.append(f"| SSE close | {snap['sse_close']:.2f} |")
    lines.append(f"| SSE percentile | {snap['sse_percentile']:.1f}% |")
    lines.append(f"| SSE near high | {snap['sse_near_high']} |")
    lines.append(f"| Rolling 5d sum (亿元) | {snap.get('rolling_sum_wan', 0) / 100_000:.2f} |")
    lines.append(f"| Rolling 5d Z-score | {snap.get('rolling_sum_z', 'NaN')} |")
    lines.append("")

    lines.append("### Signal Status")
    lines.append("")
    lines.append(f"| Signal Variant | Firing | Signal Days | Pct of Days |")
    lines.append(f"| --- | --- | --- | --- |")
    for key, label in [
        ("flow_deterioration", "A: Net flow negative"),
        ("ratio_declining", "B: Ratio declining"),
        ("rolling_deterioration", "C: Rolling deterioration"),
    ]:
        v = variants[key]
        lines.append(
            f"| {label} | {v['latest_firing']} | {v['signal_days']} | "
            f"{v['signal_pct']:.1f}% |"
        )
    lines.append(
        f"| **Any** | **{snap['signal_any']}** | "
        f"**{hist['signal_days']}** | **{hist['signal_days_pct']:.1f}%** |"
    )
    lines.append("")

    lines.append("### Parameters")
    lines.append("")
    for k, v in params.items():
        lines.append(f"- `{k}`: {v}")
    lines.append("")


def main() -> int:
    """Run validation for both data sources and produce reports."""
    print("Large-order exhaustion validation", flush=True)
    print(f"DuckDB path: {DEFAULT_DUCKDB_PATH}", flush=True)

    ts_result: LargeOrderSummary | None = None
    ths_result: LargeOrderSummary | None = None
    ts_error: str | None = None
    ths_error: str | None = None

    # ── Tushare ──
    try:
        ts_result = _run_source("tushare")
        print(f"  Tushare: {ts_result['historical_stats']['total_days']} days, "
              f"{ts_result['latest_snapshot']['trade_date']}, "
              f"signal_any={ts_result['latest_snapshot']['signal_any']}", flush=True)
    except Exception as exc:
        ts_error = f"{type(exc).__name__}: {exc}"
        print(f"  Tushare ERROR: {ts_error}", flush=True)

    # ── THS ──
    try:
        ths_result = _run_source("ths")
        print(f"  THS: {ths_result['historical_stats']['total_days']} days, "
              f"{ths_result['latest_snapshot']['trade_date']}, "
              f"signal_any={ths_result['latest_snapshot']['signal_any']}", flush=True)
    except Exception as exc:
        ths_error = f"{type(exc).__name__}: {exc}"
        print(f"  THS ERROR: {ths_error}", flush=True)

    # ── Write JSON report ──
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    json_report = {
        "validation_date": date.today().isoformat(),
        "tushare": ts_result if ts_result else {"error": ts_error},
        "ths": ths_result if ths_result else {"error": ths_error},
    }
    write_json(json_report, REPORT_DIR / "report.json")
    print(f"  Wrote {REPORT_DIR / 'report.json'}", flush=True)

    # ── Write Markdown report ──
    md = _build_markdown(ts_result, ths_result)
    (REPORT_DIR / "report.md").write_text(md, encoding="utf-8")
    print(f"  Wrote {REPORT_DIR / 'report.md'}", flush=True)

    return 0 if (ts_result is not None or ths_result is not None) else 1


if __name__ == "__main__":
    raise SystemExit(main())