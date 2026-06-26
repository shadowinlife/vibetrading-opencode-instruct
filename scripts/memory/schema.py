from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

import yaml


# Default decay rates by entry type (daily decay coefficient)
DEFAULT_DECAY_RATES: dict[str, float] = {
    "analysis": 0.05,   # half-life ~14 days
    "signal": 0.10,     # half-life ~7 days
    "mistake": 0.01,    # half-life ~69 days
    "insight": 0.02,    # half-life ~35 days
}

_VALID_TYPES = frozenset(DEFAULT_DECAY_RATES.keys())


@dataclass
class MemoryEntry:
    """A single memory unit with YAML frontmatter serialisation.

    Attributes
    ----------
    id : str
        Unique identifier, e.g. ``"mem_601777_20260604_analysis"``.
    target : str
        Stock code, e.g. ``"601777.SH"``.
    type : str
        One of ``"analysis"``, ``"signal"``, ``"mistake"``, ``"insight"``.
    created : datetime
        Creation timestamp.
    last_accessed : datetime
        Last time this entry was referenced.
    access_count : int
        Number of times referenced.
    confidence : float
        0.0–1.0 confidence score.
    decay_rate : float
        Daily decay coefficient (see ``DEFAULT_DECAY_RATES``).
    tags : list[str]
        Free-form tags for filtering.
    source : str
        Origin identifier, e.g. ``"swarm/investment_committee"``.
    resolved : bool
        Whether this entry has been resolved by an evolution.
    content : str
        The actual memory content (markdown).
    """

    id: str
    target: str
    type: str
    created: datetime
    last_accessed: datetime
    access_count: int = 0
    confidence: float = 0.5
    decay_rate: float = 0.05
    tags: list[str] = field(default_factory=list)
    source: str = ""
    resolved: bool = False
    content: str = ""

    def __post_init__(self) -> None:
        if self.type not in _VALID_TYPES:
            raise ValueError(
                f"Invalid type {self.type!r}; must be one of {sorted(_VALID_TYPES)}"
            )
        # Apply default decay rate if caller left it at the dataclass default
        # and the type has a known rate.
        if self.decay_rate == 0.05 and self.type in DEFAULT_DECAY_RATES:
            self.decay_rate = DEFAULT_DECAY_RATES[self.type]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_sticky(self) -> bool:
        """Entries with access_count >= 5 are sticky (don't decay)."""
        return self.access_count >= 5

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_yaml_frontmatter(self) -> str:
        """Serialize to YAML frontmatter + content format.

        Returns a string like::

            ---
            id: mem_601777_20260604_analysis
            target: "601777.SH"
            ...
            ---
            <content>
        """
        meta: dict[str, Any] = {
            "id": self.id,
            "target": self.target,
            "type": self.type,
            "created": self.created.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "access_count": self.access_count,
            "confidence": self.confidence,
            "decay_rate": self.decay_rate,
            "tags": self.tags,
            "source": self.source,
            "resolved": self.resolved,
        }
        yaml_block = yaml.dump(meta, default_flow_style=False, sort_keys=False).rstrip()
        return f"---\n{yaml_block}\n---\n{self.content}"

    @classmethod
    def from_yaml_frontmatter(cls, text: str) -> MemoryEntry:
        """Parse from YAML frontmatter + content format.

        Expects the text to start with ``---``, followed by YAML fields,
        another ``---`` delimiter, and then the markdown content body.
        """
        pattern = r"^---\s*\n(.*?)\n---\s*\n?(.*)$"
        match = re.match(pattern, text, re.DOTALL)
        if not match:
            raise ValueError("Text does not contain valid YAML frontmatter")

        yaml_str, content = match.group(1), match.group(2)
        meta: dict[str, Any] = yaml.safe_load(yaml_str)

        # Coerce datetime strings
        for key in ("created", "last_accessed"):
            val = meta.get(key)
            if isinstance(val, str):
                meta[key] = datetime.fromisoformat(val)
            elif isinstance(val, datetime):
                pass  # already a datetime (PyYAML auto-parses some formats)
            else:
                raise ValueError(f"Missing or invalid '{key}' in frontmatter")

        meta["content"] = content
        return cls(**meta)
