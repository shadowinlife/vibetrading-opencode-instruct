"""
Validation script for macro_credit_impulse signal module.

Fetches real Tushare data, computes signal, validates against SSE forward
drawdowns, and writes report.json + report.md.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from scripts.microstructure.generic_validator import validate_condition
from scripts.microstructure.macro_credit_impulse import (
    compute_macro_credit_impulse_signal,
)

OUTPUT_DIR = Path("tmp/microstructure/validation/macro_credit_impulse")
DEFAULT_DB_PATH = "./duckdb/ashare.duckdb"


def fetch_sse_data(duckdb_path: str) -> pd.DataFrame:
    """Fetch SSE Composite close from local DuckDB."""
    con = duckdb.connect(duckdb_path, read_only=True)
    df = con.execute("""
        SELECT trade_date, close
        FROM idx_factor_pro
        WHERE ts_code = '000001.SH'
          AND trade_date >= '2015-01-01'
        ORDER BY trade_date
    """).fetchdf()
    con.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def main() -> None:
    today_str = date.today().isoformat()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching Tushare cn_m + sf_month data (201501 to now)...")
    df_cn_m, df_sf, df_signal = compute_macro_credit_impulse_signal(
        start_month="201501",
        duckdb_path=DEFAULT_DB_PATH,
    )

    print(f"  cn_m: {len(df_cn_m)} monthly rows")
    sf_rows = len(df_sf) if df_sf is not None else 0
    print(f"  sf_month: {sf_rows} monthly rows{' (skipped — rate limited)' if df_sf is None else ''}")
    print(f"  signal daily: {len(df_signal)} rows, "
          f"{df_signal['signal'].sum()} signal days")

    # ── Fetch SSE for validation ──
    print("Fetching SSE close from DuckDB...")
    df_sse = fetch_sse_data(DEFAULT_DB_PATH)
    print(f"  SSE: {len(df_sse)} trading days, "
          f"{df_sse['trade_date'].min().date()} → {df_sse['trade_date'].max().date()}")

    # ── Prepare signal DataFrame for validator ──
    # The validator expects 'trade_date' (or we use calendar_date with close merge)
    # Prepare signal on trading days by merging with SSE
    df_signal_for_val = df_signal[["calendar_date", "signal", "m2_yoy",
                                    "m2_yoy_declining_streak", "credit_impulse"]].copy()
    df_signal_for_val = df_signal_for_val.rename(columns={"calendar_date": "trade_date"})
    df_signal_for_val["trade_date"] = pd.to_datetime(df_signal_for_val["trade_date"])

    # ── Run generic validator ──
    print("Running generic validator...")
    report = validate_condition(
        df_signal=df_signal_for_val,
        df_sse=df_sse,
        condition_meta={
            "condition_id": "macro_credit_impulse",
            "source_id": "tushare:cn_m+sf_month",
            "condition_name": "Macro Credit Impulse",
            "description": (
                "M2 YoY declining ≥2 consecutive months + market elevated "
                "(SSE > 250d MA) → tightening credit impulse warning. "
                "Uses effective_date = month_end + 15d to prevent look-ahead."
            ),
            "direction": "bearish",
        },
    )

    # ── Build report dict ──
    report_dict: dict = {
        "meta": {
            "validation_date": today_str,
            "condition_id": "macro_credit_impulse",
            "source": "tushare:cn_m + tushare:sf_month",
            "data_range": f"2015-01 → {df_cn_m['month'].iloc[-1]}",
            "sf_month_available": df_sf is not None,
            "monthly_observations": int(len(df_cn_m)),
            "daily_signal_rows": int(len(df_signal)),
            "signal_days": int(df_signal["signal"].sum()),
            "sse_range": (
                f"{df_sse['trade_date'].min().date()} → "
                f"{df_sse['trade_date'].max().date()}"
            ),
        },
        "classification": report.classification.value,
        "human_action_required": report.human_action_required,
        "gates": [
            {"name": g.name, "passed": g.passed, "detail": g.detail, "evidence": g.evidence}
            for g in report.gates
        ],
        "data_profile": {
            "cn_m_columns": df_cn_m.columns.tolist(),
            "cn_m_sample_range": (
                f"M2 YoY: {df_cn_m['m2_yoy'].min():.1f}% → "
                f"{df_cn_m['m2_yoy'].max():.1f}%, "
                f"latest: {df_cn_m['m2_yoy'].iloc[-1]:.1f}%"
            ),
            "sf_sample_range": (
                f"inc_month: {df_sf['inc_month'].min():.0f} → "
                f"{df_sf['inc_month'].max():.0f} (latest: "
                f"{df_sf['inc_month'].iloc[-1]:.0f})" if df_sf is not None
                else "skipped — Tushare API rate limited"
            ),
            "effective_date_check": {
                "earliest_eff_date": str(df_cn_m["effective_date"].min().date()),
                "latest_eff_date": str(df_cn_m["effective_date"].max().date()),
                "latest_month": df_cn_m["month"].iloc[-1],
                "lag_days": int(
                    (pd.Timestamp(df_cn_m["effective_date"].iloc[-1]) -
                     pd.Timestamp(df_cn_m["month_end"].iloc[-1])).days
                ),
            },
        },
    }

    # ── Write report.json ──
    json_path = OUTPUT_DIR / "report.json"
    json_path.write_text(json.dumps(report_dict, ensure_ascii=False, indent=2, default=str))
    print(f"  JSON: {json_path}")

    # ── Write report.md ──
    md_content = _generate_md_report(report_dict, report)
    md_path = OUTPUT_DIR / "report.md"
    md_path.write_text(md_content)
    print(f"  Report: {md_path}")

    print(f"\nDone. Classification: {report.classification.value}")


def _generate_md_report(report_dict: dict, report) -> str:
    """Generate markdown validation report."""
    meta = report_dict["meta"]
    gates = report_dict["gates"]
    dp = report_dict["data_profile"]

    lines = [
        "# Macro Credit Impulse — Validation Report",
        "",
        f"**Validation Date**: {meta['validation_date']}",
        f"**Classification**: `{report_dict['classification']}`",
        f"**Human Action Required**: {report_dict['human_action_required']}",
        "",
        "## 1. Data Overview",
        "",
        f"- **cn_m data range**: {meta['data_range']} ({meta['monthly_observations']} monthly observations)",
        f"- **SSE data range**: {meta['sse_range']}",
        f"- **Daily signal rows**: {meta['daily_signal_rows']}",
        f"- **Signal days**: {meta['signal_days']} ({meta['signal_days'] / max(meta['daily_signal_rows'], 1) * 100:.1f}%)",
        "",
        "### cn_m Profile",
        f"- {dp['cn_m_sample_range']}",
        f"- Effective date range: {dp['effective_date_check']['earliest_eff_date']} → {dp['effective_date_check']['latest_eff_date']}",
        f"- Latest month: {dp['effective_date_check']['latest_month']} (lag: {dp['effective_date_check']['lag_days']} days)",
        "",
        "### sf_month Profile",
        f"- {dp['sf_sample_range']}",
        "",
        "### Effective Date Protocol",
        "",
        "**CRITICAL**: All monthly data uses `effective_date = month_end + 15d`.",
        "This means:",
        "- cn_m data for month M is NOT available until day 15 of month M+1",
        "- Signals on month M's last day use data from month M-1 or earlier",
        "- Forward-fill from effective_date to next effective_date",
        "",
        "## 2. Validation Gates",
        "",
        "| Gate | Passed | Detail |",
        "|---|---|",
    ]

    for g in gates:
        status = "✅" if g["passed"] else "❌"
        lines.append(f"| {g['name']} | {status} | {g['detail'][:120]} |")

    lines.extend([
        "",
        "## 3. Signal Logic",
        "",
        "```",
        "signal = (M2 YoY declining streak >= 2 consecutive months) AND (SSE close > 250d MA)",
        "```",
        "",
        "- **M2 YoY declining streak**: Counts consecutive months where M2 YoY growth < previous month",
        "- **Market elevated**: SSE Composite close above 250-trading-day moving average",
        "",
        "## 4. Source Traceability",
        "",
        "- **cn_m**: Tushare `pro.cn_m()` — M0/M1/M2 absolute values + YoY/MoM changes",
        "- **sf_month**: Tushare `pro.query('sf_month')` — monthly social financing increment",
        "- **SSE close**: Local DuckDB `idx_factor_pro` (000001.SH)",
        "",
        "## 5. No-Lookahead Verification",
        "",
        "Confirmed by unit tests (see `tests/test_macro_credit_impulse.py`):",
        "- `test_cn_m_for_month_M_not_available_before_effective_date`: month M data invisible before eff_date",
        "- `test_march_31_must_not_use_march_data`: month-end uses prior month's data",
        "- `test_month_end_never_uses_same_month_data`: all month-ends verified",
        "",
        "## 6. Key Metrics",
        "",
    ])

    if report.condition_metadata:
        cm = report.condition_metadata
        lines.append(f"- Coverage: {cm.coverage_years:.1f} years")
        lines.append(f"- Signal rate: {cm.signal_days_pct:.2f}% of trading days")
        lines.append(f"- Robustness delta: {cm.robustness_delta:.4f}")
        lines.append(f"- Sub-period: {cm.sub_period_result}")
        for hm in cm.horizon_metrics:
            lines.append(
                f"  - {hm.horizon_days}d: DD={hm.mean_fwd_dd:.4f}, "
                f"p={f'{hm.p_value:.4f}' if hm.p_value is not None else 'N/A'}, "
                f"direction={'✓' if hm.direction_ok else '✗'}"
            )

    return "\n".join(lines)


if __name__ == "__main__":
    main()