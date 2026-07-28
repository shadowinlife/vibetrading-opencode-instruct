"""
Shared constants for market microstructure indicators.

Values are tuned for A-share daily-frequency analysis using local DuckDB data.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------

DEFAULT_DUCKDB_PATH: str = "./duckdb/ashare.duckdb"

# Output directory (absolute, resolved relative to repo root).
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR: Path = PROJECT_ROOT / "tmp" / "microstructure"

# Sub-directories for structured artifact placement.
VALIDATION_DIR: Path = DEFAULT_OUTPUT_DIR / "validation"
TUNING_DIR: Path = DEFAULT_OUTPUT_DIR / "tuning"
BASELINE_DIR: Path = DEFAULT_OUTPUT_DIR / "baseline"

# Evidence directory for validation / QA artifacts (must never contain secrets).
EVIDENCE_DIR: Path = PROJECT_ROOT / ".sisyphus" / "evidence"

# ---------------------------------------------------------------------------
# Index codes
# ---------------------------------------------------------------------------

# Shanghai Composite Index (Tushare format).
SSE_INDEX_CODE: str = "000001.SH"

# ---------------------------------------------------------------------------
# Board → broad-based index mapping
# ---------------------------------------------------------------------------
# Maps each A-share board prefix pattern to its canonical broad-based index.
# Used by ``board_concentration`` to identify which index best reflects
# the concentration of top-turnover stocks when a RED signal fires.

BOARD_INDEX_MAP: dict[str, dict[str, str]] = {
    "科创板": {"index_code": "000688.SH", "index_name": "科创50", "ts_prefix": "688"},
    "创业板": {"index_code": "399006.SZ", "index_name": "创业板指", "ts_prefix": "300"},
    "沪主板": {"index_code": "000016.SH", "index_name": "上证50", "ts_prefix": "60"},
    "深主板": {"index_code": "399001.SZ", "index_name": "深证成指", "ts_prefix": "00"},
}

# Ordered list of boards for classification (first match wins).
BOARD_CLASSIFY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("科创板", ("688", "689")),
    ("创业板", ("300", "301")),
    ("北交所", ("83", "87", "43", "92")),
    ("沪主板", ("60",)),
    ("深主板", ("00",)),
]

# ---------------------------------------------------------------------------
# Concentration thresholds
# ---------------------------------------------------------------------------

# Top-N% of stocks by daily turnover used for concentration computation.
CONCENTRATION_TOP_PCT: float = 5.0

# ---------------------------------------------------------------------------
# Rolling windows (trading days)
# ---------------------------------------------------------------------------

DEFAULT_ROLLING_WINDOWS: list[int] = [5, 10, 20, 60]

# ---------------------------------------------------------------------------
# Margin-buy / turnover divergence
# ---------------------------------------------------------------------------

# Z-score threshold for flagging margin-buy/turnover vs SSE divergence.
DIVERGENCE_Z_THRESHOLD: float = 2.0

# ---------------------------------------------------------------------------
# Escape-top presets
# ---------------------------------------------------------------------------

ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD: float = 0.45
ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS: int = 20

# Presets are tuned against forward drawdowns of SSE 000001.SH.
ESCAPE_TOP_PRESETS: dict[str, dict[str, float | int]] = {
    "strong": {
        "concentration_threshold": 0.50,
        "divergence_lookback_days": 40,
    },
    "balanced": {
        "concentration_threshold": 0.48,
        "divergence_lookback_days": 40,
    },
    "early": {
        "concentration_threshold": 0.52,
        "divergence_lookback_days": 60,
    },
    "extended": {
        "concentration_threshold": 0.50,
        "divergence_lookback_days": 40,
        "temporal_window_days": 5,
    },
}
