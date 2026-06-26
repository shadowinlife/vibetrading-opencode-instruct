"""Reflexion-style failure detector and reflection generator.

Implements the core error notebook mechanism:
- detect_failure(trajectory) → identifies failures from trajectory content
- generate_reflection(trajectory, failure) → returns LLM prompt for structured reflection
- append_to_mistakes(target, reflection_text) → writes to per-stock and global mistakes.md

Key principle: FAILURE-ONLY. Only generate reflections when something goes wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from scripts.memory.utils import write_atomic

# ---------------------------------------------------------------------------
# Failure detection result
# ---------------------------------------------------------------------------

@dataclass
class FailureInfo:
    """Describes a detected failure in a trajectory."""

    type: str          # "error" | "data_missing" | "user_feedback" | "hallucination"
    description: str   # human-readable description
    step_index: int    # which step in the trajectory
    evidence: str      # the actual text that triggered detection


# ---------------------------------------------------------------------------
# Failure detection patterns
# ---------------------------------------------------------------------------

# 1. General error keywords in tool outputs / assistant text
_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bError\b", re.I),
    re.compile(r"\bException\b", re.I),
    re.compile(r"Traceback \(most recent call last\)", re.I),
    re.compile(r"\bnot found\b", re.I),
    re.compile(r"\btimeout\b", re.I),
    re.compile(r"\bfailed\b", re.I),
    re.compile(r"错误"),
    re.compile(r"失败"),
    re.compile(r"超时"),
]

# 2. Data-missing patterns (more specific than generic errors)
_DATA_MISSING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Table\s+\S+\s+not\s+found", re.I),
    re.compile(r"No\s+data\s+available", re.I),
    re.compile(r"API\s+rate\s+limit", re.I),
    re.compile(r"数据缺失"),
    re.compile(r"no\s+results?\s+found", re.I),
    re.compile(r"empty\s+(?:result|dataframe|dataset)", re.I),
]

# 3. Non-zero exit code patterns
_EXIT_CODE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"exit\s+code\s*[:=]?\s*([1-9]\d*)", re.I),
    re.compile(r"returncode\s*[:=]?\s*([1-9]\d*)", re.I),
    re.compile(r"exited\s+with\s+(?:status|code)\s+([1-9]\d*)", re.I),
]

# 4. User negative feedback patterns
_USER_FEEDBACK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"不对"),
    re.compile(r"有问题"),
    re.compile(r"错了"),
    re.compile(r"\bwrong\b", re.I),
    re.compile(r"\bincorrect\b", re.I),
    re.compile(r"this\s+is\s+not\s+right", re.I),
    re.compile(r"that'?s?\s+wrong", re.I),
    re.compile(r"not\s+what\s+I\s+(?:asked|wanted)", re.I),
]

# 5. Hallucination markers — impossible financial values
_HALLUCINATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("impossible_sharpe", re.compile(
        r"(?:sharpe(?:\s*ratio)?|夏普(?:比率|率)?)\s*[:：=]\s*([+-]?\d+\.?\d*)", re.I)),
    ("impossible_win_rate", re.compile(
        r"(?:win\s*rate|胜率)\s*[:：=]\s*([+-]?\d+\.?\d*)\s*%?", re.I)),
    ("impossible_return", re.compile(
        r"(?:annual(?:ized)?\s*return|年化收益(?:率)?|收益率)\s*[:：=]\s*([+-]?\d+\.?\d*)\s*%?", re.I)),
]

# Thresholds for hallucination detection
_SHARPE_MAX = 10.0       # realistic Sharpe rarely exceeds 3-4
_WIN_RATE_MAX = 100.0    # percentage cap
_RETURN_MAX = 500.0      # annual return percentage cap (5x)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from dict or object (duck-typed)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _get_step_text(step: Any) -> list[str]:
    """Collect all searchable text from a trajectory step."""
    texts: list[str] = []
    for attr in ("content", "tool_output"):
        val = _get_attr(step, attr, "")
        if isinstance(val, str) and val:
            texts.append(val)
        elif isinstance(val, dict):
            texts.append(str(val))
    return texts


def _truncate(text: str, max_len: int = 500) -> str:
    """Truncate text for evidence field."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# detect_failure
# ---------------------------------------------------------------------------

def detect_failure(trajectory: Any) -> Optional[FailureInfo]:
    """Scan trajectory steps for failure indicators.

    Checks in priority order:
    1. Data-missing errors (most specific)
    2. User negative feedback
    3. Non-zero exit codes
    4. LLM hallucination markers (impossible values)
    5. General error patterns

    Returns a :class:`FailureInfo` if a failure is detected, else ``None``.
    Only the **first** failure found is returned (fail-fast).
    """
    steps = _get_attr(trajectory, "steps", []) or []

    for idx, step in enumerate(steps):
        role = _get_attr(step, "role", "")
        texts = _get_step_text(step)

        # --- Check user feedback (only on user messages) ---
        if role == "user":
            for text in texts:
                for pat in _USER_FEEDBACK_PATTERNS:
                    m = pat.search(text)
                    if m:
                        return FailureInfo(
                            type="user_feedback",
                            description=f"User expressed dissatisfaction at step {idx}",
                            step_index=idx,
                            evidence=_truncate(m.group(0)),
                        )

        # --- Check data-missing errors (tool outputs / assistant text) ---
        for text in texts:
            for pat in _DATA_MISSING_PATTERNS:
                m = pat.search(text)
                if m:
                    return FailureInfo(
                        type="data_missing",
                        description=f"Data source unavailable at step {idx}",
                        step_index=idx,
                        evidence=_truncate(m.group(0)),
                    )

        # --- Check non-zero exit codes ---
        for text in texts:
            for pat in _EXIT_CODE_PATTERNS:
                m = pat.search(text)
                if m:
                    return FailureInfo(
                        type="error",
                        description=f"Non-zero exit code ({m.group(1)}) at step {idx}",
                        step_index=idx,
                        evidence=_truncate(m.group(0)),
                    )

        # --- Check hallucination markers (assistant text only) ---
        if role == "assistant":
            for text in texts:
                for name, pat in _HALLUCINATION_PATTERNS:
                    m = pat.search(text)
                    if m:
                        try:
                            val = float(m.group(1))
                        except (TypeError, ValueError):
                            continue
                        is_hallucination = False
                        if name == "impossible_sharpe" and abs(val) > _SHARPE_MAX:
                            is_hallucination = True
                        elif name == "impossible_win_rate" and val > _WIN_RATE_MAX:
                            is_hallucination = True
                        elif name == "impossible_return" and abs(val) > _RETURN_MAX:
                            is_hallucination = True
                        if is_hallucination:
                            return FailureInfo(
                                type="hallucination",
                                description=f"Impossible value detected ({name}) at step {idx}",
                                step_index=idx,
                                evidence=_truncate(m.group(0)),
                            )

        # --- Check general error patterns (tool outputs / assistant text) ---
        if role in ("tool", "assistant"):
            for text in texts:
                for pat in _ERROR_PATTERNS:
                    m = pat.search(text)
                    if m:
                        return FailureInfo(
                            type="error",
                            description=f"Error pattern detected at step {idx}",
                            step_index=idx,
                            evidence=_truncate(m.group(0)),
                        )

    return None


# ---------------------------------------------------------------------------
# generate_reflection
# ---------------------------------------------------------------------------

def _summarise_trajectory_context(trajectory: Any) -> str:
    """Build a brief context summary from the trajectory for the reflection prompt."""
    target = _get_attr(trajectory, "target", "unknown") or "unknown"
    source = _get_attr(trajectory, "source", "unknown") or "unknown"
    session_id = _get_attr(trajectory, "session_id", "unknown") or "unknown"
    steps = _get_attr(trajectory, "steps", []) or []

    # Extract the user's initial request (first user step)
    initial_request = ""
    for step in steps:
        if _get_attr(step, "role", "") == "user":
            content = _get_attr(step, "content", "")
            if content:
                initial_request = _truncate(str(content), 300)
                break

    # Count steps by role
    role_counts: dict[str, int] = {}
    for step in steps:
        r = _get_attr(step, "role", "unknown")
        role_counts[r] = role_counts.get(r, 0) + 1

    step_summary = ", ".join(f"{r}={c}" for r, c in sorted(role_counts.items()))

    return (
        f"Target: {target}\n"
        f"Source: {source}\n"
        f"Session: {session_id}\n"
        f"Steps: {len(steps)} ({step_summary})\n"
        f"Initial request: {initial_request}"
    )


def generate_reflection(trajectory: Any, failure: FailureInfo) -> str:
    """Return an LLM prompt that produces a structured 4-part reflection.

    This function does **NOT** call an LLM. It returns the prompt string for
    external execution. The expected LLM output format is YAML-frontmatter
    markdown suitable for appending to the error notebook.

    Parameters
    ----------
    trajectory : Trajectory
        The full conversation trajectory where the failure occurred.
    failure : FailureInfo
        The detected failure information.

    Returns
    -------
    str
        A prompt string for external LLM execution.
    """
    context = _summarise_trajectory_context(trajectory)
    target = _get_attr(trajectory, "target", "unknown") or "unknown"

    # Collect relevant step content around the failure point
    steps = _get_attr(trajectory, "steps", []) or []
    relevant_steps: list[str] = []
    window_start = max(0, failure.step_index - 2)
    window_end = min(len(steps), failure.step_index + 3)
    for i in range(window_start, window_end):
        step = steps[i]
        role = _get_attr(step, "role", "?")
        content = _get_attr(step, "content", "")
        tool_name = _get_attr(step, "tool_name", "")
        tool_output = _get_attr(step, "tool_output", "")
        marker = " <<<FAILURE>>>" if i == failure.step_index else ""
        entry = f"[Step {i}] role={role}"
        if tool_name:
            entry += f" tool={tool_name}"
        if content:
            entry += f"\n  content: {_truncate(str(content), 400)}"
        if tool_output:
            entry += f"\n  tool_output: {_truncate(str(tool_output), 400)}"
        entry += marker
        relevant_steps.append(entry)

    steps_text = "\n\n".join(relevant_steps)

    prompt = f"""\
You are a financial analysis quality reviewer. A failure was detected during \
an analysis session. Your task is to produce a structured reflection that will \
be stored in the error notebook for future reference.

## Context

{context}

## Detected Failure

- **Type**: {failure.type}
- **Description**: {failure.description}
- **Step index**: {failure.step_index}
- **Evidence**: {failure.evidence}

## Relevant Steps (around failure point)

{steps_text}

## Instructions

Produce a structured reflection with exactly 4 sections. Be concise and actionable.

### Required Output Format

Output ONLY the following YAML-frontmatter block (no extra text before or after):

---
id: mem_{target}_{{date}}_mistake
target: {target}
type: mistake
created: {{iso_timestamp}}
last_accessed: {{iso_timestamp}}
access_count: 0
confidence: 0.5
decay_rate: 0.01
tags: [{failure.type}]
source: reflexion
resolved: false
---

## 1. What was the intended outcome?

(Describe what the analysis was trying to achieve, based on the initial request and context.)

## 2. What actually happened?

(Describe the failure: what went wrong, what error occurred, what was the unexpected result.)

## 3. What was the root cause?

(Analyse WHY it failed. Was it a data issue, a logic error, a missing prerequisite, \
an API limitation, or a misunderstanding of the user's intent?)

## 4. What should be done differently next time?

(Provide 1-3 specific, actionable improvements. These will be used as guardrails \
for future analyses of the same stock or similar tasks.)

---

IMPORTANT:
- Replace {{date}} with today's date in YYYYMMDD format.
- Replace {{iso_timestamp}} with the current ISO 8601 timestamp.
- Do NOT include raw tool output — summarise only.
- Keep the total reflection under 500 words.
- Focus on actionable insights, not blame.
"""
    return prompt


# ---------------------------------------------------------------------------
# append_to_mistakes
# ---------------------------------------------------------------------------

_MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"
_STOCKS_DIR = _MEMORY_BASE / "stocks"
_GLOBAL_DIR = _MEMORY_BASE / "global"

_GLOBAL_MISTAKES_HEADER = "# Global Error Notebook\n\n_No entries yet._\n\n"
_STOCK_MISTAKES_HEADER_TPL = "# Error Notebook: {target}\n\n_No entries yet._\n\n"


def _ensure_mistakes_file(path: Path, header: str) -> None:
    """Create the mistakes file with a header if it doesn't exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_atomic(path, header, encoding="utf-8")


def _strip_placeholder(text: str) -> str:
    """Remove the '_No entries yet._' placeholder from existing files."""
    return text.replace("_No entries yet._\n", "").replace("_No entries yet._", "")


def append_to_mistakes(
    target: str,
    reflection_text: str,
    failure_type: str = "error",
) -> str:
    """Write a reflection entry to per-stock and global mistakes files.

    Parameters
    ----------
    target : str
        Stock code, e.g. ``"601777.SH"``.
    reflection_text : str
        The full reflection markdown (including YAML frontmatter).
    failure_type : str
        Failure type tag for the entry ID.

    Returns
    -------
    str
        The generated entry ID (e.g. ``"mem_601777.SH_20260610_mistake"``).
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    iso_ts = now.isoformat()

    # Sanitise target for use in ID (keep dots, replace slashes)
    safe_target = target.replace("/", "_") if target else "unknown"
    entry_id = f"mem_{safe_target}_{date_str}_mistake"

    # Build the entry block
    entry_block = f"\n{reflection_text}\n\n---\n"

    # --- Per-stock file ---
    stock_dir = _STOCKS_DIR / safe_target
    stock_file = stock_dir / "mistakes.md"
    _ensure_mistakes_file(stock_file, _STOCK_MISTAKES_HEADER_TPL.format(target=target))
    existing = stock_file.read_text(encoding="utf-8")
    existing = _strip_placeholder(existing)
    write_atomic(stock_file, existing.rstrip() + entry_block, encoding="utf-8")

    # --- Global file ---
    global_file = _GLOBAL_DIR / "mistakes.md"
    _ensure_mistakes_file(global_file, _GLOBAL_MISTAKES_HEADER)
    existing = global_file.read_text(encoding="utf-8")
    existing = _strip_placeholder(existing)
    write_atomic(global_file, existing.rstrip() + entry_block, encoding="utf-8")

    return entry_id
