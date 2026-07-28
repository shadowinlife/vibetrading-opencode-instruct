"""Swarm worker memory bridge.

Provides a clean interface for injecting memory into Vibe-Trading Swarm
worker prompts. Designed to be called by worker.py:build_worker_prompt()
or a wrapper script.

Integration pattern follows the existing grounding_block injection in
Vibe-Trading's swarm/grounding.py.
"""

from __future__ import annotations

import logging

from .injection import get_full_memory_block

logger = logging.getLogger(__name__)


def inject_memory_into_swarm(
    target_symbol: str,
    max_tokens: int = 3000,
) -> str:
    """Build a formatted memory block for Swarm worker prompt injection.

    Calls :func:`get_full_memory_block` to retrieve combined per-stock
    mistakes and cross-stock insights, then wraps the result in a
    section header suitable for appending to a worker system prompt.

    Parameters
    ----------
    target_symbol : str
        Stock code, e.g. ``"601777.SH"``.
    max_tokens : int
        Maximum token budget passed through to
        ``get_full_memory_block`` (default 3000).

    Returns
    -------
    str
        Formatted markdown section with memory content, or empty string
        if no memory is available for the given symbol.
    """
    memory_block = get_full_memory_block(
        target_symbol,
        max_tokens=max_tokens,
    )

    if not memory_block:
        return ""

    # Wrap in a clearly delimited section.  get_full_memory_block already
    # includes a ``## 📋 Historical Knowledge`` header, so we append the
    # trailing separator only.
    formatted = f"{memory_block}\n\n---\n"

    return formatted


def get_integration_docs() -> str:
    """Return documentation explaining how to integrate with Vibe-Trading.

    Returns
    -------
    str
        Multi-line documentation string.
    """
    return """\
Integration with Vibe-Trading Swarm:

1. In worker.py:build_worker_prompt(), add:
   from scripts.memory.swarm_bridge import inject_memory_into_swarm
   memory_block = inject_memory_into_swarm(target_symbol)
   if memory_block:
       system_prompt += "\\n\\n" + memory_block

2. Or use the config snippet in scripts/memory/config/swarm_memory.json
   to enable memory injection via agent configuration.

3. The bridge follows the same pattern as grounding_block in
   /opt/Vibe-Trading/agent/src/swarm/grounding.py
"""
