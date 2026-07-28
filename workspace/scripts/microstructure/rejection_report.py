"""
Rejection / blocking report generator for escape-top microstructure validation.

Reads per-condition ``generic_validator_report.json`` files from the validation
directory, aggregates gate failure details, and produces human-readable (Markdown)
and machine-readable (JSON) reports documenting WHY each non-validated condition
was rejected, blocked, or classified as research-only.

Usage
-----
::

    from scripts.microstructure.rejection_report import write_reports
    write_reports(
        validation_dir="tmp/microstructure/validation",
        output_dir="tmp/microstructure/validation",
    )

CLI
---

::

    python -m scripts.microstructure.rejection_report \
        --validation-dir tmp/microstructure/validation \
        --output-dir tmp/microstructure/validation
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────────

_VALIDATION_DATE = "2026-05-28"

# ── Data models ────────────────────────────────────────────────────────────────


@dataclass
class FailedGate:
    """A single gate that a condition failed, with evidence and interpretation."""

    gate_name: str
    detail: str
    interpretation: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RejectedEntry:
    """Report entry for a REJECTED condition."""

    condition_id: str
    condition_name: str
    tier: str
    composite_dd: float
    signal_days_pct: float
    failed_gates: list[FailedGate] = field(default_factory=list)
    root_cause: str = ""
    known_workaround: str = ""
    next_action: str = ""


@dataclass
class ResearchOnlyEntry:
    """Report entry for a RESEARCH_ONLY condition."""

    condition_id: str
    condition_name: str
    tier: str
    composite_dd: float
    signal_days_pct: float
    why_not_production: str
    what_data_would_help: str
    next_action: str


@dataclass
class BlockedEntry:
    """Report entry for a BLOCKED_BY_DATA or BLOCKED_BY_PERMISSION condition."""

    condition_id: str
    condition_name: str
    tier: str
    block_type: str  # "blocked_by_data" or "blocked_by_permission"
    source_id: str
    blocker_detail: str
    provider_options: list[str] = field(default_factory=list)
    estimated_cost_and_effort: str = ""
    priority: str = "LOW"
    next_action: str = ""


@dataclass
class ProcurementItem:
    """A data source that needs acquisition before its condition can be validated."""

    source: str
    condition: str
    reason_blocked: str
    provider_options: list[str]
    estimated_cost: str
    estimated_effort: str
    priority: str
    wave_target: str


@dataclass
class RejectionReport:
    """Complete rejection/blocking report aggregating all non-validated conditions."""

    meta: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    rejected: list[RejectedEntry] = field(default_factory=list)
    research_only: list[ResearchOnlyEntry] = field(default_factory=list)
    blocked: list[BlockedEntry] = field(default_factory=list)
    procurement_list: list[ProcurementItem] = field(default_factory=list)
    effective_date_warnings: list[str] = field(default_factory=list)


# ── Gate failure interpretation ────────────────────────────────────────────────


def _interpret_direction_failure(evidence: dict) -> str:
    horizons = []
    for h in ("20d", "60d", "120d"):
        ok = evidence.get(f"direction_{h}", None)
        if ok is not None:
            horizons.append(f"{h}=pass" if ok else f"{h}=fail")
    status = ", ".join(horizons)
    return (
        f"Direction test evaluates whether signal-day forward drawdown is "
        f"more negative than non-signal-day drawdown at each horizon. "
        f"Result: {status}. "
        f"Failure means the signal does NOT predict elevated subsequent "
        f"drawdowns at the failing horizons."
    )


def _interpret_separation_failure(evidence: dict) -> str:
    parts = []
    for h in ("20d", "60d", "120d"):
        entry_key = f"horizon_{h}"
        if entry_key in evidence:
            e = evidence[entry_key]
            p_val = e.get("welch_p_value", 1.0)
            parts.append(
                f"{h}: p={p_val:.4f}{'*' if p_val < 0.05 else ' (not sig)'}"
            )
    return (
        f"Separation (Welch t-test) checks whether the difference between "
        f"signal and non-signal forward drawdown distributions is statistically "
        f"significant. Need ≥2 of 3 horizons with p<0.05. "
        f"Result: {'; '.join(parts)}."
    )


def _interpret_selectivity_failure(evidence: dict) -> str:
    pct = evidence.get("signal_days_pct", 0)
    n_total = evidence.get("n_total_days", 0)
    n_sig = evidence.get("n_signal_days", 0)
    if pct < 0.5:
        return (
            f"Selectivity is too LOW at {pct:.2f}% ({n_sig}/{n_total} days). "
            f"Signal fires on too few days to be practically useful. Minimum is 0.5%."
        )
    elif pct > 25.0:
        return (
            f"Selectivity is too HIGH at {pct:.2f}% ({n_sig}/{n_total} days). "
            f"Signal fires on over 25% of days — too frequent for a meaningful "
            f"contrarian indicator."
        )
    return f"Selectivity at {pct:.2f}% is within range but flagged."


def _interpret_sub_period_failure(evidence: dict) -> str:
    per_horizon = evidence.get("per_horizon", [])
    summary = "; ".join(per_horizon) if per_horizon else "no horizon detail"
    return (
        f"Sub-period stability checks whether pre-2019 and post-2019 periods "
        f"agree on directional relationship. Divergence suggests regime-change "
        f"or overfitting to a specific market era. Result: {summary}."
    )


# ── Core generation logic ──────────────────────────────────────────────────────


def _load_manifest(validation_dir: Path) -> dict:
    path = validation_dir / "condition_manifest.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _collect_all_report_paths(validation_dir: Path) -> dict[str, Path]:
    """Return {condition_id: report_path} for every non-root validation dir."""
    result: dict[str, Path] = {}
    for entry in sorted(validation_dir.iterdir()):
        if not entry.is_dir():
            continue
        report = entry / "generic_validator_report.json"
        if report.exists():
            # directory name should match condition_id
            result[entry.name] = report
    return result


def _load_single_report(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _build_rejected_entry(cond: dict, report: dict) -> RejectedEntry:
    """Build a RejectedEntry from manifest cond + per-condition report."""
    gates = report.get("gates", [])
    failed_list: list[FailedGate] = []
    root_cause_parts: list[str] = []
    workaround_parts: list[str] = []
    next_actions: list[str] = []

    direction_failed = False
    separation_failed = False
    selectivity_failed = False

    for g in gates:
        if g["passed"]:
            continue
        gname = g["name"]
        evidence = g.get("evidence", {})
        interpretation = ""

        if gname == "direction":
            direction_failed = True
            interpretation = _interpret_direction_failure(evidence)
            root_cause_parts.append(
                "Signal direction fails at one or more forward horizons. "
                "The condition does not predict elevated subsequent drawdowns."
            )
        elif gname == "separation":
            separation_failed = True
            interpretation = _interpret_separation_failure(evidence)
            root_cause_parts.append(
                "Statistical separation between signal and non-signal drawdowns "
                "is insufficient (p≥0.05 at ≥2 horizons)."
            )
        elif gname == "selectivity":
            selectivity_failed = True
            interpretation = _interpret_selectivity_failure(evidence)
            root_cause_parts.append(
                f"Signal fires on {evidence.get('signal_days_pct', 0):.2f}% of "
                f"trading days — outside the acceptable 0.5%-25% range."
            )
        elif gname == "sub_period":
            interpretation = _interpret_sub_period_failure(evidence)
            root_cause_parts.append(
                "Pre-2019 vs post-2019 directional disagreement indicates "
                "regime dependency."
            )

        failed_list.append(
            FailedGate(
                gate_name=gname,
                detail=g["detail"],
                interpretation=interpretation,
                evidence=evidence,
            )
        )

    # Build root cause, workaround, next_action per condition
    cid = cond["condition_id"]
    rc = ""
    kw = ""
    na = ""
    if cid == "concentration":
        rc = (
            "Direction reversal at 120d horizon: signal days have LESS drawdown "
            "than non-signal days. High concentration at extreme thresholds may "
            "indicate consolidation, not crash. Standalone concentration at 0.50 "
            "fails, but the JOINT condition (AND with margin divergence) worked "
            "in the baseline grid search."
        )
        kw = "Use only in pairwise (AND) with margin divergence, per ESCAPE_TOP_PRESETS baseline tuning."
        na = "Retain in ESCAPE_TOP_PRESETS as gated joint condition only. Do NOT use standalone."
    elif cid == "breadth_divergence":
        rc = (
            "Direction fails at ALL horizons. When breadth is weak but index "
            "stays elevated, the market actually does BETTER at medium/long "
            "horizons than when breadth is balanced. This is the OPPOSITE of the "
            "escape-top hypothesis."
        )
        kw = "Repurpose as a momentum/continuation indicator rather than escape-top. At extreme params (b≤10, i≥90), 27 signals over 16 years with DD -0.068 — has merit but wrong sign."
        na = "Reclassify as continuation/narrow-rally indicator. Do NOT add to ESCAPE_TOP_PRESETS. Consider for a separate 'momentum' preset group."
    elif cid == "turnover_mcap_heat":
        rc = (
            "Direction fails at ALL horizons with only 22 signal days (0.55%). "
            "The 120d DD is signifcantly DIFFERENT from non-signal (p=0.0004), "
            "but in the WRONG direction — signal days have LESS drawdown."
        )
        kw = "Best threshold (abs=0.025) gives only 22 signals in 16 years. Insufficient statistical power for standalone use."
        na = "Consider as supplemental gate alongside validated conditions. Do NOT use standalone. May need alternative thresholding approach (rolling percentile vs absolute)."
    elif cid == "valuation_percentile":
        rc = (
            "Selectivity failure (0.43%, only 17 signal days in 16 years) at "
            "PE≥80 AND PB≥80 thresholds. Direction also fails at ALL horizons "
            "despite strong separation — but the extremely small sample makes "
            "any statistical conclusion unreliable."
        )
        kw = "PE≥80 AND PB≥80 is too strict. Try PE≥70 OR PB≥70 to increase sample. Also: market-cap-weighted PE may differ from official SSE PE — validate against Tushare index_dailybasic."
        na = "Relax thresholds to PE≥70 OR PB≥70, retest. Compare with official index_dailybasic PE. Re-evaluate after threshold tuning."
    elif cid == "sector_turnover_crowding":
        rc = (
            "Counter-intuitive result: HHI≥P80 days have LESS drawdown than "
            "non-signal days at all horizons (and separation confirms p<0.0001). "
            "Sector concentration appears to be a continuation (momentum) signal, "
            "not an escape-top signal."
        )
        kw = "Repurpose as a bullish continuation indicator. The signal is strong but in the wrong direction for escape-top."
        na = "Do NOT use as escape-top. Consider as momentum/continuation indicator in a different preset group. The 49% membership gap also limits robustness."
    elif cid == "winner_rate_pressure":
        rc = (
            "Direction OK at 20d/60d but fails at 120d. Separation fails (0/3 "
            "horizons significant). Sub-period completely unstable (pre-2019 vs "
            "post-2019 opposite sign at all horizons)."
        )
        kw = "8.4 years coverage is adequate but the signal's statistical properties are regime-dependent. May work in joint conditions with other signals."
        na = "Consider as confirmation signal alongside margin_divergence or ATR expansion. Do NOT use standalone. Retest at tighter thresholds (avg_wr≥70 instead of 60)."

    root_cause = rc if rc else "; ".join(root_cause_parts)
    workaround = kw if kw else "; ".join(workaround_parts)
    next_action = na if na else "; ".join(next_actions)

    return RejectedEntry(
        condition_id=cid,
        condition_name=cond["condition_name"],
        tier=cond["tier"],
        composite_dd=cond.get("composite_dd", 0),
        signal_days_pct=cond.get("signal_days_pct", 0),
        failed_gates=failed_list,
        root_cause=root_cause,
        known_workaround=workaround,
        next_action=next_action,
    )


def _build_research_only_entry(cond: dict, report: dict) -> ResearchOnlyEntry:
    """Build a ResearchOnlyEntry for large_order_exhaustion."""
    cid = cond["condition_id"]
    gates = report.get("gates", [])
    selectivity_detail = ""
    for g in gates:
        if g["name"] == "selectivity":
            selectivity_detail = g["detail"]

    return ResearchOnlyEntry(
        condition_id=cid,
        condition_name=cond["condition_name"],
        tier=cond["tier"],
        composite_dd=cond.get("composite_dd", 0),
        signal_days_pct=cond.get("signal_days_pct", 0),
        why_not_production=(
            f"Signal fires on 59.94% of trading days ({2385}/{3979}) — more than "
            f"double the 25% maximum for a useful signal. The ANY-signal variant "
            f"aggregates all 3 sub-signals (flow_deterioration, ratio_declining, "
            f"rolling_deterioration) and becomes too noisy. "
            f"However, the directional signal is STRONG: direction OK at all 3 "
            f"horizons, separation 3/3 with p<0.01 at each horizon, composite DD "
            f"-0.0614. The underlying signal quality is excellent — the problem "
            f"is the excessive hit rate, not the signal logic."
        ),
        what_data_would_help=(
            "No additional data needed. Problem is signal aggregation, not data "
            "coverage. Needs stricter sub-signal gating: (1) use only Signal A "
            "(flow_deterioration, 18.1% hit rate) instead of signal_any, "
            "(2) require 2 of 3 sub-signals instead of 1 of 3, "
            "(3) add magnitude threshold on net flow (e.g. net_flow < -100亿)."
        ),
        next_action=(
            "Retest with Signal A only (flow_deterioration, ~18% hit rate). "
            "Re-evaluate with stricter sub-signal AND logic (2/3 required). "
            "This condition has strong potential if selectivity can be tightened."
        ),
    )


def _build_blocked_entry(cond: dict, report: dict) -> BlockedEntry:
    """Build a BlockedEntry for blocked_by_data or blocked_by_permission."""
    cid = cond["condition_id"]
    block_type = cond["classification"]
    source_id = cond.get("source_id", "unknown")

    gates = report.get("gates", [])
    detail = gates[0]["detail"] if gates else report.get("notes", "")

    provider_options: list[str]
    cost_effort: str
    priority: str
    next_action: str

    if cid == "northbound_flow":
        provider_options = ["Tushare: moneyflow_hsgt (confirmed available, FREE tier)"]
        cost_effort = "No cost. 2-3 days to implement signal module."
        priority = "HIGH"
        next_action = "Implement signal module: northbound net flow vs SSE divergence. Data already probed and available."
    elif cid == "etf_inflow_heat":
        provider_options = [
            "Local DuckDB: fund_daily (2,074 ETFs, 2020-2026)",
            "Tushare: fund_basic (ETF metadata)",
        ]
        cost_effort = "No cost. 3-5 days to implement signal module."
        priority = "HIGH"
        next_action = "Implement signal module: aggregate ETF flow heat from fund_daily + fund_basic. Data already available locally."
    elif cid == "fund_issuance":
        provider_options = [
            "AMAC (amac.org.cn): monthly fund industry reports (PDF scraping, ~3-5 days)",
            "Wind/Choice terminal: ¥20k+/yr license",
            "Tushare: fund_basic proxy (found_date trend, partial only)",
        ]
        cost_effort = "3-5 days for AMAC scraping OR ¥20k+/yr for Wind. fund_basic proxy is free but incomplete."
        priority = "MEDIUM"
        next_action = "Option A: Use fund_basic found_date trend as initial proxy (reduced scope but 0 cost). Option B: Scrape AMAC monthly reports for full historical issuance time series."
    elif cid == "liquidity_tightening":
        provider_options = [
            "Tushare: shibor (FREE tier, daily, 2006-present)",
            "Tushare: shibor_lpr (FREE tier, monthly, 2013-present)",
        ]
        cost_effort = "No cost. 2-3 days to implement signal module with effective_date awareness."
        priority = "MEDIUM"
        next_action = "Implement signal module: Shibor 3M-ON spread + LPR trend. CRITICAL: use effective_date (release date), NOT period_date, to avoid look-ahead bias."
    elif cid == "macro_credit":
        provider_options = [
            "Tushare: cn_m (M0/M1/M2, 1990-present, FREE tier)",
            "Tushare: sf_month (社融, 2010-present, FREE tier)",
        ]
        cost_effort = "No cost. 2-3 days to implement signal module with effective_date awareness."
        priority = "MEDIUM"
        next_action = "Implement signal module: M2 growth impulse + 社融 trend. CRITICAL: monthly data has ~10-15 day release lag — forward-fill from effective_date."
    elif cid == "options_iv":
        provider_options = [
            "Tushare: opt_daily (raw prices, no pre-computed IV, FREE tier)",
            "Build IV engine: Black-Scholes inversion (3-5 days work)",
            "No commercial IV feed identified in Tushare FREE tier",
        ]
        cost_effort = "No cost for data. 3-5 days for IV computation engine."
        priority = "LOW"
        next_action = "Build IV computation engine: Black-Scholes inversion using opt_daily raw prices + shibor risk-free rate + SSE close. Requires 3-5 days of focused development."
    elif cid == "investor_accounts":
        provider_options = [
            "CSDC chinaclear.cn PDF scraping (5-10 days, uncertain sustainability)",
            "Wind/Choice terminal: ¥20k+/yr license",
            "No Tushare endpoint. No free structured API.",
        ]
        cost_effort = "5-10 days for scraping OR ¥20k+/yr for Wind. No free option."
        priority = "LOW"
        next_action = "Defer indefinitely. Re-evaluate ONLY if Wind/Choice license becomes available. PDF scraper is fragile and requires ongoing maintenance."
    else:
        provider_options = ["Unknown"]
        cost_effort = "Unknown"
        priority = "LOW"
        next_action = f"Signal module not yet implemented. Pending Wave 2 development."

    return BlockedEntry(
        condition_id=cid,
        condition_name=cond["condition_name"],
        tier=cond["tier"],
        block_type=block_type,
        source_id=source_id,
        blocker_detail=detail,
        provider_options=provider_options,
        estimated_cost_and_effort=cost_effort,
        priority=priority,
        next_action=next_action,
    )


def _build_procurement_list(blocked: list[BlockedEntry]) -> list[ProcurementItem]:
    """Extract data procurement items from blocked entries."""
    items: list[ProcurementItem] = []
    for b in blocked:
        wave = "Wave 2+" if b.priority == "LOW" else "Wave 2"
        items.append(
            ProcurementItem(
                source=b.source_id,
                condition=f"#{b.condition_id.replace('_', ' ').title()}",
                reason_blocked=b.blocker_detail.split(". ")[0] if ". " in b.blocker_detail else b.blocker_detail,
                provider_options=b.provider_options,
                estimated_cost="¥0" if "No cost" in b.estimated_cost_and_effort or "free" in b.estimated_cost_and_effort else b.estimated_cost_and_effort.split(" or ")[0] if " or " in b.estimated_cost_and_effort else b.estimated_cost_and_effort,
                estimated_effort=b.estimated_cost_and_effort.split(". ")[0] if ". " in b.estimated_cost_and_effort else b.estimated_cost_and_effort,
                priority=b.priority,
                wave_target=wave,
            )
        )
    return items


def generate_rejection_report(validation_dir: str | Path) -> RejectionReport:
    """Generate the full rejection/blocking report from per-condition validation data.

    Parameters
    ----------
    validation_dir : str or Path
        Path to the validation directory containing per-condition subdirs
        and ``condition_manifest.json``.

    Returns
    -------
    RejectionReport
        Structured report with rejected, research_only, blocked, and procurement sections.
    """
    vdir = Path(validation_dir)
    manifest = _load_manifest(vdir)
    report_paths = _collect_all_report_paths(vdir)

    rejected_entries: list[RejectedEntry] = []
    research_entries: list[ResearchOnlyEntry] = []
    blocked_entries: list[BlockedEntry] = []

    for cond in manifest["conditions"]:
        cid = cond["condition_id"]
        classification = cond["classification"]

        if classification in ("validated"):
            continue

        # Load per-condition report if available
        rp = report_paths.get(cid)
        if rp is None:
            # Try alternative directory name
            for dir_name, path in report_paths.items():
                if dir_name == cid or cid.replace("_", "_") in dir_name:
                    rp = path
                    break

        report: dict = _load_single_report(rp) if rp and rp.exists() else {}

        if classification == "rejected":
            rejected_entries.append(_build_rejected_entry(cond, report))
        elif classification == "research_only":
            research_entries.append(_build_research_only_entry(cond, report))
        elif classification in ("blocked_by_data", "blocked_by_permission"):
            blocked_entries.append(_build_blocked_entry(cond, report))

    procurement = _build_procurement_list(blocked_entries)

    # Effective date warnings
    eff_warnings = [
        "fin_indicator has ~53-day median ann_date lag — quarterly signals must use ann_date, not end_date",
        "Monthly macro sources (cn_m, sf_month, shibor_lpr) have 10-15 day release lag — forward-fill from effective_date",
        "fund_daily.trade_date is VARCHAR (YYYYMMDD) — always CAST to DATE before joining",
        "17 daily trade_date sources are T+0/T+1 aligned — no look-ahead risk",
    ]

    return RejectionReport(
        meta={
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "framework": "escape-top-microstructure-validation",
            "validation_date": _VALIDATION_DATE,
            "total_conditions": manifest["summary"]["total"],
            "validated": manifest["summary"]["validated"],
        },
        summary={
            "rejected_count": len(rejected_entries),
            "research_only_count": len(research_entries),
            "blocked_by_data_count": sum(1 for b in blocked_entries if b.block_type == "blocked_by_data"),
            "blocked_by_permission_count": sum(1 for b in blocked_entries if b.block_type == "blocked_by_permission"),
            "total_failed": len(rejected_entries) + len(research_entries) + len(blocked_entries),
        },
        rejected=rejected_entries,
        research_only=research_entries,
        blocked=blocked_entries,
        procurement_list=procurement,
        effective_date_warnings=eff_warnings,
    )


# ── Serialization ──────────────────────────────────────────────────────────────


def _report_to_json(report: RejectionReport) -> dict:
    """Convert RejectionReport to JSON-serialisable dict."""
    return {
        "meta": report.meta,
        "summary": report.summary,
        "rejected": [
            {
                "condition_id": r.condition_id,
                "condition_name": r.condition_name,
                "tier": r.tier,
                "composite_dd": r.composite_dd,
                "signal_days_pct": r.signal_days_pct,
                "failed_gates": [
                    {
                        "gate_name": g.gate_name,
                        "detail": g.detail,
                        "interpretation": g.interpretation,
                        "evidence": g.evidence,
                    }
                    for g in r.failed_gates
                ],
                "root_cause": r.root_cause,
                "known_workaround": r.known_workaround,
                "next_action": r.next_action,
            }
            for r in report.rejected
        ],
        "research_only": [
            {
                "condition_id": ro.condition_id,
                "condition_name": ro.condition_name,
                "tier": ro.tier,
                "composite_dd": ro.composite_dd,
                "signal_days_pct": ro.signal_days_pct,
                "why_not_production": ro.why_not_production,
                "what_data_would_help": ro.what_data_would_help,
                "next_action": ro.next_action,
            }
            for ro in report.research_only
        ],
        "blocked": [
            {
                "condition_id": b.condition_id,
                "condition_name": b.condition_name,
                "tier": b.tier,
                "block_type": b.block_type,
                "source_id": b.source_id,
                "blocker_detail": b.blocker_detail,
                "provider_options": b.provider_options,
                "estimated_cost_and_effort": b.estimated_cost_and_effort,
                "priority": b.priority,
                "next_action": b.next_action,
            }
            for b in report.blocked
        ],
        "procurement_list": [
            {
                "source": p.source,
                "condition": p.condition,
                "reason_blocked": p.reason_blocked,
                "provider_options": p.provider_options,
                "estimated_cost": p.estimated_cost,
                "estimated_effort": p.estimated_effort,
                "priority": p.priority,
                "wave_target": p.wave_target,
            }
            for p in report.procurement_list
        ],
        "effective_date_warnings": report.effective_date_warnings,
    }


def _report_to_markdown(report: RejectionReport) -> str:
    """Convert RejectionReport to Markdown string."""
    lines: list[str] = []

    # ── Header ──
    lines.append("# Escape-Top Microstructure: Rejection & Blocking Report")
    lines.append("")
    lines.append(
        f"**Generated**: {report.meta['generated_at']}  "
        f"**Framework**: {report.meta['framework']}  "
        f"**Validation Date**: {report.meta['validation_date']}"
    )
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(
        f"Of {report.meta['total_conditions']} candidate conditions evaluated, "
        f"{report.meta['validated']} passed all validation gates, and "
        f"{report.summary['total_failed']} did not. "
        f"Every failed condition is documented below with root cause, known "
        f"workarounds (where applicable), and a concrete next action."
    )
    lines.append("")
    lines.append(
        f"| Classification | Count |"
    )
    lines.append("|---|---|")
    lines.append(f"| REJECTED | {report.summary['rejected_count']} |")
    lines.append(f"| RESEARCH_ONLY | {report.summary['research_only_count']} |")
    lines.append(f"| BLOCKED_BY_DATA | {report.summary['blocked_by_data_count']} |")
    lines.append(f"| BLOCKED_BY_PERMISSION | {report.summary['blocked_by_permission_count']} |")
    lines.append(f"| **Total Non-Validated** | **{report.summary['total_failed']}** |")
    lines.append("")

    # ── Part 1: REJECTED ──
    lines.append("---")
    lines.append("")
    lines.append("## Part 1: REJECTED Conditions (6)")
    lines.append("")
    lines.append(
        "These conditions failed one or more hard-fail validation gates "
        "(direction or separation) and should NOT be used in production "
        "as standalone escape-top signals. Some may have utility in gated "
        "joint conditions."
    )
    lines.append("")
    lines.append("| # | Condition | Tier | Composite DD | Signal % | Primary Failure |")
    lines.append("|---|---:|---:|---:|---|")
    for i, r in enumerate(report.rejected, start=1):
        primary_gate = r.failed_gates[0].gate_name if r.failed_gates else "unknown"
        lines.append(
            f"| {i} | {r.condition_name} | {r.tier} | {r.composite_dd:+.4f} | {r.signal_days_pct:.2f}% | {primary_gate} |"
        )
    lines.append("")

    for i, r in enumerate(report.rejected, start=1):
        cid_display = r.condition_id.replace("_", " ").title()
        lines.append(f"### {i}. {r.condition_name} (`{r.condition_id}`)")
        lines.append("")
        lines.append(f"**Tier**: `{r.tier}` | **Composite DD**: {r.composite_dd:+.4f} | **Signal Rate**: {r.signal_days_pct:.2f}%")
        lines.append("")

        if r.failed_gates:
            lines.append("#### Failed Gates")
            lines.append("")
            for g in r.failed_gates:
                lines.append(f"**Gate `{g.gate_name}`**: FAILED")
                lines.append(f"> {g.detail}")
                lines.append("")
                lines.append(g.interpretation)
                lines.append("")
                if g.evidence:
                    lines.append("Evidence:")
                    lines.append("```json")
                    lines.append(json.dumps(g.evidence, indent=2, ensure_ascii=False))
                    lines.append("```")
                    lines.append("")

        lines.append(f"#### Root Cause")
        lines.append(f"{r.root_cause}")
        lines.append("")

        if r.known_workaround:
            lines.append(f"#### Known Workaround")
            lines.append(f"{r.known_workaround}")
            lines.append("")

        lines.append(f"#### Recommended Next Action")
        lines.append(f"{r.next_action}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── Part 2: RESEARCH_ONLY ──
    lines.append("## Part 2: RESEARCH_ONLY Condition (1)")
    lines.append("")
    lines.append(
        "These conditions show directional merit but fail coverage or selectivity "
        "gates that prevent production use. They are candidates for further "
        "research or re-evaluation after signal refinement."
    )
    lines.append("")

    for ro in report.research_only:
        lines.append(f"### {ro.condition_name} (`{ro.condition_id}`)")
        lines.append("")
        lines.append(f"**Tier**: `{ro.tier}` | **Composite DD**: {ro.composite_dd:+.4f} | **Signal Rate**: {ro.signal_days_pct:.2f}%")
        lines.append("")

        lines.append(f"#### Why Not Production-Ready")
        lines.append(f"{ro.why_not_production}")
        lines.append("")

        lines.append(f"#### What Would Help")
        lines.append(f"{ro.what_data_would_help}")
        lines.append("")

        lines.append(f"#### Recommended Next Action")
        lines.append(f"{ro.next_action}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── Part 3: BLOCKED ──
    lines.append("## Part 3: BLOCKED Conditions (7)")
    lines.append("")
    lines.append(
        "These conditions cannot be validated because their data sources or "
        "signal modules are not yet available. Each requires a specific "
        "resolution before re-evaluation."
    )
    lines.append("")
    lines.append("| # | Condition | Tier | Block Type | Source | Priority |")
    lines.append("|---|---|---|---|---|")
    for i, b in enumerate(report.blocked, start=1):
        lines.append(f"| {i} | {b.condition_name} | {b.tier} | {b.block_type} | {b.source_id} | {b.priority} |")
    lines.append("")

    for i, b in enumerate(report.blocked, start=1):
        lines.append(f"### {i}. {b.condition_name} (`{b.condition_id}`)")
        lines.append("")
        lines.append(f"**Tier**: `{b.tier}` | **Block Type**: `{b.block_type}` | **Priority**: `{b.priority}`")
        lines.append("")

        lines.append(f"#### Blocker")
        lines.append(f"{b.blocker_detail}")
        lines.append("")

        lines.append(f"#### Provider Options")
        for opt in b.provider_options:
            lines.append(f"- {opt}")
        lines.append("")

        lines.append(f"#### Estimated Cost & Effort")
        lines.append(f"{b.estimated_cost_and_effort}")
        lines.append("")

        lines.append(f"#### Recommended Next Action")
        lines.append(f"{b.next_action}")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ── Part 4: Data Procurement List ──
    lines.append("## Part 4: Data Procurement List")
    lines.append("")
    lines.append(
        "The following table lists every data source that must be acquired or "
        "built before its associated condition can be validated. Items are "
        "sorted by priority (HIGH → MEDIUM → LOW)."
    )
    lines.append("")
    lines.append("| Priority | Source | Condition | Reason | Providers | Cost | Effort | Target |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in report.procurement_list:
        lines.append(
            f"| {p.priority} | {p.source} | {p.condition} | {p.reason_blocked[:80]}... | "
            f"{p.provider_options[0][:50]}... | {p.estimated_cost} | {p.estimated_effort} | {p.wave_target} |"
        )
    lines.append("")

    # ── Part 5: Effective Date Warnings ──
    lines.append("## Part 5: Effective Date Warnings")
    lines.append("")
    lines.append(
        "The effective-date audit identified 4 sources with look-ahead risk. "
        "These warnings apply to ALL conditions using these data sources "
        "(including the 5 blocked-by-data P1 conditions that will be built "
        "in Wave 2)."
    )
    lines.append("")
    for w in report.effective_date_warnings:
        lines.append(f"- {w}")
    lines.append("")
    lines.append("For details, see `tmp/microstructure/validation/effective_date_audit.md`.")
    lines.append("")

    # ── Part 6: Cross-Cutting Observations ──
    lines.append("## Part 6: Cross-Cutting Observations")
    lines.append("")
    lines.append("### Common Failure Pattern: 120-Day Horizon Direction Reversal")
    lines.append("")
    lines.append(
        "A striking pattern across rejected conditions: **5 of 6 fail direction "
        "at the 120-day horizon**. Short-term signals (20d, 60d) are often "
        "directional correct, but at 120d the relationship weakens or inverts. "
        "This suggests that microstructure crowding signals are primarily "
        "**short-term contrarian indicators** (1-3 months), not medium-term "
        "top-prediction tools."
    )
    lines.append("")
    lines.append(
        "The two validated conditions (margin_divergence and volatility_atr_expansion) "
        "both maintain direction at 120d, which is what makes them robust. "
        "This is the key differentiator between validated and rejected conditions."
    )
    lines.append("")

    lines.append("### The Wrong-Direction Signals")
    lines.append("")
    lines.append(
        "Breadth divergence, turnover-to-marketcap heat, and sector turnover crowding "
        "ALL show signifcantly LESS drawdown on signal days than non-signal days. "
        "These are NOT broken signals — they are **momentum/continuation signals** "
        "wearing escape-top clothing. At extreme market conditions, concentrated "
        "rallies tend to continue, not reverse."
    )
    lines.append("")

    lines.append("### Volatility is the King")
    lines.append("")
    lines.append(
        "The volatility/ATR expansion condition has the strongest composite DD "
        "(-0.0915) and direction-OK at all horizons. It also has strong "
        "statistical separation (p<0.0001 at all horizons) and sub-period "
        "stability. This is the single strongest escape-top candidate."
    )
    lines.append("")

    lines.append("### Concentration Needs Margin Divergence")
    lines.append("")
    lines.append(
        "Concentration fails standalone but was validated in the ORIGINAL "
        "baseline grid search as a JOINT condition with margin divergence "
        "(AND gate, concentration ≥ 0.50 AND margin_divergence hit). "
        "This pairwise combination already exists in ESCAPE_TOP_PRESETS "
        "and produced composite DD of -0.0591. The standalone rejection "
        "confirms that the joint AND is necessary, not optional."
    )
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Report generated {report.meta['generated_at']} by `scripts/microstructure/rejection_report.py`*")
    lines.append("")

    return "\n".join(lines)


def write_reports(
    validation_dir: str | Path,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    """Generate rejection report and write both .md and .json files.

    Parameters
    ----------
    validation_dir : str or Path
        Path to validation directory with per-condition reports.
    output_dir : str or Path
        Directory to write ``rejection_report.md`` and ``rejection_report.json``.

    Returns
    -------
    tuple[Path, Path]
        Paths to the written (md_file, json_file).
    """
    vdir = Path(validation_dir)
    odir = Path(output_dir)

    report = generate_rejection_report(vdir)

    # Write JSON
    json_path = odir / "rejection_report.json"
    json_data = _report_to_json(report)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)

    # Write Markdown
    md_path = odir / "rejection_report.md"
    md_text = _report_to_markdown(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    return md_path, json_path


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate rejection/blocking report for escape-top validation."
    )
    parser.add_argument(
        "--validation-dir",
        default="tmp/microstructure/validation",
        help="Directory containing per-condition validator reports (default: tmp/microstructure/validation)",
    )
    parser.add_argument(
        "--output-dir",
        default="tmp/microstructure/validation",
        help="Output directory for rejection_report.md and .json (default: same as validation-dir)",
    )
    args = parser.parse_args()

    md_path, json_path = write_reports(args.validation_dir, args.output_dir)
    print(f"Rejection report written to:")
    print(f"  Markdown: {md_path}")
    print(f"  JSON:     {json_path}")


if __name__ == "__main__":
    _cli_main()