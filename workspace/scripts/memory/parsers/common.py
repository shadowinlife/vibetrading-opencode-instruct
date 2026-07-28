"""Unified trajectory format shared across OpenCode and Swarm parsers."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class TrajectoryStep:
    """Single step in a conversation trajectory."""

    role: str  # "user" | "assistant" | "tool" | "system"
    content: str  # text content
    tool_name: str = ""  # if role == "tool"
    tool_input: dict = field(default_factory=dict)
    tool_output: str = ""
    timestamp: Optional[datetime] = None


@dataclass
class TrajectoryOutcome:
    """Outcome/evaluation of a trajectory."""

    score: float = 0.0  # 0.0-1.0
    type: str = ""  # "backtest" | "llm_judge" | "none"
    details: str = ""  # human-readable details


@dataclass
class Trajectory:
    """Unified conversation trajectory from any source."""

    source: str  # "opencode" | "swarm"
    session_id: str  # session or run ID
    target: str = ""  # stock code if extractable
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    steps: list = field(default_factory=list)  # list[TrajectoryStep]
    outcome: Optional[TrajectoryOutcome] = None
    metadata: dict = field(default_factory=dict)  # extra info
