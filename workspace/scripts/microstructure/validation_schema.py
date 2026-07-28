"""
Canonical condition validation schema for the escape-top microstructure framework.

Encodes validation-gate decisions (coverage, selectivity, directional correctness,
statistical separation, robustness, sub-period stability, correlation) and
classification levels that downstream consumers use to decide whether a signal
candidate can be promoted to ``ESCAPE_TOP_PRESETS``.

This module defines the data model ONLY — no validation logic, no DuckDB, no
external APIs.  It serves as the contract that candidate-evaluation scripts
must satisfy before a condition is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


# ── Classification enum ───────────────────────────────────────────────────────


class Classification(Enum):
    """Terminal status of a candidate condition after all gates are evaluated.

    ``VALIDATED`` and ``REJECTED`` are final; ``RESEARCH_ONLY`` means the
    condition has merit but insufficient data/coverage to be promoted to
    production.  ``BLOCKED_BY_DATA`` / ``BLOCKED_BY_PERMISSION`` require a
    human to resolve an external dependency before re-evaluation.
    """

    VALIDATED = "validated"
    """All gates passed — candidate is eligible for preset promotion."""

    REJECTED = "rejected"
    """One or more hard-fail gates — candidate should NOT be used."""

    RESEARCH_ONLY = "research_only"
    """Condition is directionally interesting but fails coverage/selectivity
    gates that prevent production use (e.g. < 5 years of data)."""

    BLOCKED_BY_DATA = "blocked_by_data"
    """Data source is unavailable or coverage is insufficient for gate
    evaluation.  Human must acquire the data before re-evaluation."""

    BLOCKED_BY_PERMISSION = "blocked_by_permission"
    """Data source exists but the current environment lacks credentials or
    license.  Human must grant access before re-evaluation."""


# ── Exceptions ────────────────────────────────────────────────────────────────


class ValidationError(ValueError):
    """Schema-level validation error (not a gate failure).

    Raised when required metadata is missing or malformed — e.g. an external
    source without an ``effective_date``.  This is a **pre-condition** error,
    distinct from a gate deciding ``REJECTED``.
    """


# ── Forward drawdown horizon metrics ──────────────────────────────────────────


@dataclass(frozen=True)
class HorizonMetrics:
    """Forward drawdown statistics for a single horizon.

    Horizons follow the convention in :mod:`tune_escape_top`:
    20, 60, and 120 trading days.
    """

    horizon_days: int
    """Look-ahead window in trading days (20, 60, or 120)."""

    mean_fwd_dd: float
    """Mean forward drawdown over this horizon across ``signal_days``.
    Negative values indicate the market declined after the signal fired."""

    p_value: float | None
    """Welch's *t*-test (or Mann-Whitney *U*) p-value for the null that
    signal-day DD mean equals non-signal-day DD mean.  ``None`` when sample
    size is insufficient for statistical testing."""

    direction_ok: bool
    """``True`` when signal-day forward DD is **more negative** than
    non-signal-day DD — the expected directional relationship for an
    escape-top signal."""


# ── Source metadata (with effective_date enforcement) ─────────────────────────


@dataclass
class SourceMetadata:
    """Provenance metadata for the data underlying a candidate condition.

    **Invariant**: ``is_external=True`` requires ``effective_date``.
    """

    source_id: str
    """Stable identifier for the data source (e.g. ``"tushare:moneyloan"``,
    ``"custom:csi800_concentration"``)."""

    is_external: bool = False
    """``True`` when the source is NOT in the local DuckDB snapshot and may
    drift independent of the sync pipeline."""

    effective_date: str | None = None
    """Most recent date this snapshot is current-through (``"YYYY-MM-DD"``).
    **Required** when ``is_external`` is ``True``."""

    def __post_init__(self) -> None:
        if self.is_external and self.effective_date is None:
            raise ValidationError(
                f"External source '{self.source_id}' requires effective_date; "
                "set effective_date='YYYY-MM-DD' to indicate data freshness"
            )


# ── Individual validation gate ────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationGate:
    """Result of a single validation gate.

    Seven gates are defined in the escape-top validation framework
    (see ``GATE_NAMES``).  Each gate produces a boolean pass/fail plus
    structured detail.
    """

    name: str
    """Gate identifier — one of ``GATE_NAMES``."""

    passed: bool
    """``True`` if the candidate satisfied this gate."""

    detail: str
    """Human-readable explanation of the gate's decision."""

    evidence: dict[str, Any] = field(default_factory=dict)
    """Machine-readable values backing the decision (e.g. ``{"p_value": 0.012}``,
    ``{"coverage_years": 7.2}``)."""


GATE_NAMES: tuple[str, ...] = (
    "coverage",
    "selectivity",
    "direction",
    "separation",
    "robustness",
    "sub_period",
    "correlation",
)
"""Canonical validation gates in evaluation order."""


# ── Condition metadata (aggregate) ────────────────────────────────────────────


@dataclass(frozen=True)
class ConditionMetadata:
    """Aggregate quantitative metadata for a single candidate condition.

    This is the canonical record that accompanies a **validated** condition
    into the preset registry.  It is a flat, JSON-serialisable summary of
    every gate's evidence, suitable for diffing and audit.
    """

    condition_id: str
    """Stable identifier for this signal condition (e.g. ``"margin_buy_pct"``,
    ``"concentration_top5"``)."""

    source_id: str
    """Provenance reference — maps to ``SourceMetadata.source_id``."""

    coverage_years: float
    """Total span of trading data in years (e.g. 7.2)."""

    signal_days_pct: float
    """Percentage of eligible trading days where the condition fires a signal.
    Must be between 0.5 % and 25 % for a ``VALIDATED`` classification."""

    horizon_metrics: list[HorizonMetrics]
    """Forward drawdown metrics for each horizon (20d, 60d, 120d)."""

    robustness_delta: float
    """Worst degradation in ``mean_fwd_dd`` when the threshold is perturbed
    by ±10 %.  Must be ≤ 0.02 for a ``VALIDATED`` classification."""

    sub_period_result: str
    """One of ``"same_sign"``, ``"opposite_sign"``, ``"insufficient_data"``.
    Describes whether pre-2019 and post-2019 sub-periods agree on the
    directional relationship."""

    correlation_flags: list[str]
    """Flag entries for conditions whose correlation with an already-accepted
    signal exceeds the 0.75 threshold.  Format:
    ``"high_corr:<other_condition_id>:<pearson_r>"``."""

    classification: Classification
    """Terminal status assigned by the gate evaluator."""

    human_action_required: bool
    """``True`` when a human must make a decision before this condition can
    move forward — e.g. data purchase, permission grant, or override of a
    hard-fail gate."""


# ── Aggregated validation report ──────────────────────────────────────────────


@dataclass
class ValidationReport:
    """Top-level validation report for a single candidate condition.

    Aggregates individual ``ValidationGate`` results and produces a final
    ``Classification`` with convenience query methods.
    """

    condition_id: str
    """Stable identifier for the condition under evaluation."""

    gates: list[ValidationGate]
    """Per-gate results, one entry per ``GATE_NAME``."""

    classification: Classification
    """Final classification assigned by the gate evaluator."""

    human_action_required: bool = False
    """``True`` when a human decision is required for this condition."""

    condition_metadata: ConditionMetadata | None = None
    """Aggregate metadata — populated when ``classification`` is
    ``VALIDATED`` or ``RESEARCH_ONLY``, optional otherwise."""

    # ── convenience query methods ─────────────────────────────────────────

    def is_validated(self) -> bool:
        """``True`` when all gates passed and the condition is eligible for
        preset promotion."""
        return self.classification == Classification.VALIDATED

    def is_blocked(self) -> bool:
        """``True`` when an external dependency (data or permission) prevents
        re-evaluation."""
        return self.classification in (
            Classification.BLOCKED_BY_DATA,
            Classification.BLOCKED_BY_PERMISSION,
        )

    def needs_human_review(self) -> bool:
        """``True`` when a human must review the result before the condition
        can enter production — either because ``human_action_required`` is
        set, or because the classification is non-final (``RESEARCH_ONLY``,
        ``REJECTED``)."""
        if self.human_action_required:
            return True
        return self.classification in (
            Classification.RESEARCH_ONLY,
            Classification.REJECTED,
        )


# ── Schema helpers ────────────────────────────────────────────────────────────


def make_horizon(
    horizon_days: int,
    mean_fwd_dd: float,
    p_value: float | None,
    direction_ok: bool,
) -> HorizonMetrics:
    """Factory for ``HorizonMetrics`` — convenient for tests.

    >>> h = make_horizon(20, -0.031, 0.008, True)
    >>> h.horizon_days
    20
    """
    return HorizonMetrics(
        horizon_days=horizon_days,
        mean_fwd_dd=mean_fwd_dd,
        p_value=p_value,
        direction_ok=direction_ok,
    )


def make_source(
    source_id: str,
    *,
    is_external: bool = False,
    effective_date: str | None = None,
) -> SourceMetadata:
    """Factory for ``SourceMetadata`` — raises ``ValidationError`` on
    external sources missing ``effective_date``.

    >>> src = make_source("tushare:moneyloan", is_external=True, effective_date="2025-05-28")
    >>> src.is_external
    True
    """
    return SourceMetadata(
        source_id=source_id,
        is_external=is_external,
        effective_date=effective_date,
    )