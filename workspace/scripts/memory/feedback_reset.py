"""Feedback history reset module.

When an evolution mutation improves the score, resolve related error notebook
entries to prevent stale feedback from driving unnecessary mutations.

Resolved entries are kept in files (not deleted) but excluded from future
evolution cycles.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from .schema import MemoryEntry
from .expel import Insight
from scripts.memory.utils import write_atomic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"
_STOCKS_DIR = _MEMORY_BASE / "stocks"
_GLOBAL_DIR = _MEMORY_BASE / "global"

# Regex to split a multi-entry YAML-frontmatter file into blocks.
# Each block starts with ``---``, has YAML meta, another ``---``, then body.
_BLOCK_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*?)(?=^---|\Z)",
    re.DOTALL | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _memory_dir(memory_dir: Optional[Path | str] = None) -> Path:
    """Return the memory base directory, defaulting to ``~/.vibe-trading/memory``."""
    if memory_dir is not None:
        return Path(memory_dir)
    return _MEMORY_BASE


def _parse_entries_in_file(filepath: Path) -> list[dict[str, Any]]:
    """Parse all YAML-frontmatter entries from a file.

    Returns a list of dicts, each with ``meta`` (parsed YAML dict) and
    ``body`` (content after the second ``---``).  Entries that fail to
    parse are silently skipped.
    """
    if not filepath.exists():
        return []

    content = filepath.read_text(encoding="utf-8")
    results: list[dict[str, Any]] = []

    for match in _BLOCK_RE.finditer(content):
        yaml_str, body = match.group(1), match.group(2).strip()
        try:
            meta: dict[str, Any] = yaml.safe_load(yaml_str)
            if not isinstance(meta, dict):
                continue
            results.append({"meta": meta, "body": body, "raw_yaml": yaml_str})
        except yaml.YAMLError:
            continue

    return results


def _rebuild_file(filepath: Path, entries: list[dict[str, Any]], header: str = "") -> None:
    """Rebuild a frontmatter file from a list of parsed entry dicts.

    Each entry dict must have ``meta`` and ``body`` keys.  The ``meta``
    dict is dumped as YAML frontmatter.
    """
    lines: list[str] = []
    if header:
        lines.append(header)

    for entry in entries:
        meta = entry["meta"]
        body = entry.get("body", "")
        yaml_block = yaml.dump(meta, default_flow_style=False, sort_keys=False).rstrip()
        lines.append(f"---\n{yaml_block}\n---\n{body}")
        lines.append("")  # blank separator

    write_atomic(filepath, "\n".join(lines), encoding="utf-8")


def _update_resolved_in_file(filepath: Path, entry_ids_to_resolve: set[str]) -> int:
    """Read file, find entries by ID, set resolved=true, write back.

    Parameters
    ----------
    filepath : Path
        Path to a mistakes.md or insights.md file.
    entry_ids_to_resolve : set[str]
        Entry IDs whose ``resolved`` field should be set to ``true``.

    Returns
    -------
    int
        Number of entries actually updated.
    """
    if not filepath.exists():
        return 0

    entries = _parse_entries_in_file(filepath)
    if not entries:
        return 0

    count = 0
    for entry in entries:
        entry_id = entry["meta"].get("id", "")
        if entry_id in entry_ids_to_resolve:
            entry["meta"]["resolved"] = True
            count += 1

    if count > 0:
        # Preserve the original file header (first line if it's a markdown heading)
        original = filepath.read_text(encoding="utf-8")
        header = ""
        first_line = original.split("\n", 1)[0] if original else ""
        if first_line.startswith("#"):
            header = first_line + "\n"
        _rebuild_file(filepath, entries, header=header)

    return count


def _tags_match(entry_tags: list[str], improved_tags: list[str]) -> bool:
    """Check if any entry tags overlap with improved tags (case-insensitive)."""
    entry_lower = {t.lower() for t in entry_tags}
    improved_lower = {t.lower() for t in improved_tags}
    return bool(entry_lower & improved_lower)


def _summary_keywords_match(entry_meta: dict[str, Any], entry_body: str, evolution_summary: str) -> bool:
    """Heuristic: check if the entry's content overlaps with evolution summary keywords.

    Extracts significant words (>4 chars) from the evolution summary and
    checks if any appear in the entry's tags, content, or ID.
    """
    # Extract keywords from summary (words > 4 chars, lowercased)
    summary_words = set(
        w.lower()
        for w in re.findall(r"\b\w{5,}\b", evolution_summary)
    )
    if not summary_words:
        return False

    # Build searchable text from entry
    searchable = " ".join([
        " ".join(entry_meta.get("tags", [])),
        entry_meta.get("id", ""),
        entry_body,
    ]).lower()

    # Match if at least 2 keywords appear (or all if summary has < 3 keywords)
    threshold = min(2, len(summary_words))
    matches = sum(1 for kw in summary_words if kw in searchable)
    return matches >= threshold


def _discover_mistake_files(memory_base: Path) -> list[Path]:
    """Find all mistakes.md files under the memory directory."""
    files: list[Path] = []

    # Per-stock mistakes
    stocks_dir = memory_base / "stocks"
    if stocks_dir.exists():
        for stock_dir in stocks_dir.iterdir():
            if stock_dir.is_dir():
                mistakes_file = stock_dir / "mistakes.md"
                if mistakes_file.exists():
                    files.append(mistakes_file)

    # Global mistakes
    global_mistakes = memory_base / "global" / "mistakes.md"
    if global_mistakes.exists():
        files.append(global_mistakes)

    return files


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_related_entries(
    evolution_summary: str,
    improved_tags: list[str],
    memory_dir: Optional[str | Path] = None,
) -> int:
    """Resolve error notebook entries addressed by an evolution improvement.

    Scans all ``mistakes.md`` files (per-stock and global) for entries whose
    tags match ``improved_tags`` or whose content keyword-overlaps with
    ``evolution_summary``.  Matching entries have their ``resolved`` field
    set to ``true`` in-place.

    Parameters
    ----------
    evolution_summary : str
        Text summary of what the evolution mutation improved.
    improved_tags : list[str]
        Tags associated with the improvement (e.g. ``["timeout", "data_missing"]``).
    memory_dir : str or Path, optional
        Override memory base directory (default ``~/.vibe-trading/memory``).

    Returns
    -------
    int
        Total number of entries resolved across all files.
    """
    memory_base = _memory_dir(memory_dir)
    mistake_files = _discover_mistake_files(memory_base)

    if not mistake_files:
        return 0

    total_resolved = 0

    for filepath in mistake_files:
        entries = _parse_entries_in_file(filepath)
        if not entries:
            continue

        ids_to_resolve: set[str] = set()

        for entry in entries:
            meta = entry["meta"]
            body = entry.get("body", "")

            # Skip already-resolved entries
            if meta.get("resolved", False):
                continue

            entry_tags = meta.get("tags", [])
            if isinstance(entry_tags, str):
                entry_tags = [t.strip() for t in entry_tags.split(",")]
            if not isinstance(entry_tags, list):
                entry_tags = [str(entry_tags)] if entry_tags else []

            # Match by tag overlap
            if improved_tags and _tags_match(entry_tags, improved_tags):
                entry_id = meta.get("id", "")
                if entry_id:
                    ids_to_resolve.add(entry_id)
                continue

            # Match by keyword overlap with evolution summary
            if evolution_summary and _summary_keywords_match(meta, body, evolution_summary):
                entry_id = meta.get("id", "")
                if entry_id:
                    ids_to_resolve.add(entry_id)

        if ids_to_resolve:
            updated = _update_resolved_in_file(filepath, ids_to_resolve)
            total_resolved += updated

    return total_resolved


def get_unresolved_mistakes(
    target: str,
    memory_dir: Optional[str | Path] = None,
) -> list[MemoryEntry]:
    """Read per-stock mistakes and return only unresolved entries.

    Parameters
    ----------
    target : str
        Stock code, e.g. ``"601777.SH"``.
    memory_dir : str or Path, optional
        Override memory base directory.

    Returns
    -------
    list[MemoryEntry]
        Unresolved mistake entries for the given target.
    """
    memory_base = _memory_dir(memory_dir)
    safe_target = target.replace("/", "_") if target else "unknown"
    filepath = memory_base / "stocks" / safe_target / "mistakes.md"

    if not filepath.exists():
        return []

    raw_entries = _parse_entries_in_file(filepath)
    result: list[MemoryEntry] = []

    for raw in raw_entries:
        meta = raw["meta"]

        # Skip resolved entries
        if meta.get("resolved", False):
            continue

        # Build frontmatter text for MemoryEntry.from_yaml_frontmatter()
        body = raw.get("body", "")
        yaml_block = yaml.dump(meta, default_flow_style=False, sort_keys=False).rstrip()
        frontmatter_text = f"---\n{yaml_block}\n---\n{body}"

        try:
            entry = MemoryEntry.from_yaml_frontmatter(frontmatter_text)
            result.append(entry)
        except (ValueError, KeyError, TypeError):
            continue

    return result


def get_unresolved_insights(
    memory_dir: Optional[str | Path] = None,
) -> list[Insight]:
    """Read global insights and return only active (unresolved, count > 0) entries.

    Parameters
    ----------
    memory_dir : str or Path, optional
        Override memory base directory.

    Returns
    -------
    list[Insight]
        Unresolved insights with positive count.
    """
    memory_base = _memory_dir(memory_dir)
    filepath = memory_base / "global" / "insights.md"

    if not filepath.exists():
        return []

    raw_entries = _parse_entries_in_file(filepath)
    result: list[Insight] = []

    for raw in raw_entries:
        meta = raw["meta"]

        # Skip deprecated: count <= 0
        count = meta.get("count", 0)
        if isinstance(count, (int, float)) and count <= 0:
            continue

        # Skip resolved entries
        if meta.get("resolved", False):
            continue

        # Build frontmatter text for Insight.from_yaml_frontmatter()
        body = raw.get("body", "")
        yaml_block = yaml.dump(meta, default_flow_style=False, sort_keys=False).rstrip()
        frontmatter_text = f"---\n{yaml_block}\n---\n{body}"

        try:
            insight = Insight.from_yaml_frontmatter(frontmatter_text)
            result.append(insight)
        except (ValueError, KeyError, TypeError):
            continue

    return result
