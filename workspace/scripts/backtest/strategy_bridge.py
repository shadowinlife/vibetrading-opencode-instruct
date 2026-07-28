"""Thin adapter that bridges portfolio-level factor DataFrames to existing
single-stock signal-builder functions via dynamic import resolution.

This module sits at the boundary between the reusable backtest framework
(``scripts/backtest/``) and the policy-specific signal builders
(``policy/``).  It does NOT re-implement or modify any signal logic — it
simply resolves a dotted import-path string (the ``signal_builder_ref``
from ``PortfolioConfig``), calls the referenced builder function with the
multi-stock factor DataFrame, and returns the resulting signal DataFrame.

Architecture::

    PortfolioConfig.signal_builder_ref  (e.g. "policy.601777.signal_builders.momentum.build_roc_signal")
            │
            ▼
    execute_signal_builder(ref, factor_df)
            │
            ├── importlib.import_module("policy.601777.signal_builders.momentum")
            ├── getattr(module, "build_roc_signal")
            └── build_roc_signal(factor_df)  →  signal DataFrame

The bridge is intentionally thin — it passes the input DataFrame as-is
to the builder function.  Any filtering, transformation, or normalization
is the responsibility of the builder, not the bridge.

Usage::

    from scripts.backtest.strategy_bridge import execute_signal_builder

    signal_df = execute_signal_builder(
        "policy.601777.signal_builders.momentum.build_roc_signal",
        factor_df,
    )
"""

from __future__ import annotations

import importlib
import logging
from typing import Callable, Any

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_signal_builder(
    ref: str,
    factor_df: pd.DataFrame,
) -> pd.DataFrame:
    """Resolve a dotted import path to a signal-builder function and call it.

    ``ref`` is expected to be a fully-qualified Python import path of the
    form ``"pkg.subpkg.module.function_name"``.  The module portion (all
    parts up to and including the last dot) is imported via
    ``importlib.import_module()``, and the trailing attribute name is
    resolved via ``getattr()`` on the imported module object.

    The resolved function is called with ``factor_df`` as its only
    argument.  The function is assumed to:
      - Accept a ``pd.DataFrame`` with at least ``ts_code``,
        ``trade_date``, ``close``, and whatever factor columns the
        builder requires (e.g. ``ROC5``, ``ROC10``, ...).
      - Return a ``pd.DataFrame`` with ``ts_code``, ``trade_date``,
        and a signal column (e.g. ``ROC_COMPOSITE_Z``).

    The bridge does NOT:
      - Filter, drop, or rename columns in ``factor_df`` before the call
      - Validate that ``factor_df`` contains the required factor columns
        (builders are expected to handle missing-column errors via pandas'
        own ``KeyError``, which propagates naturally)
      - Cache or memoize import resolutions — every call re-imports the
        module (which is cheap due to ``sys.modules`` caching)

    Args:
        ref: Dotted import-path string.  Example:
            ``"policy.601777.signal_builders.momentum.build_roc_signal"``.
            The portion before the last dot is the module path; the
            portion after is the function name.
        factor_df: Multi-stock factor DataFrame with ``ts_code``,
            ``trade_date``, ``close``, and relevant factor columns.
            Passed directly to the resolved builder function.

    Returns:
        Signal DataFrame as returned by the builder function.  Column
        structure depends on the specific builder, but must include
        ``ts_code`` and ``trade_date`` plus at least one signal column.

    Raises:
        ValueError: If ``ref`` does not contain a dot (i.e. is not a
            valid dotted path).
        ModuleNotFoundError: If the module portion of ``ref`` cannot
            be imported.
        AttributeError: If the function name does not exist in the
            resolved module.
        TypeError: If the resolved attribute is not callable.

    Examples:
        >>> signal_df = execute_signal_builder(
        ...     "policy.601777.signal_builders.momentum.build_roc_signal",
        ...     factor_df,
        ... )
        >>> "ROC_COMPOSITE_Z" in signal_df.columns
        True
    """
    # ------------------------------------------------------------------
    # 1. Parse the dotted path into (module_path, function_name)
    # ------------------------------------------------------------------
    # The last dot separates the module path from the attribute name.
    # Everything before it is the module; everything after is the
    # callable (function) we resolve via getattr.
    if "." not in ref:
        raise ValueError(
            f"signal_builder_ref must be a dotted import path, got: {ref!r}"
        )

    module_path, function_name = ref.rsplit(".", maxsplit=1)

    if not module_path or not function_name:
        raise ValueError(
            f"signal_builder_ref must have both module and function parts, got: {ref!r}"
        )

    # ------------------------------------------------------------------
    # 2. Import the module and resolve the function
    # ------------------------------------------------------------------
    # importlib.import_module handles nested packages (e.g.
    # "policy.601777.signal_builders.momentum") correctly.
    # The result is cached in sys.modules on first import, so repeated
    # calls to execute_signal_builder with the same ref are cheap.
    module = importlib.import_module(module_path)

    fn: Callable[..., pd.DataFrame] = getattr(module, function_name)
    if not callable(fn):
        raise TypeError(
            f"Resolved {ref!r}, but {function_name!r} in "
            f"module {module_path!r} is not callable (type={type(fn).__name__})"
        )

    # ------------------------------------------------------------------
    # 3. Call the builder with the factor DataFrame
    # ------------------------------------------------------------------
    # The builder receives factor_df as its only argument and returns
    # a DataFrame with ts_code, trade_date, plus signal columns.
    # We do NOT transform factor_df — the builder expects the full
    # multi-stock factor DataFrame and is responsible for column
    # selection internally.
    result = fn(factor_df)

    # ------------------------------------------------------------------
    # 4. Basic validation of the returned DataFrame
    # ------------------------------------------------------------------
    # We perform a lightweight structural check: the result must be a
    # DataFrame and must contain at least ts_code and trade_date.
    # We do NOT validate signal column names — builders are free to
    # name their signal column(s) however they choose.
    if not isinstance(result, pd.DataFrame):
        raise TypeError(
            f"Signal builder {ref!r} returned {type(result).__name__!r}, "
            f"expected pd.DataFrame"
        )

    for required_col in ("ts_code", "trade_date"):
        if required_col not in result.columns:
            raise ValueError(
                f"Signal builder {ref!r} returned a DataFrame without "
                f"required column {required_col!r}. Columns present: "
                f"{list(result.columns)}"
            )

    return result


def _validate_ref_format(ref: str) -> None:
    """Validate a signal_builder_ref string before attempting import.

    Performs early syntactic checks (has dots, has non-empty parts)
    to provide clear error messages before import resolution.

    Args:
        ref: Dotted import path string.

    Raises:
        ValueError: If the ref string is malformed.
    """
    if not ref or not isinstance(ref, str):
        raise ValueError(f"signal_builder_ref must be a non-empty string, got: {ref!r}")
    if "." not in ref:
        raise ValueError(
            f"signal_builder_ref must be a dotted import path, got: {ref!r}"
        )
    parts = ref.rsplit(".", maxsplit=1)
    if not parts[0] or not parts[1]:
        raise ValueError(
            f"signal_builder_ref must have both module and function parts, got: {ref!r}"
        )