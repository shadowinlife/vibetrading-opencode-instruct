from __future__ import annotations

import math
from datetime import datetime, timezone

from .schema import MemoryEntry


def relevance_score(entry: MemoryEntry, now: datetime | None = None) -> float:
    """Calculate current relevance score with decay, usage boost, and validation bonus.

    Formula
    -------
    - ``time_decay = exp(-decay_rate * days_since_access)``
    - ``usage_boost = min(log(1 + access_count), 2.0)``
    - ``score = time_decay * (1 + usage_boost) * confidence``

    Sticky entries (``access_count >= 5``) skip time_decay.

    Parameters
    ----------
    entry : MemoryEntry
        The memory entry to score.
    now : datetime, optional
        Reference time.  Defaults to ``datetime.now(tz=timezone.utc)``.

    Returns
    -------
    float
        Relevance score, roughly in the range ``[0.0, ~5.0]``.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    days_since_access = max(
        (now - entry.last_accessed).total_seconds() / 86400.0, 0.0
    )

    # Time decay — skipped for sticky entries
    if entry.is_sticky:
        time_decay = 1.0
    else:
        time_decay = math.exp(-entry.decay_rate * days_since_access)

    # Usage boost — logarithmic, capped at 2.0
    usage_boost = min(math.log(1 + entry.access_count), 2.0)

    score = time_decay * (1 + usage_boost) * entry.confidence
    return score
