"""COPRO-style candidate generator and scorer.

Generates N candidate instruction updates (unified diff format) based on
failure attributions and score history. Evaluates candidates against
held-out trajectories and selects the best one.

Key principle: Only apply candidates that score > current * 1.05 (5% improvement).

Public API
----------
- ``generate_candidates(current_instructions, attributions, score_history, n)``
- ``evaluate_candidate(candidate, trajectories, scoring_fn)``
- ``select_best(candidates, current_score, threshold)``
- ``run_evolution_cycle(workspace_dir, trajectories, attributions)``
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports for parallel-track modules (T13 attribution may not exist yet)
# ---------------------------------------------------------------------------

try:
    from scripts.memory.attribution import Attribution  # type: ignore[import]
except ImportError:
    Attribution = None  # type: ignore[assignment,misc]
    logger.debug("attribution module not available; using fallback Attribution stub")

try:
    from scripts.memory.parsers import Trajectory, load_trajectories  # type: ignore[import]
except ImportError:
    Trajectory = None  # type: ignore[assignment,misc]
    load_trajectories = None  # type: ignore[assignment]
    logger.debug("parsers module not available")

try:
    from scripts.memory.scoring import score_trajectory  # type: ignore[import]
except ImportError:
    score_trajectory = None  # type: ignore[assignment]
    logger.debug("scoring module not available")


# ---------------------------------------------------------------------------
# Candidate dataclass
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A proposed instruction update."""

    diff: str  # unified diff format
    summary: str  # human-readable summary of changes
    addresses: list = field(default_factory=list)  # Attribution.instruction_ref list
    score: float = 0.0  # evaluation score (0.0-1.0)


# ---------------------------------------------------------------------------
# Seed identity sections — NEVER modify these
# ---------------------------------------------------------------------------

_PROTECTED_SECTIONS: set[str] = {
    "环境",
    "数据采集能力",
    "增量同步速查",
    "数据分析诉求",
    "客户引导流程",
    "能力索引",
    "周期任务触发规范",
    "关键约束速查",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_unified_diff(original: str, modified: str, filename: str = "AGENTS.md") -> str:
    """Generate a unified diff between two strings."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        mod_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(diff)


def _apply_diff(original: str, diff_text: str) -> Optional[str]:
    """Apply a unified diff to original text. Returns modified text or None on failure."""
    try:
        orig_lines = original.splitlines(keepends=True)
        # Fallback: use difflib's Differ for a simpler approach
        # Parse unified diff manually
        modified = _apply_unified_diff_manual(orig_lines, diff_text)
        if modified is not None:
            return "".join(modified)
        return None
    except Exception as e:
        logger.warning("Failed to apply diff: %s", e)
        return None


def _apply_unified_diff_manual(orig_lines: list[str], diff_text: str) -> Optional[list[str]]:
    """Manually apply a unified diff. Returns list of lines or None on error."""
    result = list(orig_lines)
    offset = 0

    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
    i = 0
    diff_lines = diff_text.splitlines()

    while i < len(diff_lines):
        line = diff_lines[i]
        m = hunk_re.match(line)
        if m:
            orig_start = int(m.group(1)) - 1  # 0-indexed
            orig_count = int(m.group(2)) if m.group(2) else 1
            new_start = int(m.group(3)) - 1
            new_count = int(m.group(4)) if m.group(4) else 1

            # Collect hunk body
            i += 1
            old_chunk: list[str] = []
            new_chunk: list[str] = []
            while i < len(diff_lines):
                hline = diff_lines[i]
                if hline.startswith("@@") or hline.startswith("---") or hline.startswith("+++"):
                    break
                if hline.startswith("-"):
                    old_chunk.append(hline[1:] + "\n" if not hline[1:].endswith("\n") else hline[1:])
                elif hline.startswith("+"):
                    new_chunk.append(hline[1:] + "\n" if not hline[1:].endswith("\n") else hline[1:])
                elif hline.startswith(" "):
                    ctx = hline[1:] + "\n" if not hline[1:].endswith("\n") else hline[1:]
                    old_chunk.append(ctx)
                    new_chunk.append(ctx)
                elif hline == "":
                    # Context line with empty content
                    old_chunk.append("\n")
                    new_chunk.append("\n")
                else:
                    break
                i += 1

            # Apply the hunk
            adj_start = orig_start + offset
            result[adj_start:adj_start + len(old_chunk)] = new_chunk
            offset += len(new_chunk) - len(old_chunk)
        else:
            i += 1

    return result


def _find_section_ranges(text: str) -> dict[str, tuple[int, int]]:
    """Find line ranges for each top-level section (## or #)."""
    sections: dict[str, tuple[int, int]] = {}
    lines = text.splitlines()
    current_section: Optional[str] = None
    current_start = 0

    for i, line in enumerate(lines):
        if line.startswith("# "):
            if current_section is not None:
                sections[current_section] = (current_start, i)
            current_section = line.lstrip("# ").strip()
            current_start = i
        elif line.startswith("## ") and current_section is None:
            current_section = line.lstrip("# ").strip()
            current_start = i

    if current_section is not None:
        sections[current_section] = (current_start, len(lines))

    return sections


def _is_protected(section_name: str) -> bool:
    """Check if a section name matches a protected seed identity section."""
    for protected in _PROTECTED_SECTIONS:
        if protected in section_name:
            return True
    return False


# ---------------------------------------------------------------------------
# Attribution fallback
# ---------------------------------------------------------------------------

@dataclass
class _FallbackAttribution:
    """Minimal attribution stub when T13 attribution module is unavailable."""

    instruction_ref: str = ""
    failure_type: str = "missing"  # "missing" | "wrong" | "ambiguous"
    description: str = ""
    confidence: float = 0.5
    trajectory_id: str = ""


def _ensure_attribution(attributions: list) -> list:
    """Ensure attributions are usable, converting dicts to objects if needed."""
    result = []
    for attr in attributions:
        if isinstance(attr, dict):
            if Attribution is not None:
                try:
                    result.append(Attribution(**attr))
                    continue
                except Exception:
                    pass
            result.append(_FallbackAttribution(**{
                k: v for k, v in attr.items()
                if k in _FallbackAttribution.__dataclass_fields__
            }))
        else:
            result.append(attr)
    return result


# ---------------------------------------------------------------------------
# generate_candidates
# ---------------------------------------------------------------------------

def generate_candidates(
    current_instructions: str,
    attributions: list,
    score_history: Optional[list[dict]] = None,
    n: int = 3,
) -> list[Candidate]:
    """Generate N candidate instruction updates based on failure attributions.

    Rule-based generator that produces simple, safe changes:
    - For "missing" attributions → adds a new section with bullet points
    - For "wrong" attributions → adds clarification notes to a safe section
    - For "ambiguous" attributions → adds disambiguation notes

    Never modifies seed identity sections (环境, 数据采集能力, etc.).

    Parameters
    ----------
    current_instructions : str
        Current AGENTS.md text.
    attributions : list
        List of Attribution objects (or dicts) from the attribution analyzer.
    score_history : list[dict], optional
        Previous evolution attempts with scores, for context.
    n : int
        Maximum number of candidates to generate (capped at 5).

    Returns
    -------
    list[Candidate]
        List of candidate proposals with unified diffs.
    """
    n = min(n, 5)  # Hard cap at 5
    attributions = _ensure_attribution(attributions)

    if not attributions:
        logger.info("No attributions provided; generating no candidates")
        return []

    # Group attributions by type
    missing: list = [a for a in attributions if _get_failure_type(a) == "missing"]
    wrong: list = [a for a in attributions if _get_failure_type(a) == "wrong"]
    ambiguous: list = [a for a in attributions if _get_failure_type(a) == "ambiguous"]

    candidates: list[Candidate] = []

    # --- Candidate 1: "Lessons Learned" section from missing attributions ---
    if missing and len(candidates) < n:
        cand = _generate_lessons_learned_candidate(
            current_instructions, missing, score_history
        )
        if cand is not None:
            candidates.append(cand)

    # --- Candidate 2: "Common Pitfalls" section from wrong attributions ---
    if wrong and len(candidates) < n:
        cand = _generate_pitfalls_candidate(
            current_instructions, wrong, score_history
        )
        if cand is not None:
            candidates.append(cand)

    # --- Candidate 3: "Clarifications" from ambiguous attributions ---
    if ambiguous and len(candidates) < n:
        cand = _generate_clarifications_candidate(
            current_instructions, ambiguous, score_history
        )
        if cand is not None:
            candidates.append(cand)

    # --- Candidate 4: Combined candidate (all types) ---
    if len(attributions) > 1 and len(candidates) < n:
        cand = _generate_combined_candidate(
            current_instructions, attributions, score_history
        )
        if cand is not None:
            candidates.append(cand)

    # --- Candidate 5: Minimal single-point candidate ---
    if attributions and len(candidates) < n:
        cand = _generate_minimal_candidate(
            current_instructions, attributions[0], score_history
        )
        if cand is not None:
            candidates.append(cand)

    logger.info("Generated %d candidates from %d attributions", len(candidates), len(attributions))
    return candidates[:n]


def _get_failure_type(attr: Any) -> str:
    """Extract failure_type from an attribution object or dict."""
    if isinstance(attr, dict):
        return attr.get("failure_type", "missing")
    return getattr(attr, "failure_type", "missing")


def _get_description(attr: Any) -> str:
    """Extract description from an attribution object or dict."""
    if isinstance(attr, dict):
        return attr.get("description", "")
    return getattr(attr, "suggested_fix", "")


def _get_instruction_ref(attr: Any) -> str:
    """Extract instruction_ref from an attribution object or dict."""
    if isinstance(attr, dict):
        return attr.get("instruction_ref", "")
    return getattr(attr, "instruction_ref", "")


def _build_section_text(
    section_title: str,
    items: list[str],
    preamble: str = "",
) -> str:
    """Build a markdown section with bullet points."""
    lines = [f"\n# {section_title}\n"]
    if preamble:
        lines.append(f"{preamble}\n")
    for item in items:
        # Clean up the item text
        clean = item.strip().rstrip(".")
        if clean:
            lines.append(f"- {clean}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _generate_lessons_learned_candidate(
    current: str,
    missing_attrs: list,
    score_history: Optional[list[dict]],
) -> Optional[Candidate]:
    """Generate a candidate that adds a 'Lessons Learned' section."""
    section_title = "Lessons Learned"

    # Don't add if section already exists
    if f"# {section_title}" in current:
        # Append to existing section instead
        return _generate_append_to_section_candidate(
            current, section_title, missing_attrs, "lessons"
        )

    items = []
    refs = []
    for attr in missing_attrs:
        desc = _get_description(attr)
        ref = _get_instruction_ref(attr)
        if desc:
            items.append(f"**Missing**: {desc}")
        if ref:
            refs.append(ref)

    if not items:
        return None

    preamble = (
        "Auto-generated from failure attribution analysis. "
        "These represent gaps in the current instructions that led to failures."
    )
    new_section = _build_section_text(section_title, items, preamble)
    modified = current.rstrip() + "\n" + new_section

    diff = _make_unified_diff(current, modified)
    if not diff:
        return None

    summary = f"Add '{section_title}' section with {len(items)} bullet points from missing attributions"
    return Candidate(
        diff=diff,
        summary=summary,
        addresses=refs,
        score=0.0,
    )


def _generate_pitfalls_candidate(
    current: str,
    wrong_attrs: list,
    score_history: Optional[list[dict]],
) -> Optional[Candidate]:
    """Generate a candidate that adds a 'Common Pitfalls' section."""
    section_title = "Common Pitfalls"

    if f"# {section_title}" in current:
        return _generate_append_to_section_candidate(
            current, section_title, wrong_attrs, "pitfalls"
        )

    items = []
    refs = []
    for attr in wrong_attrs:
        desc = _get_description(attr)
        ref = _get_instruction_ref(attr)
        if desc:
            items.append(f"**Avoid**: {desc}")
        if ref:
            refs.append(ref)

    if not items:
        return None

    preamble = (
        "Auto-generated from failure attribution analysis. "
        "These represent incorrect behaviors that should be explicitly avoided."
    )
    new_section = _build_section_text(section_title, items, preamble)
    modified = current.rstrip() + "\n" + new_section

    diff = _make_unified_diff(current, modified)
    if not diff:
        return None

    summary = f"Add '{section_title}' section with {len(items)} warnings from wrong attributions"
    return Candidate(
        diff=diff,
        summary=summary,
        addresses=refs,
        score=0.0,
    )


def _generate_clarifications_candidate(
    current: str,
    ambiguous_attrs: list,
    score_history: Optional[list[dict]],
) -> Optional[Candidate]:
    """Generate a candidate that adds clarification notes."""
    section_title = "Evolution Notes"

    if f"# {section_title}" in current:
        return _generate_append_to_section_candidate(
            current, section_title, ambiguous_attrs, "clarifications"
        )

    items = []
    refs = []
    for attr in ambiguous_attrs:
        desc = _get_description(attr)
        ref = _get_instruction_ref(attr)
        if desc:
            items.append(f"**Clarify**: {desc}")
        if ref:
            refs.append(ref)

    if not items:
        return None

    preamble = (
        "Auto-generated from failure attribution analysis. "
        "These represent ambiguous instructions that need clarification."
    )
    new_section = _build_section_text(section_title, items, preamble)
    modified = current.rstrip() + "\n" + new_section

    diff = _make_unified_diff(current, modified)
    if not diff:
        return None

    summary = f"Add '{section_title}' section with {len(items)} clarifications"
    return Candidate(
        diff=diff,
        summary=summary,
        addresses=refs,
        score=0.0,
    )


def _generate_combined_candidate(
    current: str,
    all_attrs: list,
    score_history: Optional[list[dict]],
) -> Optional[Candidate]:
    """Generate a combined candidate addressing all attribution types."""
    sections_to_add: list[str] = []
    refs: list[str] = []

    missing = [a for a in all_attrs if _get_failure_type(a) == "missing"]
    wrong = [a for a in all_attrs if _get_failure_type(a) == "wrong"]
    ambiguous = [a for a in all_attrs if _get_failure_type(a) == "ambiguous"]

    if missing:
        items = [_get_description(a) for a in missing if _get_description(a)]
        if items:
            sections_to_add.append(
                _build_section_text(
                    "Lessons Learned",
                    [f"**Missing**: {i}" for i in items],
                    "Auto-generated from failure analysis.",
                )
            )
        refs.extend(_get_instruction_ref(a) for a in missing if _get_instruction_ref(a))

    if wrong:
        items = [_get_description(a) for a in wrong if _get_description(a)]
        if items:
            sections_to_add.append(
                _build_section_text(
                    "Common Pitfalls",
                    [f"**Avoid**: {i}" for i in items],
                    "Auto-generated from failure analysis.",
                )
            )
        refs.extend(_get_instruction_ref(a) for a in wrong if _get_instruction_ref(a))

    if ambiguous:
        items = [_get_description(a) for a in ambiguous if _get_description(a)]
        if items:
            sections_to_add.append(
                _build_section_text(
                    "Evolution Notes",
                    [f"**Clarify**: {i}" for i in items],
                    "Auto-generated from failure analysis.",
                )
            )
        refs.extend(_get_instruction_ref(a) for a in ambiguous if _get_instruction_ref(a))

    if not sections_to_add:
        return None

    # Filter out sections that already exist
    filtered = []
    for sec in sections_to_add:
        title_match = re.search(r"^# (.+)$", sec, re.MULTILINE)
        if title_match and f"# {title_match.group(1)}" not in current:
            filtered.append(sec)

    if not filtered:
        return None

    modified = current.rstrip() + "\n" + "\n".join(filtered)
    diff = _make_unified_diff(current, modified)
    if not diff:
        return None

    summary = f"Combined update: add {len(filtered)} new sections addressing {len(all_attrs)} attributions"
    return Candidate(
        diff=diff,
        summary=summary,
        addresses=refs,
        score=0.0,
    )


def _generate_minimal_candidate(
    current: str,
    attr: Any,
    score_history: Optional[list[dict]],
) -> Optional[Candidate]:
    """Generate a minimal single-point candidate from one attribution."""
    desc = _get_description(attr)
    ref = _get_instruction_ref(attr)
    ftype = _get_failure_type(attr)

    if not desc:
        return None

    section_title = "Evolution Notes"
    prefix_map = {"missing": "Gap", "wrong": "Fix", "ambiguous": "Clarify"}
    prefix = prefix_map.get(ftype, "Note")

    note_line = f"- **{prefix}**: {desc}"

    if f"# {section_title}" in current:
        # Append to existing section
        return _generate_append_to_section_candidate(
            current, section_title, [attr], "minimal"
        )

    new_section = _build_section_text(
        section_title,
        [f"**{prefix}**: {desc}"],
        "Auto-generated from failure analysis.",
    )
    modified = current.rstrip() + "\n" + new_section
    diff = _make_unified_diff(current, modified)
    if not diff:
        return None

    summary = f"Minimal update: add single {prefix.lower()} note from attribution"
    return Candidate(
        diff=diff,
        summary=summary,
        addresses=[ref] if ref else [],
        score=0.0,
    )


def _generate_append_to_section_candidate(
    current: str,
    section_title: str,
    attrs: list,
    tag: str,
) -> Optional[Candidate]:
    """Generate a candidate that appends bullet points to an existing section."""
    lines = current.splitlines()
    section_start = None
    section_end = len(lines)

    for i, line in enumerate(lines):
        if line.strip() == f"# {section_title}":
            section_start = i
        elif section_start is not None and line.startswith("# ") and i > section_start:
            section_end = i
            break

    if section_start is None:
        return None

    items = []
    refs = []
    for attr in attrs:
        desc = _get_description(attr)
        ref = _get_instruction_ref(attr)
        if desc:
            items.append(f"- [{tag}] {desc}")
        if ref:
            refs.append(ref)

    if not items:
        return None

    # Insert items before section_end
    new_lines = list(lines)
    insert_point = section_end
    for item in reversed(items):
        new_lines.insert(insert_point, item)

    modified = "\n".join(new_lines)
    if not modified.endswith("\n"):
        modified += "\n"

    diff = _make_unified_diff(current, modified)
    if not diff:
        return None

    summary = f"Append {len(items)} {tag} notes to existing '{section_title}' section"
    return Candidate(
        diff=diff,
        summary=summary,
        addresses=refs,
        score=0.0,
    )


# ---------------------------------------------------------------------------
# evaluate_candidate
# ---------------------------------------------------------------------------

def evaluate_candidate(
    candidate: Candidate,
    trajectories: Optional[list] = None,
    scoring_fn: Optional[Callable] = None,
) -> float:
    """Evaluate a candidate by applying its diff and scoring the result.

    Parameters
    ----------
    candidate : Candidate
        The candidate to evaluate.
    trajectories : list, optional
        List of Trajectory objects for scoring.
    scoring_fn : callable, optional
        Custom scoring function ``(modified_text, trajectories) -> float``.

    Returns
    -------
    float
        Score between 0.0 and 1.0.
    """
    if scoring_fn is not None:
        try:
            score = scoring_fn(candidate.diff, trajectories)
            candidate.score = max(0.0, min(1.0, float(score)))
            return candidate.score
        except Exception as e:
            logger.warning("Custom scoring_fn failed: %s", e)

    # Default heuristic: score based on how many attributions are addressed
    total_attributions = max(len(candidate.addresses), 1)

    if trajectories is not None and len(trajectories) > 0:
        # Score based on trajectory coverage
        addressed_targets = set()
        for traj in trajectories:
            target = getattr(traj, "target", "") or ""
            session_id = getattr(traj, "session_id", "") or ""
            for ref in candidate.addresses:
                if ref and (ref in target or ref in session_id):
                    addressed_targets.add(session_id or target)

        if addressed_targets:
            score = len(addressed_targets) / max(len(trajectories), 1)
        else:
            # Fallback: ratio of addresses to a reasonable baseline
            score = min(total_attributions / 5.0, 1.0) * 0.5
    else:
        # No trajectories: score based on address count
        score = min(total_attributions / 5.0, 1.0) * 0.5

    candidate.score = max(0.0, min(1.0, score))
    return candidate.score


# ---------------------------------------------------------------------------
# select_best
# ---------------------------------------------------------------------------

def select_best(
    candidates: list[Candidate],
    current_score: float = 0.5,
    threshold: float = 1.05,
) -> Optional[Candidate]:
    """Select the best candidate that exceeds the improvement threshold.

    Parameters
    ----------
    candidates : list[Candidate]
        Evaluated candidates with scores.
    current_score : float
        Current instruction score baseline.
    threshold : float
        Minimum improvement multiplier (default 1.05 = 5% improvement).

    Returns
    -------
    Candidate or None
        Best candidate if any exceeds threshold, else None.
    """
    if not candidates:
        return None

    min_score = current_score * threshold
    qualifying = [c for c in candidates if c.score > min_score]

    if not qualifying:
        logger.info(
            "No candidate exceeds threshold (%.2f > %.2f * %.2f); "
            "best score was %.4f",
            min_score, current_score, threshold,
            max((c.score for c in candidates), default=0.0),
        )
        return None

    best = max(qualifying, key=lambda c: c.score)
    logger.info(
        "Selected candidate: score=%.4f (threshold=%.4f) — %s",
        best.score, min_score, best.summary,
    )
    return best


# ---------------------------------------------------------------------------
# run_evolution_cycle
# ---------------------------------------------------------------------------

def run_evolution_cycle(
    workspace_dir: str = "/opt/qdata",
    trajectories: Optional[list] = None,
    attributions: Optional[list] = None,
) -> dict:
    """Orchestrate a full COPRO-style evolution cycle.

    Steps:
    1. Read current AGENTS.md
    2. Load trajectories if not provided
    3. Run attribution analysis if not provided
    4. Generate candidates
    5. Evaluate candidates
    6. Select best
    7. Return result dict

    Parameters
    ----------
    workspace_dir : str
        Path to the workspace root containing AGENTS.md.
    trajectories : list, optional
        Pre-loaded trajectories.
    attributions : list, optional
        Pre-computed attributions.

    Returns
    -------
    dict
        Result with keys: candidate, attributions, candidates_evaluated,
        candidates_generated, timestamp.
    """
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    agents_path = Path(workspace_dir) / "AGENTS.md"

    # Step 1: Read current instructions
    if not agents_path.exists():
        logger.error("AGENTS.md not found at %s", agents_path)
        return {
            "candidate": None,
            "attributions": [],
            "candidates_evaluated": 0,
            "candidates_generated": 0,
            "error": f"AGENTS.md not found at {agents_path}",
            "timestamp": timestamp,
        }

    current_instructions = agents_path.read_text(encoding="utf-8")

    # Step 2: Load trajectories if not provided
    if trajectories is None:
        trajectories = _load_trajectories_safe()

    # Step 3: Run attribution analysis if not provided
    if attributions is None:
        attributions = _run_attribution_safe(trajectories)

    # Step 4: Generate candidates
    candidates = generate_candidates(
        current_instructions=current_instructions,
        attributions=attributions,
        n=3,
    )

    # Step 5: Evaluate candidates
    for cand in candidates:
        evaluate_candidate(cand, trajectories=trajectories)

    # Step 6: Select best
    # Estimate current score from trajectory outcomes
    current_score = _estimate_current_score(trajectories)
    best = select_best(candidates, current_score=current_score)

    result = {
        "candidate": best,
        "attributions": attributions,
        "candidates_evaluated": len(candidates),
        "candidates_generated": len(candidates),
        "current_score": current_score,
        "timestamp": timestamp,
    }

    if best:
        result["best_score"] = best.score
        result["best_summary"] = best.summary
        logger.info("Evolution cycle complete: selected candidate with score %.4f", best.score)
    else:
        logger.info("Evolution cycle complete: no candidate exceeded threshold")

    return result


def _load_trajectories_safe() -> list:
    """Safely load trajectories, returning empty list on failure."""
    if load_trajectories is None:
        logger.debug("Trajectory loader not available")
        return []
    try:
        return load_trajectories()
    except Exception as e:
        logger.warning("Failed to load trajectories: %s", e)
        return []


def _run_attribution_safe(trajectories: list) -> list:
    """Safely run attribution analysis, returning empty list on failure."""
    try:
        from scripts.memory.attribution import attribute_failures  # type: ignore[import]
        return attribute_failures(trajectories)
    except ImportError:
        logger.debug("Attribution module not available")
        return []
    except Exception as e:
        logger.warning("Attribution analysis failed: %s", e)
        return []


def _estimate_current_score(trajectories: Optional[list]) -> float:
    """Estimate the current instruction quality score from trajectory outcomes."""
    if not trajectories:
        return 0.5  # Default baseline

    scores = []
    for traj in trajectories:
        outcome = getattr(traj, "outcome", None)
        if outcome is not None:
            s = getattr(outcome, "score", None)
            if s is not None and isinstance(s, (int, float)):
                scores.append(float(s))

    if not scores:
        return 0.5

    return sum(scores) / len(scores)
