"""SignalEngineAdapter — wraps Qdata signal builders for Vibe-Trading interop.

Bridge pattern: Qdata signal builders produce Z-score Series from raw OHLCV.
Vibe-Trading's SignalEngine contract expects ``generate(data_map)`` where
``data_map`` is ``{symbol: DataFrame}`` with OHLCV columns.

This adapter:
1. Validates the builder name against ``SIGNAL_REGISTRY``.
2. Iterates over ``data_map`` symbols, calling the Qdata builder per symbol.
3. Returns a flat DataFrame with columns:
   ``symbol, date, signal_value, entry_signal, exit_signal``.

No DuckDB access — all data comes from ``data_map``.

This is a bridge, not an engine replacement.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from scripts.backtest.signal_builders import SIGNAL_REGISTRY, get_signal_builder


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS = ["symbol", "date", "signal_value", "entry_signal", "exit_signal"]

# Default Z-score thresholds (mirror HpoStrategyConfig defaults)
DEFAULT_ENTRY_Z = 1.5
DEFAULT_EXIT_Z = -0.5


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SignalEngineAdapter:
    """Bridge adapter: Qdata signal builder → Vibe-Trading SignalEngine contract.

    Parameters
    ----------
    builder_name : str
        Name of a registered Qdata signal builder (must exist in SIGNAL_REGISTRY).
    entry_z : float, optional
        Z-score threshold above which ``entry_signal = 1``. Default 1.5.
    exit_z : float, optional
        Z-score threshold below which ``exit_signal = 1``. Default -0.5.

    Raises
    ------
    ValueError
        If ``builder_name`` is not found in ``SIGNAL_REGISTRY``.
    """

    def __init__(
        self,
        builder_name: str,
        entry_z: float = DEFAULT_ENTRY_Z,
        exit_z: float = DEFAULT_EXIT_Z,
    ) -> None:
        if builder_name not in SIGNAL_REGISTRY:
            available = ", ".join(sorted(SIGNAL_REGISTRY))
            raise ValueError(
                f"Unknown signal builder: {builder_name!r}. "
                f"Available builders: {available}"
            )
        self.builder_name = builder_name
        self._builder = get_signal_builder(builder_name)
        self.entry_z = entry_z
        self.exit_z = exit_z

    # ------------------------------------------------------------------
    # Vibe-Trading-compatible generate()
    # ------------------------------------------------------------------

    def generate(self, data_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Generate signals for all symbols in *data_map*.

        Parameters
        ----------
        data_map : dict[str, pd.DataFrame]
            Mapping of ``{symbol: DataFrame}``. Each DataFrame must contain
            columns ``close``, ``high``, ``low``. Optional columns ``vol``
            and ``amount`` default to zero Series if absent.

            The DataFrame index is used as the date axis. If the index is a
            ``DatetimeIndex``, dates are preserved as-is. Otherwise a
            ``trade_date`` column is used if present; falling back to the
            integer index.

        Returns
        -------
        pd.DataFrame
            Flat DataFrame with columns:
            ``symbol``, ``date``, ``signal_value``, ``entry_signal``, ``exit_signal``.
            Returns an empty DataFrame (with correct columns) when *data_map*
            is empty.
        """
        if not data_map:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)

        frames: list[pd.DataFrame] = []

        for symbol, df in data_map.items():
            if df.empty:
                continue

            # Extract OHLCV Series expected by Qdata signal builders
            close = df["close"]
            high = df["high"]
            low = df["low"]
            vol = df["vol"] if "vol" in df.columns else pd.Series(0.0, index=df.index)
            amount = (
                df["amount"] if "amount" in df.columns else pd.Series(0.0, index=df.index)
            )

            # Call the Qdata signal builder
            signal_series: pd.Series = self._builder(
                close=close, high=high, low=low, vol=vol, amount=amount,
            )

            # Resolve date axis
            dates = _resolve_dates(df)

            # Build per-symbol result
            sig_vals = signal_series.to_numpy()
            entry_sig = np.where(
                pd.notna(signal_series) & (sig_vals > self.entry_z), 1, 0,
            )
            exit_sig = np.where(
                pd.notna(signal_series) & (sig_vals < self.exit_z), 1, 0,
            )

            sym_df = pd.DataFrame({
                "symbol": symbol,
                "date": dates,
                "signal_value": sig_vals,
                "entry_signal": entry_sig.astype(int),
                "exit_signal": exit_sig.astype(int),
            })
            frames.append(sym_df)

        if not frames:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)

        return pd.concat(frames, ignore_index=True)

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def list_builders() -> list[str]:
        """Return sorted list of all registered builder names."""
        return sorted(SIGNAL_REGISTRY)

    def __repr__(self) -> str:
        return (
            f"SignalEngineAdapter(builder={self.builder_name!r}, "
            f"entry_z={self.entry_z}, exit_z={self.exit_z})"
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_dates(df: pd.DataFrame) -> pd.Series | pd.Index:
    """Extract date labels from a DataFrame.

    Priority:
    1. ``trade_date`` column if present.
    2. ``DatetimeIndex`` index.
    3. Fallback: integer range index.
    """
    if "trade_date" in df.columns:
        return df["trade_date"]
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index
    return pd.RangeIndex(len(df))
