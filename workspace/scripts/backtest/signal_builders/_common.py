"""Shared helpers for generic signal builders.

Provides rolling Z-score normalization, linear regression primitives,
and equal-weight composite construction — all computed from raw OHLCV
Series without depending on pre-computed Alpha158 columns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


# ---------------------------------------------------------------------------
# Rolling Z-score (standard Alpha158 convention)
# ---------------------------------------------------------------------------

def rolling_zscore(
    s: pd.Series,
    window: int = 250,
    min_periods: int = 63,
) -> pd.Series:
    """250-day rolling Z-score with *min_periods* warm-up guard."""
    mu = s.rolling(window, min_periods=min_periods).mean()
    sg = s.rolling(window, min_periods=min_periods).std() + EPS
    return (s - mu) / sg


def equal_weight_z(
    series_map: dict[str, pd.Series],
    cols: list[str],
) -> pd.Series:
    """Z-score each column in *cols* (looked up in *series_map*), return
    the row-wise equal-weight mean."""
    z_parts: list[pd.Series] = []
    for col in cols:
        z_parts.append(rolling_zscore(series_map[col]))
    return pd.concat(z_parts, axis=1).mean(axis=1)


# ---------------------------------------------------------------------------
# Linear regression primitives (mirror scripts/alpha158/core.py)
# ---------------------------------------------------------------------------

def _slope(y: np.ndarray) -> float:
    """Slope of OLS regression of *y* on equally-spaced x = arange(len(y))."""
    d = len(y)
    x = np.arange(d, dtype=float)
    dx = x - x.mean()
    dy = y - y.mean()
    return float(np.dot(dx, dy) / (np.dot(dx, dx) + EPS))


def _rsquare(y: np.ndarray) -> float:
    """R-squared of OLS regression of *y* on equally-spaced x."""
    d = len(y)
    x = np.arange(d, dtype=float)
    dx = x - x.mean()
    dy = y - y.mean()
    ss_xy = np.dot(dx, dy)
    ss_xx = np.dot(dx, dx)
    ss_yy = np.dot(dy, dy)
    if ss_xx < EPS or ss_yy < EPS:
        return 0.0
    return float((ss_xy ** 2) / (ss_xx * ss_yy + EPS))


def _resi(y: np.ndarray) -> float:
    """Residual of the *last* point from OLS regression of *y* on x."""
    d = len(y)
    x = np.arange(d, dtype=float)
    dx = x - x.mean()
    dy = y - y.mean()
    slope = np.dot(dx, dy) / (np.dot(dx, dx) + EPS)
    intercept = y.mean() - slope * x.mean()
    predicted = slope * float(d - 1) + intercept
    return float(y[-1] - predicted)


# ---------------------------------------------------------------------------
# Alpha158 factor computation helpers (from raw OHLCV Series)
# ---------------------------------------------------------------------------

def compute_roc(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """ROC{w} = close.shift(w) / (close + EPS)."""
    c_safe = close + EPS
    return {f"ROC{w}": close.shift(w) / c_safe for w in windows}


def compute_ma(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """MA{w} = close.rolling(w).mean() / (close + EPS)."""
    c_safe = close + EPS
    return {f"MA{w}": close.rolling(w).mean() / c_safe for w in windows}


def compute_std(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """STD{w} = close.rolling(w).std(ddof=0) / (close + EPS)."""
    c_safe = close + EPS
    return {f"STD{w}": close.rolling(w).std(ddof=0) / c_safe for w in windows}


def compute_beta(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """BETA{w} = Slope($close, w) / (close + EPS)."""
    c_safe = close + EPS
    return {f"BETA{w}": close.rolling(w).apply(_slope, raw=True) / c_safe for w in windows}


def compute_rsqr(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """RSQR{w} = Rsquare($close, w)."""
    return {f"RSQR{w}": close.rolling(w).apply(_rsquare, raw=True) for w in windows}


def compute_resi(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """RESI{w} = Resi($close, w) / (close + EPS)."""
    c_safe = close + EPS
    return {f"RESI{w}": close.rolling(w).apply(_resi, raw=True) / c_safe for w in windows}


def compute_max(high: pd.Series, close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """MAX{w} = high.rolling(w).max() / (close + EPS)."""
    c_safe = close + EPS
    return {f"MAX{w}": high.rolling(w).max() / c_safe for w in windows}


def compute_min(low: pd.Series, close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """MIN{w} = low.rolling(w).min() / (close + EPS)."""
    c_safe = close + EPS
    return {f"MIN{w}": low.rolling(w).min() / c_safe for w in windows}


def compute_qtlu(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """QTLU{w} = close.rolling(w).quantile(0.8) / (close + EPS)."""
    c_safe = close + EPS
    return {f"QTLU{w}": close.rolling(w).quantile(0.8) / c_safe for w in windows}


def compute_qtld(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """QTLD{w} = close.rolling(w).quantile(0.2) / (close + EPS)."""
    c_safe = close + EPS
    return {f"QTLD{w}": close.rolling(w).quantile(0.2) / c_safe for w in windows}


def compute_rank(close: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """RANK{w} = percentile rank of latest close within rolling window."""
    def _rank_fn(x: np.ndarray) -> float:
        return float((x < x[-1]).sum()) / max(len(x) - 1, 1)
    return {f"RANK{w}": close.rolling(w).apply(_rank_fn, raw=True) for w in windows}


def compute_rsv(
    close: pd.Series, high: pd.Series, low: pd.Series, windows: list[int],
) -> dict[str, pd.Series]:
    """RSV{w} = (close - Min(low,w)) / (Max(high,w) - Min(low,w) + EPS)."""
    result = {}
    for w in windows:
        lo_min = low.rolling(w).min()
        hi_max = high.rolling(w).max()
        result[f"RSV{w}"] = (close - lo_min) / (hi_max - lo_min + EPS)
    return result


def compute_imax(high: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """IMAX{w} = IdxMax(high, w) / w — fraction of window since max."""
    def _imax_fn(x: np.ndarray) -> float:
        return (len(x) - 1 - int(np.argmax(x))) / len(x)
    return {f"IMAX{w}": high.rolling(w).apply(_imax_fn, raw=True) for w in windows}


def compute_imin(low: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """IMIN{w} = IdxMin(low, w) / w — fraction of window since min."""
    def _imin_fn(x: np.ndarray) -> float:
        return (len(x) - 1 - int(np.argmin(x))) / len(x)
    return {f"IMIN{w}": low.rolling(w).apply(_imin_fn, raw=True) for w in windows}


def compute_corr(close: pd.Series, vol: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """CORR{w} = Corr(close, Log(vol+1), w)."""
    lv = pd.Series(np.log(vol.to_numpy() + 1.0), index=close.index)
    return {f"CORR{w}": close.rolling(w).corr(lv) for w in windows}


def compute_cord(close: pd.Series, vol: pd.Series, windows: list[int]) -> dict[str, pd.Series]:
    """CORD{w} = Corr(close/Ref(close,1), Log(vol/Ref(vol,1)+1), w)."""
    c_ratio = close / (close.shift(1) + EPS)
    vol_ratio = vol / (vol.shift(1) + EPS)
    cord_y = pd.Series(np.log(vol_ratio.to_numpy() + 1.0), index=close.index)
    return {f"CORD{w}": c_ratio.rolling(w).corr(cord_y) for w in windows}


def compute_cntp_cntn_cntd(
    close: pd.Series, windows: list[int],
) -> tuple[dict[str, pd.Series], dict[str, pd.Series], dict[str, pd.Series]]:
    """CNTP{w}, CNTN{w}, CNTD{w} = up-day / down-day / difference proportions."""
    dc = close.diff().to_numpy()
    up_mask = (dc > 0.0).astype(float)
    dn_mask = (dc < 0.0).astype(float)
    up_mask[0] = 0.0
    dn_mask[0] = 0.0
    up_s = pd.Series(up_mask, index=close.index)
    dn_s = pd.Series(dn_mask, index=close.index)

    cntp = {f"CNTP{w}": up_s.rolling(w).mean() for w in windows}
    cntn = {f"CNTN{w}": dn_s.rolling(w).mean() for w in windows}
    cntd = {f"CNTD{w}": cntp[f"CNTP{w}"] - cntn[f"CNTN{w}"] for w in windows}
    return cntp, cntn, cntd


def compute_sump_sumn_sumd(
    close: pd.Series, windows: list[int],
) -> tuple[dict[str, pd.Series], dict[str, pd.Series], dict[str, pd.Series]]:
    """SUMP{w}, SUMN{w}, SUMD{w} — gain/loss ratio and difference."""
    dc = close.diff()
    adc = dc.abs()
    up_mask = (dc > 0).astype(float)
    dn_mask = (dc < 0).astype(float)
    up_mask.iloc[0] = 0.0
    dn_mask.iloc[0] = 0.0

    sump, sumn, sumd = {}, {}, {}
    for w in windows:
        sg = up_mask.rolling(w).sum()
        sl = dn_mask.rolling(w).sum()
        sa = adc.rolling(w).sum()
        sump[f"SUMP{w}"] = sg / (sa + EPS)
        sumn[f"SUMN{w}"] = sl / (sa + EPS)
        sumd[f"SUMD{w}"] = sump[f"SUMP{w}"] - sumn[f"SUMN{w}"]
    return sump, sumn, sumd


def compute_vma_vstd(
    vol: pd.Series, windows: list[int],
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """VMA{w} = vol.rolling(w).mean() / (vol + EPS).
    VSTD{w} = vol.rolling(w).std(ddof=0) / (vol + EPS)."""
    v_safe = vol + EPS
    vma = {f"VMA{w}": vol.rolling(w).mean() / v_safe for w in windows}
    vstd = {f"VSTD{w}": vol.rolling(w).std(ddof=0) / v_safe for w in windows}
    return vma, vstd


def compute_wvma(
    close: pd.Series, vol: pd.Series, windows: list[int],
) -> dict[str, pd.Series]:
    """WVMA{w} = Std(AVR, w) / (Mean(AVR, w) + EPS).
    AVR = Abs(close/Ref(close,1) - 1) * vol."""
    c = close.to_numpy()
    v = vol.to_numpy()
    avr = np.abs(c / (np.roll(c, 1) + EPS) - 1.0) * v
    avr[0] = 0.0
    avr_s = pd.Series(avr, index=close.index)
    result = {}
    for w in windows:
        result[f"WVMA{w}"] = (
            avr_s.rolling(w).std(ddof=0) / (avr_s.rolling(w).mean() + EPS)
        )
    return result


def compute_vsump_vsumn_vsumd(
    vol: pd.Series, windows: list[int],
) -> tuple[dict[str, pd.Series], dict[str, pd.Series], dict[str, pd.Series]]:
    """VSUMP{w}, VSUMN{w}, VSUMD{w} — volume gain/loss ratio."""
    dv = vol.diff()
    adv = dv.abs()
    up_mask = (dv > 0).astype(float)
    dn_mask = (dv < 0).astype(float)
    up_mask.iloc[0] = 0.0
    dn_mask.iloc[0] = 0.0

    vsump, vsumn, vsumd = {}, {}, {}
    for w in windows:
        su = up_mask.rolling(w).sum()
        sd = dn_mask.rolling(w).sum()
        sa = adv.rolling(w).sum()
        vsump[f"VSUMP{w}"] = su / (sa + EPS)
        vsumn[f"VSUMN{w}"] = sd / (sa + EPS)
        vsumd[f"VSUMD{w}"] = vsump[f"VSUMP{w}"] - vsumn[f"VSUMN{w}"]
    return vsump, vsumn, vsumd


# ---------------------------------------------------------------------------
# K-bar helpers (need open price)
# ---------------------------------------------------------------------------

def compute_kbar_features(
    open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series,
) -> dict[str, pd.Series]:
    """Compute all 9 kbar features from OHLC (mirrors _add_kbar in core.py)."""
    o_safe = open_ + EPS
    hl_range = high - low + EPS
    greater_oc = pd.concat([open_, close], axis=1).max(axis=1)
    less_oc = pd.concat([open_, close], axis=1).min(axis=1)

    return {
        "KMID": (close - open_) / o_safe,
        "KLEN": (high - low) / o_safe,
        "KMID2": (close - open_) / hl_range,
        "KUP": (high - greater_oc) / o_safe,
        "KUP2": (high - greater_oc) / hl_range,
        "KLOW": (less_oc - low) / o_safe,
        "KLOW2": (less_oc - low) / hl_range,
        "KSFT": (2.0 * close - high - low) / o_safe,
        "KSFT2": (2.0 * close - high - low) / hl_range,
    }
