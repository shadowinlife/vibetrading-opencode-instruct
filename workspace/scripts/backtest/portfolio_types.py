"""Portfolio-level dataclasses for reusable experiment configuration and result payloads.

Additive module — separate from the single-stock ``StrategyConfig`` in ``config.py``.
v1 scope: A-share, long-only, equal-weight, fixed-interval rebalance.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class PortfolioConfig:
    """Portfolio experiment configuration — independent of single-stock StrategyConfig.

    Fields cover the v1 workflow: universe → validation → data → signal →
    selection → simulation → metrics.
    """

    name: str
    universe_name: str
    start_date: str
    end_date: str
    rebalance_freq: int
    max_positions: int

    # v1 fixed / optional knobs
    weighting_method: str = "equal_weight"
    benchmark_code: str | None = None
    signal_builder_ref: str | None = None
    signal_col: str = "signal"
    signal_higher_better: bool = True
    validator_settings: dict[str, Any] = field(default_factory=dict)

    # v1 fixed costs (aligned with single-stock engine)
    one_way_cost: float = 0.0015

    def to_dict(self) -> dict[str, Any]:
        """Serialize config to JSON-safe dict (strings, numbers, lists, dicts)."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Result payloads
# ---------------------------------------------------------------------------

@dataclass
class NavSummary:
    """Portfolio NAV summary statistics."""

    start_date: str
    end_date: str
    initial_nav: float
    final_nav: float
    total_return: float
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    calmar_ratio: float

    # optional daily NAV series — each entry is {"date": "YYYY-MM-DD", "nav": float}
    daily_nav: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RebalanceLogEntry:
    """One rebalance event: which stocks were selected, with what weights, at what cost."""

    rebalance_date: str
    selected_codes: list[str]
    weights: list[float]
    turnover: float
    cost: float
    cash_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SelectionLogEntry:
    """Diagnostic record of a single selection event — shows eligible, selected, excluded."""

    rebalance_date: str
    eligible_count: int
    selected_count: int
    excluded_reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationSummary:
    """Result of the prerequisite validation pass."""

    passed: bool
    total_codes: int
    passed_codes: int
    failed_codes: int
    alpha158_missing: list[str] = field(default_factory=list)
    hfq_missing: list[str] = field(default_factory=list)
    universe_missing: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return not self.passed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioResult:
    """Complete result payload for a portfolio experiment run.

    Provides all artifacts needed downstream: config, NAV curve, logs, validation.
    All fields are JSON-serializable via ``to_dict()``.
    """

    config: dict[str, Any]
    nav_summary: NavSummary | None = None
    rebalance_log: list[RebalanceLogEntry] = field(default_factory=list)
    selection_log: list[SelectionLogEntry] = field(default_factory=list)
    validation: ValidationSummary | None = None
    benchmark_summary: NavSummary | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize entire result to a JSON-safe dict."""
        return {
            "config": self.config,
            "nav_summary": self.nav_summary.to_dict() if self.nav_summary else None,
            "rebalance_log": [e.to_dict() for e in self.rebalance_log],
            "selection_log": [e.to_dict() for e in self.selection_log],
            "validation": self.validation.to_dict() if self.validation else None,
            "benchmark_summary": (
                self.benchmark_summary.to_dict() if self.benchmark_summary else None
            ),
            "metadata": self.metadata,
        }