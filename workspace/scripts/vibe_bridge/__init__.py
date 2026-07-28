"""Vibe-Trading bridge — adapts Qdata signal builders to Vibe-Trading contract.

This is a bridge, not an engine replacement. Qdata remains the factor authority;
the adapter exposes Qdata signal builders through the Vibe-Trading
``generate(data_map)`` interface for interoperability.
"""

from scripts.vibe_bridge.signal_engine_adapter import SignalEngineAdapter

__all__ = ["SignalEngineAdapter"]
