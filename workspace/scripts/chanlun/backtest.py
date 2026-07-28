"""Chanlun signal backtest: use buy/sell points as entry/exit signals."""
from __future__ import annotations

from typing import Any, cast

import pandas as pd

ONE_WAY_COST = 0.0015  # 0.15% per side


def backtest_chanlun_signals(
    df: pd.DataFrame,
    signals: list[dict],
    initial_capital: float = 100_000.0,
) -> dict:
    """Backtest chanlun buy/sell signals on OHLCV data.

    Args:
        df: Raw OHLCV DataFrame with trade_date, close columns.
        signals: Output from detect_buy_sell_points().
        initial_capital: Starting capital.

    Returns dict with keys:
        trades: list of trade dicts
        summary: dict with total_return, win_rate, trade_count, etc.
        equity_curve: list of (date, equity) tuples
    """
    if not signals:
        return {
            "trades": [],
            "summary": {
                "total_return": 0.0,
                "win_rate": 0.0,
                "trade_count": 0,
                "avg_return": 0.0,
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0,
            },
            "equity_curve": [],
        }

    df_sorted = df.sort_values("trade_date").reset_index(drop=True)
    date_close: dict[str, float] = {}
    for _, row in df_sorted.iterrows():
        d = str(pd.Timestamp(cast(Any, row["trade_date"])).date())
        date_close[d] = float(row["close"])

    all_dates = sorted(date_close.keys())

    buy_signals: dict[str, dict] = {}
    sell_signals: dict[str, dict] = {}
    for sig in signals:
        if sig["type"].startswith("buy"):
            buy_signals[sig["date"]] = sig
        elif sig["type"].startswith("sell"):
            sell_signals[sig["date"]] = sig

    trades: list[dict] = []
    position: dict | None = None
    capital = initial_capital
    equity_curve: list[tuple[str, float]] = []

    for d in all_dates:
        if d in buy_signals and position is None:
            sig = buy_signals[d]
            price = date_close.get(d) or float(sig["price"])
            shares = int(capital * 0.95 / (price * (1 + ONE_WAY_COST)))
            if shares > 0:
                cost = shares * price * (1 + ONE_WAY_COST)
                position = {
                    "entry_date": d,
                    "entry_price": price,
                    "shares": shares,
                    "cost": cost,
                    "signal_type": sig["type"],
                    "reason": sig["reason"],
                }
                capital -= cost

        elif position is not None:
            price = date_close[d]
            should_sell = False
            exit_reason = ""

            if d in sell_signals:
                sig = sell_signals[d]
                should_sell = True
                exit_reason = f"{sig['type']}: {sig['reason']}"

            if should_sell:
                proceeds = position["shares"] * price * (1 - ONE_WAY_COST)
                pnl = proceeds - position["cost"]
                pnl_pct = pnl / position["cost"]
                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": d,
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "shares": position["shares"],
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "entry_signal": position["signal_type"],
                    "exit_signal": exit_reason,
                })
                capital += proceeds
                position = None

        equity = capital
        if position is not None:
            equity += position["shares"] * date_close[d]
        equity_curve.append((d, equity))

    if position is not None and all_dates:
        last_date = all_dates[-1]
        price = date_close[last_date]
        proceeds = position["shares"] * price * (1 - ONE_WAY_COST)
        pnl = proceeds - position["cost"]
        pnl_pct = pnl / position["cost"]
        trades.append({
            "entry_date": position["entry_date"],
            "exit_date": last_date,
            "entry_price": position["entry_price"],
            "exit_price": price,
            "shares": position["shares"],
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "entry_signal": position["signal_type"],
            "exit_signal": "end_of_data",
        })
        capital += proceeds

    final_equity = capital
    total_return = (final_equity / initial_capital) - 1.0
    trade_count = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / trade_count if trade_count > 0 else 0.0
    avg_return = sum(t["pnl_pct"] for t in trades) / trade_count if trade_count > 0 else 0.0

    peak = initial_capital
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    if len(equity_curve) > 1:
        eq_values = [eq for _, eq in equity_curve]
        daily_returns = [
            (eq_values[i] / eq_values[i - 1]) - 1.0
            for i in range(1, len(eq_values))
        ]
        if daily_returns:
            import numpy as np
            mean_ret = float(cast(float, pd.Series(daily_returns).mean()))
            std_ret = float(cast(float, pd.Series(daily_returns).std()))
            sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    return {
        "trades": trades,
        "summary": {
            "total_return": total_return,
            "win_rate": win_rate,
            "trade_count": trade_count,
            "avg_return": avg_return,
            "max_drawdown": max_dd,
            "sharpe_ratio": sharpe,
            "final_equity": final_equity,
            "initial_capital": initial_capital,
        },
        "equity_curve": equity_curve,
    }
