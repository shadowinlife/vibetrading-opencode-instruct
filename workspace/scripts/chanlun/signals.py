"""Chanlun buy/sell point detection based on strokes and centers.

Simplified 1st/2nd/3rd class buy/sell point detection:
- 1st buy:  Down stroke ends below center lower bound (zd), then reverses up
- 2nd buy:  First pullback after 1st buy that stays above center zd
- 3rd buy:  Up stroke starts above center upper bound (zg), confirming breakout
- 1st sell: Up stroke ends above center upper bound (zg), then reverses down
- 2nd sell: First bounce after 1st sell that stays below center zg
- 3rd sell: Down stroke starts below center lower bound (zd), confirming breakdown
"""
from __future__ import annotations


def detect_buy_sell_points(
    strokes: list[dict],
    centers: list[dict],
) -> list[dict]:
    """Detect buy/sell points from strokes and centers.

    Returns list of dicts with keys:
        date, type ('buy1'|'buy2'|'buy3'|'sell1'|'sell2'|'sell3'),
        price, stroke_index, center_index, reason
    """
    signals: list[dict] = []
    if not strokes or not centers:
        return signals

    for si, stroke in enumerate(strokes):
        for ci, center in enumerate(centers):
            if stroke["end_date"] < center["start_date"]:
                continue
            if stroke["start_date"] > center["end_date"]:
                continue

            zg = center["zg"]
            zd = center["zd"]

            # --- Buy signals ---
            if stroke["direction"] == "down":
                # 1st buy: down stroke ends below center zd
                if stroke["end_price"] < zd and stroke["low"] < zd:
                    signals.append({
                        "date": stroke["end_date"],
                        "type": "buy1",
                        "price": stroke["end_price"],
                        "stroke_index": si,
                        "center_index": ci,
                        "reason": f"下跌笔终点 {stroke['end_price']:.3f} < 中枢下沿 {zd:.3f}",
                    })

                # 3rd sell: down stroke starts below center zd (breakdown confirmed)
                if stroke["start_price"] < zd and stroke["end_price"] < zd:
                    # Only if previous stroke was up and ended near/above zg
                    if si > 0 and strokes[si - 1]["direction"] == "up":
                        prev_end = strokes[si - 1]["end_price"]
                        if prev_end >= zd:
                            signals.append({
                                "date": stroke["start_date"],
                                "type": "sell3",
                                "price": stroke["start_price"],
                                "stroke_index": si,
                                "center_index": ci,
                                "reason": f"向下笔起点 {stroke['start_price']:.3f} 跌破中枢下沿 {zd:.3f}",
                            })

            elif stroke["direction"] == "up":
                # 1st sell: up stroke ends above center zg
                if stroke["end_price"] > zg and stroke["high"] > zg:
                    signals.append({
                        "date": stroke["end_date"],
                        "type": "sell1",
                        "price": stroke["end_price"],
                        "stroke_index": si,
                        "center_index": ci,
                        "reason": f"上涨笔终点 {stroke['end_price']:.3f} > 中枢上沿 {zg:.3f}",
                    })

                # 3rd buy: up stroke starts above center zg (breakout confirmed)
                if stroke["start_price"] > zg and stroke["end_price"] > zg:
                    # Only if previous stroke was down and ended near/above zd
                    if si > 0 and strokes[si - 1]["direction"] == "down":
                        prev_end = strokes[si - 1]["end_price"]
                        if prev_end <= zg:
                            signals.append({
                                "date": stroke["start_date"],
                                "type": "buy3",
                                "price": stroke["start_price"],
                                "stroke_index": si,
                                "center_index": ci,
                                "reason": f"向上笔起点 {stroke['start_price']:.3f} 突破中枢上沿 {zg:.3f}",
                            })

    # 2nd buy/sell: pullback after 1st class signals
    first_buys = [s for s in signals if s["type"] == "buy1"]
    for fb in first_buys:
        si = fb["stroke_index"]
        # Look for next up stroke followed by a down stroke that stays above center zd
        if si + 2 < len(strokes):
            next_up = strokes[si + 1]
            next_down = strokes[si + 2] if si + 2 < len(strokes) else None
            if (next_up["direction"] == "up"
                    and next_down is not None
                    and next_down["direction"] == "down"
                    and next_down["end_price"] >= fb["price"]):
                ci = fb["center_index"]
                if ci < len(centers) and next_down["end_price"] >= centers[ci]["zd"]:
                    signals.append({
                        "date": next_down["end_date"],
                        "type": "buy2",
                        "price": next_down["end_price"],
                        "stroke_index": si + 2,
                        "center_index": ci,
                        "reason": f"一买后回调不破前低, 终点 {next_down['end_price']:.3f}",
                    })

    first_sells = [s for s in signals if s["type"] == "sell1"]
    for fs in first_sells:
        si = fs["stroke_index"]
        if si + 2 < len(strokes):
            next_down = strokes[si + 1]
            next_up = strokes[si + 2] if si + 2 < len(strokes) else None
            if (next_down["direction"] == "down"
                    and next_up is not None
                    and next_up["direction"] == "up"
                    and next_up["end_price"] <= fs["price"]):
                ci = fs["center_index"]
                if ci < len(centers) and next_up["end_price"] <= centers[ci]["zg"]:
                    signals.append({
                        "date": next_up["end_date"],
                        "type": "sell2",
                        "price": next_up["end_price"],
                        "stroke_index": si + 2,
                        "center_index": ci,
                        "reason": f"一卖后反弹不破前高, 终点 {next_up['end_price']:.3f}",
                    })

    signals.sort(key=lambda s: s["date"])
    return signals
