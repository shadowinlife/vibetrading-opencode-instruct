"""Shared artifact-path utilities for microstructure validation / tuning / QA.

Provides deterministic output-path resolution and evidence-dir management
so CLIs can name-space artifacts under run-id subdirectories.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from scripts.microstructure.metadata import (
    DEFAULT_OUTPUT_DIR,
    EVIDENCE_DIR,
    VALIDATION_DIR,
    TUNING_DIR,
    BASELINE_DIR,
)


def generate_run_id() -> str:
    """Return a compact timestamp run-id: ``YYYYMMDD_HHMMSS``."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def resolve_output_path(
    output_path: str | None,
    default_dir: Path = DEFAULT_OUTPUT_DIR,
    run_id: str | None = None,
) -> Path | None:
    """Resolve an output-path string into a deterministic ``Path``.

    Rules (applied in order):
    1. ``None`` → ``None`` (caller decides skip-write).
    2. Bare filename (no directory component) → ``default_dir / <run_id>/name``
       when *run_id* is provided, otherwise ``default_dir / name``.
    3. Relative or absolute path → returned as-is (caller's own layout).

    This prevents accidental overwrites when *run_id* namespaces the output.
    """
    if output_path is None:
        return None

    dest = Path(output_path)

    # Bare filename -- place under the default directory.
    if dest.parent == Path("."):
        if run_id is not None:
            target_dir = default_dir / run_id
        else:
            target_dir = default_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / dest.name

    # Already has a directory component -- return as-is.
    return dest


def ensure_evidence_dir() -> Path:
    """Create ``.sisyphus/evidence/`` (idempotent) and return its path."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    return EVIDENCE_DIR


# ---------------------------------------------------------------------------
# Helpers for secret / token scanning in evidence artefacts
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Typical token-looking strings (alphanumeric + dash/underscore, 20+ chars).
    re.compile(r"[A-Za-z0-9_\-]{20,}"),
    # Common env-var assignment patterns.
    re.compile(r"(?:DASHSCOPE|TUSHARE|OPENAI|ANTHROPIC)[_A-Z]*\s*=\s*\S+", re.IGNORECASE),
    # Bearer / API key headers.
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"),
]


def scan_for_secrets(file_path: Path) -> list[str]:
    """Scan a single evidence file for token-like patterns.

    Returns a list of matching line excerpts (empty = clean).
    """
    hits: list[str] = []
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return hits

    for line_no, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        for pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(f"{file_path}:{line_no}: {line[:120]}")
    return hits