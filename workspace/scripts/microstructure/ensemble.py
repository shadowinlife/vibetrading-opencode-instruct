"""
Ensemble module for multi-condition escape-top warning resolution.

Extends the original 2-signal ``AND`` joint mode with two production-safe
modes — ``VOTE_K_OF_M`` and ``WEIGHTED_SCORE`` — while preserving the
legacy ``AND`` as the baseline for backward compatibility.

**Active-window semantics** are provided via :func:`expand_active_window`,
which forward-fills boolean signals for a configurable number of trading
days.  This is the only correct way to handle slow monthly/macro conditions
(e.g. M2 / social-financing / LPR) whose `effective_date` lags the
`period_date` by 10–15 calendar days.

**Constraints** inherited from the escape-top validation framework:

* Only `VALIDATED` conditions are eligible for promotion to production
  presets (currently #2 *margin_divergence* and #5
  *volatility_atr_expansion*).
* ``RESEARCH_ONLY`` conditions (e.g. #8 *large_order_exhaustion*) are
  **not** included by default — they must be explicitly opted-in.
* No ML / logistic-regression weights — all weights are human-configured
  and transparent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# ── Public type aliases ───────────────────────────────────────────────────────

EnsembleMode = Literal["AND", "VOTE_K_OF_M", "WEIGHTED_SCORE"]
WarningLevel = Literal["RED", "YELLOW", "GREEN"]


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConditionResult:
    """The contribution of one escape-top condition on a given evaluation day.

    Parameters
    ----------
    condition_id:
        Stable identifier (e.g. ``"margin_divergence"``).
    hit:
        Whether the condition fired a boolean signal on this day.
    score:
        Optional continuous score in [0.0, 1.0].  Used by
        ``WEIGHTED_SCORE`` mode.  When ``None``, ``hit`` is converted to
        1.0 (True) or 0.0 (False) at resolution time.
    """

    condition_id: str
    hit: bool
    score: float | None = None

    def __post_init__(self) -> None:
        if self.score is not None and not (0.0 <= self.score <= 1.0):
            raise ValueError(
                f"ConditionResult.score must be in [0.0, 1.0], got {self.score!r}"
            )

    @property
    def effective_score(self) -> float:
        """Continuous score, derived from ``hit`` when ``score`` is ``None``."""
        if self.score is not None:
            return float(self.score)
        return 1.0 if self.hit else 0.0


@dataclass(frozen=True)
class EnsembleConfig:
    """Configuration for ensemble warning resolution.

    Only the parameters relevant to ``mode`` need to be supplied; the
    others default to sensible placeholders.

    Parameters
    ----------
    mode:
        Resolution mode (``AND``, ``VOTE_K_OF_M``, ``WEIGHTED_SCORE``).
    k_yellow:
        Minimum number of firing conditions for YELLOW in K_OF_M mode.
        Default: ``ceil(M / 3)`` computed at resolution time.
    k_red:
        Minimum number of firing conditions for RED in K_OF_M mode.
        Default: ``ceil(2 * M / 3)`` computed at resolution time.
    weights:
        Per-condition weight mapping (condition_id → float > 0).
        Default: equal weights (1 / M each).
    red_threshold:
        Weighted-score threshold for RED.  Default: ``0.7``.
    yellow_threshold:
        Weighted-score threshold for YELLOW.  Default: ``0.3``.
    """

    mode: EnsembleMode
    k_yellow: int | None = None
    k_red: int | None = None
    weights: dict[str, float] | None = None
    red_threshold: float | None = None
    yellow_threshold: float | None = None


# ── Public API ────────────────────────────────────────────────────────────────


def resolve_ensemble(
    condition_results: list[ConditionResult],
    config: EnsembleConfig,
) -> WarningLevel:
    """Resolve a set of condition results into a single warning level.

    This is the main entry point for the ensemble module.  It dispatches to
    the appropriate internal resolver based on ``config.mode``.

    Parameters
    ----------
    condition_results:
        One ``ConditionResult`` per active condition for the evaluation day.
        May be empty for ``WEIGHTED_SCORE`` mode (returns ``"GREEN"``).
    config:
        Resolution configuration.

    Returns
    -------
    WarningLevel
        ``"RED"``, ``"YELLOW"``, or ``"GREEN"``.

    Raises
    ------
    ValueError
        If ``config.mode`` is ``"AND"`` or ``"VOTE_K_OF_M"`` and
        ``condition_results`` is empty, or if K > M in K_OF_M mode.

    Examples
    --------
    Legacy AND mode (backward-compatible with ``escape_top._resolve_warning``):

    >>> from scripts.microstructure.ensemble import resolve_ensemble, ConditionResult, EnsembleConfig
    >>> results = [
    ...     ConditionResult("concentration", hit=True),
    ...     ConditionResult("margin_divergence", hit=True),
    ... ]
    >>> config = EnsembleConfig(mode="AND")
    >>> resolve_ensemble(results, config)
    'RED'
    """
    m = len(condition_results)

    if config.mode == "AND":
        return _resolve_and(condition_results, m)
    if config.mode == "VOTE_K_OF_M":
        return _resolve_vote_k_of_m(condition_results, m, config)
    if config.mode == "WEIGHTED_SCORE":
        return _resolve_weighted_score(condition_results, m, config)

    # Should be unreachable due to Literal typing.
    raise ValueError(f"Unsupported ensemble mode: {config.mode!r}")


def expand_active_window(
    hit_series: list[bool],
    window_trading_days: int,
) -> list[bool]:
    """Forward-fill ``True`` for *window_trading_days* after each trigger.

    This is the canonical helper for **slow macro / monthly conditions**
    whose effective date lags the period date.  The expanded series can
    then be passed to ``resolve_ensemble`` as a regular condition result.

    Parameters
    ----------
    hit_series:
        Daily boolean signal series (aligned to trading days).
    window_trading_days:
        Number of **trading days** the signal should remain active after
        a trigger, inclusive of the trigger day.

    Returns
    -------
    list[bool]
        Same length as ``hit_series``, with ``True`` forward-filled for
        ``window_trading_days`` after each ``True`` in the input.

    Examples
    --------
    >>> expand_active_window([True, False, False, False, False], 3)
    [True, True, True, False, False]

    >>> expand_active_window([False, False, True, False, False], 2)
    [False, False, True, True, False]

    Multiple triggers within the window extend the active period:

    >>> expand_active_window([True, False, False, True, False], 3)
    [True, True, True, True, True]
    """
    if window_trading_days <= 0:
        raise ValueError(
            f"window_trading_days must be >= 1, got {window_trading_days}"
        )
    if not hit_series:
        return []

    n = len(hit_series)
    result = [False] * n
    active_until = -1  # index through which the signal is active

    for i in range(n):
        if hit_series[i]:
            # Extend the active window.
            active_until = max(active_until, i + window_trading_days - 1)
        if i <= active_until:
            result[i] = True

    return result


# ── Private resolvers ─────────────────────────────────────────────────────────


def _resolve_and(
    results: list[ConditionResult],
    m: int,
) -> WarningLevel:
    """AND mode — RED when **all** conditions fire, YELLOW when any fire.

    With exactly *m == 2* this matches the legacy ``_resolve_warning``
    behaviour in :mod:`escape_top`.
    """
    if m == 0:
        raise ValueError("AND mode requires at least 1 condition, got 0")

    hits = sum(1 for r in results if r.hit)
    if hits == m:
        return "RED"
    if hits > 0:
        return "YELLOW"
    return "GREEN"


def _resolve_vote_k_of_m(
    results: list[ConditionResult],
    m: int,
    config: EnsembleConfig,
) -> WarningLevel:
    """K-of-M voting mode.

    * YELLOW when ``hits >= k_yellow``
    * RED    when ``hits >= k_red``
    * GREEN  otherwise

    Default thresholds when not explicitly configured:
    ``k_yellow = ceil(m / 3)``, ``k_red = ceil(2 * m / 3)``.
    """
    if m == 0:
        raise ValueError(
            "VOTE_K_OF_M mode requires at least 1 condition, got 0"
        )

    k_yellow = config.k_yellow
    k_red = config.k_red

    if k_yellow is None:
        k_yellow = _ceil_div(m, 3)
    if k_red is None:
        k_red = _ceil_div(2 * m, 3)

    if k_yellow < 1:
        raise ValueError(f"k_yellow must be >= 1, got {k_yellow}")
    if k_red < k_yellow:
        raise ValueError(
            f"k_red ({k_red}) must be >= k_yellow ({k_yellow})"
        )
    if k_red > m:
        raise ValueError(
            f"k_red ({k_red}) must be <= m ({m})"
        )

    hits = sum(1 for r in results if r.hit)
    if hits >= k_red:
        return "RED"
    if hits >= k_yellow:
        return "YELLOW"
    return "GREEN"


def _resolve_weighted_score(
    results: list[ConditionResult],
    m: int,
    config: EnsembleConfig,
) -> WarningLevel:
    """Weighted-score mode — sum of weighted scores vs thresholds.

    When ``m == 0``, returns ``"GREEN"`` (no conditions = no signal).
    """
    red_threshold = config.red_threshold or 0.7
    yellow_threshold = config.yellow_threshold or 0.3

    if not (0.0 <= yellow_threshold < red_threshold <= 1.0):
        raise ValueError(
            f"Thresholds must satisfy 0 <= yellow_threshold ({yellow_threshold})"
            f" < red_threshold ({red_threshold}) <= 1"
        )

    if m == 0:
        return "GREEN"

    # Determine weights — equal weight when not provided.
    if config.weights is None:
        equal_w = 1.0 / m
        weight_series = [equal_w] * m
    else:
        weight_series = _resolve_weights(results, config.weights)

    # Validate score range.
    for r in results:
        if r.score is not None and not (0.0 <= r.score <= 1.0):
            raise ValueError(
                f"ConditionResult.score must be in [0.0, 1.0],"
                f" got {r.score!r} for {r.condition_id!r}"
            )

    score_sum = sum(
        w * r.effective_score for w, r in zip(weight_series, results)
    )

    if score_sum >= red_threshold:
        return "RED"
    if score_sum >= yellow_threshold:
        return "YELLOW"
    return "GREEN"


# ── Private utilities ─────────────────────────────────────────────────────────


def _ceil_div(a: int, b: int) -> int:
    """Integer ceiling division: ``ceil(a / b)``."""
    return (a + b - 1) // b


def _resolve_weights(
    results: list[ConditionResult],
    weight_map: dict[str, float],
) -> list[float]:
    """Resolve per-condition weights from a ``condition_id → weight`` map.

    Every ``condition_id`` in ``results`` must appear in ``weight_map``.
    Raises ``ValueError`` if any condition is missing or any weight is
    non-positive.
    """
    weights: list[float] = []
    for r in results:
        w = weight_map.get(r.condition_id)
        if w is None:
            raise ValueError(
                f"Weight missing for condition {r.condition_id!r};"
                f" available keys: {sorted(weight_map)}"
            )
        if w <= 0:
            raise ValueError(
                f"Weight for {r.condition_id!r} must be > 0, got {w}"
            )
        weights.append(w)
    return weights