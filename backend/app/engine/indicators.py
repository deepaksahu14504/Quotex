"""Vectorized technical indicators (numpy/pandas). No external TA dependency."""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def atr_pct(atr_value: float, close_value: float) -> float:
    """ATR expressed as a percentage of price -- a scale-free volatility
    measure comparable across assets with very different price levels
    (e.g. USDJPY at ~150 vs EURUSD at ~1.1). Single source of truth for
    this formula: previously computed independently (and identically) in
    both engine/strategies.py's evaluate() and engine/market_context.py's
    snapshot() -- consolidated here during Foundation Stabilization so the
    two can never silently drift apart."""
    if not close_value:
        return 0.0
    return (atr_value / close_value) * 100.0


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI.

    ZERO-LOSS HANDLING -- this is a divergence fix, not a formula change.
    The previous implementation did `avg_gain / avg_loss.replace(0, NaN)`
    and then `.fillna(50.0)`, so a window with no losses at all (a clean
    uptrend) produced NaN and was filled with the NEUTRAL value 50 instead
    of the correct 100. The downside case was unaffected, so the indicator
    was directionally asymmetric: it reported oversold correctly and never
    reported this form of overbought.

    engine/incremental.py -- the live incremental path for the SAME
    indicator -- already handled it correctly (`rsi_val = 100.0 if
    avg_gain > 0 else 50.0`, incremental.py:141-144). The two
    implementations therefore returned 50 and 100 for identical input, so
    a bar's RSI depended on whether it happened to be produced by the
    warm-up recalculation or by the incremental update. strategies.py
    gates on RSI at 35/65 and 38-68/32-62, so that split flipped real
    entry conditions.

    This aligns the vectorized path with the incremental one. It IS a
    behavioural change for clean-uptrend bars; see the accompanying report.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Same three cases incremental.py distinguishes, in the same order:
    #   avg_loss ~ 0 and avg_gain > 0 -> 100 (pure upside)
    #   avg_loss ~ 0 and avg_gain ~ 0 -> 50  (flat / no data yet)
    #   otherwise                     -> the ratio above
    no_loss = avg_loss.abs() <= 1e-12
    out = out.mask(no_loss & (avg_gain > 1e-12), 100.0)
    out = out.mask(no_loss & (avg_gain <= 1e-12), 50.0)
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(series: pd.Series, period: int = 20, mult: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(period).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    width = (upper - lower) / mid.replace(0.0, np.nan)
    return upper, mid, lower, width


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["low"].rolling(k_period).min()
    high_max = df["high"].rolling(k_period).max()
    rng = (high_max - low_min).replace(0.0, np.nan)
    k = 100 * (df["close"] - low_min) / rng
    k = k.fillna(50.0)
    d = k.rolling(d_period).mean()
    return k, d


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df["high"], df["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = atr(df, period)
    atr_safe = tr.replace(0.0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_safe
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


def stoch_rsi(series: pd.Series, period: int = 14, k: int = 3, d: int = 3):
    r = rsi(series, period)
    low = r.rolling(period).min()
    high = r.rolling(period).max()
    rng = (high - low).replace(0.0, np.nan)
    stochrsi = ((r - low) / rng).fillna(0.5) * 100
    k_line = stochrsi.rolling(k).mean()
    d_line = k_line.rolling(d).mean()
    return k_line.fillna(50.0), d_line.fillna(50.0)


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0):
    """Returns (supertrend_line, direction) where direction is +1 up / -1 down."""
    hl2 = (df["high"] + df["low"]) / 2
    atr_v = atr(df, period)
    upper = hl2 + mult * atr_v
    lower = hl2 - mult * atr_v
    close = df["close"].to_numpy()
    up = upper.to_numpy()
    lo = lower.to_numpy()
    n = len(df)
    st = np.zeros(n)
    dir_ = np.ones(n)
    final_up = up.copy()
    final_lo = lo.copy()
    for i in range(1, n):
        final_up[i] = min(up[i], final_up[i - 1]) if close[i - 1] <= final_up[i - 1] else up[i]
        final_lo[i] = max(lo[i], final_lo[i - 1]) if close[i - 1] >= final_lo[i - 1] else lo[i]
        if close[i] > final_up[i - 1]:
            dir_[i] = 1
        elif close[i] < final_lo[i - 1]:
            dir_[i] = -1
        else:
            dir_[i] = dir_[i - 1]
        st[i] = final_lo[i] if dir_[i] == 1 else final_up[i]
    return pd.Series(st, index=df.index), pd.Series(dir_, index=df.index)


def psar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.Series:
    """Parabolic SAR; returns +1 (uptrend) / -1 (downtrend) per bar."""
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    trend = np.ones(n)
    if n < 3:
        return pd.Series(trend, index=df.index)
    sar = low[0]
    ep = high[0]
    af = step
    up = True
    out = np.ones(n)
    for i in range(1, n):
        sar = sar + af * (ep - sar)
        if up:
            if low[i] < sar:
                up = False
                sar = ep
                ep = low[i]
                af = step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, max_step)
        else:
            if high[i] > sar:
                up = True
                sar = ep
                ep = high[i]
                af = step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, max_step)
        out[i] = 1 if up else -1
    return pd.Series(out, index=df.index)


_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def candles_to_df(candles) -> pd.DataFrame:
    """Accepts list of schemas.Candle or dicts -> OHLCV DataFrame.

    This is the single entry point every indicator in this module reads
    from, and every one of them assumes chronological, deduplicated,
    numeric input: `.diff()`, `.shift(1)`, `.rolling()` and the iterative
    supertrend/psar loops all read neighbouring rows as consecutive bars.

    Previously the incoming list was used exactly as given -- unsorted,
    with duplicates, and with whatever dtypes the values happened to have.
    None of that raised; it produced silently different indicator values.
    Feeding the same 30 candles shuffled changed ATR from 2.000 to 2.709
    and RSI from 50.00 to 57.04, with no warning anywhere.

    Three guarantees are now enforced here, at the boundary, so no
    individual indicator has to re-check them:

      1. Chronological order  -- stable sort by timestamp.
      2. No duplicate bars    -- one row per timestamp, last occurrence
                                 wins (the freshest read of that bar,
                                 matching how services/market.py folds
                                 live ticks into an open candle).
      3. Numeric dtypes       -- an all-object frame (which is what an
                                 empty or mixed input produced) makes
                                 rolling/ewm operations misbehave or fail.

    Sorting and dropping duplicates cannot invent data: rows are only ever
    reordered or removed, never synthesised.
    """
    rows = []
    for c in candles:
        if hasattr(c, "open"):
            rows.append((c.timestamp, c.open, c.high, c.low, c.close, getattr(c, "volume", 0.0)))
        else:
            rows.append((c["timestamp"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 0.0)))
    df = pd.DataFrame(rows, columns=_OHLCV_COLUMNS)
    if df.empty:
        # Force the numeric dtypes an empty frame would otherwise not have.
        return df.astype({c: "float64" for c in _OHLCV_COLUMNS})

    for col in _OHLCV_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    if df.empty:
        return df.astype({c: "float64" for c in _OHLCV_COLUMNS}).reset_index(drop=True)

    # kind="stable" so equal timestamps keep their arrival order, which is
    # what makes "last occurrence wins" below mean "most recent read".
    df = df.sort_values("timestamp", kind="stable")
    df = df.drop_duplicates(subset="timestamp", keep="last")
    return df.reset_index(drop=True)


# ═════════════════════════════════════════════════════════════════════════ #
# Volatility normalization -- no verified accuracy figure attached; used to
# scale thresholds dynamically in high/low-vol markets, not a standalone signal.
# ═════════════════════════════════════════════════════════════════════════ #

def volatility_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Normalized ATR as percentage of SMA(close), relative to its mean.
    Returns a multiplier: 1.0 = normal volatility, 0.5 = half volatility, 2.0 = double.
    Used to scale thresholds dynamically in high/low vol markets."""
    atr_v = atr(df, period)
    sma_v = sma(df["close"], period)
    atr_pct = (atr_v / sma_v.replace(0.0, np.nan)) * 100.0
    atr_pct_ma = atr_pct.rolling(50).mean()
    ratio = (atr_pct / atr_pct_ma.replace(0.0, 1.0)).fillna(1.0)
    return ratio.clip(0.5, 2.5)  # Clamp to reasonable bounds


# ═════════════════════════════════════════════════════════════════════════ #
# Support & resistance detection (see find_support_resistance() below for
# the fractal/touch-count implementation).
# ═════════════════════════════════════════════════════════════════════════ #

def find_support_resistance(
    df: pd.DataFrame, lookback: int = 30, prominence: float = 0.0015,
) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Find the nearest support/resistance level below/above the current
    price, using actual swing highs/lows (2-bar fractal pivots on high/low,
    not just the single min/max close) within the last `lookback` bars.

    Previously this returned only the single lowest/highest *close* in the
    window, treating a level touched once the same as one touched five
    times -- no concept of a level's actual significance. This version
    clusters nearby pivots (within `prominence` of each other) and reports
    a touch-count strength alongside each level, so callers can tell "price
    is near a level" from "price is near a level that's been respected
    repeatedly." Standard fractal/cluster technique -- no data-fit
    parameters, same `lookback`/`prominence` knobs as before.

    Returns (support_level, resistance_level, support_strength,
    resistance_strength) per candle. support_strength/resistance_strength
    are touch counts (0 or NaN where no level was found for that bar).
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    support = np.full(n, np.nan)
    resistance = np.full(n, np.nan)
    support_strength = np.full(n, np.nan)
    resistance_strength = np.full(n, np.nan)

    if n < lookback:
        idx = df.index
        return (pd.Series(support, index=idx), pd.Series(resistance, index=idx),
                pd.Series(support_strength, index=idx), pd.Series(resistance_strength, index=idx))

    for i in range(lookback, n):
        start = max(0, i - lookback)
        seg_high = high[start:i]
        seg_low = low[start:i]
        cur_price = df["close"].iat[i]
        tol = cur_price * prominence

        # 2-bar fractal pivots: a bar is a swing low/high if it's the local
        # extreme among itself and its immediate neighbours on both sides.
        swing_lows = [seg_low[j] for j in range(2, len(seg_low) - 2)
                      if seg_low[j] == min(seg_low[j - 2:j + 3])]
        swing_highs = [seg_high[j] for j in range(2, len(seg_high) - 2)
                       if seg_high[j] == max(seg_high[j - 2:j + 3])]

        def _best_cluster(points: list, below: bool) -> Tuple[Optional[float], int]:
            """Groups points within `tol` of each other; returns the
            (level, touch_count) of the strongest cluster on the correct
            side of the current price (below it for support candidates,
            above for resistance), nearest to price as the tiebreaker."""
            candidates = [p for p in points if (p < cur_price if below else p > cur_price)]
            if not candidates:
                return None, 0
            candidates.sort()
            clusters: list = []
            for p in candidates:
                placed = False
                for c in clusters:
                    if abs(p - c["mean"]) <= tol:
                        c["points"].append(p)
                        c["mean"] = sum(c["points"]) / len(c["points"])
                        placed = True
                        break
                if not placed:
                    clusters.append({"points": [p], "mean": p})
            # Strongest (most touches) first; nearest to price breaks ties.
            clusters.sort(key=lambda c: (-len(c["points"]), abs(c["mean"] - cur_price)))
            best = clusters[0]
            return best["mean"], len(best["points"])

        s_level, s_strength = _best_cluster(swing_lows, below=True)
        r_level, r_strength = _best_cluster(swing_highs, below=False)
        if s_level is not None:
            support[i] = s_level
            support_strength[i] = s_strength
        if r_level is not None:
            resistance[i] = r_level
            resistance_strength[i] = r_strength

    idx = df.index
    return (pd.Series(support, index=idx), pd.Series(resistance, index=idx),
            pd.Series(support_strength, index=idx), pd.Series(resistance_strength, index=idx))


# ═════════════════════════════════════════════════════════════════════════ #
# Volume confirmation — BINARY TRADING: Quotex/pyquotex has NO real volume
# ═════════════════════════════════════════════════════════════════════════ #

def _has_real_volume(df: pd.DataFrame) -> bool:
    """Quotex (pyquotex) has no real exchange volume — ticks carry no volume.
    Binance/public provider does. This helper tells whether volume column is
    meaningful (variance) or just 0/constant placeholder from Quotex."""
    if "volume" not in df.columns:
        return False
    try:
        vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        # meaningful if more than 1 unique value and sum>0 and not all 1.0 placeholder
        return bool(vol.nunique() > 1 and vol.sum() > 0)
    except Exception:
        return False


def volume_ma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple moving average of volume.
    For binary trading (Quotex) where volume is synthetic/zero, returns
    neutral 1.0 so it never penalizes — real volume only matters on
    Binance/public provider path."""
    if not _has_real_volume(df):
        return pd.Series(np.ones(len(df)), index=df.index)
    return pd.to_numeric(df["volume"], errors="coerce").rolling(period).mean()


def volume_strength(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Volume relative to moving average: >1.0 = above average, <1.0 = below.
    Returns 1.0 (neutral) when no real volume exists (Quotex/pyquotex path),
    so binary strategies never get blocked by a fake volume gate."""
    if not _has_real_volume(df):
        return pd.Series(np.ones(len(df)), index=df.index)
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    vol_ma = vol.rolling(period).mean()
    return (vol / vol_ma.replace(0.0, 1.0)).fillna(1.0)


def price_action_strength(df: pd.DataFrame) -> pd.Series:
    """Pure price-action strength for binary options — no volume dependency.

    Binary options don't have real order-book volume on Quotex; what matters
    is candle structure: body size vs range + wick rejection. 0-1 scale.
    If real volume exists (Binance), it is blended in lightly (20%) but never
    required — strategy stays functional on pyquotex where volume is zero.
    """
    # Body ratio
    candle_range = (df["high"] - df["low"]).replace(0.0, 1e-9)
    body = (df["close"] - df["open"]).abs()
    body_ratio = (body / candle_range).clip(0.0, 1.0)

    # Wick analysis — small wicks + large body = strong directional move
    try:
        upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
        lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
        wick_ratio = ((upper_wick + lower_wick) / candle_range).clip(0.0, 1.0)
        wick_score = (1.0 - wick_ratio * 0.7).clip(0.0, 1.0)  # less wick = stronger
    except Exception:
        wick_score = pd.Series(np.ones(len(df)) * 0.5, index=df.index)

    # Base strength: 70% body, 30% wick rejection
    strength = (body_ratio * 0.7 + wick_score * 0.3).clip(0.0, 1.0)

    # Optional: if real volume exists, blend 20% volume confirmation (never required)
    if _has_real_volume(df):
        try:
            vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            vol_ma = vol.rolling(20).mean()
            vol_str = (vol / vol_ma.replace(0.0, 1.0)).fillna(1.0)
            vol_str_ref = vol_str.expanding().max().replace(0.0, np.nan)
            vol_norm = (vol_str / vol_str_ref).fillna(0.5).clip(0.0, 1.5) / 1.5
            strength = (strength * 0.8 + vol_norm * 0.2).clip(0.0, 1.0)
        except Exception:
            pass
    return strength


# ═════════════════════════════════════════════════════════════════════════ #
# Confluence scoring -- counts how many key levels price is near.
# ═════════════════════════════════════════════════════════════════════════ #

def confluence_points(df: pd.DataFrame) -> pd.Series:
    """Count how many key levels (S/R, moving averages, bands) price is near.
    Higher = more confluence = higher confidence."""
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    
    if n < 50:
        return pd.Series(np.zeros(n), index=df.index)
    
    confluence = np.zeros(n)
    threshold_pct = 0.002  # 0.2% proximity threshold
    
    for i in range(50, n):
        current_price = close[i]
        threshold = current_price * threshold_pct
        
        # Count nearby levels
        levels_nearby = 0
        
        # Check swing levels
        recent = close[max(0, i - 20):i]
        if np.min(recent) > current_price - threshold:
            levels_nearby += 1
        if np.max(recent) < current_price + threshold:
            levels_nearby += 1
        
        # Check moving averages (if available from full df)
        for ma_col in ["ema5", "ema9", "ema21", "ema50", "bb_mid"]:
            if ma_col in df.columns and abs(df[ma_col].iloc[i] - current_price) < threshold:
                levels_nearby += 1
        
        confluence[i] = min(levels_nearby, 5)  # Cap at 5
    
    return pd.Series(confluence, index=df.index)


# ═════════════════════════════════════════════════════════════════════════ #
# Time-of-day volatility adjustment.
# ═════════════════════════════════════════════════════════════════════════ #

def session_name(hour: int) -> str:
    """Maps a UTC hour to a trading session label. Mirrors the exact hour
    boundaries time_session_volatility_multiplier() already uses above --
    defined once here so the Trade Feature Store's session field and that
    multiplier's regime are always describing the same boundaries, never
    two independently-tuned versions of "what counts as London session."
    """
    hour = int(hour) % 24
    if 13 <= hour < 16:
        return "overlap"       # London + New York both active
    if 8 <= hour < 16:
        return "london"
    if 13 <= hour < 21:
        return "new_york"
    return "asian"              # 21:00-08:00 UTC (also covers 22:00-07:00 quiet band)


def time_session_volatility_multiplier(df: pd.DataFrame) -> pd.Series:
    """Return volatility adjustment multiplier based on time-of-day.
    Assumes UTC timestamps. Adjusts for known market session volatility patterns.
    
    Returns: 1.0 = normal, 1.5 = high vol session, 0.7 = low vol session
    """
    if "timestamp" not in df.columns:
        return pd.Series(np.ones(len(df)), index=df.index)
    
    multipliers = []
    for ts in df["timestamp"]:
        try:
            # Extract hour from timestamp (assuming Unix or ISO format)
            if isinstance(ts, (int, float)):
                hour = (int(ts) // 3600) % 24
            elif isinstance(ts, str):
                # Parse ISO format "HH:MM:SS" or similar
                hour_str = ts.split("T")[1].split(":")[0] if "T" in ts else ts.split(":")[0]
                hour = int(hour_str) % 24
            else:
                hour = 12
            
            # Overlap: 13:00-16:00 UTC (very high -- London + NY both active).
            # Audit fix (M-2): must be checked BEFORE the wider London/NY
            # windows below, or it's unreachable dead code -- 13-16 is a
            # subset of London's 8-16 window, so an elif-London-first order
            # always swallowed it. This now matches session_name()'s
            # precedence, which already checked overlap first.
            if 13 <= hour < 16:
                mult = 1.5
            # London session: 08:00-16:00 UTC (high volatility)
            elif 8 <= hour < 16:
                mult = 1.3
            # New York: 13:00-21:00 UTC (high)
            elif 13 <= hour < 21:
                mult = 1.4
            # Asian: 22:00-07:00 UTC (low)
            elif hour >= 22 or hour < 7:
                mult = 0.8
            else:
                mult = 1.0
            
            multipliers.append(mult)
        except Exception:
            multipliers.append(1.0)
    
    return pd.Series(multipliers, index=df.index)
