"""Shared backtest framework for single-stock strategy evaluation."""

from scripts.backtest.config import StrategyConfig
from scripts.backtest.metrics import calc_metrics

__all__ = ["StrategyConfig", "calc_metrics"]