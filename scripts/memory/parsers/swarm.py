"""Swarm run trajectory parser.

Reads Vibe-Trading Swarm JSONL event logs and normalizes them to the
unified Trajectory format defined in ``common.py``.
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .common import Trajectory, TrajectoryStep, TrajectoryOutcome


logger = logging.getLogger(__name__)

SWARM_RUNS_DIR = "/opt/Vibe-Trading/agent/.swarm/runs"

# Event types we skip entirely (noise)
_SKIP_EVENTS = frozenset({"task_heartbeat"})

# Max chars to keep from a tool result_preview to avoid bloating trajectories
_TOOL_OUTPUT_MAX_CHARS = 2000


def _parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to a datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _extract_target(user_vars: dict) -> str:
    """Extract the primary stock/symbol target from user_vars.

    Handles multiple key conventions:
    - ``target``: "588000.SH 科创ETF50" → "588000.SH"
    - ``symbol``: direct code
    - ``goal``: free-text goal (no code extraction, return empty)
    """
    for key in ("target", "symbol"):
        val = user_vars.get(key, "")
        if val:
            # Extract leading stock code pattern (e.g. "588000.SH 科创ETF50")
            m = re.match(r"([A-Za-z0-9.]+)", val.strip())
            return m.group(1) if m else val.strip()
    return ""


def _read_run_json(run_dir: Path) -> Optional[dict]:
    """Read and parse run.json from a run directory."""
    run_json_path = run_dir / "run.json"
    if not run_json_path.is_file():
        return None
    try:
        return json.loads(run_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", run_json_path, exc)
        return None


def _read_events(run_dir: Path) -> List[dict]:
    """Read events.jsonl, skipping malformed lines."""
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    events = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed JSON at %s:%d, skipping", events_path, lineno
                )
    return events


def _read_artifact_reports(run_dir: Path) -> List[TrajectoryStep]:
    """Read artifacts/*/report.md files as final trajectory steps."""
    artifacts_dir = run_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return []
    steps = []
    for agent_dir in sorted(artifacts_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        report = agent_dir / "report.md"
        if not report.is_file():
            continue
        try:
            content = report.read_text(encoding="utf-8")
        except OSError:
            continue
        if content.strip():
            steps.append(
                TrajectoryStep(
                    role="assistant",
                    content=f"## {agent_dir.name} Report\n\n{content}",
                    timestamp=None,
                )
            )
    return steps


def _events_to_steps(events: List[dict]) -> List[TrajectoryStep]:
    """Convert a list of swarm events into TrajectorySteps.

    Strategy:
    - ``worker_text`` events are streaming tokens.  We concatenate them
      per ``(task_id, iteration)`` to form coherent assistant messages.
    - ``tool_call`` + ``tool_result`` pairs are merged into single tool steps.
    - ``task_started`` / ``task_completed`` become system steps.
    - ``run_started`` / ``run_completed`` are handled at the trajectory level.
    - ``task_heartbeat`` events are skipped.
    """
    steps: List[TrajectoryStep] = []

    # Accumulate streaming text per (task_id, iteration)
    text_buffers: dict[tuple, list] = defaultdict(list)
    text_timestamps: dict[tuple, Optional[datetime]] = {}

    # Track tool_call events to merge with subsequent tool_result
    pending_tool_calls: dict[tuple, dict] = {}  # (task_id, idx) -> event

    # First pass: collect all worker_text into buffers
    tool_call_counter: dict[str, int] = defaultdict(int)

    for ev in events:
        ev_type = ev.get("type", "")
        if ev_type in _SKIP_EVENTS:
            continue

        task_id = ev.get("task_id") or ""
        agent_id = ev.get("agent_id") or ""
        data = ev.get("data", {})
        ts = _parse_iso(ev.get("timestamp", ""))

        if ev_type == "worker_text":
            iteration = data.get("iteration", 0)
            key = (task_id, agent_id, iteration)
            text_buffers[key].append(data.get("content", ""))
            if key not in text_timestamps:
                text_timestamps[key] = ts

        elif ev_type == "tool_call":
            idx = tool_call_counter[task_id]
            tool_call_counter[task_id] += 1
            pending_tool_calls[(task_id, idx)] = {
                "event": ev,
                "timestamp": ts,
                "result": None,
            }

        elif ev_type == "tool_result":
            # Match to most recent unmatched tool_call for this task
            idx = tool_call_counter[task_id] - 1
            key = (task_id, idx)
            if key in pending_tool_calls and pending_tool_calls[key]["result"] is None:
                pending_tool_calls[key]["result"] = data

    # Second pass: build steps in event order
    text_emitted: set = set()
    tool_emitted: set = set()
    tool_call_counter2: dict[str, int] = defaultdict(int)

    for ev in events:
        ev_type = ev.get("type", "")
        if ev_type in _SKIP_EVENTS:
            continue

        task_id = ev.get("task_id") or ""
        agent_id = ev.get("agent_id") or ""
        data = ev.get("data", {})
        ts = _parse_iso(ev.get("timestamp", ""))

        if ev_type == "task_started":
            steps.append(
                TrajectoryStep(
                    role="system",
                    content=f"[Task started: {task_id} (agent: {agent_id})]",
                    timestamp=ts,
                )
            )

        elif ev_type == "worker_text":
            iteration = data.get("iteration", 0)
            key = (task_id, agent_id, iteration)
            if key not in text_emitted:
                text_emitted.add(key)
                full_text = "".join(text_buffers[key])
                if full_text.strip():
                    steps.append(
                        TrajectoryStep(
                            role="assistant",
                            content=full_text,
                            timestamp=text_timestamps.get(key, ts),
                        )
                    )

        elif ev_type == "tool_call":
            idx = tool_call_counter2[task_id]
            tool_call_counter2[task_id] += 1
            tc_key = (task_id, idx)
            if tc_key in pending_tool_calls:
                tc_info = pending_tool_calls[tc_key]
                tc_data = tc_info["event"].get("data", {})
                tool_name = tc_data.get("tool", "")
                arguments = tc_data.get("arguments", {})
                result_data = tc_info.get("result") or {}
                result_preview = result_data.get("result_preview", "")
                result_status = result_data.get("status", "")

                # Truncate long tool outputs
                if len(result_preview) > _TOOL_OUTPUT_MAX_CHARS:
                    result_preview = (
                        result_preview[:_TOOL_OUTPUT_MAX_CHARS] + "...[truncated]"
                    )

                steps.append(
                    TrajectoryStep(
                        role="tool",
                        content=f"[{tool_name}] status={result_status}",
                        tool_name=tool_name,
                        tool_input=arguments if isinstance(arguments, dict) else {},
                        tool_output=result_preview,
                        timestamp=tc_info["timestamp"],
                    )
                )
                tool_emitted.add(tc_key)

        elif ev_type == "tool_result":
            # Already handled in tool_call merge above
            pass

        elif ev_type == "task_completed":
            status = data.get("status", "unknown")
            iterations = data.get("iterations", 0)
            in_tok = data.get("input_tokens", 0)
            out_tok = data.get("output_tokens", 0)
            steps.append(
                TrajectoryStep(
                    role="system",
                    content=(
                        f"[Task completed: {task_id} | status={status} | "
                        f"iterations={iterations} | "
                        f"tokens={in_tok}+{out_tok}]"
                    ),
                    timestamp=ts,
                )
            )

        elif ev_type == "worker_completed":
            # Redundant with task_completed, skip to avoid noise
            pass

        elif ev_type == "worker_failed":
            steps.append(
                TrajectoryStep(
                    role="system",
                    content=f"[Worker failed: {agent_id} on {task_id}]",
                    timestamp=ts,
                )
            )

        elif ev_type == "task_retry":
            steps.append(
                TrajectoryStep(
                    role="system",
                    content=f"[Task retry: {task_id}]",
                    timestamp=ts,
                )
            )

        # run_started, run_completed, layer_started, worker_started
        # are handled at the trajectory metadata level

    return steps


def _parse_single_run(run_dir: Path) -> Optional[Trajectory]:
    """Parse a single swarm run directory into a Trajectory."""
    run_data = _read_run_json(run_dir)
    if run_data is None:
        return None

    run_id = run_data.get("id", run_dir.name)
    preset = run_data.get("preset_name", "")
    status = run_data.get("status", "unknown")
    user_vars = run_data.get("user_vars", {})
    target = _extract_target(user_vars)
    created_at = _parse_iso(run_data.get("created_at", ""))
    completed_at = _parse_iso(run_data.get("completed_at", ""))
    total_in = run_data.get("total_input_tokens", 0)
    total_out = run_data.get("total_output_tokens", 0)
    final_report = run_data.get("final_report", "")

    # Parse events into steps
    events = _read_events(run_dir)
    steps = _events_to_steps(events)

    # Append artifact report.md files as final steps
    artifact_steps = _read_artifact_reports(run_dir)
    steps.extend(artifact_steps)

    # Append final_report as last step if present and non-empty
    if final_report and final_report.strip():
        steps.append(
            TrajectoryStep(
                role="assistant",
                content=f"## Final Report\n\n{final_report}",
                timestamp=completed_at,
            )
        )

    # Build task summaries from run.json tasks array
    task_summaries = {}
    for task in run_data.get("tasks", []):
        task_id = task.get("id", "")
        task_summaries[task_id] = {
            "agent_id": task.get("agent_id", ""),
            "status": task.get("status", ""),
            "summary": task.get("summary", ""),
            "worker_iterations": task.get("worker_iterations", 0),
            "started_at": task.get("started_at", ""),
            "completed_at": task.get("completed_at", ""),
        }

    # Build outcome from run status
    outcome = None
    if status == "completed":
        outcome = TrajectoryOutcome(
            score=1.0,
            type="swarm_run",
            details=f"Preset: {preset} | Status: {status} | Tokens: {total_in}+{total_out}",
        )
    elif status in ("failed", "cancelled"):
        outcome = TrajectoryOutcome(
            score=0.0,
            type="swarm_run",
            details=f"Preset: {preset} | Status: {status}",
        )

    metadata = {
        "preset_name": preset,
        "status": status,
        "user_vars": user_vars,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "created_at": run_data.get("created_at", ""),
        "completed_at": run_data.get("completed_at", ""),
        "task_summaries": task_summaries,
        "num_events": len(events),
        "num_steps": len(steps),
    }

    return Trajectory(
        source="swarm",
        session_id=run_id,
        target=target,
        timestamp=created_at or datetime.now(timezone.utc),
        steps=steps,
        outcome=outcome,
        metadata=metadata,
    )


def load_trajectories(
    run_dir: str = SWARM_RUNS_DIR,
    target: Optional[str] = None,
) -> List[Trajectory]:
    """Load Swarm run trajectories from JSONL event logs.

    Args:
        run_dir: Path to swarm runs directory.
        target: Optional stock code filter.  When provided, only runs
            whose extracted target matches (case-insensitive prefix)
            are returned.

    Returns:
        List of Trajectory objects, sorted by timestamp (oldest first).
    """
    runs_path = Path(run_dir)
    if not runs_path.is_dir():
        logger.info("Swarm runs directory not found: %s", run_dir)
        return []

    trajectories: List[Trajectory] = []

    for entry in sorted(runs_path.iterdir()):
        if not entry.is_dir():
            continue
        if not entry.name.startswith("swarm-"):
            continue

        traj = _parse_single_run(entry)
        if traj is None:
            continue

        # Apply target filter if specified
        if target and traj.target:
            if not traj.target.upper().startswith(target.upper()):
                continue
        elif target and not traj.target:
            # Filter requested but run has no extractable target — skip
            continue

        trajectories.append(traj)

    # Sort by timestamp (oldest first)
    trajectories.sort(key=lambda t: t.timestamp)
    return trajectories
