"""GEPA-style failure attribution analyzer.

Classifies trajectory failures into instruction-level attributions so that
the memory system can propose targeted fixes to AGENTS.md / skill docs rather
than generic "be more careful" advice.

Pipeline:
    trajectories → detect_failure() → _classify_failure() → Attribution list
    Attribution list → aggregate_attributions() → sorted, deduplicated list

No LLM calls.  All classification is pattern-based.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .reflexion import FailureInfo, detect_failure

# ---------------------------------------------------------------------------
# Attribution result
# ---------------------------------------------------------------------------


@dataclass
class Attribution:
    """A single failure attributed to a specific instruction gap."""

    instruction_ref: str  # e.g., "data_source_priority" or "missing:announcement_check"
    failure_type: str  # "wrong" | "missing" | "ambiguous"
    suggested_fix: str
    evidence_ids: list = field(default_factory=list)
    confidence: float = 0.5


# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

# Each category: (instruction_ref, patterns, suggested_fix_template)
# Patterns are matched against the combined text of failure evidence +
# failure description + surrounding step content.

_CATEGORY_DEFS: list[tuple[str, list[re.Pattern], str]] = [
    (
        "data_source_priority",
        [
            re.compile(r"duckdb|local\s+(?:db|database|数据)", re.I),
            re.compile(r"tushare|akshare|yfinance", re.I),
            re.compile(r"data\s+source|数据源", re.I),
            re.compile(r"table\s+\S+\s+not\s+found", re.I),
            re.compile(r"no\s+data\s+available", re.I),
            re.compile(r"API\s+(?:rate\s+)?limit", re.I),
            re.compile(r"数据缺失|数据不可用", re.I),
            re.compile(r"source\s*(?:priority|selection|选择)", re.I),
        ],
        "Add explicit data source priority rule: DuckDB → Tushare/akshare → yfinance. "
        "Check local data availability before external API calls.",
    ),
    (
        "missing:announcement_check",
        [
            re.compile(r"announce(?:ment)?|公告", re.I),
            re.compile(r"disclosure|披露", re.I),
            re.compile(r"停牌|susp(?:end|ension)", re.I),
            re.compile(r"复牌|resum(?:e|ption)", re.I),
            re.compile(r"分红|dividend", re.I),
            re.compile(r"配股|rights?\s+issue", re.I),
            re.compile(r"重大(?:事项|事件)|material\s+event", re.I),
        ],
        "Add mandatory announcement/disclosure check before analysis. "
        "Query recent announcements from DuckDB or Tushare before proceeding.",
    ),
    (
        "backtest_config",
        [
            re.compile(r"backtest|回测", re.I),
            re.compile(r"walk[\s-]?forward", re.I),
            re.compile(r"sharpe|夏普", re.I),
            re.compile(r"drawdown|回撤", re.I),
            re.compile(r"win\s*rate|胜率", re.I),
            re.compile(r"StrategyConfig", re.I),
            re.compile(r"simulate_segment", re.I),
            re.compile(r"Alpha158|alpha[\s_]?158", re.I),
            re.compile(r"(?:HFQ|后复权|复权)", re.I),
            re.compile(r"warm(?:up)?\s*(?:window|period)|预热", re.I),
        ],
        "Validate backtest configuration: check date ranges, ensure HFQ prices "
        "for returns, raw prices for Alpha158 factors, sufficient warmup window.",
    ),
    (
        "signal_interpretation",
        [
            re.compile(r"signal|信号", re.I),
            re.compile(r"(?:buy|sell)\s*(?:signal|触发)", re.I),
            re.compile(r"MACD|RSI|KDJ|布林|bollinger", re.I),
            re.compile(r"均线|moving\s+average|MA\d+", re.I),
            re.compile(r"金叉|死叉|cross(?:over)?", re.I),
            re.compile(r"超买|超卖|overbought|oversold", re.I),
            re.compile(r"缠论|chanlun", re.I),
            re.compile(r"因子|factor", re.I),
            re.compile(r"IC(?:\s*value)?|IR(?:\s*value)?|ICIR", re.I),
        ],
        "Add signal interpretation guardrails: require multi-factor confirmation, "
        "check signal strength thresholds, distinguish raw vs adjusted values.",
    ),
    (
        "report_structure",
        [
            re.compile(r"report|报告", re.I),
            re.compile(r"analysis[_/]|分析[_/]", re.I),
            re.compile(r"_index\.json", re.I),
            re.compile(r"section|章节|段落", re.I),
            re.compile(r"conclusion|结论", re.I),
            re.compile(r"risk\s*(?:warning|alert)|风险(?:提示|预警)", re.I),
            re.compile(r"standard\s*(?:format|template)|标准(?:格式|模板)", re.I),
        ],
        "Enforce report structure: introduction, data method, results, "
        "conclusion, risk warnings. Save to analysis/<code>/ and update _index.json.",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from dict or object (duck-typed)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _collect_step_text(trajectory: Any, step_index: int, window: int = 3) -> str:
    """Collect text from steps around the failure point for richer matching."""
    steps = _get_attr(trajectory, "steps", []) or []
    parts: list[str] = []
    start = max(0, step_index - window)
    end = min(len(steps), step_index + window + 1)
    for i in range(start, end):
        step = steps[i]
        for attr in ("content", "tool_output", "tool_name"):
            val = _get_attr(step, attr, "")
            if isinstance(val, str) and val:
                parts.append(val)
            elif isinstance(val, dict):
                parts.append(str(val))
    return "\n".join(parts)


def _make_evidence_id(trajectory: Any, failure: FailureInfo) -> str:
    """Create a stable evidence ID from trajectory + failure info."""
    session_id = _get_attr(trajectory, "session_id", "unknown") or "unknown"
    target = _get_attr(trajectory, "target", "") or ""
    return f"{session_id}:{target}:step{failure.step_index}:{failure.type}"


def _classify_failure(
    failure_info: FailureInfo, trajectory: Any
) -> Optional[tuple[str, str]]:
    """Pattern-match failure evidence to an instruction category.

    Parameters
    ----------
    failure_info : FailureInfo
        The detected failure from ``detect_failure()``.
    trajectory : Trajectory
        The full trajectory for context enrichment.

    Returns
    -------
    tuple[str, str] or None
        ``(instruction_ref, suggested_fix)`` if a category matches, else ``None``.
    """
    # Build a rich text blob for matching: evidence + description + surrounding steps
    context_text = _collect_step_text(trajectory, failure_info.step_index)
    match_text = "\n".join([
        failure_info.evidence,
        failure_info.description,
        context_text,
    ])

    best_category: Optional[str] = None
    best_fix: Optional[str] = None
    best_score = 0

    for instruction_ref, patterns, suggested_fix in _CATEGORY_DEFS:
        score = sum(1 for pat in patterns if pat.search(match_text)) / max(len(patterns), 1)
        if score > best_score:
            best_score = score
            best_category = instruction_ref
            best_fix = suggested_fix

    if best_score == 0:
        return None

    return (best_category, best_fix)  # type: ignore[return-value]


def _determine_failure_type(
    instruction_ref: str,
    current_instructions: Optional[str],
) -> str:
    """Determine whether the failure is wrong, missing, or ambiguous.

    Parameters
    ----------
    instruction_ref : str
        The instruction category reference.
    current_instructions : str or None
        The full text of current AGENTS.md / skill instructions.

    Returns
    -------
    str
        One of ``"wrong"``, ``"missing"``, ``"ambiguous"``.
    """
    if not current_instructions:
        return "missing"

    # Normalise the instruction_ref for searching in instructions text
    # Strip "missing:" prefix if present — that already implies missing
    if instruction_ref.startswith("missing:"):
        # Check if the topic is mentioned at all
        topic = instruction_ref.split(":", 1)[1]
        topic_patterns = {
            "announcement_check": [r"announce", r"公告", r"disclosure", r"披露"],
        }
        search_terms = topic_patterns.get(topic, [topic.replace("_", r"[\s_]?")])
        found = any(
            re.search(term, current_instructions, re.I)
            for term in search_terms
        )
        return "ambiguous" if found else "missing"

    # For non-"missing:" refs, check if the topic is covered
    ref_patterns = {
        "data_source_priority": [
            r"data\s+source\s+priority",
            r"数据源.*优先",
            r"DuckDB.*Tushare|Tushare.*DuckDB",
            r"local\s+(?:db|data).*first",
        ],
        "backtest_config": [
            r"backtest.*config",
            r"回测.*配置",
            r"StrategyConfig",
            r"walk[\s-]?forward",
            r"HFQ.*复权|复权.*HFQ",
        ],
        "signal_interpretation": [
            r"signal.*interpret",
            r"信号.*解读",
            r"multi[\s-]?factor",
            r"因子.*确认",
        ],
        "report_structure": [
            r"report.*struct",
            r"报告.*结构",
            r"analysis.*report",
            r"标准.*报告",
            r"_index\.json",
        ],
    }

    search_terms = ref_patterns.get(instruction_ref, [instruction_ref])
    matches = [
        re.search(term, current_instructions, re.I)
        for term in search_terms
    ]
    found_count = sum(1 for m in matches if m)

    if found_count == 0:
        return "missing"
    elif found_count == 1:
        return "ambiguous"
    else:
        return "wrong"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attribute_failures(
    trajectories: list,
    current_instructions: Optional[str] = None,
    max_trajectories: int = 10,
) -> list[Attribution]:
    """Analyse trajectories and attribute failures to instruction gaps.

    GEPA-style pipeline:
    1. Filter to trajectories with detected failures.
    2. Classify each failure into an instruction category.
    3. Determine failure type (wrong / missing / ambiguous) against current docs.
    4. Return a list of :class:`Attribution` objects.

    Parameters
    ----------
    trajectories : list
        List of :class:`~scripts.memory.parsers.common.Trajectory` objects.
    current_instructions : str, optional
        Full text of current AGENTS.md or skill instructions, used to
        determine whether a failure is ``wrong``, ``missing``, or ``ambiguous``.
    max_trajectories : int
        Maximum number of trajectories to process (most recent first).

    Returns
    -------
    list[Attribution]
        One attribution per detected failure.
    """
    # Limit to max_trajectories (assume list is already sorted newest-first)
    limited = trajectories[:max_trajectories]

    attributions: list[Attribution] = []

    for traj in limited:
        failure = detect_failure(traj)
        if failure is None:
            continue

        classification = _classify_failure(failure, traj)
        if classification is None:
            # Unclassifiable failure — attribute to a generic bucket
            instruction_ref = f"unclassified:{failure.type}"
            suggested_fix = (
                f"Review failure of type '{failure.type}': {failure.description}"
            )
        else:
            instruction_ref, suggested_fix = classification

        failure_type = _determine_failure_type(
            instruction_ref, current_instructions
        )

        evidence_id = _make_evidence_id(traj, failure)

        # Confidence: higher when classification had strong pattern matches
        # and the failure type is "wrong" (clearer signal)
        if classification is not None:
            base_confidence = 0.7
        else:
            base_confidence = 0.5
        if failure_type == "wrong":
            base_confidence = min(base_confidence + 0.1, 0.9)
        elif failure_type == "missing":
            base_confidence = min(base_confidence + 0.05, 0.85)

        attributions.append(
            Attribution(
                instruction_ref=instruction_ref,
                failure_type=failure_type,
                suggested_fix=suggested_fix,
                evidence_ids=[evidence_id],
                confidence=round(base_confidence, 2),
            )
        )

    return attributions


def aggregate_attributions(attributions: list[Attribution]) -> list[Attribution]:
    """Group attributions by instruction_ref, merge evidence, sort by frequency.

    Parameters
    ----------
    attributions : list[Attribution]
        Raw attribution list from :func:`attribute_failures`.

    Returns
    -------
    list[Attribution]
        Deduplicated, sorted by frequency (descending), then confidence.
    """
    if not attributions:
        return []

    # Group by instruction_ref
    groups: dict[str, list[Attribution]] = defaultdict(list)
    for attr in attributions:
        groups[attr.instruction_ref].append(attr)

    aggregated: list[Attribution] = []
    for instruction_ref, group in groups.items():
        # Merge evidence_ids (deduplicated, preserving order)
        seen_ids: set[str] = set()
        merged_evidence: list[str] = []
        for attr in group:
            for eid in attr.evidence_ids:
                if eid not in seen_ids:
                    seen_ids.add(eid)
                    merged_evidence.append(eid)

        # Pick the most common failure_type in the group
        type_counts: dict[str, int] = defaultdict(int)
        for attr in group:
            type_counts[attr.failure_type] += 1
        dominant_type = max(type_counts, key=lambda t: type_counts[t])

        # Average confidence
        avg_confidence = sum(a.confidence for a in group) / len(group)

        # Use the first suggested_fix (they should be identical for same ref)
        suggested_fix = group[0].suggested_fix

        aggregated.append(
            Attribution(
                instruction_ref=instruction_ref,
                failure_type=dominant_type,
                suggested_fix=suggested_fix,
                evidence_ids=merged_evidence,
                confidence=round(avg_confidence, 2),
            )
        )

    # Sort: primary by evidence count (frequency), secondary by confidence
    aggregated.sort(
        key=lambda a: (len(a.evidence_ids), a.confidence),
        reverse=True,
    )

    return aggregated
