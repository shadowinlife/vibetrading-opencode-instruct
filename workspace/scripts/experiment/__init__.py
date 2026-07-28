"""Experiment runner package for multi-stock backtest experiments.

Provides CLI and programmatic interface to validate experiment configs
against the JSON schema and execute backtests via the appropriate engine.
"""

from scripts.experiment.runner import run_experiment

__all__ = ["run_experiment"]
