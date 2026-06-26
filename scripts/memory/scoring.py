"""Scoring module for analysis trajectories.

Provides two scoring methods:
- **Backtest scoring** (primary): normalises quantitative metrics (Sharpe,
  win-rate, max-drawdown, IC/IR) into a weighted 0-1 score.
- **LLM-as-Judge** (fallback): constructs an evaluation prompt for external
  LLM execution; does NOT call any LLM directly.

Public API
----------
- ``score_trajectory(trajectory)`` — auto-detect method and score.
- ``score_backtest(trajectory)`` — explicit backtest scoring.
- ``score_llm_judge(trajectory)`` — explicit LLM-judge prompt builder.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class ScoreResult:
    """Normalised scoring result."""

    score: float  # 0.0-1.0
    method: str  # "backtest" | "llm_judge"
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backtest scoring
# ---------------------------------------------------------------------------

# Weights for the four metric dimensions.
_BACKTEST_WEIGHTS: dict[str, float] = {
    "sharpe": 0.35,
    "win_rate": 0.25,
    "max_drawdown": 0.20,
    "ic_mean": 0.20,
}

# Regex patterns for extracting metrics from free-text content.
_METRIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sharpe", re.compile(r"(?:sharpe(?:\s*ratio)?|夏普(?:比率|率)?)\s*[:：=]\s*([+-]?\d+\.?\d*)", re.I)),
    ("win_rate", re.compile(r"(?:win\s*rate|胜率)\s*[:：=]\s*([+-]?\d+\.?\d*)\s*%?", re.I)),
    ("max_drawdown", re.compile(r"(?:max(?:imum)?\s*drawdown|最大回撤|max_dd)\s*[:：=]\s*([+-]?\d+\.?\d*)\s*%?", re.I)),
    ("annual_return", re.compile(r"(?:annual(?:ized)?\s*return|年化收益(?:率)?)\s*[:：=]\s*([+-]?\d+\.?\d*)\s*%?", re.I)),
    ("ic_mean", re.compile(r"(?:ic\s*mean|ic均值|ic_mean)\s*[:：=]\s*([+-]?\d+\.?\d*)", re.I)),
    ("ir_mean", re.compile(r"(?:ir\s*mean|ir均值|ir_mean)\s*[:：=]\s*([+-]?\d+\.?\d*)", re.I)),
]


def _clip(value: float, lo: float, hi: float) -> float:
    """Clip *value* to [lo, hi]."""
    return max(lo, min(hi, value))


def _normalise_sharpe(v: float) -> float:
    """Sharpe ∈ [-1, 3] → [0, 1]."""
    return (_clip(v, -1.0, 3.0) + 1.0) / 4.0


def _normalise_win_rate(v: float) -> float:
    """Win-rate: if > 1 assume percentage, clip to [0, 1]."""
    if v > 1.0:
        v = v / 100.0
    return _clip(v, 0.0, 1.0)


def _normalise_max_drawdown(v: float) -> float:
    """Max-drawdown ∈ [-0.5, 0] → [0, 1] (inverted: lower drawdown = higher score).

    Accepts both negative (conventional) and positive (absolute) values.
    """
    if v > 0:
        v = -v  # convert absolute to negative convention
    return 1.0 - (_clip(v, -0.5, 0.0) / -0.5)


def _normalise_ic_mean(v: float) -> float:
    """IC-mean ∈ [-0.1, 0.1] → [0, 1]."""
    return (_clip(v, -0.1, 0.1) + 0.1) / 0.2


_NORMALISERS: dict[str, Any] = {
    "sharpe": _normalise_sharpe,
    "win_rate": _normalise_win_rate,
    "max_drawdown": _normalise_max_drawdown,
    "ic_mean": _normalise_ic_mean,
}


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from dict or object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_backtest_metrics(trajectory: Any) -> dict[str, float]:
    """Extract backtest metrics from a trajectory.

    Searches in order:
    1. ``trajectory.metadata`` dict for known keys.
    2. ``trajectory.steps`` list — each step's result/output for known keys.
    3. Free-text content (``trajectory.content``, step text) via regex.

    Returns a dict with whatever metrics were found (may be partial or empty).
    """
    metrics: dict[str, float] = {}

    # --- 1. Direct metadata lookup ---
    metadata = _get_attr(trajectory, "metadata", {}) or {}
    if isinstance(metadata, dict):
        _known_keys = [
            "sharpe", "sharpe_ratio",
            "win_rate",
            "max_drawdown", "max_dd",
            "annual_return", "annual_ret",
            "ic_mean", "ir_mean",
        ]
        _alias_map: dict[str, str] = {
            "sharpe_ratio": "sharpe",
            "max_dd": "max_drawdown",
            "annual_ret": "annual_return",
        }
        for k in _known_keys:
            if k in metadata and k not in metrics:
                canonical = _alias_map.get(k, k)
                try:
                    metrics[canonical] = float(metadata[k])
                except (TypeError, ValueError):
                    pass

    # --- 2. Step-level lookup ---
    steps = _get_attr(trajectory, "steps", []) or []
    for step in steps:
        step_result = (
            _get_attr(step, "result", None)
            or _get_attr(step, "output", None)
            or _get_attr(step, "tool_output", None)
            or {}
        )
        if isinstance(step_result, dict):
            for k in ("sharpe", "win_rate", "max_drawdown", "max_dd", "annual_return", "ic_mean", "ir_mean"):
                canonical = {"max_dd": "max_drawdown", "annual_ret": "annual_return"}.get(k, k)
                if canonical not in metrics and k in step_result:
                    try:
                        metrics[canonical] = float(step_result[k])
                    except (TypeError, ValueError):
                        pass

    # --- 3. Regex extraction from text ---
    text_chunks: list[str] = []
    content = _get_attr(trajectory, "content", "")
    if content:
        text_chunks.append(str(content))
    for step in steps:
        for attr in ("content", "text", "output", "result", "tool_output"):
            val = _get_attr(step, attr, "")
            if isinstance(val, str):
                text_chunks.append(val)
            elif isinstance(val, dict):
                text_chunks.append(str(val))

    for name, pattern in _METRIC_PATTERNS:
        canonical = {"max_dd": "max_drawdown"}.get(name, name)
        if canonical in metrics:
            continue
        for chunk in text_chunks:
            m = pattern.search(chunk)
            if m:
                try:
                    raw = float(m.group(1))
                    # Heuristic: if win_rate or drawdown look like percentages
                    if name == "win_rate" and raw > 1:
                        raw = raw / 100.0
                    if name == "max_drawdown":
                        # If abs value > 1, treat as percentage (e.g. -12.5 → -0.125)
                        if abs(raw) > 1:
                            raw = raw / 100.0
                        # Ensure negative convention
                        if raw > 0:
                            raw = -raw
                    metrics[canonical] = raw
                    break
                except (TypeError, ValueError):
                    pass

    return metrics


def score_backtest(trajectory: Any) -> ScoreResult:
    """Score a trajectory using backtest metrics.

    Extracts metrics via :func:`extract_backtest_metrics`, normalises each
    to 0-1, and returns the weighted sum.  When only a subset of metrics is
    available the weights are re-normalised so they still sum to 1.0.

    If **no** metrics can be extracted the score defaults to 0.0.
    """
    metrics = extract_backtest_metrics(trajectory)

    scored_dims: dict[str, float] = {}
    for dim, weight in _BACKTEST_WEIGHTS.items():
        if dim in metrics:
            normaliser = _NORMALISERS[dim]
            scored_dims[dim] = normaliser(metrics[dim])

    if not scored_dims:
        return ScoreResult(
            score=0.0,
            method="backtest",
            details={"metrics": metrics, "normalised": {}, "note": "no metrics found"},
        )

    # Re-normalise weights for available dimensions.
    total_weight = sum(_BACKTEST_WEIGHTS[d] for d in scored_dims)
    weighted_sum = sum(
        scored_dims[d] * (_BACKTEST_WEIGHTS[d] / total_weight) for d in scored_dims
    )

    return ScoreResult(
        score=round(_clip(weighted_sum, 0.0, 1.0), 6),
        method="backtest",
        details={
            "metrics": metrics,
            "normalised": {d: round(v, 6) for d, v in scored_dims.items()},
            "weights_used": {d: round(_BACKTEST_WEIGHTS[d] / total_weight, 4) for d in scored_dims},
        },
    )


# ---------------------------------------------------------------------------
# LLM-as-Judge scoring
# ---------------------------------------------------------------------------

# The five sections from analysis/_template.md that define structural completeness.
_TEMPLATE_SECTIONS = [
    "Basic Information",
    "Strategy / Method Description",
    "Key Metrics",
    "Signal Timeline",
    "Conclusion and Recommendations",
]

# Chinese equivalents commonly seen in reports.
_TEMPLATE_SECTIONS_CN = [
    "基本信息",
    "策略/方法描述",
    "关键指标",
    "信号时间线",
    "结论与建议",
]

_LLM_JUDGE_WEIGHTS: dict[str, float] = {
    "structural_completeness": 0.40,
    "logic_chain_quality": 0.35,
    "actionability": 0.25,
}


def _build_judge_prompt(trajectory: Any) -> str:
    """Construct the LLM-as-Judge evaluation prompt."""
    content = _get_attr(trajectory, "content", "") or ""
    target = _get_attr(trajectory, "target", "unknown")
    traj_type = _get_attr(trajectory, "type", "analysis")

    sections_en = ", ".join(_TEMPLATE_SECTIONS)
    sections_cn = ", ".join(_TEMPLATE_SECTIONS_CN)

    prompt = f"""\
You are an expert financial analysis evaluator. Score the following analysis \
trajectory on three dimensions. Each dimension is scored from 0.0 to 1.0.

## Target: {target} ({traj_type})

## Evaluation Dimensions

### 1. 结构完整性 (structural_completeness) — weight 0.40
Does the report contain all required sections?
Required sections (EN): {sections_en}
Required sections (CN): {sections_cn}
- 1.0: All 5 sections present with substantive content
- 0.7: 4 sections present
- 0.5: 3 sections present
- 0.3: 2 sections present
- 0.0: 0-1 sections present

### 2. 逻辑链质量 (logic_chain_quality) — weight 0.35
Are conclusions supported by data and analysis? Is the reasoning coherent?
- 1.0: Every conclusion cites specific data/metrics; reasoning chain is clear
- 0.7: Most conclusions supported; minor gaps in reasoning
- 0.4: Some conclusions lack supporting evidence
- 0.1: Conclusions appear arbitrary or contradictory to presented data
- 0.0: No discernible logical chain

### 3. 可操作性 (actionability) — weight 0.25
Does the analysis give clear, actionable recommendations?
- 1.0: Specific buy/sell/hold with price levels, timeframes, and risk limits
- 0.7: Clear direction with some specifics
- 0.4: General recommendation without specifics
- 0.1: Vague or ambiguous recommendation
- 0.0: No recommendation given

## Analysis Content

```
{content[:8000]}
```

## Response Format

Respond with ONLY a JSON object (no markdown fences, no extra text):
{{
  "structural_completeness": <float 0.0-1.0>,
  "logic_chain_quality": <float 0.0-1.0>,
  "actionability": <float 0.0-1.0>,
  "reasoning": "<brief explanation of scores in 2-3 sentences>"
}}
"""
    return prompt


def score_llm_judge(trajectory: Any) -> ScoreResult:
    """Score a trajectory using LLM-as-Judge evaluation.

    This function does **NOT** call an LLM.  It constructs the evaluation
    prompt and returns it inside a :class:`ScoreResult` so the caller can
    execute the LLM call externally.

    The returned ``details`` dict contains:
    - ``prompt``: the full evaluation prompt string
    - ``dimensions``: the three scoring dimensions and their weights
    - ``parse_instructions``: how to parse the LLM response into a final score

    After the caller obtains the LLM response, they should:
    1. Parse the JSON from the LLM response.
    2. Compute weighted score using the weights in ``details["dimensions"]``.
    """
    prompt = _build_judge_prompt(trajectory)

    return ScoreResult(
        score=-1.0,  # sentinel: score not yet computed (needs LLM execution)
        method="llm_judge",
        details={
            "prompt": prompt,
            "dimensions": dict(_LLM_JUDGE_WEIGHTS),
            "parse_instructions": (
                "Parse the LLM JSON response, validate each dimension is in "
                "[0.0, 1.0], then compute: score = sum(dim_score * weight for "
                "dim, weight in dimensions.items()). Replace this ScoreResult's "
                "score with the computed value."
            ),
        },
    )


def compute_llm_judge_score(llm_response: dict) -> float:
    """Compute the final weighted score from an LLM judge response dict.

    Parameters
    ----------
    llm_response : dict
        Parsed JSON from the LLM, expected to contain keys matching
        ``_LLM_JUDGE_WEIGHTS`` (structural_completeness, logic_chain_quality,
        actionability).

    Returns
    -------
    float
        Weighted score in [0.0, 1.0].
    """
    total = 0.0
    for dim, weight in _LLM_JUDGE_WEIGHTS.items():
        val = llm_response.get(dim, 0.0)
        try:
            val = float(val)
        except (TypeError, ValueError):
            val = 0.0
        total += _clip(val, 0.0, 1.0) * weight
    return round(_clip(total, 0.0, 1.0), 6)


# ---------------------------------------------------------------------------
# Auto-detect dispatcher
# ---------------------------------------------------------------------------

def _has_backtest_signal(trajectory: Any) -> bool:
    """Heuristic: does this trajectory look like it has backtest results?"""
    # Direct metadata check
    metadata = _get_attr(trajectory, "metadata", {}) or {}
    if isinstance(metadata, dict):
        bt_keys = {"sharpe", "sharpe_ratio", "win_rate", "max_drawdown", "max_dd", "annual_return"}
        if bt_keys & set(metadata.keys()):
            return True

    # Step-level check
    steps = _get_attr(trajectory, "steps", []) or []
    for step in steps:
        step_result = (
            _get_attr(step, "result", None)
            or _get_attr(step, "output", None)
            or _get_attr(step, "tool_output", None)
            or {}
        )
        if isinstance(step_result, dict):
            if {"sharpe", "win_rate", "max_drawdown", "max_dd"} & set(step_result.keys()):
                return True
        # Tool name hint
        tool = _get_attr(step, "tool", "") or _get_attr(step, "tool_name", "") or ""
        if "backtest" in str(tool).lower():
            return True
        # Also scan tool_output text for metric patterns
        tool_out = _get_attr(step, "tool_output", "") or ""
        if isinstance(tool_out, str) and tool_out:
            for _name, pattern in _METRIC_PATTERNS[:3]:
                if pattern.search(tool_out):
                    return True

    # Tags / type hint
    tags = _get_attr(trajectory, "tags", []) or []
    if any("backtest" in str(t).lower() for t in tags):
        return True
    traj_type = _get_attr(trajectory, "type", "") or ""
    if "backtest" in str(traj_type).lower():
        return True

    # Quick regex scan on content for metric patterns
    content = _get_attr(trajectory, "content", "") or ""
    if content:
        for name, pattern in _METRIC_PATTERNS[:3]:  # sharpe, win_rate, max_drawdown
            if pattern.search(str(content)):
                return True

    return False


def score_trajectory(trajectory: Any) -> ScoreResult:
    """Auto-detect the best scoring method and score a trajectory.

    - If the trajectory contains backtest results → :func:`score_backtest`
    - Otherwise → :func:`score_llm_judge`
    """
    if _has_backtest_signal(trajectory):
        return score_backtest(trajectory)
    return score_llm_judge(trajectory)
