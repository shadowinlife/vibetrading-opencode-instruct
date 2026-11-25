"""Make the skill root importable so tests can use ``scripts.reports.*``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
