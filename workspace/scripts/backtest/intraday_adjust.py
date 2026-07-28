"""Intraday data adjustment for A-share market.

Adjusts volume and amount columns by extrapolating partial-day data
to full-day estimates based on A-share trading hours.
A-share: 9:30-11:30 (120 min) + 13:00-15:00 (120 min) = 240 min total.
"""
from __future__ import annotations
import pandas as pd


def _calc_trading_minutes(current_time: str) -> int:
    """Calculate elapsed trading minutes from current time.
    
    A-share sessions: Morning 9:30-11:30, Afternoon 13:00-15:00.
    Lunch break 11:30-13:00: elapsed stays at 120.
    """
    parts = current_time.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    total_min = hour * 60 + minute
    
    morning_start = 9 * 60 + 30   # 9:30 = 570
    morning_end = 11 * 60 + 30    # 11:30 = 690
    afternoon_start = 13 * 60     # 13:00 = 780
    afternoon_end = 15 * 60       # 15:00 = 900
    
    if total_min < morning_start:
        raise ValueError(f"Time {current_time} is before market open (9:30)")
    elif total_min <= morning_end:
        return total_min - morning_start
    elif total_min <= afternoon_start:
        return 120  # lunch break: morning complete, afternoon not started
    elif total_min <= afternoon_end:
        return 120 + (total_min - afternoon_start)
    else:
        return 240  # market closed


def adjust_intraday_data(df: pd.DataFrame, current_time: str) -> pd.DataFrame:
    """Adjust intraday volume/amount by extrapolating to full-day estimates.
    
    Args:
        df: DataFrame with 'vol' and 'amount' columns. Only the LAST row is adjusted.
        current_time: Current time in "HH:MM" or "HH:MM:SS" format.
    
    Returns:
        Copy of df with adjusted last row. Does NOT modify price columns.
    """
    result = df.copy()
    elapsed = _calc_trading_minutes(current_time)
    ratio = elapsed / 240.0
    
    if ratio >= 0.95:
        return result  # near close, no adjustment needed
    
    if ratio <= 0:
        return result  # no trading yet
    
    # Adjust only the last row's vol and amount
    for col in ["vol", "amount"]:
        if col in result.columns:
            result.iloc[-1, result.columns.get_loc(col)] = result.iloc[-1][col] / ratio
    
    return result
