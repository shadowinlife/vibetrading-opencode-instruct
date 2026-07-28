"""Reusable market microstructure indicators and helpers."""

from .base import format_date, get_connection, pct_rank, rolling_zscore, top_pct_mask, write_json
from .concentration import compute_concentration
from .ensemble import ConditionResult, EnsembleConfig, resolve_ensemble
from .escape_top import compute_escape_warning
from .joint_escape_top import compute_joint_warning
from .margin_buy_vs_sse import compute_margin_buy_vs_sse
from .metadata import (
    CONCENTRATION_TOP_PCT,
    DEFAULT_DUCKDB_PATH,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_ROLLING_WINDOWS,
    DIVERGENCE_Z_THRESHOLD,
    ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD,
    ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS,
    ESCAPE_TOP_PRESETS,
    SSE_INDEX_CODE,
)
from .tune_escape_top import compute_forward_drawdowns, compute_signal_series, generate_labels, grid_search

__version__ = "0.2.0"
__all__ = [
    "get_connection",
    "write_json",
    "format_date",
    "pct_rank",
    "top_pct_mask",
    "rolling_zscore",
    "compute_concentration",
    "compute_margin_buy_vs_sse",
    "compute_escape_warning",
    "compute_joint_warning",
    "compute_forward_drawdowns",
    "generate_labels",
    "compute_signal_series",
    "grid_search",
    "ConditionResult",
    "EnsembleConfig",
    "resolve_ensemble",
    "DEFAULT_DUCKDB_PATH",
    "DEFAULT_OUTPUT_DIR",
    "SSE_INDEX_CODE",
    "CONCENTRATION_TOP_PCT",
    "DEFAULT_ROLLING_WINDOWS",
    "DIVERGENCE_Z_THRESHOLD",
    "ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD",
    "ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS",
    "ESCAPE_TOP_PRESETS",
]
