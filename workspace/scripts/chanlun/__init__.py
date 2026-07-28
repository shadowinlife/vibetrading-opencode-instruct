"""Chanlun (缠论) analysis module.

Public API:
    analyze(ts_code, db_path) -> dict

Pipeline: load_data → remove_inclusion → detect_fractals → build_strokes
          → find_centers → detect_buy_sell_points → backtest → report
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import duckdb
import pandas as pd

from .core import remove_inclusion, detect_fractals, build_strokes, find_centers
from .signals import detect_buy_sell_points
from .backtest import backtest_chanlun_signals
from .report import render_chanlun_report


def _detect_table(ts_code: str, db_path: str) -> str:
    code_prefix = ts_code.split(".")[0]
    # ETFs: 1xxxxx (SZ) or 5xxxxx (SH)
    if code_prefix.startswith("1") or code_prefix.startswith("5"):
        return "fund_daily"
    return "stk_factor_pro"


def _load_data(ts_code: str, db_path: str) -> pd.DataFrame:
    table = _detect_table(ts_code, db_path)
    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            f"""
            SELECT trade_date, open, high, low, close, vol, amount
            FROM {table}
            WHERE ts_code = ?
            ORDER BY trade_date
            """,
            [ts_code],
        ).fetchdf()
    finally:
        con.close()

    if df.empty:
        raise ValueError(f"No data for {ts_code} in table {table}")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values("trade_date").reset_index(drop=True)


def analyze(
    ts_code: str,
    db_path: str = "./duckdb/ashare.duckdb",
    output_dir: str | None = None,
) -> dict:
    """Run full chanlun analysis pipeline.

    Args:
        ts_code: Stock/ETF code (e.g. '588000.SH', '000001.SZ').
        db_path: Path to DuckDB database.
        output_dir: Optional directory to save artifacts. If None, no files saved.

    Returns dict with keys:
        ts_code, coverage, fractals, strokes, centers, signals,
        backtest, report (markdown string)
    """
    raw = _load_data(ts_code, db_path)

    merged = remove_inclusion(raw)
    fractals = detect_fractals(merged)
    strokes = build_strokes(merged, fractals)
    centers = find_centers(strokes)

    signals = detect_buy_sell_points(strokes, centers)

    backtest_result = backtest_chanlun_signals(raw, signals)

    date_start = str(raw["trade_date"].min().date())
    date_end = str(raw["trade_date"].max().date())
    report = render_chanlun_report(
        ts_code=ts_code,
        raw_bars=len(raw),
        merged_bars=len(merged),
        date_start=date_start,
        date_end=date_end,
        fractals=fractals,
        strokes=strokes,
        centers=centers,
        signals=signals,
        backtest_result=backtest_result,
    )

    result = {
        "ts_code": ts_code,
        "coverage": {
            "start": date_start,
            "end": date_end,
            "bars": len(raw),
            "merged_bars": len(merged),
        },
        "fractal_count": len(fractals),
        "stroke_count": len(strokes),
        "center_count": len(centers),
        "fractals": fractals,
        "strokes": strokes,
        "centers": centers,
        "signals": signals,
        "backtest": backtest_result,
        "report": report,
    }

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        pd.DataFrame(fractals).to_csv(out / "fractals.csv", index=False)
        pd.DataFrame(strokes).to_csv(out / "strokes.csv", index=False)
        pd.DataFrame(centers).to_csv(out / "centers.csv", index=False)
        if signals:
            pd.DataFrame(signals).to_csv(out / "signals.csv", index=False)

        (out / "report.md").write_text(report, encoding="utf-8")

        summary = {
            "coverage": result["coverage"],
            "fractal_count": len(fractals),
            "stroke_count": len(strokes),
            "center_count": len(centers),
            "signal_count": len(signals),
            "backtest_summary": backtest_result["summary"],
            "recent_strokes": strokes[-8:] if strokes else [],
            "recent_centers": centers[-3:] if centers else [],
            "recent_signals": signals[-10:] if signals else [],
        }
        (out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result
