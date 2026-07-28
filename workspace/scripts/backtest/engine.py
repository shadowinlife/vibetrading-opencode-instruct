"""Backtest simulation engine: simulate_segment with confirmation gate support.

Cost model (Phase 5 Task 23):
    Commission is applied to both buy and sell sides at ``cfg.commission_rate``.
    Stamp tax is applied only to the sell side at ``cfg.stamp_tax_rate``.

    Round-trip cost = 2 * commission_rate + stamp_tax_rate.

    ONE_WAY_COST is retained as a backward-compatible constant for
    simulate_buy_hold (which has no StrategyConfig).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.backtest.config import StrategyConfig
from scripts.backtest.metrics import calc_metrics

# Backward-compatible constant for simulate_buy_hold (no config access).
# Represents a generic per-side friction cost (commission + half stamp tax).
ONE_WAY_COST = 0.0015
PRICE_LIMIT_PCT = 0.10
DEFAULT_IMPACT_COEFFICIENT = 0.01
DEFAULT_ADV_SHARES = 1_000_000
DEFAULT_CAPITAL = 1_000_000


def _check_limit_up(today_close: float, prev_close: float) -> bool:
    """Return True if today's close hits the +10% limit-up threshold."""
    if prev_close <= 0:
        return False
    return today_close >= prev_close * (1.0 + PRICE_LIMIT_PCT) - 1e-9


def _check_limit_down(today_close: float, prev_close: float) -> bool:
    """Return True if today's close hits the -10% limit-down threshold."""
    if prev_close <= 0:
        return False
    return today_close <= prev_close * (1.0 - PRICE_LIMIT_PCT) + 1e-9


def _compute_adv(volume: pd.Series) -> float:
    """Compute average daily volume. Returns DEFAULT_ADV_SHARES if unavailable."""
    if volume is None or len(volume) == 0:
        return float(DEFAULT_ADV_SHARES)
    adv = float(volume.mean())
    if np.isnan(adv) or adv <= 0:
        return float(DEFAULT_ADV_SHARES)
    return adv


def _apply_linear_impact(
    signal_price: float,
    side: str,
    order_shares: float,
    adv: float,
    impact_coefficient: float = DEFAULT_IMPACT_COEFFICIENT,
) -> float:
    """Compute fill price with linear market impact.

    Buy:  fill = signal_price * (1 + coeff * order_shares / ADV)
    Sell: fill = signal_price * (1 - coeff * order_shares / ADV)
    """
    participation = order_shares / adv if adv > 0 else 0.0
    if side == "buy":
        return signal_price * (1.0 + impact_coefficient * participation)
    else:
        return signal_price * (1.0 - impact_coefficient * participation)


def simulate_segment(
    df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    cfg: StrategyConfig,
) -> tuple[pd.Series, pd.DataFrame, dict, list[dict]]:
    """Simulate trading a single segment of data with entry/exit rules.

    Entry requires signal > entry_z (and optional confirmation gate pass).
    Exit via signal < exit_z, max_hold expiry, or stop-loss breach.
    Gate only affects entry — exit is never gated.

    Phase 5 execution-model constraints (opt-in via cfg):
        t_plus_1: Reject sell on the same calendar day as buy (reason: 't_plus_1').
        price_limit: Reject buy on limit-up day (reason: 'limit_up_no_buy'),
                     reject sell on limit-down day (reason: 'limit_down_no_sell').
        max_turnover_ratio: Cap buy amount at ratio * daily_amount (liquidity constraint).
        slippage_model: 'linear_impact' applies side-aware price adjustment;
                        None means no slippage (fill at signal price).

    Returns:
        (seg_rets, trades_df, metrics, rejected_trades)
    """
    dates = df["trade_date"].iloc[start_idx : end_idx + 1].reset_index(drop=True)
    seg_rets = pd.Series(0.0, index=dates)
    pos = 0.0
    entry_price = np.nan
    entry_signal_price = np.nan
    days_held = 0
    entry_idx = None
    trades: list[dict] = []
    rejected_trades: list[dict] = []

    # Pre-compute ADV for slippage model
    adv = _compute_adv(df["volume"]) if "volume" in df.columns else float(DEFAULT_ADV_SHARES)

    for t in range(start_idx, end_idx):
        today = df.iloc[t]
        next_ret = float(df.iloc[t + 1]["daily_ret"])
        desired_pos = pos
        slippage = 0.0
        sig = float(today[cfg.signal_col]) if pd.notna(today[cfg.signal_col]) else np.nan

        prev_close = float(df.iloc[t - 1]["close"]) if t > start_idx else 0.0
        today_close = float(today["close"])

        if pos == 0.0:
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
                if cfg.price_limit and prev_close > 0 and _check_limit_up(today_close, prev_close):
                    rejected_trades.append({
                        "date": str(today["trade_date"].date()),
                        "action": "buy",
                        "reason": "limit_up_no_buy",
                        "close": today_close,
                        "prev_close": prev_close,
                        "signal": sig,
                    })
                else:
                    desired_pos = 1.0
                    if cfg.vol_target:
                        vol = (
                            float(today["realized_vol_20d_ann"])
                            if pd.notna(today["realized_vol_20d_ann"])
                            else np.nan
                        )
                        desired_pos = (
                            float(min(1.0, 0.30 / max(vol, 0.05)))
                            if pd.notna(vol)
                            else 1.0
                        )
                    # Liquidity cap: limit position by daily turnover
                    if cfg.max_turnover_ratio is not None and "amount" in df.columns:
                        daily_amount = float(today["amount"])
                        if daily_amount > 0 and today_close > 0:
                            max_pos = (
                                cfg.max_turnover_ratio * daily_amount / today_close
                            ) / (DEFAULT_CAPITAL / today_close)
                            desired_pos = min(desired_pos, max_pos)
                    # Slippage: adjust entry price
                    pos_delta = abs(desired_pos - pos)
                    if cfg.slippage_model == "linear_impact" and pos_delta > 0:
                        order_shares = pos_delta * DEFAULT_CAPITAL / today_close
                        entry_price = _apply_linear_impact(
                            today_close, "buy", order_shares, adv,
                        )
                        slippage = abs(entry_price - today_close) / today_close
                    else:
                        entry_price = float(today["close"])
                    entry_signal_price = float(today["close"])
                    days_held = 0
                    entry_idx = t
        else:
            days_held += 1
            trade_ret = (
                float(today["close"] / entry_price - 1)
                if entry_price == entry_price
                else 0.0
            )
            should_exit = False
            exit_reason = ""
            if pd.notna(sig) and sig < cfg.exit_z:
                should_exit = True
                exit_reason = "signal"
            elif days_held >= cfg.max_hold:
                should_exit = True
                exit_reason = "max_hold"
            elif trade_ret <= cfg.stop_loss:
                should_exit = True
                exit_reason = "stop_loss"

            if should_exit:
                if cfg.t_plus_1 and entry_idx is not None and entry_idx == t:
                    rejected_trades.append({
                        "date": str(today["trade_date"].date()),
                        "action": "sell",
                        "reason": "t_plus_1",
                        "entry_date": str(df.iloc[entry_idx]["trade_date"].date()),
                        "close": today_close,
                        "exit_reason": exit_reason,
                    })
                elif cfg.price_limit and prev_close > 0 and _check_limit_down(today_close, prev_close):
                    rejected_trades.append({
                        "date": str(today["trade_date"].date()),
                        "action": "sell",
                        "reason": "limit_down_no_sell",
                        "entry_date": str(df.iloc[entry_idx]["trade_date"].date()) if entry_idx is not None else "",
                        "close": today_close,
                        "prev_close": prev_close,
                        "exit_reason": exit_reason,
                    })
                else:
                    desired_pos = 0.0
                    exit_price = float(today["close"])
                    # Slippage: adjust exit price
                    pos_delta = abs(desired_pos - pos)
                    if cfg.slippage_model == "linear_impact" and pos_delta > 0:
                        order_shares = pos_delta * DEFAULT_CAPITAL / today_close
                        exit_price = _apply_linear_impact(
                            today_close, "sell", order_shares, adv,
                        )
                        slippage = abs(today_close - exit_price) / today_close
                    round_trip_cost = (
                        2.0 * cfg.commission_rate + cfg.stamp_tax_rate
                    )
                    trades.append(
                        {
                            "entry_date": (
                                str(df.iloc[entry_idx]["trade_date"].date())
                                if entry_idx is not None
                                else ""
                            ),
                            "exit_date": str(today["trade_date"].date()),
                            "hold_days": days_held,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "gross_return": trade_ret,
                            "net_return": trade_ret - round_trip_cost,
                            "exit_reason": exit_reason,
                            "position_size": pos,
                            "signal_col": cfg.signal_col,
                            "fill_price": exit_price,
                            "signal_price": float(today["close"]),
                            "entry_signal_price": entry_signal_price,
                            "slippage_model": cfg.slippage_model,
                            "commission_rate": cfg.commission_rate,
                            "stamp_tax_rate": cfg.stamp_tax_rate,
                        }
                    )
                    entry_price = np.nan
                    entry_signal_price = np.nan
                    days_held = 0
                    entry_idx = None

        delta = abs(desired_pos - pos)
        if delta > 0:
            if desired_pos > pos:
                trade_cost = delta * cfg.commission_rate
            else:
                trade_cost = delta * (cfg.commission_rate + cfg.stamp_tax_rate)
        else:
            trade_cost = 0.0
        seg_rets.iloc[t - start_idx + 1] = (
            desired_pos * next_ret - trade_cost - slippage
        )
        pos = desired_pos

    metrics = calc_metrics(
        seg_rets.iloc[1:],
        len(trades),
        [int(t["hold_days"]) for t in trades],
    )
    return seg_rets, pd.DataFrame(trades), metrics, rejected_trades


def simulate_buy_hold(df: pd.DataFrame, start_idx: int, end_idx: int) -> dict:
    dates = df["trade_date"].iloc[start_idx : end_idx + 1].reset_index(drop=True)
    rets = pd.Series(0.0, index=dates)
    for t in range(start_idx, end_idx):
        rets.iloc[t - start_idx + 1] = float(df.iloc[t + 1]["daily_ret"])
    rets.iloc[1] -= ONE_WAY_COST
    rets.iloc[-1] -= ONE_WAY_COST
    return calc_metrics(rets.iloc[1:], 1, [end_idx - start_idx])


def run_fold_evaluation(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    fold_len: int = 126,
    fold_step: int = 21,
    first_oos_start: int = 0,
) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    fold_rows: list[dict] = []
    all_trades: list[pd.DataFrame] = []
    fold_id = 0
    for fold_start in range(first_oos_start, len(df) - 2, fold_step):
        fold_end = min(fold_start + fold_len - 1, len(df) - 1)
        if fold_end - fold_start < 40:
            continue
        strat_rets, trades, m, _rejected = simulate_segment(df, fold_start, fold_end, cfg)
        bh = simulate_buy_hold(df, fold_start, fold_end)
        row = {
            "fold_id": fold_id,
            "start_date": str(df.iloc[fold_start]["trade_date"].date()),
            "end_date": str(df.iloc[fold_end]["trade_date"].date()),
            "strategy": cfg.name,
            "strategy_total_ret": m["total_ret"],
            "strategy_annual_ret": m["annual_ret"],
            "strategy_sharpe": m["sharpe"],
            "strategy_max_dd": m["max_dd"],
            "strategy_calmar": m["calmar"],
            "trade_count": m["trade_count"],
            "median_hold_days": m["median_hold_days"],
            "max_hold_days": m["max_hold_days"],
            "buyhold_total_ret": bh["total_ret"],
            "buyhold_annual_ret": bh["annual_ret"],
            "buyhold_sharpe": bh["sharpe"],
            "buyhold_max_dd": bh["max_dd"],
            "buyhold_calmar": bh["calmar"],
            "excess_total_ret": m["total_ret"] - bh["total_ret"],
        }
        fold_rows.append(row)
        if not trades.empty:
            trades = trades.copy()
            trades["fold_id"] = fold_id
            trades["strategy"] = cfg.name
            all_trades.append(trades)
        fold_id += 1

    fold_df = pd.DataFrame(fold_rows)
    full_rets, full_trades, full_m, _full_rejected = simulate_segment(df, first_oos_start, len(df) - 1, cfg)
    bh_full = simulate_buy_hold(df, first_oos_start, len(df) - 1)
    top3_mask = full_rets.iloc[1:].abs().sort_values(ascending=False).head(3).index
    hold_days_col = full_trades["hold_days"].tolist() if "hold_days" in full_trades.columns else []
    robust_full = calc_metrics(full_rets.iloc[1:].drop(index=top3_mask), full_m["trade_count"], [int(x) for x in hold_days_col])

    summary = {
        "strategy": cfg.name,
        "oos_start": str(df.iloc[first_oos_start]["trade_date"].date()),
        "oos_end": str(df.iloc[len(df) - 1]["trade_date"].date()),
        "full_total_ret": full_m["total_ret"],
        "full_annual_ret": full_m["annual_ret"],
        "full_sharpe": full_m["sharpe"],
        "full_max_dd": full_m["max_dd"],
        "full_calmar": full_m["calmar"],
        "full_trade_count": full_m["trade_count"],
        "full_median_hold_days": full_m["median_hold_days"],
        "full_max_hold_days": full_m["max_hold_days"],
        "buyhold_total_ret": bh_full["total_ret"],
        "buyhold_annual_ret": bh_full["annual_ret"],
        "buyhold_max_dd": bh_full["max_dd"],
        "buyhold_calmar": bh_full["calmar"],
        "excess_total_ret": full_m["total_ret"] - bh_full["total_ret"],
        "fold_count": int(len(fold_df)),
        "positive_sharpe_fold_ratio": float((fold_df["strategy_sharpe"] > 0).mean()) if not fold_df.empty else 0.0,
        "positive_calmar_fold_ratio": float((fold_df["strategy_calmar"] > 0).mean()) if not fold_df.empty else 0.0,
        "median_fold_calmar": float(fold_df["strategy_calmar"].median()) if not fold_df.empty else 0.0,
        "median_fold_excess_ret": float(fold_df["excess_total_ret"].median()) if not fold_df.empty else 0.0,
        "robust_total_ret_ex_top3_days": robust_full["total_ret"],
        "robust_calmar_ex_top3_days": robust_full["calmar"],
    }
    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else full_trades
    return fold_df, summary, trades_df


def choose_best(summary_df: pd.DataFrame, max_hold_days: int = 126) -> str:
    ranked = summary_df.copy()
    ranked = ranked[ranked["full_max_hold_days"] <= max_hold_days]
    ranked = ranked.sort_values(
        ["full_calmar", "positive_calmar_fold_ratio", "median_fold_excess_ret", "full_total_ret"],
        ascending=[False, False, False, False],
    )  # pyright: ignore[reportCallIssue]
    return str(ranked.iloc[0]["strategy"])