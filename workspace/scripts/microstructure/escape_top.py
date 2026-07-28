"""
Unified escape-top warning module.

Combines the turnover-concentration indicator and the margin-buy / SSE
divergence indicator into a single joint warning signal with three levels:

* **RED**   — both concentration and divergence fire
* **YELLOW** — exactly one fires
* **GREEN**  — neither fires

Default ``joint_mode`` is ``"AND"`` which requires both conditions for RED.
"""

from __future__ import annotations

from typing import Any, Literal

from .concentration import compute_concentration
from .margin_buy_vs_sse import compute_margin_buy_vs_sse
from .metadata import (
    CONCENTRATION_TOP_PCT,
    DEFAULT_DUCKDB_PATH,
    ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD,
    ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS,
)

JointMode = Literal["AND"]
WarningLevel = Literal["RED", "YELLOW", "GREEN"]


# ── Public API ───────────────────────────────────────────────────────────────


def compute_escape_warning(
    duckdb_path: str = DEFAULT_DUCKDB_PATH,
    *,
    concentration_threshold: float = ESCAPE_TOP_DEFAULT_CONCENTRATION_THRESHOLD,
    divergence_lookback_days: int = ESCAPE_TOP_DEFAULT_DIVERGENCE_LOOKBACK_DAYS,
    joint_mode: JointMode = "AND",
    concentration_top_pct: float = CONCENTRATION_TOP_PCT,
    temporal_window_days: int = 0,
    preset: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Compute joint escape-top warning from concentration + divergence.

    Parameters
    ----------
    duckdb_path : str
        Path to the DuckDB database.
    concentration_threshold : float
        Top-N% turnover share that triggers a concentration hit (default 0.45).
    divergence_lookback_days : int
        Trading-day lookback for margin-buy / SSE divergence (default 20).
    joint_mode : str
        Joint logic mode.  Currently only ``"AND"`` is supported, where RED
        requires both indicators to fire, YELLOW means exactly one fires,
        and GREEN means neither fires.
    concentration_top_pct : float
        Top percentage of stocks for concentration (default 5.0).
    temporal_window_days : int
        When > 0, a divergence hit within the last *temporal_window_days*
        trading days is treated as a current hit (relaxed temporal AND).
        Used by the ``extended`` preset.  Default 0 (strict same-day AND).
    preset : str or None
        Optional preset name used by CLI callers.  Included in output metadata
        only; parameter resolution happens before this function is called.
    start_date : str or None
        Optional lower-bound date filter (YYYY-MM-DD).
    end_date : str or None
        Optional upper-bound date filter (YYYY-MM-DD).

    Returns
    -------
    dict
        Keys: ``report_date``, ``concentration``, ``divergence``,
        ``joint_warning``.
    """
    _validate_joint_mode(joint_mode)

    conc = compute_concentration(
        duckdb_path,
        top_pct=concentration_top_pct,
        start_date=start_date,
        end_date=end_date,
    )
    div = compute_margin_buy_vs_sse(
        duckdb_path,
        start_date=start_date,
        end_date=end_date,
        divergence_lookback_days=divergence_lookback_days,
    )

    concentration_hit = bool(
        conc["latest_top5_share"] >= concentration_threshold
    )
    divergence_hit = bool(div["latest_is_divergence"])

    # Temporal relaxation: check if divergence fired in the last N trading days.
    temporal_divergence_present = False
    if temporal_window_days > 0:
        if divergence_hit:
            temporal_divergence_present = True
        else:
            import duckdb
            con = duckdb.connect(duckdb_path, read_only=True)
            lb = divergence_lookback_days
            latest_dt = conc["latest_trade_date"]
            result = con.execute(f"""
                WITH daily AS (
                    SELECT m.trade_date,
                           SUM(m.rzye) AS rzye,
                           SUM(m.rzmre) AS rzmre,
                           SUM(f.amount) AS t_amount
                    FROM stk_margin m
                    JOIN stk_factor_pro f ON m.trade_date = f.trade_date
                    WHERE f.amount > 0
                    GROUP BY m.trade_date
                ),
                changes AS (
                    SELECT trade_date,
                           rzye / LAG(rzye, {lb}) OVER (ORDER BY trade_date) - 1 AS rzye_chg,
                           (rzmre / NULLIF(t_amount, 0)) /
                           NULLIF(LAG(rzmre / NULLIF(t_amount, 0), {lb}) OVER (ORDER BY trade_date), 0) - 1 AS ratio_chg
                    FROM daily
                ),
                recent AS (
                    SELECT *
                    FROM changes
                    WHERE trade_date <= '{latest_dt}'
                    ORDER BY trade_date DESC
                    LIMIT {temporal_window_days}
                )
                SELECT MAX(CASE WHEN rzye_chg > 0 AND ratio_chg < 0 THEN 1 ELSE 0 END)
                FROM recent
            """).fetchone()
            con.close()
            temporal_divergence_present = bool(result and result[0] == 1)
        if temporal_divergence_present:
            divergence_hit = True

    warning_level = _resolve_warning(concentration_hit, divergence_hit, joint_mode)

    params: dict[str, Any] = {
        "preset": preset,
        "concentration_threshold": concentration_threshold,
        "concentration_top_pct": concentration_top_pct,
        "divergence_lookback_days": divergence_lookback_days,
    }
    if temporal_window_days > 0:
        params["temporal_window_days"] = temporal_window_days

    return {
        "report_date": conc["latest_trade_date"],
        "concentration": {
            "hit": concentration_hit,
            "threshold": concentration_threshold,
            "latest_top5_share_pct": conc["latest_top5_share_pct"],
            "summary": conc,
        },
        "divergence": {
            "hit": divergence_hit,
            "lookback_days": divergence_lookback_days,
            "temporal_window_days": temporal_window_days,
            "temporal_divergence_present": temporal_divergence_present,
            "summary": div,
        },
        "joint_warning": {
            "concentration_hit": concentration_hit,
            "divergence_hit": divergence_hit,
            "warning_level": warning_level,
            "joint_mode": joint_mode,
            "parameters": params,
        },
    }


# ── Private helpers ──────────────────────────────────────────────────────────


def _resolve_warning(
    concentration_hit: bool,
    divergence_hit: bool,
    joint_mode: JointMode,
) -> WarningLevel:
    if concentration_hit and divergence_hit:
        return "RED"
    if concentration_hit or divergence_hit:
        return "YELLOW"
    return "GREEN"


def _validate_joint_mode(joint_mode: str) -> None:
    if joint_mode not in {"AND"}:
        raise ValueError(
            f"Unsupported joint_mode: {joint_mode!r}. Only 'AND' is supported."
        )
