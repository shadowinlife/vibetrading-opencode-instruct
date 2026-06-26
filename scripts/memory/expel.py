"""ExpeL-style insight extractor with counter mechanism.

Processes trajectory pairs (success + failure) to extract cross-task insights.
Each insight has an integer counter that tracks community support:
- ADD: new insight, starts at count=2
- AGREE: existing insight validated, count += 1
- EDIT: existing insight refined, text updated, count += 1
- DISAGREE: existing insight contradicted, count -= 1
- REMOVE: insight strongly contradicted, count -= 3

Insights with count <= 0 are deprecated (moved to _deprecated.md).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

from scripts.memory.utils import write_atomic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_INSIGHT_TEXT = 200
_MAX_PAIRS = 20
_JACCARD_THRESHOLD = 0.5

# Counter operation deltas
_OP_DELTAS: dict[str, int] = {
    "ADD": 2,       # new insight starts at 2
    "AGREE": 1,     # validation +1
    "EDIT": 1,      # refinement +1
    "DISAGREE": -1, # contradiction -1
    "REMOVE": -3,   # strong contradiction -3
}

_MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"
_GLOBAL_DIR = _MEMORY_BASE / "global"
_INSIGHTS_PATH = _GLOBAL_DIR / "insights.md"
_DEPRECATED_PATH = _GLOBAL_DIR / "_deprecated.md"

_INSIGHTS_HEADER = "# Cross-Stock Insights\n\n"
_DEPRECATED_HEADER = "# Deprecated Insights\n\n"

# ---------------------------------------------------------------------------
# Insight dataclass
# ---------------------------------------------------------------------------


@dataclass
class Insight:
    """A single cross-task insight with counter-based promotion/demotion.

    Attributes
    ----------
    id : str
        Unique identifier, e.g. ``"insight_20260610_001"``.
    text : str
        Concise insight text (max 200 characters).
    count : int
        Support counter.  New insights start at 2.
    created : datetime
        Creation timestamp.
    last_updated : datetime
        Last modification timestamp.
    tags : list[str]
        Free-form tags for filtering.
    """

    id: str
    text: str
    count: int = 2
    created: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.text) > _MAX_INSIGHT_TEXT:
            self.text = self.text[:_MAX_INSIGHT_TEXT]

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_yaml_frontmatter(self) -> str:
        """Serialize to YAML frontmatter + text."""
        meta: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "count": self.count,
            "created": self.created.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "tags": self.tags,
        }
        yaml_block = yaml.dump(meta, default_flow_style=False, sort_keys=False).rstrip()
        return f"---\n{yaml_block}\n---\n{self.text}"

    @classmethod
    def from_yaml_frontmatter(cls, text: str) -> Insight:
        """Parse from YAML frontmatter + text."""
        pattern = r"^---\s*\n(.*?)\n---\s*\n?(.*)$"
        match = re.match(pattern, text, re.DOTALL)
        if not match:
            raise ValueError("Text does not contain valid YAML frontmatter")

        yaml_str, content = match.group(1), match.group(2)
        meta: dict[str, Any] = yaml.safe_load(yaml_str)

        for key in ("created", "last_updated"):
            val = meta.get(key)
            if isinstance(val, str):
                meta[key] = datetime.fromisoformat(val)
            elif not isinstance(val, datetime):
                raise ValueError(f"Missing or invalid '{key}' in frontmatter")

        # Use content as text if not in meta
        if "text" not in meta:
            meta["text"] = content.strip()

        return cls(**meta)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Get attribute from dict or object (duck-typed)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _generate_insight_id() -> str:
    """Generate a unique insight ID."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    short_uuid = uuid.uuid4().hex[:6]
    return f"insight_{date_str}_{short_uuid}"


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard word overlap between two texts."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _is_contradictory(text_a: str, text_b: str) -> bool:
    """Heuristic: check if two insights contradict each other.

    Detects negation patterns where one insight says "do X" and another
    says "don't do X" or "avoid X".
    """
    negation_words = {"not", "don't", "avoid", "never", "no", "不", "别", "勿", "避免"}
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())

    # Check if one has negation words the other doesn't, but they share
    # significant content words
    shared = words_a & words_b
    if len(shared) < 2:
        return False

    neg_a = words_a & negation_words
    neg_b = words_b & negation_words

    # Contradictory if one has negations and the other doesn't
    return bool(neg_a) != bool(neg_b)


def _extract_tool_names(trajectory: Any) -> list[str]:
    """Extract tool names used in a trajectory's steps."""
    tools: list[str] = []
    steps = _get_attr(trajectory, "steps", []) or []
    for step in steps:
        tool_name = _get_attr(step, "tool_name", "")
        if tool_name:
            tools.append(tool_name)
    return tools


def _extract_error_patterns(trajectory: Any) -> list[str]:
    """Extract error pattern keywords from a failed trajectory."""
    error_keywords: list[str] = []
    steps = _get_attr(trajectory, "steps", []) or []
    error_re = re.compile(
        r"\b(Error|Exception|Traceback|timeout|failed|not found)\b", re.I
    )
    for step in steps:
        for attr in ("content", "tool_output"):
            val = _get_attr(step, attr, "")
            if isinstance(val, str):
                matches = error_re.findall(val)
                error_keywords.extend(m.lower() for m in matches)
    return list(set(error_keywords))


def _strip_placeholder(text: str) -> str:
    """Remove placeholder text from existing files."""
    return text.replace("_No insights yet._\n", "").replace("_No insights yet._", "")


# ---------------------------------------------------------------------------
# extract_insights
# ---------------------------------------------------------------------------


def extract_insights(
    trajectories: list[Any],
    existing_insights: Optional[list[Insight]] = None,
    max_pairs: int = _MAX_PAIRS,
) -> list[Insight]:
    """Extract cross-task insights from success/failure trajectory pairs.

    Groups trajectories by target stock, pairs successful and failed ones,
    and generates rule-based insights from the patterns observed.

    Parameters
    ----------
    trajectories : list
        List of Trajectory objects (from parsers/common.py).
    existing_insights : list[Insight], optional
        Current insights to check for AGREE/DISAGREE operations.
    max_pairs : int
        Maximum number of trajectory pairs to process (default 20).

    Returns
    -------
    list[Insight]
        Updated list of insights (existing + new).
    """
    try:
        from .scoring import score_trajectory
    except ImportError:
        logger.warning("scoring module not available, skipping trajectory scoring")
        score_trajectory = None

    if existing_insights is None:
        existing_insights = []

    insights = list(existing_insights)

    # --- Group by target ---
    by_target: dict[str, list[Any]] = defaultdict(list)
    for traj in trajectories:
        target = _get_attr(traj, "target", "") or ""
        if target:
            by_target[target].append(traj)

    # --- Pair success/failure per target ---
    pairs: list[tuple[Any, Any, str]] = []  # (success, failure, target)
    for target, trajs in by_target.items():
        successes: list[Any] = []
        failures: list[Any] = []
        if score_trajectory is not None:
            for traj in trajs:
                result = score_trajectory(traj)
                if result.score >= 0.5:
                    successes.append(traj)
                elif result.score >= 0 and result.score < 0.3:
                    failures.append(traj)

        if len(successes) != len(failures):
            logger.warning(
                "target=%s: successes=%d, failures=%d — zip() will silently "
                "truncate to %d pairs from %d total combinations",
                target, len(successes), len(failures),
                min(len(successes), len(failures)),
                len(successes) * len(failures),
            )

        # Pair up to available combinations
        for s, f in zip(successes, failures):
            if len(pairs) >= max_pairs:
                break
            pairs.append((s, f, target))

    # --- Generate rule-based insights from pairs ---
    proposed_texts: list[tuple[str, list[str]]] = []  # (text, tags)

    for success_traj, failure_traj, target in pairs:
        # Extract patterns from successful trajectory
        success_tools = _extract_tool_names(success_traj)
        failure_tools = _extract_tool_names(failure_traj)
        failure_errors = _extract_error_patterns(failure_traj)

        # Pattern 1: Tools that appear in success but not failure
        success_only_tools = set(success_tools) - set(failure_tools)
        for tool in success_only_tools:
            text = f"Backtest with {tool} tends to succeed for {target}"
            if len(text) <= _MAX_INSIGHT_TEXT:
                proposed_texts.append((text, [target, tool, "tool-success"]))

        # Pattern 2: Error patterns in failures
        for error in failure_errors[:2]:  # limit to 2 per pair
            text = f"Avoid {error} errors when analysing {target}"
            if len(text) <= _MAX_INSIGHT_TEXT:
                proposed_texts.append((text, [target, error, "error-pattern"]))

        # Pattern 3: Tools in failure that may indicate problematic approaches
        failure_only_tools = set(failure_tools) - set(success_tools)
        for tool in failure_only_tools:
            text = f"Using {tool} alone may not suffice for {target}"
            if len(text) <= _MAX_INSIGHT_TEXT:
                proposed_texts.append((text, [target, tool, "tool-warning"]))

    # --- Deduplicate proposed texts ---
    seen_texts: set[str] = set()
    unique_proposals: list[tuple[str, list[str]]] = []
    for text, tags in proposed_texts:
        if text not in seen_texts:
            seen_texts.add(text)
            unique_proposals.append((text, tags))

    # --- Match against existing insights ---
    for text, tags in unique_proposals:
        matched = False
        for existing in insights:
            sim = _jaccard_similarity(text, existing.text)
            if sim > _JACCARD_THRESHOLD:
                # AGREE: similar insight exists
                insights = apply_counter_operation(
                    insights, "AGREE", insight_id=existing.id
                )
                matched = True
                break
            if _is_contradictory(text, existing.text) and sim > 0.3:
                # DISAGREE: contradictory insight
                insights = apply_counter_operation(
                    insights, "DISAGREE", insight_id=existing.id
                )
                matched = True
                break

        if not matched:
            # ADD: new insight
            new_insight = Insight(
                id=_generate_insight_id(),
                text=text,
                count=_OP_DELTAS["ADD"],
                created=datetime.now(timezone.utc),
                last_updated=datetime.now(timezone.utc),
                tags=tags,
            )
            insights.append(new_insight)

    return insights


# ---------------------------------------------------------------------------
# apply_counter_operation
# ---------------------------------------------------------------------------


def apply_counter_operation(
    insights: list[Insight],
    operation: str,
    insight_id: Optional[str] = None,
    new_text: Optional[str] = None,
) -> list[Insight]:
    """Apply a single counter operation to the insights list.

    Parameters
    ----------
    insights : list[Insight]
        Current list of insights.
    operation : str
        One of ``"ADD"``, ``"AGREE"``, ``"EDIT"``, ``"DISAGREE"``, ``"REMOVE"``.
    insight_id : str, optional
        Target insight ID (required for all operations except ADD).
    new_text : str, optional
        Updated text (required for EDIT operation).

    Returns
    -------
    list[Insight]
        Updated insights list.

    Raises
    ------
    ValueError
        If operation is invalid or insight_id not found.
    """
    if operation not in _OP_DELTAS:
        raise ValueError(
            f"Invalid operation {operation!r}; "
            f"must be one of {sorted(_OP_DELTAS.keys())}"
        )

    now = datetime.now(timezone.utc)

    if operation == "ADD":
        text = new_text or ""
        if len(text) > _MAX_INSIGHT_TEXT:
            text = text[:_MAX_INSIGHT_TEXT]
        new_insight = Insight(
            id=insight_id or _generate_insight_id(),
            text=text,
            count=_OP_DELTAS["ADD"],
            created=now,
            last_updated=now,
        )
        insights.append(new_insight)
        return insights

    # All other operations require insight_id
    if not insight_id:
        raise ValueError(f"Operation {operation!r} requires insight_id")

    target: Optional[Insight] = None
    for ins in insights:
        if ins.id == insight_id:
            target = ins
            break

    if target is None:
        raise ValueError(f"Insight {insight_id!r} not found")

    if operation == "AGREE":
        target.count += _OP_DELTAS["AGREE"]
        target.last_updated = now

    elif operation == "EDIT":
        if new_text:
            if len(new_text) > _MAX_INSIGHT_TEXT:
                new_text = new_text[:_MAX_INSIGHT_TEXT]
            target.text = new_text
        target.count += _OP_DELTAS["EDIT"]
        target.last_updated = now

    elif operation == "DISAGREE":
        target.count += _OP_DELTAS["DISAGREE"]  # -1
        target.last_updated = now

    elif operation == "REMOVE":
        target.count += _OP_DELTAS["REMOVE"]  # -3
        target.last_updated = now

    return insights


# ---------------------------------------------------------------------------
# write_insights
# ---------------------------------------------------------------------------


def write_insights(
    insights: list[Insight],
    path: Optional[Path] = None,
) -> None:
    """Write insights to file, separating active from deprecated.

    Active insights (count > 0) go to ``insights.md``.
    Deprecated insights (count <= 0) go to ``_deprecated.md``.

    Parameters
    ----------
    insights : list[Insight]
        Insights to write.
    path : Path, optional
        Override path for active insights (default: ``~/.vibe-trading/memory/global/insights.md``).
    """
    insights_path = path or _INSIGHTS_PATH
    deprecated_path = insights_path.parent / "_deprecated.md"

    # Separate active and deprecated
    active: list[Insight] = []
    deprecated: list[Insight] = []
    for ins in insights:
        if ins.count <= 0:
            deprecated.append(ins)
        else:
            active.append(ins)

    # Sort active by count (highest first)
    active.sort(key=lambda x: x.count, reverse=True)
    deprecated.sort(key=lambda x: x.last_updated, reverse=True)

    # --- Write active insights ---
    insights_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [_INSIGHTS_HEADER]
    if not active:
        lines.append("_No insights yet._\n")
    else:
        for ins in active:
            lines.append(ins.to_yaml_frontmatter())
            lines.append("")  # blank line between entries
    write_atomic(insights_path, "\n".join(lines), encoding="utf-8")

    # --- Write deprecated insights ---
    if deprecated:
        dep_lines: list[str] = [_DEPRECATED_HEADER]
        for ins in deprecated:
            dep_lines.append(ins.to_yaml_frontmatter())
            dep_lines.append("")
        write_atomic(deprecated_path, "\n".join(dep_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# get_top_insights
# ---------------------------------------------------------------------------


def get_top_insights(
    tags: Optional[list[str]] = None,
    limit: int = 10,
    min_count: int = 2,
    path: Optional[Path] = None,
) -> list[Insight]:
    """Read insights from file, filter, and return top N.

    Parameters
    ----------
    tags : list[str], optional
        Filter to insights containing at least one of these tags.
    limit : int
        Maximum number of insights to return (default 10).
    min_count : int
        Minimum count threshold (default 2).
    path : Path, optional
        Override path for insights file.

    Returns
    -------
    list[Insight]
        Filtered and sorted insights.
    """
    insights_path = path or _INSIGHTS_PATH

    if not insights_path.exists():
        return []

    content = insights_path.read_text(encoding="utf-8")
    content = _strip_placeholder(content)

    # Parse YAML frontmatter blocks using the same pattern as schema.py
    _BLOCK_RE = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n?(.*?)(?=^---|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    parsed: list[Insight] = []
    for match in _BLOCK_RE.finditer(content):
        yaml_str, body = match.group(1), match.group(2).strip()
        try:
            meta: dict[str, Any] = yaml.safe_load(yaml_str)
            if not isinstance(meta, dict):
                continue
            for key in ("created", "last_updated"):
                val = meta.get(key)
                if isinstance(val, str):
                    meta[key] = datetime.fromisoformat(val)
            if "text" not in meta:
                meta["text"] = body
            parsed.append(Insight(**meta))
        except (ValueError, KeyError, TypeError, yaml.YAMLError):
            continue

    # Filter by min_count
    filtered = [ins for ins in parsed if ins.count >= min_count]

    # Filter by tags
    if tags:
        tag_set = set(tags)
        filtered = [
            ins for ins in filtered
            if tag_set & set(ins.tags)
        ]

    # Sort by count (highest first)
    filtered.sort(key=lambda x: x.count, reverse=True)

    return filtered[:limit]
