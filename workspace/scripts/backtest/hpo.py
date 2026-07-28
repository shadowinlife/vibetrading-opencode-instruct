"""Hyperparameter Optimization (HPO) engine for backtest strategies.

Two-stage grid search over StrategyConfig parameters:
  Stage 1: Coarse search on entry_z x exit_z with fixed defaults.
  Stage 2: Fine search on stop_loss x take_profit x max_hold x profit_action
           for top candidates from Stage 1.

Uses shared components:
  - StrategyConfig from scripts.backtest.config
  - simulate_segment, simulate_buy_hold from scripts.backtest.engine
  - calc_metrics from scripts.backtest.metrics
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from scripts.backtest.config import StrategyConfig
from scripts.backtest.engine import simulate_buy_hold, simulate_segment
from scripts.backtest.metrics import calc_metrics

ONE_WAY_COST = 0.0015
BASE_POSITION = 1.0
MIN_HISTORY = 252
EPS = 1e-12

ENTRY_Z_COARSE = [0.3, 0.5, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5]
EXIT_Z_COARSE = [-0.5, -0.3, -0.2, -0.1, 0.0, 0.2]

ENTRY_Z_FINE = [0.5, 0.7, 0.8, 0.9, 1.0, 1.2]
EXIT_Z_FINE = [-0.4, -0.3, -0.2, -0.1, 0.0]

STOP_LOSS_RANGE = [-0.12, -0.10, -0.08, -0.06]
TAKE_PROFIT_RANGE: list[float | None] = [0.08, 0.10, 0.12, 0.16, 0.20, None]
PROFIT_TRIGGER_RANGE = [0.010, 0.015, 0.020, 0.025]
MAX_HOLD_RANGE = [63, 90, 126]
ACTION_MAP: dict[str, float] = {
    "hold": 0.6,
    "add": 1.0,
    "reduce": 0.3,
    "clear": 0.0,
}

SMALL_ENTRY = [0.5, 0.8, 1.0, 1.2]
SMALL_EXIT = [-0.3, -0.1, 0.0]
SMALL_STOP = [-0.10, -0.08]
SMALL_TP: list[float | None] = [0.12, None]
SMALL_TRIGGER = [0.015]
SMALL_HOLD = [63, 126]
SMALL_ACTIONS: dict[str, float] = {"hold": 0.6, "add": 1.0}


@dataclass
class HpoStrategyConfig(StrategyConfig):
    """Extended StrategyConfig with take-profit and profit-action fields.

    Inherits from shared StrategyConfig:
      name, signal_col, entry_z, exit_z, stop_loss, max_hold,
      vol_target, confirm_col, confirm_positive, confirmation_threshold

    Adds HPO-specific fields for profit-taking logic that the shared
    engine does not support.
    """

    take_profit: float | None = None
    profit_trigger: float = 0.015
    profit_action: str = "add"
    profit_target_position: float = 1.0


@dataclass
class HpoResult:
    """Container for HPO search results."""

    best_config: HpoStrategyConfig
    best_metrics: dict[str, Any]
    all_results: list[dict[str, Any]]
    stage1_results: list[dict[str, Any]]
    stage2_results: list[dict[str, Any]]


def simulate_hpo_segment(
    df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    cfg: HpoStrategyConfig,
) -> tuple[pd.Series, pd.DataFrame, dict]:
    """Simulate a trading segment with HPO-extended config.

    Adds take-profit and profit-trigger/action logic on top of the
    shared engine's entry/exit/stop-loss/max-hold rules.

    Uses calc_metrics from scripts.backtest.metrics for metric computation.

    Args:
        df: DataFrame with trade_date, close, daily_ret, and signal column.
        start_idx: First row index (inclusive).
        end_idx: Last row index (inclusive).
        cfg: HpoStrategyConfig with extended fields.

    Returns:
        (seg_rets, trades_df, metrics_dict)
    """
    dates = df["trade_date"].iloc[start_idx : end_idx + 1].reset_index(drop=True)
    seg_rets = pd.Series(0.0, index=dates)
    pos = 0.0
    entry_price = np.nan
    days_held = 0
    entry_idx: int | None = None
    profit_done = False
    trades: list[dict[str, Any]] = []

    for t in range(start_idx, end_idx):
        today = df.iloc[t]
        next_ret = float(df.iloc[t + 1]["daily_ret"])
        desired_pos = pos
        sig = float(today[cfg.signal_col]) if pd.notna(today[cfg.signal_col]) else np.nan

        if pos == 0.0:
            # --- Entry logic (same as shared engine) ---
            sig_ok = pd.notna(sig) and sig > cfg.entry_z
            gate_ok = True
            if cfg.confirm_col:
                gate_val = today.get(cfg.confirm_col, np.nan)
                if pd.notna(gate_val):
                    gate_ok = bool(
                        gate_val > cfg.confirmation_threshold
                        if cfg.confirm_positive
                        else gate_val < cfg.confirmation_threshold
                    )
                else:
                    gate_ok = False
            if sig_ok and gate_ok:
                desired_pos = BASE_POSITION
                entry_price = float(today["close"])
                days_held = 0
                entry_idx = t
                profit_done = False
        else:
            days_held += 1
            trade_ret = (
                float(today["close"] / entry_price - 1)
                if entry_price == entry_price
                else 0.0
            )

            # --- Profit-trigger check (HPO extension) ---
            if not profit_done and trade_ret >= cfg.profit_trigger:
                profit_done = True
                if cfg.profit_action == "clear":
                    desired_pos = 0.0
                    trades.append(_make_trade(cfg, df, entry_idx, today, days_held, entry_price, trade_ret, "profit_clear", pos))
                    entry_price = np.nan
                    days_held = 0
                    entry_idx = None
                elif cfg.profit_action == "reduce":
                    desired_pos = cfg.profit_target_position
                elif cfg.profit_action == "add":
                    desired_pos = cfg.profit_target_position
                # "hold" → no position change

            # --- Exit checks (if still holding) ---
            if desired_pos > 0.0:
                should_exit = False
                exit_reason = ""

                # Signal exit (same as shared engine)
                if pd.notna(sig) and sig < cfg.exit_z:
                    should_exit = True
                    exit_reason = "signal"
                # Stop-loss (same as shared engine)
                elif trade_ret <= cfg.stop_loss:
                    should_exit = True
                    exit_reason = "stop_loss"
                # Take-profit (HPO extension)
                elif cfg.take_profit is not None and trade_ret >= cfg.take_profit:
                    should_exit = True
                    exit_reason = "take_profit"
                # Max-hold (same as shared engine)
                elif days_held >= cfg.max_hold:
                    should_exit = True
                    exit_reason = "max_hold"

                if should_exit:
                    desired_pos = 0.0
                    trades.append(_make_trade(cfg, df, entry_idx, today, days_held, entry_price, trade_ret, exit_reason, pos))
                    entry_price = np.nan
                    days_held = 0
                    entry_idx = None

        delta = abs(desired_pos - pos)
        seg_rets.iloc[t - start_idx + 1] = desired_pos * next_ret - delta * ONE_WAY_COST
        pos = desired_pos

    trades_df = pd.DataFrame(trades)
    hold_days_list = [int(t["hold_days"]) for t in trades]
    metrics = calc_metrics(seg_rets.iloc[1:], len(trades), hold_days_list)
    return seg_rets, trades_df, metrics


def _make_trade(
    cfg: HpoStrategyConfig,
    df: pd.DataFrame,
    entry_idx: int | None,
    exit_row: pd.Series,
    hold_days: int,
    entry_price: float,
    gross_return: float,
    reason: str,
    position: float,
) -> dict[str, Any]:
    """Build a trade record dict."""
    entry_date_str = ""
    if entry_idx is not None:
        td = df.iloc[entry_idx]["trade_date"]
        entry_date_str = str(td.date()) if hasattr(td, "date") else str(td)
    return {
        "strategy": cfg.name,
        "entry_date": entry_date_str,
        "exit_date": str(exit_row["trade_date"].date()) if hasattr(exit_row["trade_date"], "date") else str(exit_row["trade_date"]),  # pyright: ignore[reportAttributeAccessIssue]
        "hold_days": hold_days,
        "entry_price": entry_price,
        "exit_price": float(exit_row["close"]),
        "gross_return": gross_return,
        "net_return": gross_return - 2.0 * ONE_WAY_COST,
        "exit_reason": reason,
        "position_size": position,
        "signal_col": cfg.signal_col,
    }


def compute_rank_score(m: dict[str, Any]) -> float:
    """Compute composite ranking score from metrics dict.

    Formula from existing HPO:
      sharpe * 1000 + calmar * 150 + positive_excess_ratio * 20
      + median_fold_excess * 10 + robust_calmar * 30 - |max_dd| * 5
    """
    return (
        m.get("sharpe", 0) * 1000
        + m.get("calmar", 0) * 150
        + m.get("positive_excess_ratio", 0) * 20
        + m.get("median_fold_excess", 0) * 10
        + m.get("robust_calmar", 0) * 30
        - abs(m.get("max_dd", 0)) * 5
    )


def _quick_evaluate(
    df: pd.DataFrame,
    cfg: HpoStrategyConfig,
    start_oos: int,
) -> dict[str, Any]:
    """Quick single-segment evaluation for Stage 1."""
    _, trades_df, metrics = simulate_hpo_segment(df, start_oos, len(df) - 1, cfg)
    bh = simulate_buy_hold(df, start_oos, len(df) - 1)
    return {
        "sharpe": metrics["sharpe"],
        "calmar": metrics["calmar"],
        "total_ret": metrics["total_ret"],
        "max_dd": metrics["max_dd"],
        "trade_count": metrics["trade_count"],
        "excess_ret": metrics["total_ret"] - bh["total_ret"],
    }


def _walk_forward_evaluate(
    df: pd.DataFrame,
    cfg: HpoStrategyConfig,
    start_oos: int,
    test_days: int,
) -> dict[str, Any]:
    """Walk-forward evaluation with fold-level metrics for Stage 2."""
    folds: list[dict[str, Any]] = []
    for fold_start in range(start_oos, len(df) - test_days, test_days):
        fold_end = min(fold_start + test_days, len(df) - 1)
        _, _, m = simulate_hpo_segment(df, fold_start, fold_end, cfg)
        bh = simulate_buy_hold(df, fold_start, fold_end)
        folds.append({
            "sharpe": m["sharpe"],
            "calmar": m["calmar"],
            "excess_ret": m["total_ret"] - bh["total_ret"],
        })

    full_rets, full_trades, full_m = simulate_hpo_segment(df, start_oos, len(df) - 1, cfg)
    bh_full = simulate_buy_hold(df, start_oos, len(df) - 1)

    worst_days = full_rets.iloc[1:].abs().sort_values(ascending=False).head(3).index
    hold_days_col = [int(x) for x in full_trades["hold_days"].tolist()] if "hold_days" in full_trades.columns else []
    robust_m = calc_metrics(full_rets.iloc[1:].drop(index=worst_days, errors="ignore"), full_m["trade_count"], hold_days_col)

    fold_df = pd.DataFrame(folds)
    return {
        "sharpe": full_m["sharpe"],
        "calmar": full_m["calmar"],
        "total_ret": full_m["total_ret"],
        "annual_ret": full_m["annual_ret"],
        "max_dd": full_m["max_dd"],
        "trade_count": full_m["trade_count"],
        "win_rate": 0.0,
        "buyhold_total_ret": bh_full["total_ret"],
        "excess_ret": full_m["total_ret"] - bh_full["total_ret"],
        "fold_count": len(fold_df),
        "positive_excess_ratio": float((fold_df["excess_ret"] > 0).mean()) if not fold_df.empty else 0.0,
        "median_fold_excess": float(fold_df["excess_ret"].median()) if not fold_df.empty else 0.0,
        "robust_calmar": robust_m["calmar"],
    }


def run_hpo(
    df: pd.DataFrame,
    signal_builders: list[tuple[str, Callable]],
    search_space: str = "small",
    train_days: int = 350,
    test_days: int = 63,
) -> HpoResult:
    """Run two-stage HPO grid search.

    Args:
        df: DataFrame with OHLCV + daily_ret columns.
            Must contain: trade_date, close, daily_ret.
        signal_builders: List of (name, callable) pairs.
            Each callable: (close, high, low, vol, amount) -> pd.Series.
        search_space: "small" for quick validation, "full" for thorough search.
        train_days: Number of initial rows to skip (warmup + training).
        test_days: Fold length for walk-forward evaluation in Stage 2.

    Returns:
        HpoResult with best_config, best_metrics, and all search results.
    """
    assert "daily_ret" in df.columns, "df must contain 'daily_ret' column (close.pct_change())"
    assert len(df) > MIN_HISTORY, f"Need >{MIN_HISTORY} rows, got {len(df)}"

    start_oos = max(MIN_HISTORY, train_days)

    if search_space == "small":
        entry_range, exit_range = SMALL_ENTRY, SMALL_EXIT
        stop_range, tp_range = SMALL_STOP, SMALL_TP
        trigger_range, hold_range = SMALL_TRIGGER, SMALL_HOLD
        action_map = SMALL_ACTIONS
        top_n_stage1 = 2
    else:
        entry_range, exit_range = ENTRY_Z_COARSE, EXIT_Z_COARSE
        stop_range, tp_range = STOP_LOSS_RANGE, TAKE_PROFIT_RANGE
        trigger_range, hold_range = PROFIT_TRIGGER_RANGE, MAX_HOLD_RANGE
        action_map = ACTION_MAP
        top_n_stage1 = 3

    work_df = df.copy()
    for name, builder in signal_builders:
        if name not in work_df.columns:
            signal = builder(work_df["close"], work_df["high"], work_df["low"], work_df["vol"], work_df["amount"])
            work_df[name] = signal

    print("=" * 60)
    print("STAGE 1: Coarse grid on entry_z x exit_z")
    print("=" * 60)
    t0 = time.time()

    stage1_all: list[dict[str, Any]] = []
    total_s1 = len(signal_builders) * len(entry_range) * len(exit_range)
    print(f"  Signals: {len(signal_builders)}, Entry: {len(entry_range)}, Exit: {len(exit_range)}")
    print(f"  Total configs: {total_s1}")

    for sig_name, _ in signal_builders:
        for entry_z, exit_z in itertools.product(entry_range, exit_range):
            cfg = HpoStrategyConfig(
                name=f"s1_{sig_name}",
                signal_col=sig_name,
                entry_z=entry_z,
                exit_z=exit_z,
                stop_loss=-0.08,
                max_hold=126,
                take_profit=0.12,
                profit_trigger=0.015,
                profit_action="add",
                profit_target_position=1.0,
            )
            m = _quick_evaluate(work_df, cfg, start_oos)
            stage1_all.append({
                "signal_name": sig_name,
                "entry_z": entry_z,
                "exit_z": exit_z,
                **m,
            })

        print(f"  {sig_name}: {len(entry_range) * len(exit_range)} combos done")

    print(f"Stage 1 done: {len(stage1_all)} configs in {time.time() - t0:.1f}s")

    stage1_top: list[dict[str, Any]] = []
    for sig_name, _ in signal_builders:
        sig_results = [r for r in stage1_all if r["signal_name"] == sig_name]
        eligible = [r for r in sig_results if r["trade_count"] >= 2 and r["max_dd"] > -0.25]
        if not eligible:
            eligible = sig_results
        eligible.sort(key=lambda x: x["calmar"], reverse=True)
        stage1_top.extend(eligible[:top_n_stage1])
        if eligible:
            b = eligible[0]
            print(f"  Best {sig_name}: entry={b['entry_z']}, exit={b['exit_z']}, calmar={b['calmar']:.3f}")

    print()
    print("=" * 60)
    print("STAGE 2: Fine grid on top candidates")
    print("=" * 60)
    t1 = time.time()

    combos_per = len(stop_range) * len(tp_range) * len(action_map) * len(hold_range)
    total_s2 = len(stage1_top) * combos_per
    print(f"  Candidates: {len(stage1_top)}, combos each: {combos_per}")
    print(f"  Total configs: {total_s2}")

    stage2_all: list[dict[str, Any]] = []
    for cand in stage1_top:
        sig_name = cand["signal_name"]
        entry_z = cand["entry_z"]
        exit_z = cand["exit_z"]

        for stop_loss, take_profit, (action, target), max_hold in itertools.product(
            stop_range, tp_range, action_map.items(), hold_range
        ):
            cfg = HpoStrategyConfig(
                name=f"s2_{sig_name}_{action}",
                signal_col=sig_name,
                entry_z=entry_z,
                exit_z=exit_z,
                stop_loss=stop_loss,
                max_hold=max_hold,
                take_profit=take_profit,
                profit_trigger=0.015,
                profit_action=action,
                profit_target_position=target,
            )
            m = _walk_forward_evaluate(work_df, cfg, start_oos, test_days)
            rank = compute_rank_score(m)
            stage2_all.append({
                "signal_name": sig_name,
                "entry_z": entry_z,
                "exit_z": exit_z,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "profit_action": action,
                "profit_target_position": target,
                "max_hold": max_hold,
                "rank_score": rank,
                **m,
            })

        print(f"  {sig_name} (entry={entry_z}, exit={exit_z}): {combos_per} combos, {time.time() - t1:.1f}s")

    print(f"Stage 2 done: {len(stage2_all)} configs in {time.time() - t1:.1f}s")

    stage2_all.sort(key=lambda x: x["rank_score"], reverse=True)

    best = stage2_all[0]
    best_config = HpoStrategyConfig(
        name=best["signal_name"],
        signal_col=best["signal_name"],
        entry_z=best["entry_z"],
        exit_z=best["exit_z"],
        stop_loss=best["stop_loss"],
        max_hold=best["max_hold"],
        take_profit=best["take_profit"],
        profit_trigger=0.015,
        profit_action=best["profit_action"],
        profit_target_position=best["profit_target_position"],
    )
    best_metrics = {k: v for k, v in best.items() if k not in (
        "signal_name", "entry_z", "exit_z", "stop_loss", "take_profit",
        "profit_action", "profit_target_position", "max_hold", "rank_score",
    )}

    return HpoResult(
        best_config=best_config,
        best_metrics=best_metrics,
        all_results=stage2_all,
        stage1_results=stage1_all,
        stage2_results=stage2_all,
    )


def today_signal(
    df: pd.DataFrame,
    cfg: HpoStrategyConfig,
) -> dict[str, Any]:
    """Compute today's signal value for the given config.

    Args:
        df: DataFrame with signal column already computed.
        cfg: HpoStrategyConfig with signal_col set.

    Returns:
        Dict with date, close, signal_value, entry_threshold, and entry_decision.
    """
    latest = df.iloc[-1]
    sig = float(latest[cfg.signal_col]) if pd.notna(latest[cfg.signal_col]) else None

    entry_yes = False
    decision = "NO"
    reason = ""
    if sig is None:
        decision = "SIGNAL_UNAVAILABLE"
        reason = f"Signal '{cfg.signal_col}' is NaN for latest row"
    elif sig > cfg.entry_z:
        entry_yes = True
        decision = "YES"
        reason = f"Signal {sig:.4f} > entry_z {cfg.entry_z}"
    else:
        reason = f"Signal {sig:.4f} <= entry_z {cfg.entry_z}"

    td = latest["trade_date"]
    return {
        "date": str(td.date()) if hasattr(td, "date") else str(td),
        "close": float(latest["close"]),
        "signal_col": cfg.signal_col,
        "signal_value": sig,
        "entry_z": cfg.entry_z,
        "exit_z": cfg.exit_z,
        "entry_decision": decision,
        "entry_yes": entry_yes,
        "reason": reason,
    }
