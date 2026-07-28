"""Progressive truncation handler with 4-level fallback.

Manages LLM context overflow by progressively reducing trajectory data:
- Level 1 (full): All trajectories as-is (when budget allows)
- Level 2 (summaries): Drop tool outputs, keep message text + metadata
- Level 3 (top-N): Select top-N trajectories by error count
- Level 4 (single): Single worst trajectory for deep-dive
"""

from __future__ import annotations

import copy
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.memory.parsers.common import Trajectory, TrajectoryStep

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Approximate token count for mixed EN/CN text.

    Uses a script-aware heuristic: CJK characters (~1.5 tokens each),
    ASCII (~4 chars/token), and others (~3 chars/token).  This is
    intentionally simple — it avoids pulling in ``tiktoken`` while
    staying within ~20 % accuracy for typical code + prose mixes.
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - cjk - ascii_chars
    return int(cjk / 1.5 + ascii_chars / 4.0 + other / 3.0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _step_text_size(step: TrajectoryStep) -> int:
    """Return character count of all text-bearing fields in a step."""
    size = len(step.content or "")
    size += len(step.tool_name or "")
    size += len(str(step.tool_input))
    size += len(step.tool_output or "")
    return size


def _trajectory_text(trajectory: Trajectory) -> str:
    """Concatenate all text from a trajectory for token estimation."""
    parts: list[str] = []
    for step in trajectory.steps:
        parts.append(step.content or "")
        if step.tool_name:
            parts.append(step.tool_name or "")
        if step.tool_input:
            parts.append(str(step.tool_input))
        if step.tool_output:
            parts.append(step.tool_output or "")
    if trajectory.metadata:
        parts.append(str(trajectory.metadata))
    return "\n".join(parts)


def _estimate_trajectory_tokens(trajectory: Trajectory) -> int:
    """Estimate token count for a single trajectory."""
    return estimate_tokens(_trajectory_text(trajectory))


def _strip_tool_outputs(trajectory: Trajectory) -> Trajectory:
    """Return a *copy* of *trajectory* with tool outputs stripped.

    - ``tool_output`` → ``"[output stripped]"``
    - ``tool_input``  → keys only (values replaced with empty string)
    - ``content``, ``role``, ``tool_name``, ``timestamp``, ``metadata`` kept
    """
    stripped_steps: list[TrajectoryStep] = []
    for step in trajectory.steps:
        new_input = {k: "" for k in step.tool_input} if step.tool_input else {}
        new_step = replace(
            step,
            tool_output="[output stripped]" if step.tool_output else "",
            tool_input=new_input,
        )
        stripped_steps.append(new_step)

    return replace(trajectory, steps=stripped_steps)


def _count_errors(trajectory: Trajectory) -> int:
    """Count steps containing error indicators in content or tool_output."""
    count = 0
    for step in trajectory.steps:
        text = f"{step.content} {step.tool_output}"
        if "error" in text or "Error" in text:
            count += 1
    return count


def _truncate_content_to_budget(
    trajectory: Trajectory, max_tokens: int
) -> Trajectory:
    """Truncate step content strings so the trajectory fits *max_tokens*.

    Works by proportionally trimming each step's ``content`` field.
    This is the last-resort Level 4 fallback — never drops below one
    trajectory.
    """
    current_tokens = _estimate_trajectory_tokens(trajectory)
    if current_tokens <= max_tokens:
        return trajectory

    # How many chars we need to shed (4 chars ≈ 1 token)
    excess_chars = (current_tokens - max_tokens) * 4
    total_content_chars = sum(len(s.content) for s in trajectory.steps)
    if total_content_chars == 0:
        return trajectory

    # Proportional trim ratio
    trim_ratio = max(0.0, 1.0 - excess_chars / total_content_chars)

    trimmed_steps: list[TrajectoryStep] = []
    for step in trajectory.steps:
        if len(step.content) > 20 and trim_ratio < 1.0:
            new_len = max(20, int(len(step.content) * trim_ratio))
            new_content = step.content[:new_len] + "...[truncated]"
            trimmed_steps.append(replace(step, content=new_content))
        else:
            trimmed_steps.append(step)

    return replace(trajectory, steps=trimmed_steps)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def truncate_trajectories(
    trajectories: list[Trajectory],
    context_budget: int,
    log_level: bool = True,
) -> tuple[list[Trajectory], int]:
    """Progressively truncate *trajectories* to fit *context_budget* tokens.

    Returns ``(truncated_trajectories, level_used)`` where *level_used* is
    1–4 indicating which fallback level was applied.

    Levels
    ------
    1 – **full**: all trajectories unchanged (total < 70 % of budget).
    2 – **summaries**: tool outputs stripped from every trajectory.
    3 – **top-N**: keep only the N trajectories with the most errors
        (with Level 2 stripping).
    4 – **single**: the single worst trajectory, content-truncated if needed.
    """
    if not trajectories:
        if log_level:
            logger.info(
                "Truncation level 1: 0 trajectories, ~0 tokens (budget: %d)",
                context_budget,
            )
        return [], 1

    # ------------------------------------------------------------------
    # Level 1: full — check if everything fits comfortably
    # ------------------------------------------------------------------
    total_tokens = sum(_estimate_trajectory_tokens(t) for t in trajectories)
    threshold = int(context_budget * 0.7)

    if total_tokens < threshold:
        if log_level:
            logger.info(
                "Truncation level 1: %d trajectories, ~%d tokens (budget: %d)",
                len(trajectories),
                total_tokens,
                context_budget,
            )
        return list(trajectories), 1

    # ------------------------------------------------------------------
    # Level 2: summaries — strip tool outputs
    # ------------------------------------------------------------------
    stripped = [_strip_tool_outputs(t) for t in trajectories]
    stripped_tokens = sum(_estimate_trajectory_tokens(t) for t in stripped)

    if stripped_tokens < threshold:
        if log_level:
            logger.info(
                "Truncation level 2: %d trajectories, ~%d tokens (budget: %d)",
                len(stripped),
                stripped_tokens,
                context_budget,
            )
        return stripped, 2

    # ------------------------------------------------------------------
    # Level 3: top-N by error count (with stripping)
    # ------------------------------------------------------------------
    error_counts = [_count_errors(t) for t in trajectories]
    indexed = sorted(
        enumerate(stripped),
        key=lambda pair: error_counts[pair[0]],
        reverse=True,
    )

    avg_tokens = max(stripped_tokens // max(len(stripped), 1), 1)
    n = max(context_budget // avg_tokens, 1)
    n = min(n, len(stripped))

    top_n = [traj for _, traj in indexed[:n]]
    top_n_tokens = sum(_estimate_trajectory_tokens(t) for t in top_n)

    if top_n_tokens < context_budget:
        if log_level:
            logger.info(
                "Truncation level 3: %d trajectories, ~%d tokens (budget: %d)",
                len(top_n),
                top_n_tokens,
                context_budget,
            )
        return top_n, 3

    # ------------------------------------------------------------------
    # Level 4: single worst trajectory, content-truncated if needed
    # ------------------------------------------------------------------
    worst_idx = max(range(len(trajectories)), key=lambda i: error_counts[i])
    worst = stripped[worst_idx]
    worst = _truncate_content_to_budget(worst, context_budget)

    final_tokens = _estimate_trajectory_tokens(worst)
    if log_level:
        logger.info(
            "Truncation level 4: %d trajectories, ~%d tokens (budget: %d)",
            1,
            final_tokens,
            context_budget,
        )
    return [worst], 4
