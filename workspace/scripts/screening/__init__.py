"""Stock screening module — DuckDB-based multi-layer filtering.

Provides parameterized SQL templates and screening functions for:
- Layer 1: Fundamental screening (ROE, revenue growth, cash flow, valuation)
- Layer 2: Narrative momentum screening (sector heat, turnover, volume)
- Layer 3: Capital flow screening (main force, margin trading, northbound)
- Composite: Cross-sectional Z-score + multi-factor scoring + Top-N selection
"""

from scripts.screening.layer1_fundamental import screen_fundamental
from scripts.screening.layer2_narrative import screen_narrative
from scripts.screening.layer3_flow import screen_flow
from scripts.screening.composite import composite_screen
from scripts.screening.ablation import run_ablation
from scripts.screening.sql_templates import (
    LAYER1_FUNDAMENTAL_SQL,
    FULL_MARKET_VALUATION_SQL,
)

__all__ = [
    "screen_fundamental",
    "screen_narrative",
    "screen_flow",
    "composite_screen",
    "run_ablation",
    "LAYER1_FUNDAMENTAL_SQL",
    "FULL_MARKET_VALUATION_SQL",
]
