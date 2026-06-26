"""Shared utilities extracted from multiple memory modules.

- _get_attr: duck-typed attribute accessor (from reflexion.py, attribution.py, scoring.py, expel.py)
- _MEMORY_BASE: single source of truth for memory path (from feedback_reset.py)
- _BLOCK_RE: compiled regex for YAML frontmatter parsing (from expel.py, feedback_reset.py)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEMORY_BASE = Path.home() / ".vibe-trading" / "memory"

_BLOCK_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*?)(?=^---|\Z)",
    re.DOTALL | re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_attr(obj, key, default=None):
    """Get attribute from dict or object (duck-typed)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def write_atomic(path, content, encoding="utf-8"):
    """Write content to a file atomically using tempfile + fsync + os.replace.

    Writes to a tempfile in the same directory, fsyncs it, then atomically
    replaces the target. This prevents corruption from mid-write crashes.
    """
    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", delete=False, dir=path.parent, encoding=encoding,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)