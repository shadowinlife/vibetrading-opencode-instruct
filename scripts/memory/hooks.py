"""Session and run completion hooks.

Orchestrates the memory pipeline after a session or swarm run completes:
1. Load trajectory from parser
2. Detect failures (reflexion.py)
3. If failure → generate reflection + append to mistakes
4. If backtest results → score trajectory (scoring.py) + create analysis memory entry

These hooks are called explicitly by cron jobs or manual triggers.
They do NOT run automatically (no file watchers).
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .parsers.common import Trajectory
from .schema import MemoryEntry
from .scoring import ScoreResult, score_trajectory
from scripts.memory.utils import _MEMORY_BASE, write_atomic

# ---------------------------------------------------------------------------
# Graceful import of reflexion module (parallel task T6 — may not exist yet)
# ---------------------------------------------------------------------------
try:
    from .reflexion import detect_failure, generate_reflection, append_to_mistakes
    _HAS_REFLEXION = True
except ImportError:
    _HAS_REFLEXION = False

# ---------------------------------------------------------------------------
# Graceful imports for evolution orchestration (parallel tasks — may not exist yet)
# ---------------------------------------------------------------------------
try:
    from .scope_controller import (
        get_performance_metrics,
        determine_evolution_scope,
        should_run_evolution,
        ScopeLevel,
    )
    _HAS_SCOPE_CONTROLLER = True
except ImportError:
    _HAS_SCOPE_CONTROLLER = False

try:
    from .expel import extract_insights, write_insights, get_top_insights
    _HAS_EXPEL = True
except ImportError:
    _HAS_EXPEL = False

try:
    from .evolution import run_evolution_cycle as _evolution_run_cycle
    _HAS_EVOLUTION = True
except ImportError:
    _HAS_EVOLUTION = False

try:
    from .truncation import truncate_trajectories
    _HAS_TRUNCATION = True
except ImportError:
    _HAS_TRUNCATION = False

try:
    from .attribution import attribute_failures, aggregate_attributions
    _HAS_ATTRIBUTION = True
except ImportError:
    _HAS_ATTRIBUTION = False

try:
    from .git_committer import commit_evolution, send_evolution_notification
    _HAS_GIT_COMMITTER = True
except ImportError:
    _HAS_GIT_COMMITTER = False

try:
    from .feedback_reset import resolve_related_entries
    _HAS_FEEDBACK_RESET = True
except ImportError:
    _HAS_FEEDBACK_RESET = False

try:
    from .parsers.opencode import load_trajectories as _load_oc_trajectories
    _HAS_OC_PARSER = True
except ImportError:
    _HAS_OC_PARSER = False

try:
    from .parsers.swarm import load_trajectories as _load_swarm_trajectories
    _HAS_SWARM_PARSER = True
except ImportError:
    _HAS_SWARM_PARSER = False

logger = logging.getLogger(__name__)

# Memory root directory
_MEMORY_ROOT = Path.home() / ".vibe-trading" / "memory"


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class HookResult:
    """Summary of actions taken by a completion hook."""

    session_id: str
    target: str
    failure_detected: bool
    failure_type: str = ""
    mistake_entry_id: str = ""
    score: float = 0.0
    score_method: str = ""
    analysis_entry_id: str = ""
    errors: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_trajectory(
    trajectories: list[Trajectory],
    session_id: str,
) -> Optional[Trajectory]:
    """Find a trajectory matching the given session_id."""
    for traj in trajectories:
        if traj.session_id == session_id:
            return traj
    return None


def _summarise_trajectory(trajectory: Trajectory, max_steps: int = 5) -> str:
    """Build a concise summary of a trajectory for memory entry content.

    Includes the first few steps and the outcome.
    """
    lines: list[str] = []
    lines.append(f"# Session: {trajectory.session_id}")
    lines.append(f"**Source:** {trajectory.source}")
    lines.append(f"**Target:** {trajectory.target or 'N/A'}")
    lines.append(f"**Timestamp:** {trajectory.timestamp.isoformat()}")
    lines.append("")

    # First N steps
    lines.append("## Key Steps")
    for i, step in enumerate(trajectory.steps[:max_steps]):
        role = step.role
        content = step.content[:200] if step.content else ""
        if step.tool_name:
            lines.append(f"- [{role}] `{step.tool_name}`: {content}")
        else:
            lines.append(f"- [{role}] {content}")
    if len(trajectory.steps) > max_steps:
        lines.append(f"- ... ({len(trajectory.steps) - max_steps} more steps)")
    lines.append("")

    # Outcome
    if trajectory.outcome:
        lines.append("## Outcome")
        lines.append(f"- Score: {trajectory.outcome.score}")
        lines.append(f"- Type: {trajectory.outcome.type}")
        if trajectory.outcome.details:
            lines.append(f"- Details: {trajectory.outcome.details}")

    return "\n".join(lines)


def _create_analysis_entry(
    trajectory: Trajectory,
    score_result: ScoreResult,
) -> MemoryEntry:
    """Create a MemoryEntry of type='analysis' from a scored trajectory.

    Parameters
    ----------
    trajectory : Trajectory
        The parsed conversation trajectory.
    score_result : ScoreResult
        The scoring result (score, method, details).

    Returns
    -------
    MemoryEntry
        A new analysis memory entry.
    """
    now = datetime.now(tz=timezone.utc)
    target = trajectory.target or "unknown"
    date_str = now.strftime("%Y%m%d")
    short_id = uuid.uuid4().hex[:8]
    entry_id = f"mem_{target}_{date_str}_{short_id}"

    # Build content from trajectory summary + scoring details
    content = _summarise_trajectory(trajectory)
    content += f"\n\n## Scoring\n"
    content += f"- **Score:** {score_result.score:.4f}\n"
    content += f"- **Method:** {score_result.method}\n"
    if score_result.details:
        content += f"- **Details:** {score_result.details}\n"

    # Collect tags from trajectory metadata
    tags: list[str] = []
    metadata = trajectory.metadata or {}
    if isinstance(metadata, dict):
        raw_tags = metadata.get("tags", [])
        if isinstance(raw_tags, list):
            tags.extend(str(t) for t in raw_tags)
        # Add source-based tags
        if trajectory.source:
            tags.append(f"source:{trajectory.source}")
        if score_result.method:
            tags.append(f"scored:{score_result.method}")
        preset = metadata.get("preset_name", "")
        if preset:
            tags.append(f"preset:{preset}")

    # Determine source string
    source = trajectory.source
    if trajectory.source == "swarm":
        preset = metadata.get("preset_name", "")
        if preset:
            source = f"swarm/{preset}"

    return MemoryEntry(
        id=entry_id,
        target=target,
        type="analysis",
        created=now,
        last_accessed=now,
        access_count=0,
        confidence=max(0.0, min(1.0, score_result.score)),
        tags=tags,
        source=source,
        resolved=False,
        content=content,
    )


def _write_analysis_entry(entry: MemoryEntry) -> Path:
    """Write an analysis MemoryEntry to disk.

    Writes to ``~/.vibe-trading/memory/stocks/{target}/analyses/{date}_{id}.md``.
    Creates the directory if it doesn't exist.

    Returns the path to the written file.
    """
    target = entry.target or "unknown"
    analyses_dir = _MEMORY_ROOT / "stocks" / target / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)

    date_str = entry.created.strftime("%Y%m%d")
    filename = f"{date_str}_{entry.id}.md"
    filepath = analyses_dir / filename

    write_atomic(filepath, entry.to_yaml_frontmatter(), encoding="utf-8")
    logger.info("Wrote analysis entry: %s", filepath)
    return filepath


def _build_simplified_reflection(
    trajectory: Trajectory,
    failure: Any,
    result: HookResult,
) -> str:
    """Build a simplified reflection from failure info without calling an LLM.

    Extracts key facts from the FailureInfo object and trajectory to create
    a human-readable reflection suitable for the mistakes notebook.
    """
    # Extract failure attributes (handle both dict and dataclass)
    if isinstance(failure, dict):
        f_type = failure.get("type", "unknown")
        f_desc = failure.get("description", "")
        f_step = failure.get("step_index", -1)
        f_evidence = failure.get("evidence", "")
    else:
        f_type = getattr(failure, "type", "unknown")
        f_desc = getattr(failure, "description", "")
        f_step = getattr(failure, "step_index", -1)
        f_evidence = getattr(failure, "evidence", "")

    target = result.target or "N/A"
    source = trajectory.source or "unknown"

    lines = [
        f"## Failure Reflection — {result.session_id}",
        f"",
        f"- **Date:** {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Target:** {target}",
        f"- **Source:** {source}",
        f"- **Failure type:** {f_type}",
        f"- **Description:** {f_desc}",
        f"- **Step index:** {f_step}",
    ]

    if f_evidence:
        # Truncate evidence to keep reflections concise
        ev = str(f_evidence)[:500]
        lines.append(f"- **Evidence:** {ev}")

    # Add context from nearby steps
    steps = trajectory.steps or []
    if 0 <= f_step < len(steps):
        window_start = max(0, f_step - 1)
        window_end = min(len(steps), f_step + 2)
        lines.append("")
        lines.append("### Context (nearby steps)")
        for i in range(window_start, window_end):
            step = steps[i]
            role = step.role if hasattr(step, "role") else "?"
            content = (step.content if hasattr(step, "content") else "")[:200]
            marker = " <<<" if i == f_step else ""
            lines.append(f"- Step {i} [{role}]{marker}: {content}")

    return "\n".join(lines)


def _run_failure_detection(
    trajectory: Trajectory,
    result: HookResult,
) -> None:
    """Run failure detection on a trajectory and update HookResult.

    If reflexion module is available, uses it. Otherwise logs a warning
    and skips failure detection.

    On failure:
    - Sets result.failure_detected = True
    - Sets result.failure_type
    - Creates a simplified reflection (no LLM) and appends to mistakes
    - Sets result.mistake_entry_id
    """
    if not _HAS_REFLEXION:
        logger.warning(
            "reflexion module not available — skipping failure detection "
            "for session %s",
            result.session_id,
        )
        return

    try:
        failure = detect_failure(trajectory)
    except Exception as exc:
        logger.error("detect_failure raised for %s: %s", result.session_id, exc)
        result.errors.append(f"detect_failure: {exc}")
        return

    if failure is None:
        return

    result.failure_detected = True

    # Extract failure type — handle both dict and object returns
    if isinstance(failure, dict):
        result.failure_type = failure.get("type", failure.get("failure_type", "unknown"))
    else:
        result.failure_type = getattr(failure, "type", "") or getattr(
            failure, "failure_type", "unknown"
        )

    # Generate a simplified reflection without LLM.
    # generate_reflection() returns an LLM prompt (not the reflection itself),
    # so we build a direct reflection from the failure info instead.
    reflection_text = _build_simplified_reflection(trajectory, failure, result)

    # Append to mistakes registry
    try:
        mistake_id = append_to_mistakes(result.target, reflection_text)
        result.mistake_entry_id = str(mistake_id) if mistake_id else ""
    except Exception as exc:
        logger.error("append_to_mistakes raised for %s: %s", result.session_id, exc)
        result.errors.append(f"append_to_mistakes: {exc}")


def _run_scoring(
    trajectory: Trajectory,
    result: HookResult,
) -> None:
    """Score a trajectory and create an analysis entry if applicable.

    Updates HookResult with score, method, and analysis_entry_id.
    """
    try:
        score_result = score_trajectory(trajectory)
    except Exception as exc:
        logger.error("score_trajectory raised for %s: %s", result.session_id, exc)
        result.errors.append(f"score_trajectory: {exc}")
        return

    result.score = score_result.score
    result.score_method = score_result.method

    # Only create analysis entry if we got a real score (not LLM sentinel -1.0)
    if score_result.score < 0:
        logger.info(
            "LLM-judge scoring returned sentinel — skipping analysis entry "
            "(needs external LLM execution) for %s",
            result.session_id,
        )
        return

    try:
        entry = _create_analysis_entry(trajectory, score_result)
        _write_analysis_entry(entry)
        result.analysis_entry_id = entry.id
    except Exception as exc:
        logger.error("Failed to create/write analysis entry for %s: %s", result.session_id, exc)
        result.errors.append(f"analysis_entry: {exc}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def on_session_complete(
    session_id: str,
    db_path: Optional[str] = None,
) -> HookResult:
    """Process a completed OpenCode session.

    1. Load trajectory from OpenCode parser
    2. Run failure detection (if reflexion available)
    3. Score trajectory and create analysis entry (if backtest results present)

    Parameters
    ----------
    session_id : str
        The OpenCode session ID to process.
    db_path : str, optional
        Path to OpenCode SQLite database. Defaults to standard location.

    Returns
    -------
    HookResult
        Summary of actions taken.
    """
    result = HookResult(
        session_id=session_id,
        target="",
        failure_detected=False,
    )

    # 1. Load trajectory
    try:
        if not (_HAS_OC_PARSER and _load_oc_trajectories):
            raise ImportError("OpenCode parser not available")
        kwargs: dict[str, Any] = {"days": 1}
        if db_path:
            kwargs["db_path"] = db_path
        trajectories = _load_oc_trajectories(**kwargs)
    except Exception as exc:
        logger.error("Failed to load OpenCode trajectories: %s", exc)
        result.errors.append(f"load_trajectories: {exc}")
        return result

    trajectory = _find_trajectory(trajectories, session_id)
    if trajectory is None:
        result.errors.append(
            f"Session {session_id} not found in last 1 day(s) of OpenCode sessions"
        )
        return result

    result.target = trajectory.target or ""

    # 2. Failure detection
    _run_failure_detection(trajectory, result)

    # 3. Scoring + analysis entry
    _run_scoring(trajectory, result)

    return result


def on_swarm_run_complete(
    run_id: str,
    run_dir: Optional[str] = None,
) -> HookResult:
    """Process a completed Swarm run.

    1. Load trajectory from Swarm parser
    2. Run failure detection (if reflexion available)
    3. Score trajectory and create analysis entry (if backtest results present)

    Parameters
    ----------
    run_id : str
        The Swarm run ID to process.
    run_dir : str, optional
        Path to swarm runs directory. Defaults to standard location.

    Returns
    -------
    HookResult
        Summary of actions taken.
    """
    result = HookResult(
        session_id=run_id,
        target="",
        failure_detected=False,
    )

    # 1. Load trajectory
    try:
        if not (_HAS_SWARM_PARSER and _load_swarm_trajectories):
            raise ImportError("Swarm parser not available")
        kwargs: dict[str, Any] = {}
        if run_dir:
            kwargs["run_dir"] = run_dir
        trajectories = _load_swarm_trajectories(**kwargs)
    except Exception as exc:
        logger.error("Failed to load Swarm trajectories: %s", exc)
        result.errors.append(f"load_trajectories: {exc}")
        return result

    trajectory = _find_trajectory(trajectories, run_id)
    if trajectory is None:
        result.errors.append(
            f"Run {run_id} not found in Swarm runs directory"
        )
        return result

    result.target = trajectory.target or ""

    # 2. Failure detection
    _run_failure_detection(trajectory, result)

    # 3. Scoring + analysis entry
    _run_scoring(trajectory, result)

    return result


# ---------------------------------------------------------------------------
# Orchestration helpers for cron-driven memory evolution
# ---------------------------------------------------------------------------

_CONTEXT_BUDGET = 120_000  # tokens for truncation


def _send_dingtalk(title: str, markdown: str) -> bool:
    """Send a DingTalk notification. Returns True on success."""
    try:
        import json
        from urllib import request as url_request

        webhook = os.environ.get("DINGTALK_WEBHOOK", "")
        if not webhook:
            logger.warning("DINGTALK_WEBHOOK not set — skipping notification")
            return False

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": markdown},
        }
        data = json.dumps(payload).encode("utf-8")
        req = url_request.Request(
            webhook,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with url_request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            if resp.status != 200:
                logger.warning(
                    "DingTalk HTTP %d: %s", resp.status, body.decode("utf-8", errors="replace")
                )
                return False
            response_data = json.loads(body)
            errcode = response_data.get("errcode", -1)
            if errcode != 0:
                logger.warning(
                    "DingTalk errcode=%d, errmsg=%s",
                    errcode,
                    response_data.get("errmsg", "unknown"),
                )
                return False
        return True
    except Exception as exc:
        logger.error("DingTalk notification failed: %s", exc)
        return False


def _load_all_trajectories(days: int = 7) -> list:
    """Load trajectories from both OpenCode and Swarm parsers."""
    trajectories: list = []
    if _HAS_OC_PARSER:
        try:
            trajectories.extend(_load_oc_trajectories(days=days))
        except Exception as exc:
            logger.warning("Failed to load OpenCode trajectories: %s", exc)
    if _HAS_SWARM_PARSER:
        try:
            trajectories.extend(_load_swarm_trajectories())
        except Exception as exc:
            logger.warning("Failed to load Swarm trajectories: %s", exc)
    return trajectories


# ---------------------------------------------------------------------------
# Public API: Weekly Insight Extraction (ExpeL)
# ---------------------------------------------------------------------------

def run_weekly_insight_extraction() -> dict:
    """Orchestrate weekly insight extraction (ExpeL).

    1. Check scope controller for evolution intensity
    2. Load trajectories from OpenCode + Swarm parsers
    3. Run truncation handler
    4. Run insight extraction (ExpeL)
    5. Write insights to file
    6. Send DingTalk notification with summary

    Returns
    -------
    dict
        Summary with keys: scope, trajectories_loaded, insights_extracted,
        insights_written, errors, timestamp.
    """
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    summary: dict[str, Any] = {
        "scope": "UNKNOWN",
        "trajectories_loaded": 0,
        "insights_extracted": 0,
        "insights_written": False,
        "errors": [],
        "timestamp": timestamp,
    }

    try:
        # 1. Check scope controller
        scope_name = "UNKNOWN"
        flags: dict[str, bool] = {"insights": True}
        if _HAS_SCOPE_CONTROLLER:
            metrics = get_performance_metrics()
            scope = determine_evolution_scope(metrics)
            scope_name = scope.name if hasattr(scope, "name") else str(scope)
            flags = should_run_evolution(scope)
            summary["scope"] = scope_name
            logger.info("Scope controller: %s — flags=%s", scope_name, flags)
        else:
            logger.warning("scope_controller unavailable — running with defaults")

        # If scope says skip insights, still run error notebook only
        if not flags.get("insights", True):
            logger.info("Scope SKIP: insights disabled, running error notebook only")
            summary["scope"] = f"{scope_name} (insights skipped)"
            _send_dingtalk(
                "OPENCODE Weekly Insight",
                f"### OPENCODE Weekly Insight Extraction | {timestamp[:10]}\n\n"
                f"- Scope: **{scope_name}** — insights skipped (system healthy)\n"
                f"- Error notebook: updated\n",
            )
            return summary

        # 2. Load trajectories
        trajectories = _load_all_trajectories(days=7)
        summary["trajectories_loaded"] = len(trajectories)
        logger.info("Loaded %d trajectories", len(trajectories))

        # 3. Truncation
        if _HAS_TRUNCATION and trajectories:
            trajectories, level = truncate_trajectories(trajectories, _CONTEXT_BUDGET)
            logger.info("Truncation level %d — %d trajectories remain", level, len(trajectories))

        # 4. Extract insights
        if not _HAS_EXPEL:
            summary["errors"].append("expel module not available")
            logger.error("expel module not available — cannot extract insights")
        else:
            # Load existing insights for dedup
            existing = get_top_insights(limit=100, min_count=0)
            insights = extract_insights(trajectories, existing_insights=existing)
            summary["insights_extracted"] = len(insights)
            logger.info("Extracted %d insights (%d new)",
                        len(insights), len(insights) - len(existing))

            # 5. Write insights
            write_insights(insights)
            summary["insights_written"] = True
            logger.info("Insights written to disk")

        # 6. Notification
        new_count = summary["insights_extracted"]
        _send_dingtalk(
            "OPENCODE Weekly Insight",
            f"### OPENCODE Weekly Insight Extraction | {timestamp[:10]}\n\n"
            f"- Scope: **{scope_name}**\n"
            f"- Trajectories: {summary['trajectories_loaded']}\n"
            f"- Insights: {new_count} total\n"
            f"- Errors: {', '.join(summary['errors']) if summary['errors'] else 'none'}\n",
        )

    except Exception as exc:
        logger.error("run_weekly_insight_extraction failed: %s", exc, exc_info=True)
        summary["errors"].append(str(exc))
        _send_dingtalk(
            "OPENCODE Weekly Insight",
            f"### OPENCODE Weekly Insight Extraction | {timestamp[:10]}\n\n"
            f"- **FAILED**: {exc}\n",
        )

    return summary


# ---------------------------------------------------------------------------
# Public API: Bi-Weekly Evolution Cycle (A-Evolve)
# ---------------------------------------------------------------------------

def _append_evolution_log(
    scope: str,
    candidates_generated: int,
    commit_hash: str,
    score_before: float,
    score_after: float,
) -> None:
    """Append an entry to the skills_evolution log.

    Writes to ``_MEMORY_BASE / "skills_evolution" / "_evolution_log.md"``.
    Creates the file with a header row if it doesn't exist yet.
    """
    log_path = _MEMORY_BASE / "skills_evolution" / "_evolution_log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    score_change = score_after - score_before
    line = (
        f"| {timestamp} | {scope} | {candidates_generated} "
        f"| {commit_hash} | {score_change:+.4f} |\n"
    )

    # If file is empty or only has the "no evolutions" placeholder, add header
    needs_header = (
        not log_path.exists()
        or log_path.stat().st_size < 10
        or "No evolutions yet" in log_path.read_text(encoding="utf-8")
    )
    if needs_header:
        header = (
            "| Timestamp | Scope | Candidates Generated | Commit Hash | Score Change |\n"
            "|-----------|-------|---------------------|-------------|-------------|\n"
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(header)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)

    logger.info("Appended evolution log entry: %s", line.strip())


def run_evolution_cycle() -> dict:
    """Orchestrate bi-weekly workspace evolution (A-Evolve / COPRO).

    1. Check scope controller — if SKIP, only run error notebook + notify
    2. Load trajectories from OpenCode + Swarm parsers
    3. Run truncation handler
    4. Run insight extraction
    5. If scope >= TARGETED: run failure attribution + candidate generation
    6. If candidate selected: commit evolution + check stability
    7. If improvement found: reset feedback history
    8. Send DingTalk notification with full summary

    Returns
    -------
    dict
        Summary with keys: scope, trajectories_loaded, insights_extracted,
        attributions, candidate_selected, commit_hash, feedback_resolved,
        errors, timestamp.
    """
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    summary: dict[str, Any] = {
        "scope": "UNKNOWN",
        "trajectories_loaded": 0,
        "insights_extracted": 0,
        "attributions": 0,
        "candidate_selected": False,
        "commit_hash": "",
        "feedback_resolved": 0,
        "errors": [],
        "timestamp": timestamp,
    }

    try:
        # 1. Check scope controller
        scope_name = "UNKNOWN"
        flags: dict[str, bool] = {
            "error_notebook": True,
            "insights": True,
            "agents_md": True,
            "new_skills": False,
        }
        if _HAS_SCOPE_CONTROLLER:
            metrics = get_performance_metrics()
            scope = determine_evolution_scope(metrics)
            scope_name = scope.name if hasattr(scope, "name") else str(scope)
            flags = should_run_evolution(scope)
            summary["scope"] = scope_name
            logger.info("Scope controller: %s — flags=%s", scope_name, flags)
        else:
            logger.warning("scope_controller unavailable — running with defaults")

        # If SKIP: only error notebook + notify
        if scope_name == "SKIP":
            logger.info("Scope SKIP: system healthy — only error notebook")
            _send_dingtalk(
                "OPENCODE Evolution Cycle",
                f"### OPENCODE Evolution Cycle | {timestamp[:10]}\n\n"
                f"- Scope: **SKIP** (system healthy, no evolution needed)\n"
                f"- Error notebook: updated\n"
                f"- Cron success rate: high, no stagnant cycles\n",
            )
            return summary

        # 2. Load trajectories
        trajectories = _load_all_trajectories(days=14)
        summary["trajectories_loaded"] = len(trajectories)
        logger.info("Loaded %d trajectories", len(trajectories))

        # 3. Truncation
        if _HAS_TRUNCATION and trajectories:
            trajectories, level = truncate_trajectories(trajectories, _CONTEXT_BUDGET)
            logger.info("Truncation level %d — %d trajectories remain", level, len(trajectories))

        # 4. Insight extraction (always run if not SKIP)
        if _HAS_EXPEL and flags.get("insights", True):
            existing = get_top_insights(limit=100, min_count=0)
            insights = extract_insights(trajectories, existing_insights=existing)
            summary["insights_extracted"] = len(insights)
            write_insights(insights)
            logger.info("Extracted %d insights", len(insights))

        # 5. If scope >= TARGETED: attribution + candidate generation
        if not flags.get("agents_md", False):
            logger.info("Scope %s: agents_md evolution skipped", scope_name)
            _send_dingtalk(
                "OPENCODE Evolution Cycle",
                f"### OPENCODE Evolution Cycle | {timestamp[:10]}\n\n"
                f"- Scope: **{scope_name}**\n"
                f"- Trajectories: {summary['trajectories_loaded']}\n"
                f"- Insights: {summary['insights_extracted']}\n"
                f"- AGENTS.md evolution: skipped (scope < TARGETED)\n",
            )
            return summary

        # Run attribution
        attributions: list = []
        if _HAS_ATTRIBUTION and trajectories:
            try:
                attributions = attribute_failures(trajectories)
                attributions = aggregate_attributions(attributions)
                summary["attributions"] = len(attributions)
                logger.info("Attributed %d failure patterns", len(attributions))
            except Exception as exc:
                logger.error("Attribution failed: %s", exc)
                summary["errors"].append(f"attribution: {exc}")

        # Run evolution cycle from evolution module
        if not _HAS_EVOLUTION:
            summary["errors"].append("evolution module not available")
            logger.error("evolution module not available — cannot generate candidates")
        else:
            evo_result = _evolution_run_cycle(
                workspace_dir="/opt/qdata",
                trajectories=trajectories,
                attributions=attributions,
            )

            candidate = evo_result.get("candidate")
            if candidate is None:
                logger.info("No candidate exceeded improvement threshold")
                _send_dingtalk(
                    "OPENCODE Evolution Cycle",
                    f"### OPENCODE Evolution Cycle | {timestamp[:10]}\n\n"
                    f"- Scope: **{scope_name}**\n"
                    f"- Trajectories: {summary['trajectories_loaded']}\n"
                    f"- Insights: {summary['insights_extracted']}\n"
                    f"- Attributions: {summary['attributions']}\n"
                    f"- Candidates evaluated: {evo_result.get('candidates_evaluated', 0)}\n"
                    f"- Result: no candidate exceeded threshold\n",
                )
                return summary

            # 6. Candidate selected — commit evolution
            summary["candidate_selected"] = True
            diff_text = getattr(candidate, "diff", "") or ""
            cand_summary = getattr(candidate, "summary", "") or ""
            score_before = evo_result.get("current_score", 0.5)
            score_after = getattr(candidate, "score", 0.0)

            if _HAS_GIT_COMMITTER and diff_text:
                commit_result = commit_evolution(
                    diff=diff_text,
                    summary=cand_summary,
                    context=f"scope={scope_name}, attributions={summary['attributions']}",
                    score_before=score_before,
                    score_after=score_after,
                )
                if commit_result.success:
                    summary["commit_hash"] = commit_result.commit_hash
                    logger.info("Evolution committed: %s", commit_result.commit_hash)

                    # Append to evolution log
                    _append_evolution_log(
                        scope=scope_name,
                        candidates_generated=evo_result.get("candidates_evaluated", 0),
                        commit_hash=commit_result.commit_hash,
                        score_before=score_before,
                        score_after=score_after,
                    )

                    # Send evolution-specific notification
                    send_evolution_notification(commit_result, diff_preview=diff_text)

                    # 7. If improvement: reset feedback history
                    if _HAS_FEEDBACK_RESET and score_after > score_before:
                        improved_tags = []
                        for attr in attributions:
                            ref = getattr(attr, "instruction_ref", "")
                            if ref:
                                improved_tags.append(ref)
                        resolved = resolve_related_entries(
                            evolution_summary=cand_summary,
                            improved_tags=improved_tags,
                        )
                        summary["feedback_resolved"] = resolved
                        logger.info("Resolved %d feedback entries", resolved)
                else:
                    error_msg = getattr(commit_result, "error", "unknown")
                    summary["errors"].append(f"commit: {error_msg}")
                    logger.error("Evolution commit failed: %s", error_msg)
            else:
                logger.warning("git_committer unavailable or no diff — skipping commit")
                summary["errors"].append("commit skipped (no git_committer or diff)")

        # 8. Final notification
        _send_dingtalk(
            "OPENCODE Evolution Cycle",
            f"### OPENCODE Evolution Cycle | {timestamp[:10]}\n\n"
            f"- Scope: **{scope_name}**\n"
            f"- Trajectories: {summary['trajectories_loaded']}\n"
            f"- Insights: {summary['insights_extracted']}\n"
            f"- Attributions: {summary['attributions']}\n"
            f"- Candidate: {'YES' if summary['candidate_selected'] else 'none'}\n"
            f"- Commit: `{summary['commit_hash'] or 'N/A'}`\n"
            f"- Feedback resolved: {summary['feedback_resolved']}\n"
            f"- Errors: {', '.join(summary['errors']) if summary['errors'] else 'none'}\n",
        )

    except Exception as exc:
        logger.error("run_evolution_cycle failed: %s", exc, exc_info=True)
        summary["errors"].append(str(exc))
        _send_dingtalk(
            "OPENCODE Evolution Cycle",
            f"### OPENCODE Evolution Cycle | {timestamp[:10]}\n\n"
            f"- **FAILED**: {exc}\n",
        )

    return summary
