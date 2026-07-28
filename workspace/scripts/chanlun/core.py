"""Chanlun (缠论) core structure analysis: inclusion, fractals, strokes, centers.

All structures use plain dicts — no dataclasses.
"""
from __future__ import annotations

from typing import Any, cast

import pandas as pd

MIN_FX_GAP = 4
EPS = 1e-12


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return cast(pd.Series, df[name])


def _scalar_date(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def remove_inclusion(df: pd.DataFrame) -> pd.DataFrame:
    """Conservative K-line inclusion handling.

    Merges contained bars following the current trend direction.
    Input must have columns: trade_date, open, high, low, close, vol, amount.
    Returns a DataFrame of merged bars (dicts).
    """
    rows: list[dict[str, Any]] = []
    direction = 1
    for pos, (_, row) in enumerate(df.iterrows()):
        row_s = cast(pd.Series, row)
        item: dict[str, Any] = {
            "orig_start": pos,
            "orig_end": pos,
            "trade_date": row_s["trade_date"],
            "open": float(row_s["open"]),
            "high": float(row_s["high"]),
            "low": float(row_s["low"]),
            "close": float(row_s["close"]),
            "vol": float(row_s["vol"]),
            "amount": float(row_s["amount"]),
        }
        if len(rows) < 2:
            rows.append(item)
            continue

        prev = rows[-1]
        prev_high = float(prev["high"])
        prev_low = float(prev["low"])
        high = float(item["high"])
        low = float(item["low"])
        contains = (
            (high <= prev_high and low >= prev_low)
            or (high >= prev_high and low <= prev_low)
        )
        if not contains:
            if high > prev_high and low > prev_low:
                direction = 1
            elif high < prev_high and low < prev_low:
                direction = -1
            rows.append(item)
            continue

        if direction >= 0:
            prev["high"] = max(prev_high, high)
            prev["low"] = max(prev_low, low)
        else:
            prev["high"] = min(prev_high, high)
            prev["low"] = min(prev_low, low)
        prev["close"] = float(item["close"])
        prev["trade_date"] = item["trade_date"]
        prev["orig_end"] = item["orig_end"]
        prev["vol"] = float(prev["vol"]) + float(item["vol"])
        prev["amount"] = float(prev["amount"]) + float(item["amount"])

    return pd.DataFrame(rows)


def detect_fractals(df: pd.DataFrame) -> list[dict]:
    """Detect top/bottom fractals from merged K-line data.

    Returns list of dicts with keys:
        index, date, mark ('top'|'bottom'), price, high, low
    """
    fractals: list[dict] = []
    highs = _col(df, "high")
    lows = _col(df, "low")

    for i in range(1, len(df) - 1):
        h_prev, h_cur, h_next = highs.iloc[i - 1], highs.iloc[i], highs.iloc[i + 1]
        l_prev, l_cur, l_next = lows.iloc[i - 1], lows.iloc[i], lows.iloc[i + 1]

        is_top = h_cur > h_prev and h_cur > h_next and l_cur > l_prev and l_cur > l_next
        is_bottom = h_cur < h_prev and h_cur < h_next and l_cur < l_prev and l_cur < l_next

        if is_top:
            row_s = cast(pd.Series, df.iloc[i])
            fractals.append({
                "index": i,
                "date": _scalar_date(row_s["trade_date"]),
                "mark": "top",
                "price": float(h_cur),
                "high": float(h_cur),
                "low": float(l_cur),
            })
        elif is_bottom:
            row_s = cast(pd.Series, df.iloc[i])
            fractals.append({
                "index": i,
                "date": _scalar_date(row_s["trade_date"]),
                "mark": "bottom",
                "price": float(l_cur),
                "high": float(h_cur),
                "low": float(l_cur),
            })

    return _normalize_fractals(fractals)


def _normalize_fractals(fractals: list[dict]) -> list[dict]:
    selected: list[dict] = []
    for fx in fractals:
        if not selected:
            selected.append(fx)
            continue
        last = selected[-1]
        if fx["mark"] == last["mark"]:
            if fx["mark"] == "top" and fx["price"] >= last["price"]:
                selected[-1] = fx
            elif fx["mark"] == "bottom" and fx["price"] <= last["price"]:
                selected[-1] = fx
            continue
        if fx["index"] - last["index"] < MIN_FX_GAP:
            if fx["mark"] == "top" and fx["price"] > last["price"]:
                selected[-1] = fx
            elif fx["mark"] == "bottom" and fx["price"] < last["price"]:
                selected[-1] = fx
            continue
        selected.append(fx)
    return selected


def build_strokes(df: pd.DataFrame, fractals: list[dict]) -> list[dict]:
    """Build strokes from alternating fractals.

    Returns list of dicts with keys:
        start_index, end_index, start_date, end_date, direction ('up'|'down'),
        start_price, end_price, high, low, return_pct, bars
    """
    strokes: list[dict] = []
    for left, right in zip(fractals, fractals[1:]):
        if left["mark"] == right["mark"]:
            continue
        direction = "up" if left["mark"] == "bottom" and right["mark"] == "top" else "down"
        start = min(left["index"], right["index"])
        end = max(left["index"], right["index"])
        seg = df.iloc[start: end + 1]
        start_price = float(left["price"])
        end_price = float(right["price"])
        strokes.append({
            "start_index": left["index"],
            "end_index": right["index"],
            "start_date": left["date"],
            "end_date": right["date"],
            "direction": direction,
            "start_price": start_price,
            "end_price": end_price,
            "high": float(_col(seg, "high").max()),
            "low": float(_col(seg, "low").min()),
            "return_pct": float(end_price / (start_price + EPS) - 1.0),
            "bars": int(end - start + 1),
        })
    return strokes


def find_centers(strokes: list[dict]) -> list[dict]:
    """Detect centers from overlapping stroke groups.

    Returns list of dicts with keys:
        start_date, end_date, zg, zd, gg, dd, stroke_count
    """
    centers: list[dict] = []
    for i in range(len(strokes) - 2):
        group = strokes[i: i + 3]
        zg = min(s["high"] for s in group)
        zd = max(s["low"] for s in group)
        if zg > zd:
            centers.append({
                "start_date": group[0]["start_date"],
                "end_date": group[-1]["end_date"],
                "zg": float(zg),
                "zd": float(zd),
                "gg": float(max(s["high"] for s in group)),
                "dd": float(min(s["low"] for s in group)),
                "stroke_count": 3,
            })

    merged: list[dict] = []
    for center in centers:
        if not merged:
            merged.append(center)
            continue
        last = merged[-1]
        overlap_zg = min(last["zg"], center["zg"])
        overlap_zd = max(last["zd"], center["zd"])
        if overlap_zg > overlap_zd:
            merged[-1] = {
                "start_date": last["start_date"],
                "end_date": center["end_date"],
                "zg": overlap_zg,
                "zd": overlap_zd,
                "gg": max(last["gg"], center["gg"]),
                "dd": min(last["dd"], center["dd"]),
                "stroke_count": last["stroke_count"] + 1,
            }
        else:
            merged.append(center)
    return merged
