"""Graduated scope controller for evolution cycles.

Determines how much of the evolution pipeline to run based on recent
performance metrics.  High-performing systems skip most work; struggling
systems get a comprehensive overhaul.

Scope levels (ascending intensity):
- SKIP:          Only update the error notebook; no insight/agent/skill work.
- MINIMAL:       Error notebook + insight refresh.
- TARGETED:      Error notebook + insights + AGENTS.md updates.
- COMPREHENSIVE: Full pipeline including new skill generation.

Public API
----------
- ``determine_evolution_scope(metrics)`` → ScopeLevel
- ``should_run_evolution(scope)``        → dict of component flags
- ``get_performance_metrics(logs_dir)``  → PerformanceMetrics
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"
_DEFAULT_LOGS_DIR = Path("/opt/qdata/cron_jobs/logs")
_EVOLUTION_LOG_NAME = "evolution_log.json"

# ---------------------------------------------------------------------------
# ScopeLevel enum
# ---------------------------------------------------------------------------


class ScopeLevel(str, Enum):
    """Graduated evolution scope, from lightest to heaviest."""

    SKIP = "skip"
    MINIMAL = "minimal"
    TARGETED = "targeted"
    COMPREHENSIVE = "comprehensive"


# ---------------------------------------------------------------------------
# PerformanceMetrics dataclass
# ---------------------------------------------------------------------------


@dataclass
class PerformanceMetrics:
    """Snapshot of recent system performance."""

    cron_success_rate: float  # 0.0–1.0
    no_improvement_cycles: int  # consecutive cycles with no score gain
    error_notebook_entries: int  # unresolved mistakes.md entries
    last_evolution_score: float  # most recent evolution candidate score (0.0–1.0)


# ---------------------------------------------------------------------------
# Scope determination
# ---------------------------------------------------------------------------


_SCOPE_ORDER = (
    ScopeLevel.SKIP,
    ScopeLevel.MINIMAL,
    ScopeLevel.TARGETED,
    ScopeLevel.COMPREHENSIVE,
)


def _escalate_scope(scope, *, steps=1):
    """Move *scope* up *steps* levels, capped at COMPREHENSIVE."""
    idx = _SCOPE_ORDER.index(scope)
    return _SCOPE_ORDER[min(idx + steps, len(_SCOPE_ORDER) - 1)]


def determine_evolution_scope(metrics: PerformanceMetrics) -> ScopeLevel:
    """Map *metrics* to the appropriate :class:`ScopeLevel`.

    Rules (evaluated in order):
    1. cron_success_rate ≥ 0.90 **and** ≥ 2 stagnant cycles → SKIP
    2. cron_success_rate ≥ 0.80 → MINIMAL
    3. cron_success_rate ≥ 0.60 → TARGETED
    4. cron_success_rate <  0.60 → COMPREHENSIVE

    *error_notebook_entries* and *last_evolution_score* provide
    secondary signals that escalate the scope when they indicate
    systemic trouble.
    """
    if metrics.cron_success_rate >= 0.90 and metrics.no_improvement_cycles >= 2:
        scope = ScopeLevel.SKIP
    elif metrics.cron_success_rate >= 0.80:
        scope = ScopeLevel.MINIMAL
    elif metrics.cron_success_rate >= 0.60:
        scope = ScopeLevel.TARGETED
    else:
        scope = ScopeLevel.COMPREHENSIVE

    # Escalate when many unresolved mistakes remain (stale error notebook)
    if metrics.error_notebook_entries > 5:
        scope = _escalate_scope(scope)

    # Escalate when the last evolution candidate scored poorly
    if metrics.last_evolution_score < 0.5:
        scope = _escalate_scope(scope)

    return scope


# ---------------------------------------------------------------------------
# Component flags
# ---------------------------------------------------------------------------

# Maps each ScopeLevel to the set of pipeline components that should run.
_SCOPE_FLAGS: Dict[ScopeLevel, Dict[str, bool]] = {
    ScopeLevel.SKIP: {
        "error_notebook": True,
        "insights": False,
        "agents_md": False,
        "new_skills": False,
    },
    ScopeLevel.MINIMAL: {
        "error_notebook": True,
        "insights": True,
        "agents_md": False,
        "new_skills": False,
    },
    ScopeLevel.TARGETED: {
        "error_notebook": True,
        "insights": True,
        "agents_md": True,
        "new_skills": False,
    },
    ScopeLevel.COMPREHENSIVE: {
        "error_notebook": True,
        "insights": True,
        "agents_md": True,
        "new_skills": True,
    },
}


def should_run_evolution(scope: ScopeLevel) -> Dict[str, bool]:
    """Return a dict of component flags for the given *scope*.

    Keys: ``error_notebook``, ``insights``, ``agents_md``, ``new_skills``.
    """
    return dict(_SCOPE_FLAGS[scope])


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------


def _scan_cron_logs(logs_dir: Path) -> tuple[int, int]:
    """Return (successes, total) from cron log files.

    Scans timestamped log files (``*_YYYYMMDDTHHMMSSZ.log``) for exit-code
    and keyword patterns.  The bare ``<task>.log`` pointer files are skipped.
    """
    successes = 0
    total = 0

    if not logs_dir.is_dir():
        logger.warning("logs directory not found: %s", logs_dir)
        return successes, total

    # Match timestamped log files only (skip bare pointer logs)
    log_pattern = re.compile(r"^.+_\d{8}T\d{6}Z\.log$")

    for log_file in sorted(logs_dir.iterdir()):
        if not log_file.is_file() or not log_pattern.match(log_file.name):
            continue

        total += 1
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("could not read %s", log_file)
            continue

        # Check for success indicators:
        # 1. JSON exit code 0
        # 2. Explicit SUCCESS / OK keywords
        # 3. errcode 0 (DingTalk success)
        is_success = False

        # Fast check: look for exit code patterns in JSON lines
        if '"exit":0' in content or '"exit": 0' in content:
            # Verify no fatal errors override the exit code
            if '"exit":1' not in content and '"exit": 1' not in content:
                is_success = True

        # Fallback: keyword-based detection
        if not is_success:
            upper = content.upper()
            if "SUCCESS" in upper or "VERIFICATION_RESULT=OK" in upper:
                is_success = True

        if is_success:
            successes += 1

    return successes, total


def _count_mistakes_entries(memory_base: Path | None = None) -> int:
    """Count unresolved entries across all ``mistakes.md`` files."""
    base = memory_base or _MEMORY_BASE
    count = 0

    # Pattern matches YAML-frontmatter entries (--- delimited blocks)
    entry_sep = re.compile(r"^---\s*$", re.MULTILINE)

    search_dirs = [
        base / "stocks",
        base / "global",
    ]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for mistakes_file in search_dir.rglob("mistakes.md"):
            try:
                text = mistakes_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Count YAML-frontmatter-delimited entries.
            # Heuristic: entries ≈ separators // 2 because each entry is
            # bracketed by an opening --- and a closing ---.  This is an
            # approximation – a lone leading/trailing ---, horizontal rules
            # inside entry bodies, or entries without closing --- will
            # over-count or under-count.  Resolution: count --- lines
            # and divide by two; floor at zero to avoid negative counts.
            separators = entry_sep.findall(text)
            count += max(len(separators) // 2, 0)

    return count


def _read_last_evolution_score(memory_base: Path | None = None) -> float:
    """Read the most recent evolution score from the evolution log."""
    base = memory_base or _MEMORY_BASE
    log_path = base / _EVOLUTION_LOG_NAME

    if not log_path.exists():
        # Also check workspace-level location
        alt_path = Path("/opt/qdata") / _EVOLUTION_LOG_NAME
        if alt_path.exists():
            log_path = alt_path
        else:
            return 0.0

    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("could not parse evolution log: %s", log_path)
        return 0.0

    # Support both list-of-entries and single-object formats
    if isinstance(data, list) and data:
        last_entry = data[-1]
    elif isinstance(data, dict):
        last_entry = data
    else:
        return 0.0

    # Try common score field names
    for key in ("score", "best_score", "candidate_score", "final_score"):
        if key in last_entry:
            try:
                return float(last_entry[key])
            except (TypeError, ValueError):
                continue

    return 0.0


def _count_stagnant_cycles(memory_base: Path | None = None) -> int:
    """Count consecutive evolution cycles with no score improvement."""
    base = memory_base or _MEMORY_BASE
    log_path = base / _EVOLUTION_LOG_NAME

    if not log_path.exists():
        alt_path = Path("/opt/qdata") / _EVOLUTION_LOG_NAME
        if alt_path.exists():
            log_path = alt_path
        else:
            return 0

    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0

    if not isinstance(data, list) or len(data) < 2:
        return 0

    # Walk backwards counting consecutive non-improvements
    stagnant = 0
    for i in range(len(data) - 1, 0, -1):
        curr_score = data[i].get("score", data[i].get("best_score", 0))
        prev_score = data[i - 1].get("score", data[i - 1].get("best_score", 0))
        try:
            if float(curr_score) <= float(prev_score):
                stagnant += 1
            else:
                break
        except (TypeError, ValueError):
            stagnant += 1

    return stagnant


def get_performance_metrics(
    logs_dir: str | Path = _DEFAULT_LOGS_DIR,
) -> PerformanceMetrics:
    """Collect live performance metrics from the filesystem.

    Parameters
    ----------
    logs_dir : str or Path
        Directory containing cron job log files.
        Defaults to ``/opt/qdata/cron_jobs/logs``.

    Returns
    -------
    PerformanceMetrics
        Populated metrics snapshot.
    """
    logs_path = Path(logs_dir)

    # 1. Cron success rate
    successes, total = _scan_cron_logs(logs_path)
    cron_success_rate = (successes / total) if total > 0 else 0.0

    # 2. Error notebook entries
    error_notebook_entries = _count_mistakes_entries()

    # 3. Last evolution score
    last_evolution_score = _read_last_evolution_score()

    # 4. Stagnant cycles
    no_improvement_cycles = _count_stagnant_cycles()

    metrics = PerformanceMetrics(
        cron_success_rate=round(cron_success_rate, 4),
        no_improvement_cycles=no_improvement_cycles,
        error_notebook_entries=error_notebook_entries,
        last_evolution_score=round(last_evolution_score, 4),
    )

    logger.info(
        "PerformanceMetrics: success_rate=%.2f, stagnant=%d, mistakes=%d, last_score=%.3f",
        metrics.cron_success_rate,
        metrics.no_improvement_cycles,
        metrics.error_notebook_entries,
        metrics.last_evolution_score,
    )

    return metrics
