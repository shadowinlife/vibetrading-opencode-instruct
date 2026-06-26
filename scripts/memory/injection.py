"""Worker prompt injection module.

Reads memory entries (mistakes, insights) and formats them as a text block
for injection into Swarm worker system prompts.

Follows the grounding_block pattern from Vibe-Trading's worker.py.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .decay import relevance_score
from .schema import MemoryEntry

logger = logging.getLogger(__name__)

# Graceful T9 (expel) import — module may not exist yet.
try:
    from .expel import get_top_insights, Insight  # type: ignore[import-untyped]

    _HAS_EXPEL = True
except ImportError:
    _HAS_EXPEL = False

# ---------------------------------------------------------------------------
# Token estimation for truncation
# ---------------------------------------------------------------------------


def _estimate_chars_per_token(text):
    """Estimate chars-per-token ratio based on text content.

    English ~4 chars/token, Chinese ~1.5 chars/token.
    Returns a weighted estimate biased toward the dominant script.
    """
    if not text:
        return 4.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    cjk_ratio = cjk / len(text)
    # Linear interpolation: pure ASCII → 4.0, pure CJK → 1.5
    return max(1.5, 4.0 - cjk_ratio * 2.5)

# Patterns that indicate a key lesson in content
_LESSON_PATTERNS = re.compile(
    r"(教训|经验|lesson|next time|should have|must|关键|启示|"
    r"避免|注意|建议| takeaway|key point|结论|总结)",
    re.IGNORECASE,
)

# Memory base directory
_MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"


def _parse_mistakes_file(filepath: Path) -> list[MemoryEntry]:
    """Parse a mistakes.md file containing multiple YAML-frontmatter entries.

    Parameters
    ----------
    filepath : Path
        Path to the mistakes.md file.

    Returns
    -------
    list[MemoryEntry]
        Parsed entries. Malformed entries are skipped with a warning.
    """
    if not filepath.exists():
        return []

    text = filepath.read_text(encoding="utf-8")
    if not text.strip():
        return []

    # Find all frontmatter blocks: ---\n{yaml}\n---\n{content}
    # Content extends until the next --- at line start or end of file.
    pattern = re.compile(
        r"^---[ \t]*\n(.*?)\n---[ \t]*\n(.*?)(?=^---[ \t]*$|\Z)",
        re.DOTALL | re.MULTILINE,
    )

    entries: list[MemoryEntry] = []
    for match in pattern.finditer(text):
        yaml_str = match.group(1)
        content = match.group(2).strip()
        entry_text = f"---\n{yaml_str}\n---\n{content}"
        try:
            entry = MemoryEntry.from_yaml_frontmatter(entry_text)
            entries.append(entry)
        except Exception as exc:
            logger.warning(
                "Skipping malformed entry in %s: %s", filepath, exc
            )

    return entries


def _extract_key_lesson(content: str, max_sentences: int = 2) -> str:
    """Extract the key lesson from entry content.

    Looks for sentences containing lesson-related keywords.
    Falls back to the first ``max_sentences`` sentences if no match.

    Parameters
    ----------
    content : str
        The entry content (markdown).
    max_sentences : int
        Maximum number of sentences to return.

    Returns
    -------
    str
        Extracted lesson text, or empty string if content is empty.
    """
    if not content.strip():
        return ""

    # Split into sentences (handles Chinese and English punctuation)
    sentences = re.split(r"(?<=[。！？.!?])\s*", content.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return content.strip()[:200]

    # Look for sentences with lesson keywords
    lesson_sentences = [
        s for s in sentences if _LESSON_PATTERNS.search(s)
    ]

    if lesson_sentences:
        result = " ".join(lesson_sentences[:max_sentences])
    else:
        result = " ".join(sentences[:max_sentences])

    return result


def _format_entry(entry: MemoryEntry) -> str:
    """Format a single MemoryEntry as a concise text block for prompt injection.

    Parameters
    ----------
    entry : MemoryEntry
        The memory entry to format.

    Returns
    -------
    str
        Formatted text block.
    """
    date_str = entry.created.strftime("%Y-%m-%d")

    # Summary: first non-empty line of content
    lines = [
        line.strip()
        for line in entry.content.split("\n")
        if line.strip()
    ]
    summary = lines[0] if lines else entry.id
    # Strip leading markdown header markers for cleaner display
    summary = re.sub(r"^#+\s*", "", summary)

    # Key lesson extraction
    lesson = _extract_key_lesson(entry.content)

    # Build formatted block
    parts = [
        f"**[{date_str}] {summary}** "
        f"(confidence: {entry.confidence:.2f}, accessed {entry.access_count}x)",
    ]
    if lesson:
        parts.append(lesson)

    return "\n".join(parts)


def _extract_lessons_summary(entries: list[MemoryEntry]) -> list[str]:
    """Extract a bullet-point list of key lessons from entries.

    Parameters
    ----------
    entries : list[MemoryEntry]
        The entries to extract lessons from.

    Returns
    -------
    list[str]
        List of lesson strings.
    """
    lessons: list[str] = []
    seen: set[str] = set()

    for entry in entries:
        lesson = _extract_key_lesson(entry.content, max_sentences=1)
        if lesson and lesson not in seen:
            seen.add(lesson)
            lessons.append(lesson)

    return lessons


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to approximately ``max_tokens`` tokens.

    Uses a simple chars / 4 approximation.

    Parameters
    ----------
    text : str
        Text to truncate.
    max_tokens : int
        Maximum token budget.

    Returns
    -------
    str
        Truncated text. If truncation occurs, appends "\\n... (truncated)".
    """
    max_chars = int(max_tokens * _estimate_chars_per_token(text))
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars].rsplit("\n", 1)[0]
    return truncated + "\n... (truncated)"


def get_memory_block(
    target: str,
    max_entries: int = 5,
    max_tokens: int = 2000,
    now: Optional[datetime] = None,
) -> str:
    """Build a formatted memory block for injection into worker prompts.

    Reads mistakes.md for the given target, filters out resolved entries,
    sorts by relevance score, and formats as a structured markdown block.

    Parameters
    ----------
    target : str
        Stock code, e.g. ``"601777.SH"``.
    max_entries : int
        Maximum number of entries to include (default 5).
    max_tokens : int
        Maximum token budget for the output (default 2000).
    now : datetime, optional
        Reference time for relevance scoring. Defaults to ``datetime.now(tz=timezone.utc)``.

    Returns
    -------
    str
        Formatted markdown block, or empty string if no entries found.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    # Sanitize target to prevent path traversal attacks.
    # Whitelist: alphanumeric, dots, underscores, hyphens — matches stock codes like "601777.SH".
    safe_target = re.sub(r'[^a-zA-Z0-9._-]', '_', target)
    if safe_target != target:
        logger.warning(
            "Path traversal attempt detected in target parameter: original=%r, sanitized=%r",
            target,
            safe_target,
        )
        return ""

    # Locate mistakes file
    mistakes_path = _MEMORY_BASE / "stocks" / safe_target / "mistakes.md"
    entries = _parse_mistakes_file(mistakes_path)

    if not entries:
        return ""

    # Filter out resolved entries
    active_entries = [e for e in entries if not e.resolved]

    if not active_entries:
        return ""

    # Sort by relevance score (highest first).
    # Guard against malformed entries that might cause scoring errors.
    def _safe_score(entry: MemoryEntry) -> float:
        try:
            return relevance_score(entry, now)
        except (ValueError, OverflowError):
            return 0.0

    active_entries.sort(key=_safe_score, reverse=True)

    # Take top N
    selected = active_entries[:max_entries]

    # Build the output block
    lines: list[str] = [
        f"## 📋 Historical Knowledge ({target})",
        "",
        "### ⚠️ Previous Mistakes",
        "",
    ]

    for entry in selected:
        lines.append(_format_entry(entry))
        lines.append("")

    # Key lessons summary
    lessons = _extract_lessons_summary(selected)
    if lessons:
        lines.append("### 💡 Key Lessons")
        for lesson in lessons:
            lines.append(f"- {lesson}")
        lines.append("")

    result = "\n".join(lines)

    # Truncate to token budget
    result = _truncate_to_tokens(result, max_tokens)

    return result


def get_insight_block(
    tags: Optional[list[str]] = None,
    limit: int = 5,
    max_tokens: int = 1000,
) -> str:
    """Build a formatted cross-stock insights block for prompt injection.

    Reads top insights from the expel module (T9), filters by tags and
    minimum count, and formats as a structured markdown block.

    If the expel module is not available (T9 not yet deployed), returns
    an empty string (graceful degradation).

    Parameters
    ----------
    tags : list[str], optional
        Filter insights by these tags. ``None`` means no tag filter.
    limit : int
        Maximum number of insights to include (default 5).
    max_tokens : int
        Maximum token budget for the output (default 1000).

    Returns
    -------
    str
        Formatted markdown block, or empty string if no insights available.
    """
    if not _HAS_EXPEL:
        return ""

    try:
        insights = get_top_insights(tags=tags, limit=limit, min_count=2)
    except Exception as exc:
        logger.warning("Failed to fetch insights: %s", exc)
        return ""

    # Filter out deprecated insights and those with count < 2.
    # In expel.py, deprecated = count <= 0 (moved to _deprecated.md).
    # We also check for an explicit ``deprecated`` attribute for forward
    # compatibility, and enforce count >= 2 as belt-and-suspenders.
    active_insights = []
    for insight in insights:
        if getattr(insight, "deprecated", False):
            continue
        count = getattr(insight, "count", 0)
        if count <= 0:
            continue
        if count < 2:
            continue
        active_insights.append(insight)

    if not active_insights:
        return ""

    # Build the output block
    lines: list[str] = [
        "### 💡 Cross-Stock Insights",
        "",
    ]

    for insight in active_insights:
        count = getattr(insight, "count", 0)
        text = getattr(insight, "text", "")
        insight_tags = getattr(insight, "tags", [])

        # Format tag markers
        tag_str = " ".join(f"`[{t}]`" for t in insight_tags) if insight_tags else ""
        line = f"- **[{count}x]** {text}"
        if tag_str:
            line += f" {tag_str}"
        lines.append(line)

    lines.append("")

    result = "\n".join(lines)

    # Truncate to token budget
    result = _truncate_to_tokens(result, max_tokens)

    return result


def get_full_memory_block(
    target: str,
    max_tokens: int = 3000,
) -> str:
    """Build a unified memory block combining mistakes and insights.

    Combines per-stock mistakes (from ``get_memory_block``) and cross-stock
    insights (from ``get_insight_block``) into a single formatted block
    suitable for injection into worker system prompts.

    Parameters
    ----------
    target : str
        Stock code, e.g. ``"601777.SH"``.
    max_tokens : int
        Maximum token budget for the total output (default 3000).

    Returns
    -------
    str
        Combined formatted markdown block, or empty string if no content.
    """
    # Get per-stock mistakes block (already includes header)
    mistakes_block = get_memory_block(target)

    # Get cross-stock insights block
    insights_block = get_insight_block()

    # Combine
    if mistakes_block and insights_block:
        # Mistakes block already has the header; append insights after it
        combined = mistakes_block.rstrip() + "\n\n" + insights_block
    elif mistakes_block:
        combined = mistakes_block
    elif insights_block:
        # No mistakes but have insights — add the header
        combined = (
            f"## 📋 Historical Knowledge ({target})\n\n"
            + insights_block
        )
    else:
        return ""

    # Truncate total output to max_tokens
    combined = _truncate_to_tokens(combined, max_tokens)

    return combined
