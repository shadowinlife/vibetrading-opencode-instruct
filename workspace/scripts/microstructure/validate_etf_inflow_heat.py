"""Run generic validation on ETF inflow heat signal."""
import json
from pathlib import Path

import duckdb
import pandas as pd

from scripts.microstructure.etf_inflow_heat import load_etf_signal_series
from scripts.microstructure.generic_validator import validate_condition
from scripts.microstructure.base import write_json

DUCKDB_PATH = "./duckdb/ashare.duckdb"
OUT_DIR = Path("tmp/microstructure/validation/etf_inflow_heat")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Load ETF signal series ─────────────────────────────────────────
print("Loading ETF signal series...")
df_signal = load_etf_signal_series(
    DUCKDB_PATH,
    percentile_threshold=95.0,
    rolling_window=252,
    concentration_threshold=0.50,
    broad_coverage_only=True,
)
print(f"  Signal coverage: {df_signal['trade_date'].iloc[0].strftime('%Y-%m-%d')} to {df_signal['trade_date'].iloc[-1].strftime('%Y-%m-%d')}")
print(f"  {len(df_signal)} trading days, {int(df_signal['signal'].sum())} signal days ({df_signal['signal'].mean()*100:.2f}%)")

# ── Load SSE index close ───────────────────────────────────────────
print("Loading SSE index close...")
con = duckdb.connect(DUCKDB_PATH, read_only=True)
df_sse = con.execute("""
    SELECT trade_date, close
    FROM idx_factor_pro
    WHERE ts_code = '000001.SH'
    ORDER BY trade_date
""").fetchdf()
con.close()
df_sse["trade_date"] = pd.to_datetime(df_sse["trade_date"])
print(f"  SSE coverage: {df_sse['trade_date'].iloc[0].strftime('%Y-%m-%d')} to {df_sse['trade_date'].iloc[-1].strftime('%Y-%m-%d')}")
print(f"  {len(df_sse)} trading days")

# ── Validate ───────────────────────────────────────────────────────
print("Running validation...")
report = validate_condition(
    df_signal[["trade_date", "signal"]],
    df_sse,
    {
        "condition_id": "etf_inflow_heat",
        "source_id": "local:duckdb:fund_daily+stk_factor_pro",
        "condition_name": "ETF Inflow Heat",
        "description": (
            "ETF turnover ratio vs total market turnover in rolling percentile; "
            "combined with top-5 ETF concentration. Retail euphoria contrarian signal."
        ),
    },
)

# ── Write report.json ──────────────────────────────────────────────
report_dict = {
    "condition_id": report.condition_id,
    "classification": report.classification.value,
    "human_action_required": report.human_action_required,
    "gates": [
        {
            "name": g.name,
            "passed": g.passed,
            "detail": g.detail,
            "evidence": g.evidence,
        }
        for g in report.gates
    ],
}
if report.condition_metadata:
    m = report.condition_metadata
    report_dict["condition_metadata"] = {
        "condition_id": m.condition_id,
        "source_id": m.source_id,
        "coverage_years": m.coverage_years,
        "signal_days_pct": m.signal_days_pct,
        "horizon_metrics": [
            {
                "horizon_days": hm.horizon_days,
                "mean_fwd_dd": hm.mean_fwd_dd,
                "p_value": hm.p_value,
                "direction_ok": hm.direction_ok,
            }
            for hm in m.horizon_metrics
        ],
        "robustness_delta": m.robustness_delta,
        "sub_period_result": m.sub_period_result,
        "correlation_flags": m.correlation_flags,
        "classification": m.classification.value,
        "human_action_required": m.human_action_required,
    }

report_json_path = OUT_DIR / "report.json"
write_json(report_dict, report_json_path)
print(f"\nReport written to {report_json_path}")

# ── Print summary ─────────────────────────────────────────────────
print(f"\n=== VALIDATION RESULT ===")
print(f"Classification: {report.classification.value}")
print(f"Human action required: {report.human_action_required}")
for g in report.gates:
    status = "PASS" if g.passed else "FAIL"
    print(f"  [{status}] {g.name}: {g.detail}")

if report.condition_metadata:
    m = report.condition_metadata
    print(f"\n=== CONDITION METADATA ===")
    print(f"Coverage: {m.coverage_years} years")
    print(f"Signal days: {m.signal_days_pct}%")
    for hm in m.horizon_metrics:
        print(f"  {hm.horizon_days}d: mean_fwd_dd={hm.mean_fwd_dd:.4f}, p={hm.p_value}, dir_ok={hm.direction_ok}")

# ── Generate report.md ─────────────────────────────────────────────
from datetime import datetime
md_lines = []
md_lines.append("# ETF Inflow Heat — Validation Report")
md_lines.append("")
md_lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
md_lines.append(f"**Classification**: `{report.classification.value}`")
md_lines.append(f"**Source**: `local:duckdb:fund_daily + stk_factor_pro + idx_factor_pro`")
md_lines.append("")

md_lines.append("## Hypothesis")
md_lines.append("")
md_lines.append("ETF aggregate turnover spikes to historically extreme levels (rolling percentile) ")
md_lines.append("while market is elevated signals retail euphoria / late-cycle chasing — historically ")
md_lines.append("a bearish forward-drawdown signal. Combined with top-5 ETF concentration as confirmation ")
md_lines.append("that flows are concentrated in popular ETFs, not broad-based.")
md_lines.append("")

md_lines.append("## Signal Definition")
md_lines.append("")
md_lines.append("- **ETF turnover ratio** = `SUM(fund_daily.amount) / SUM(stk_factor_pro.amount)` (both in 千元)")
md_lines.append("- **Percentile hit**: rolling 252-day percentile of ratio >= 95.0")
md_lines.append("- **Concentration hit**: top-5 ETF share of total ETF turnover >= 50.0%")
md_lines.append("- **Composite signal**: BOTH percentile AND concentration hit simultaneously")
md_lines.append("")

md_lines.append("## Data Coverage")
md_lines.append("")
md_lines.append(f"- ETF data (fund_daily): {m.coverage_years:.1f} years (broad coverage from 2023-07-31)")
md_lines.append("- SSE close (idx_factor_pro): 2010-01-04 to present, ~3,979 trading days")
md_lines.append("")

md_lines.append("## Gate Results")
md_lines.append("")
md_lines.append("| Gate | Status | Detail |")
md_lines.append("|------|--------|--------|")
for g in report.gates:
    status = "PASS" if g.passed else "FAIL"
    md_lines.append(f"| {g.name} | {status} | {g.detail} |")
md_lines.append("")

md_lines.append("## Horizon Metrics")
md_lines.append("")
if report.condition_metadata:
    md_lines.append("| Horizon | Mean Forward DD | p-value | Direction OK |")
    md_lines.append("|---------|----------------|---------|-------------|")
    for hm in m.horizon_metrics:
        p_str = f"{hm.p_value:.6f}" if hm.p_value else "N/A"
        d_str = "YES" if hm.direction_ok else "NO"
        md_lines.append(f"| {hm.horizon_days}d | {hm.mean_fwd_dd:.4f} | {p_str} | {d_str} |")
md_lines.append("")

md_lines.append("## Signal Statistics")
md_lines.append("")
md_lines.append(f"- **Signal days**: {int(df_signal['signal'].sum())} / {len(df_signal)} ({df_signal['signal'].mean()*100:.2f}%)")
md_lines.append(f"- **Percentile hit days**: {int(df_signal['pct_hit'].fillna(False).sum())}")
md_lines.append(f"- **Concentration hit days**: {int(df_signal['conc_hit'].fillna(False).sum())}")
md_lines.append("")

md_lines.append("## Latest Values")
md_lines.append("")
latest = df_signal.iloc[-1]
md_lines.append(f"- **Latest date**: {latest['trade_date'].strftime('%Y-%m-%d')}")
md_lines.append(f"- **Latest ETF turnover ratio**: {latest['etf_turnover_ratio']:.4f} ({latest['etf_turnover_ratio']*100:.2f}%)")
if pd.notna(latest['roll_pct']):
    md_lines.append(f"- **Latest rolling percentile**: {latest['roll_pct']:.1f}")
md_lines.append(f"- **Latest top-5 concentration**: {latest['top_n_concentration']:.4f} ({latest['top_n_concentration']*100:.1f}%)")
md_lines.append(f"- **Current signal**: {'YES' if latest['signal'] else 'NO'}")
md_lines.append("")

md_lines.append("## Risk Disclaimer")
md_lines.append("")
md_lines.append("This signal is a research tool, not a trading recommendation. It identifies ")
md_lines.append("market microstructure conditions historically associated with elevated forward ")
md_lines.append("drawdown risk. Past performance does not guarantee future results.")

report_md_path = OUT_DIR / "report.md"
report_md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
print(f"Report written to {report_md_path}")
print("\nDone.")