"""OpenCode session trajectory parser.

Reads from the OpenCode SQLite database and normalizes conversations
to the unified Trajectory format defined in common.py.

Database schema (as of 2026-06-10):
  session:  id, project_id, title, time_created (ms), time_updated, model, agent,
            tokens_input, tokens_output, cost, metadata
  message:  id, session_id, time_created (ms), time_updated, data (JSON)
            data.role: "user" | "assistant"
  part:     id, message_id, session_id, time_created (ms), time_updated, data (JSON)
            data.type: "text" | "tool" | "reasoning" | "step-start" | "step-finish"
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .common import Trajectory, TrajectoryOutcome, TrajectoryStep

DB_PATH = "/root/.local/share/opencode/opencode.db"

# Regex for A-share / HK / US stock symbols: 6 digits + dot + 2-letter exchange
_SYMBOL_RE = re.compile(r"\b(\d{6}\.\w{2})\b")

# Max lookback in days
_MAX_DAYS = 90


def _ms_to_dt(ms: int) -> datetime:
    """Convert millisecond Unix timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _parse_json(raw: str) -> dict:
    """Safely parse a JSON string, returning empty dict on failure."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_symbols(text: str) -> list[str]:
    """Extract stock symbol codes from text content."""
    return list(set(_SYMBOL_RE.findall(text)))


def _build_steps_from_messages(
    conn: sqlite3.Connection, session_id: str
) -> list[TrajectoryStep]:
    """Reconstruct trajectory steps from messages and parts for a session."""
    steps: list[TrajectoryStep] = []

    cur = conn.execute(
        "SELECT id, time_created, data FROM message "
        "WHERE session_id = ? ORDER BY time_created ASC",
        (session_id,),
    )
    messages = cur.fetchall()

    for msg_id, msg_ts, msg_data_raw in messages:
        msg_data = _parse_json(msg_data_raw)
        role = msg_data.get("role", "unknown")
        msg_dt = _ms_to_dt(msg_ts)

        # Load parts for this message
        part_cur = conn.execute(
            "SELECT id, time_created, data FROM part "
            "WHERE message_id = ? ORDER BY time_created ASC",
            (msg_id,),
        )
        parts = part_cur.fetchall()

        for _part_id, part_ts, part_data_raw in parts:
            part_data = _parse_json(part_data_raw)
            part_type = part_data.get("type", "")
            part_dt = _ms_to_dt(part_ts)

            if part_type == "text":
                text = part_data.get("text", "")
                if not text.strip():
                    continue
                steps.append(
                    TrajectoryStep(
                        role=role,
                        content=text,
                        timestamp=part_dt,
                    )
                )

            elif part_type == "tool":
                tool_name = part_data.get("tool", "")
                state = part_data.get("state", {})
                tool_input = state.get("input", {})
                tool_output = state.get("output", "")
                # Truncate very long outputs to keep trajectories manageable
                if isinstance(tool_output, str) and len(tool_output) > 4000:
                    tool_output = tool_output[:4000] + "...[truncated]"
                steps.append(
                    TrajectoryStep(
                        role="tool",
                        content=f"[tool:{tool_name}]",
                        tool_name=tool_name,
                        tool_input=tool_input if isinstance(tool_input, dict) else {},
                        tool_output=tool_output,
                        timestamp=part_dt,
                    )
                )

            elif part_type == "compaction":
                # Compaction parts contain a summary replacing earlier history
                text = part_data.get("text", "")
                if text.strip():
                    steps.append(
                        TrajectoryStep(
                            role="system",
                            content=f"[compaction summary] {text}",
                            timestamp=part_dt,
                        )
                    )

            # Skip: reasoning, step-start, step-finish

    return steps


def load_trajectories(
    days: int = 30,
    target: Optional[str] = None,
    db_path: str = DB_PATH,
) -> list[Trajectory]:
    """Load OpenCode session trajectories from SQLite database.

    Args:
        days: Load sessions from the last N days (default 30, max 90).
        target: Optional stock code filter (e.g. "601777.SH"). Only returns
                sessions whose content mentions this symbol.
        db_path: Path to OpenCode SQLite database.

    Returns:
        List of Trajectory objects, one per session.
    """
    days = min(max(1, days), _MAX_DAYS)
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"OpenCode database not found: {db_path}")

    cutoff_ms = int(
        (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )

    # Read-only connection
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = None  # tuples, not Row objects — faster
    try:
        try:
            # Fetch recent sessions
            sess_cur = conn.execute(
                "SELECT id, title, time_created, time_updated, model, agent, "
                "tokens_input, tokens_output, cost, metadata "
                "FROM session WHERE time_created > ? "
                "ORDER BY time_created ASC",
                (cutoff_ms,),
            )
            sessions = sess_cur.fetchall()
        except sqlite3.OperationalError:
            return []

        trajectories: list[Trajectory] = []

        for sess_row in sessions:
            (
                sess_id,
                title,
                time_created,
                time_updated,
                model,
                agent,
                tokens_in,
                tokens_out,
                cost,
                metadata_raw,
            ) = sess_row

            steps = _build_steps_from_messages(conn, sess_id)
            if not steps:
                continue

            # Extract stock symbols from all text content
            all_text = " ".join(
                s.content for s in steps if s.role in ("user", "assistant")
            )
            all_text += " " + (title or "")
            symbols = _extract_symbols(all_text)

            # Apply target filter if specified
            if target and target not in symbols:
                # Also check raw text for partial matches
                if target not in all_text:
                    continue

            # Determine primary target symbol
            primary_target = target if target else (symbols[0] if symbols else "")

            metadata = _parse_json(metadata_raw) if metadata_raw else {}
            metadata.update(
                {
                    "title": title or "",
                    "model": model or "",
                    "agent": agent or "",
                    "tokens_input": tokens_in,
                    "tokens_output": tokens_out,
                    "cost": cost,
                    "symbols_found": symbols,
                    "step_count": len(steps),
                }
            )

            traj = Trajectory(
                source="opencode",
                session_id=sess_id,
                target=primary_target,
                timestamp=_ms_to_dt(time_created),
                steps=steps,
                outcome=None,
                metadata=metadata,
            )
            trajectories.append(traj)

        return trajectories
    finally:
        conn.close()
