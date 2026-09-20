"""Strategy modules + a confluence scorer.

Each strategy inspects the indicator frame and returns a vote:
    (direction | None, weight, reason)

The confluence engine aggregates weighted votes into a 0..100 confidence score
and a final direction. Signals fire only when the dominant side clearly wins,
the market isn't dead/choppy, and the score clears the user's threshold
(threshold enforced by the orchestrator).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from . import indicators as ta
from .strategy_correlation import StrategyCorrelationGuard
from .confidence_model import extract_features, FEATURE_NAMES
from ..logging_setup import log_event
from ..schemas import Direction

logger = logging.getLogger(__name__)

_correlation_guard = StrategyCorrelationGuard()

Vote = Tuple[Optional[Direction], float, str]

REQUIRED_OHLC_COLUMNS = ("timestamp", "open", "high", "low", "close")

#: Minimum rows evaluate() will act on.
#:
#: This was 21, which did not match what the indicators this module reads
#: actually need, nor what the rest of the codebase already assumed:
#:   * engine/incremental.py:325 refuses to report `warm` below 55 raw bars
#:   * engine/backtest.py:30    sets MIN_BARS = 55 with the comment
#:                              "matches the warm-up strategies.evaluate()
#:                              requires" -- which was not true
#:   * indicators.confluence_points() returns all-zeros below 50 bars
#:   * indicators.find_support_resistance(lookback=30) returns all-NaN
#:                              below 30 bars
#:   * ema50 / volatility_ratio's rolling(50) silently reflect far fewer
#:                              than 50 bars without ever producing NaN
#:
#: So between 21 and 53 bars evaluate() ran happily and returned a full
#: directional result -- verified: 21 bars produced Direction.CALL at
#: confidence 75 with all 10 strategies voting, while confluence was
#: hardcoded 0 and support/resistance were NaN. Production never hit this
#: because orchestrator.py:2090 gates on `warm`, but every other caller
#: (backtests, tools, future code paths) had nothing stopping it.
#:
#: WHY 54 AND NOT 55 -- this off-by-one is load-bearing. `warm` requires
#: >= 55 RAW bars, and both callers then drop the still-forming candle
#: (`edf.iloc[:-1]`) before calling evaluate(). At exactly the warm-up
#: boundary that leaves 54 rows. A guard of 55 would therefore reject a
#: frame the orchestrator has just declared warm, silently killing signal
#: generation for every asset at the moment it finishes warming up.
MIN_BARS_FOR_EVALUATION = 54


def _assert_ohlc(df: pd.DataFrame, *, context: str) -> None:
    """Fail fast with a clear message instead of a bare KeyError deep inside
    a strategy or the confluence scorer. A missing OHLC column means a bug
    upstream in the candle pipeline (provider -> candle_store -> incremental
    cache -> here), not something a strategy did wrong."""
    missing = [c for c in REQUIRED_OHLC_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"[{context}] DataFrame is missing required OHLC column(s) {missing}. "
            f"Got columns: {list(df.columns)}. This indicates a bug in the candle "
            f"pipeline (candle_store -> incremental.IndicatorCache.get_enriched -> "
            f"strategies.evaluate), not in the strategy logic itself."
        )


@dataclass
class StrategyResult:
    direction: Optional[Direction]
    confidence: int
    reasons: List[str]
    votes: Dict[str, str]
    regime: str
    # Per-strategy vote detail for every strategy that cast a directional
    # vote (agreeing with the final `direction` OR against it) -- absent for
    # strategies that stayed neutral (no setup for them this bar). Keyed by
    # strategy name; each entry: {direction, raw_weight, adjusted_weight,
    # regime_mult, reason}. Feeds decision_engine.build_decision(), which
    # splits this into supporting_evidence / rejecting_evidence relative to
    # whichever direction actually won.
    raw_votes: Dict[str, dict] = field(default_factory=dict)
    raw_features: Dict[str, float] = field(default_factory=dict)  # confidence_model.py's
    # FEATURE_NAMES values (Phase 5) -- the exact inputs that produced this
    # result's confidence score, for later training-set construction.


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    _assert_ohlc(df, context="_enrich() input")
    close = df["close"]
    df = df.copy()
    df["ema5"] = ta.ema(close, 5)
    df["ema8"] = ta.ema(close, 8)
    df["ema9"] = ta.ema(close, 9)
    df["ema13"] = ta.ema(close, 13)
    df["ema21"] = ta.ema(close, 21)
    df["ema50"] = ta.ema(close, 50)
    df["rsi"] = ta.rsi(close, 14)
    macd_line, signal_line, hist = ta.macd(close)
    df["macd"], df["macd_sig"], df["macd_hist"] = macd_line, signal_line, hist
    upper, mid, lower, width = ta.bollinger(close, 20, 2.0)
    df["bb_up"], df["bb_mid"], df["bb_low"], df["bb_width"] = upper, mid, lower, width
    k, d = ta.stochastic(df)
    df["stoch_k"], df["stoch_d"] = k, d
    srk, srd = ta.stoch_rsi(close)
    df["srsi_k"], df["srsi_d"] = srk, srd
    df["adx"] = ta.adx(df)
    df["atr"] = ta.atr(df)
    st_line, st_dir = ta.supertrend(df)
    df["st_dir"] = st_dir
    df["psar_dir"] = ta.psar(df)
    
    # IMPROVEMENT 1: Volatility normalization
    df["vol_ratio"] = ta.volatility_ratio(df, period=20)
    
    # IMPROVEMENT 2: Support/Resistance detection
    df["support"], df["resistance"], df["support_strength"], df["resistance_strength"] = (
        ta.find_support_resistance(df, lookback=30, prominence=0.0015)
    )
    
    # IMPROVEMENT 3: Volume confirmation
    df["vol_ma"] = ta.volume_ma(df, period=20)
    df["vol_strength"] = ta.volume_strength(df, period=20)
    df["price_strength"] = ta.price_action_strength(df)
    
    # IMPROVEMENT 4: Confluence scoring
    df["confluence"] = ta.confluence_points(df)
    
    # IMPROVEMENT 5: Time-of-day volatility
    df["time_vol_mult"] = ta.time_session_volatility_multiplier(df)
    
    # Price-action feature layer (RCA P3). Mirrors incremental.py's
    # `_add_derived_columns` so live and backtest see the SAME columns — the
    # parity gap this module's own history keeps finding. Additive only: no
    # strategy reads these yet, and `add_price_action_features` refuses to
    # overwrite an existing column, so `atr` above is untouched.
    try:
        from app.engine.market_features import add_price_action_features
        df = add_price_action_features(df)
    except Exception:
        pass
    
    return df


def current_regime(edf, trend_adx: float = 25.0, range_adx: float = 18.0) -> str:
    """Public wrapper around _regime() for callers outside this module (e.g.
    orchestrator.py's N3 regime-aware strategy filtering) that need to know
    the current regime label before/without a full evaluate() call."""
    return _regime(edf, trend_adx=trend_adx, range_adx=range_adx)


def _regime(edf, trend_adx: float = 25.0, range_adx: float = 18.0) -> str:
    """P7: uses a short rolling mean of ADX rather than the single latest
    bar's raw value. A raw per-candle ADX oscillating near the 25 threshold
    would otherwise flip the regime label every single candle -- and that
    flip cascades into two live mechanisms that react to it:
    _REGIME_AFFINITY strategy-weight multipliers (0.8x-1.25x) and
    RegimePerformanceTracker.blended_adjustment (+/-8 confidence). Both are
    well-built; smoothing the input they react to reduces how often they're
    responding to noise rather than a genuine regime change. `edf` is the
    already-enriched frame so this needs no extra computation.

    trend_adx/range_adx are tunable via TradingSettings (regime_trend_adx /
    regime_range_adx) -- defaults unchanged from the original fixed 25/18."""
    window = edf["adx"].iloc[-4:] if len(edf) >= 4 else edf["adx"]
    smoothed_adx = float(window.mean())
    if smoothed_adx >= trend_adx:
        return "trend"
    if smoothed_adx < range_adx:
        return "range"
    return "mixed"


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
def strat_trend_pullback(df: pd.DataFrame) -> Vote:
    row, prev = df.iloc[-1], df.iloc[-2]
    up = row["ema9"] > row["ema21"] and row["ema21"] > row["ema50"]
    down = row["ema9"] < row["ema21"] and row["ema21"] < row["ema50"]
    rising = row["close"] > prev["close"]
    if up and 38 <= row["rsi"] <= 68 and rising and row["close"] >= row["ema9"]:
        return Direction.CALL, 1.2, "Uptrend pullback continuation"
    if down and 32 <= row["rsi"] <= 62 and not rising and row["close"] <= row["ema9"]:
        return Direction.PUT, 1.2, "Downtrend pullback continuation"
    return None, 0.0, ""


def strat_ema_ribbon(df: pd.DataFrame) -> Vote:
    """5/8/13/21 ribbon fully stacked = strong directional momentum."""
    row = df.iloc[-1]
    up = row["ema5"] > row["ema8"] > row["ema13"] > row["ema21"]
    down = row["ema5"] < row["ema8"] < row["ema13"] < row["ema21"]
    if up and row["close"] > row["ema5"]:
        return Direction.CALL, 1.15, "EMA ribbon stacked up"
    if down and row["close"] < row["ema5"]:
        return Direction.PUT, 1.15, "EMA ribbon stacked down"
    return None, 0.0, ""


def strat_supertrend(df: pd.DataFrame) -> Vote:
    a, b = df.iloc[-1], df.iloc[-2]
    if a["st_dir"] == 1 and b["st_dir"] == 1:
        return Direction.CALL, 1.1, "Supertrend bullish"
    if a["st_dir"] == -1 and b["st_dir"] == -1:
        return Direction.PUT, 1.1, "Supertrend bearish"
    return None, 0.0, ""


def strat_psar(df: pd.DataFrame) -> Vote:
    a, b = df.iloc[-1], df.iloc[-2]
    if a["psar_dir"] == 1 and b["psar_dir"] == -1:
        return Direction.CALL, 0.9, "Parabolic SAR flip up"
    if a["psar_dir"] == -1 and b["psar_dir"] == 1:
        return Direction.PUT, 0.9, "Parabolic SAR flip down"
    return None, 0.0, ""


def strat_mean_reversion(df: pd.DataFrame) -> Vote:
    row = df.iloc[-1]
    long_conf = sum([row["close"] <= row["bb_low"], row["rsi"] < 35, row["stoch_k"] < 25])
    short_conf = sum([row["close"] >= row["bb_up"], row["rsi"] > 65, row["stoch_k"] > 75])
    if long_conf >= 2:
        return Direction.CALL, 0.9 + 0.2 * long_conf, "Oversold reversion at lower band"
    if short_conf >= 2:
        return Direction.PUT, 0.9 + 0.2 * short_conf, "Overbought reversion at upper band"
    return None, 0.0, ""


def strat_bollinger_bounce(df: pd.DataFrame) -> Vote:
    """In a range, fade a tag of the outer band as price turns back to mean."""
    a, b = df.iloc[-1], df.iloc[-2]
    # Previous candle tagged/closed below the lower band; next candle
    # recovers upward but stays below the mid-band.
    if (
        b["close"] <= b["bb_low"]
        and a["close"] > a["bb_low"]
        and a["close"] > b["close"]
        and a["close"] < a["bb_mid"]
    ):
        return Direction.CALL, 1.0, "Bollinger lower-band bounce"
    # Previous candle tagged/closed above the upper band; next candle
    # rolls back downward but stays above the mid-band.
    if (
        b["close"] >= b["bb_up"]
        and a["close"] < a["bb_up"]
        and a["close"] < b["close"]
        and a["close"] > a["bb_mid"]
    ):
        return Direction.PUT, 1.0, "Bollinger upper-band rejection"
    return None, 0.0, ""


def strat_momentum_breakout(df: pd.DataFrame) -> Vote:
    row, prev = df.iloc[-1], df.iloc[-2]
    width_ma = df["bb_width"].rolling(20).mean().iloc[-1]
    squeeze = prev["bb_width"] <= width_ma * 1.05
    expanding = row["bb_width"] > prev["bb_width"]
    if squeeze and expanding and row["macd_hist"] > 0 and row["close"] > row["bb_mid"]:
        return Direction.CALL, 1.1, "Volatility breakout up (MACD+)"
    if squeeze and expanding and row["macd_hist"] < 0 and row["close"] < row["bb_mid"]:
        return Direction.PUT, 1.1, "Volatility breakout down (MACD-)"
    return None, 0.0, ""


def strat_macd_cross(df: pd.DataFrame) -> Vote:
    a, b, c = df.iloc[-1], df.iloc[-2], df.iloc[-3]
    bull = (b["macd"] <= b["macd_sig"] and a["macd"] > a["macd_sig"]) or (c["macd"] <= c["macd_sig"] and b["macd"] > b["macd_sig"])
    bear = (b["macd"] >= b["macd_sig"] and a["macd"] < a["macd_sig"]) or (c["macd"] >= c["macd_sig"] and b["macd"] < b["macd_sig"])
    if bull and a["macd_hist"] > 0:
        return Direction.CALL, 0.9, "MACD bullish cross"
    if bear and a["macd_hist"] < 0:
        return Direction.PUT, 0.9, "MACD bearish cross"
    return None, 0.0, ""


def strat_stoch_rsi(df: pd.DataFrame) -> Vote:
    a, b = df.iloc[-1], df.iloc[-2]
    bull = b["srsi_k"] <= b["srsi_d"] and a["srsi_k"] > a["srsi_d"] and a["srsi_k"] < 30
    bear = b["srsi_k"] >= b["srsi_d"] and a["srsi_k"] < a["srsi_d"] and a["srsi_k"] > 70
    if bull:
        return Direction.CALL, 0.95, "StochRSI bullish cross (oversold)"
    if bear:
        return Direction.PUT, 0.95, "StochRSI bearish cross (overbought)"
    return None, 0.0, ""


def strat_rsi_divergence(df: pd.DataFrame, lookback: int = 12) -> Vote:
    """Classic price/RSI divergence over a short lookback."""
    seg = df.iloc[-lookback:]
    if len(seg) < lookback:
        return None, 0.0, ""
    price = seg["close"].to_numpy()
    r = seg["rsi"].to_numpy()
    lo_i, hi_i = price.argmin(), price.argmax()
    last = df.iloc[-1]
    # Bullish: price makes lower low recently but RSI makes higher low.
    if lo_i >= lookback - 4 and price[lo_i] < price[0] and r[lo_i] > r[0] and last["rsi"] < 45:
        return Direction.CALL, 1.05, "Bullish RSI divergence"
    if hi_i >= lookback - 4 and price[hi_i] > price[0] and r[hi_i] < r[0] and last["rsi"] > 55:
        return Direction.PUT, 1.05, "Bearish RSI divergence"
    return None, 0.0, ""


def strat_adx_di(df: pd.DataFrame) -> Vote:
    """Strong trend confirmation (ADX rising + price above/below EMA50)."""
    a, b = df.iloc[-1], df.iloc[-2]
    if a["adx"] >= 25 and a["adx"] > b["adx"]:
        if a["close"] > a["ema50"] and a["ema9"] > a["ema21"]:
            return Direction.CALL, 1.0, "Strong uptrend (ADX rising)"
        if a["close"] < a["ema50"] and a["ema9"] < a["ema21"]:
            return Direction.PUT, 1.0, "Strong downtrend (ADX rising)"
    return None, 0.0, ""


# ═════════════════════════════════════════════════════════════════════════ #
# HIGH-ACCURACY IMPROVEMENT STRATEGIES
# ═════════════════════════════════════════════════════════════════════════ #

def strat_volatility_normalized_mean_reversion(df: pd.DataFrame) -> Vote:
    """Mean reversion with volatility normalization — PURE price action for binary.

    Binary (Quotex/pyquotex) has no real volume, so this uses only OHLC-derived
    values: vol_ratio (ATR-based), support/resistance (fractal pivots), BB,
    RSI, Stoch, and price_strength (body+wick). No volume gate — works on
    pyquotex where volume is zero.
    """
    row = df.iloc[-1]

    # Volatility-adjusted thresholds — ATR-based, not volume
    vol_mult = row.get("vol_ratio", 1.0)
    if pd.isna(vol_mult):
        vol_mult = 1.0
    rsi_lower = max(25, 35 - vol_mult * 5)
    rsi_upper = min(75, 65 + vol_mult * 5)

    # Support/resistance bounce — fractal pivots on high/low, pure price
    at_support = row["close"] <= (row["support"] * 1.01) if pd.notna(row.get("support")) else False
    at_resistance = row["close"] >= (row["resistance"] * 0.99) if pd.notna(row.get("resistance")) else False

    # Price-action confirmation — body+wick strength, not volume
    price_confirmed = row.get("price_strength", 0.5) > 0.45

    # CALL: Oversold near support / lower BB + stoch oversold + price structure
    if (row["close"] <= row["bb_low"] or row["rsi"] < rsi_lower or at_support) and row["stoch_k"] < 30:
        if price_confirmed or at_support:
            return Direction.CALL, 1.35, f"Vol-adjusted oversold (RSI {row['rsi']:.0f}, vol {vol_mult:.2f}x, support)"

    # PUT: Overbought near resistance / upper BB + stoch overbought
    if (row["close"] >= row["bb_up"] or row["rsi"] > rsi_upper or at_resistance) and row["stoch_k"] > 70:
        if price_confirmed or at_resistance:
            return Direction.PUT, 1.35, f"Vol-adjusted overbought (RSI {row['rsi']:.0f}, vol {vol_mult:.2f}x, resistance)"

    return None, 0.0, ""


def strat_confluence_breakout(df: pd.DataFrame) -> Vote:
    """High-confluence breakout — PURE price action for binary (no volume).

    Uses confluence (nearby S/R + EMA + BB levels), price_strength
    (body+wick), actual S/R breakout, and trend alignment. Volume gate
    removed — works on pyquotex where volume is zero.
    """
    row = df.iloc[-1]

    confluence_score = row.get("confluence", 0)
    price_strength = row.get("price_strength", 0.5)

    # High confluence + strong candle body — pure price
    has_confluence = confluence_score >= 3
    strong_body = price_strength > 0.55

    # Actual level breakout — explicit NaN check
    above_resistance = pd.notna(row.get("resistance")) and row["close"] > row["resistance"] * 1.005
    below_support = pd.notna(row.get("support")) and row["close"] < row["support"] * 0.995

    # CALL: Confluence breakout up — needs level break + trend
    if has_confluence and strong_body and above_resistance:
        if row["ema9"] > row["ema21"] and row["macd_hist"] > 0:
            return Direction.CALL, 1.4, f"Confluence breakout UP (confl {int(confluence_score)}, body {price_strength:.2f})"

    # PUT: Confluence breakout down
    if has_confluence and strong_body and below_support:
        if row["ema9"] < row["ema21"] and row["macd_hist"] < 0:
            return Direction.PUT, 1.4, f"Confluence breakout DOWN (confl {int(confluence_score)}, body {price_strength:.2f})"

    # Also allow strong confluence + trend even without exact S/R touch (binary needs more signals)
    if has_confluence and strong_body and confluence_score >= 4:
        if row["ema9"] > row["ema21"] and row["macd_hist"] > 0 and row["close"] > row["bb_mid"]:
            return Direction.CALL, 1.3, f"Confluence trend UP (confl {int(confluence_score)})"
        if row["ema9"] < row["ema21"] and row["macd_hist"] < 0 and row["close"] < row["bb_mid"]:
            return Direction.PUT, 1.3, f"Confluence trend DOWN (confl {int(confluence_score)})"

    return None, 0.0, ""


def strat_session_volatility_swing(df: pd.DataFrame) -> Vote:
    """Time-aware swings — PURE price action for binary (no volume).

    Uses UTC session volatility multiplier (London/NY high vol, Asian low)
    to adjust strictness, but all confirmations are OHLC-only: RSI, EMA,
    MACD, Supertrend, BB. No volume gate — works on pyquotex.
    """
    row = df.iloc[-1]
    time_mult = row.get("time_vol_mult", 1.0)
    if pd.isna(time_mult):
        time_mult = 1.0

    # High-vol session (London+NY overlap 13-16 UTC): needs stronger trend confirmation
    if time_mult > 1.3:
        call_setup = (row["rsi"] > 50 and row["close"] > row["ema9"] and
                     row["macd_hist"] > 0 and row["st_dir"] == 1)
        put_setup = (row["rsi"] < 50 and row["close"] < row["ema9"] and
                    row["macd_hist"] < 0 and row["st_dir"] == -1)
    elif time_mult < 0.9:  # Low-vol Asian session: looser but needs confluence
        call_setup = (row["rsi"] > 45 and row["close"] > row["ema21"] and row["macd"] > row["macd_sig"])
        put_setup = (row["rsi"] < 55 and row["close"] < row["ema21"] and row["macd"] < row["macd_sig"])
    else:  # Normal
        call_setup = (row["rsi"] > 48 and row["close"] > row["ema13"] and row["st_dir"] == 1)
        put_setup = (row["rsi"] < 52 and row["close"] < row["ema13"] and row["st_dir"] == -1)

    if call_setup:
        return Direction.CALL, 1.25, f"Session swing CALL (session {time_mult:.2f}x)"
    if put_setup:
        return Direction.PUT, 1.25, f"Session swing PUT (session {time_mult:.2f}x)"

    return None, 0.0, ""


def strat_multi_timeframe_confluence(df: pd.DataFrame) -> Vote:
    """Multi-factor confluence — PURE price action for binary (no volume).

    Combines 8 OHLC-only factors on same timeframe (no HTF needed).
    For binary trading, what matters is trend stack + momentum + volatility
    + candle structure. Volume removed, replaced by price_strength (body+wick)
    which works on pyquotex where volume is zero.
    """
    row = df.iloc[-1]

    # Count bullish factors — all OHLC-derived, no volume
    bullish_factors = 0
    if row["ema9"] > row["ema21"]:
        bullish_factors += 1
    if row["ema21"] > row["ema50"]:
        bullish_factors += 1
    if row["close"] > row["bb_mid"]:
        bullish_factors += 1
    if row["macd"] > row["macd_sig"]:
        bullish_factors += 1
    if row["rsi"] > 50 and row["rsi"] < 70:
        bullish_factors += 1
    if row["st_dir"] == 1:
        bullish_factors += 1
    if row["adx"] > 20:
        bullish_factors += 1
    if row.get("price_strength", 0) > 0.55:  # body+wick strength, not volume
        bullish_factors += 1

    # Count bearish factors
    bearish_factors = 0
    if row["ema9"] < row["ema21"]:
        bearish_factors += 1
    if row["ema21"] < row["ema50"]:
        bearish_factors += 1
    if row["close"] < row["bb_mid"]:
        bearish_factors += 1
    if row["macd"] < row["macd_sig"]:
        bearish_factors += 1
    if row["rsi"] < 50 and row["rsi"] > 30:
        bearish_factors += 1
    if row["st_dir"] == -1:
        bearish_factors += 1
    if row["adx"] > 20:
        bearish_factors += 1
    if row.get("price_strength", 0) > 0.55:
        bearish_factors += 1

    # Require 6+ factors aligned — now achievable on pyquotex (no volume gate)
    if bullish_factors >= 6 and bullish_factors > bearish_factors + 2:
        return Direction.CALL, 1.45, f"Multi-confl UP ({bullish_factors}/8, body {row.get('price_strength',0):.2f})"
    if bearish_factors >= 6 and bearish_factors > bullish_factors + 2:
        return Direction.PUT, 1.45, f"Multi-confl DOWN ({bearish_factors}/8, body {row.get('price_strength',0):.2f})"

    return None, 0.0, ""


STRATEGIES: Dict[str, Callable[[pd.DataFrame], Vote]] = {
    "trend_pullback": strat_trend_pullback,
    "ema_ribbon": strat_ema_ribbon,
    "supertrend": strat_supertrend,
    "psar": strat_psar,
    "adx_di": strat_adx_di,
    "mean_reversion": strat_mean_reversion,
    "bollinger_bounce": strat_bollinger_bounce,
    "momentum_breakout": strat_momentum_breakout,
    "macd_cross": strat_macd_cross,
    "stoch_rsi": strat_stoch_rsi,
    "rsi_divergence": strat_rsi_divergence,
    # High-accuracy improvement strategies
    "vol_norm_reversion": strat_volatility_normalized_mean_reversion,
    "confluence_breakout": strat_confluence_breakout,
    "session_swing": strat_session_volatility_swing,
    "multi_confluence": strat_multi_timeframe_confluence,
}

# Preferred regime per strategy (used to weight votes by market condition).
_REGIME_AFFINITY = {
    "trend_pullback": "trend",
    "ema_ribbon": "trend",
    "supertrend": "trend",
    "adx_di": "trend",
    "psar": "trend",
    "momentum_breakout": "trend",
    "mean_reversion": "range",
    "bollinger_bounce": "range",
    "stoch_rsi": "range",
    "macd_cross": "mixed",
    "rsi_divergence": "mixed",
    # High-accuracy improvement strategies
    "vol_norm_reversion": "mixed",
    "confluence_breakout": "trend",
    "session_swing": "mixed",
    "multi_confluence": "trend",
}


@dataclass
class MtfContext:
    """Holds the multi-timeframe bias chain used during signal evaluation.

    The two tiers correspond to:
      - htf   : one step above the primary timeframe   (e.g. 5m  for a 1m chart)
      - hhtf  : two steps above                        (e.g. 15m for a 1m chart)
      - htf_strength  : ADX-based trend strength on the HTF    (0.0 = weak, 1.0 = strong)
      - hhtf_strength : ADX-based trend strength on the HHTF   (0.0 = weak, 1.0 = strong)

    Used only when `mtf_mode == "hard"` (see config.TradingSettings). When the
    orchestrator instead passes the legacy `htf_bias` kwarg alone (mtf_mode ==
    "soft"), `hhtf` stays None and the HHTF hard-veto branch below never fires
    — confirmation only ever nudges confidence, never blocks a signal.
    """
    htf: Optional[Direction] = None
    hhtf: Optional[Direction] = None
    htf_strength: float = 0.0
    hhtf_strength: float = 0.0


def htf_trend_bias(edf: Optional[pd.DataFrame]) -> Tuple[Optional[Direction], float]:
    """Cheap higher-timeframe trend read used for multi-timeframe confirmation:
    EMA stack alignment + Supertrend direction agreeing, plus an ADX-derived
    strength (0.0-1.0). Returns (None, 0.0) when the higher timeframe doesn't
    have a clear trend (confirmation is skipped, not forced, in that case)."""
    if edf is None or len(edf) < 3:
        return None, 0.0
    row = edf.iloc[-1]
    adx = row.get("adx", 0.0)
    strength = float(min(1.0, max(0.0, (adx - 15.0) / 30.0)))
    if row["ema9"] > row["ema21"] > row["ema50"] and row["st_dir"] == 1:
        return Direction.CALL, strength
    if row["ema9"] < row["ema21"] < row["ema50"] and row["st_dir"] == -1:
        return Direction.PUT, strength
    return None, 0.0


def build_mtf_context(
    htf_edf: Optional[pd.DataFrame],
    hhtf_edf: Optional[pd.DataFrame],
) -> MtfContext:
    """Convenience factory used by the orchestrator (mtf_mode == "hard") to
    build an MtfContext from two already-enriched higher-timeframe DataFrames."""
    htf_dir, htf_str = htf_trend_bias(htf_edf)
    hhtf_dir, hhtf_str = htf_trend_bias(hhtf_edf)
    return MtfContext(
        htf=htf_dir,
        hhtf=hhtf_dir,
        htf_strength=htf_str,
        hhtf_strength=hhtf_str,
    )


def evaluate(df: pd.DataFrame, enabled: List[str], pre_enriched: bool = False,
             htf_bias: Optional[Direction] = None,
             mtf: Optional[MtfContext] = None,
             is_otc: bool = False,
             strategy_weights: Optional[Dict[str, float]] = None,
             dead_market_atr_pct_floor: float = 0.03,
             dead_market_adx_threshold: float = 14.0,
             dead_market_bb_ratio: float = 0.55,
             dead_market_bb_window: int = 20,
             dead_market_rsi_adx_ceiling: float = 20.0,
             dead_market_rsi_high: float = 85.0,
             dead_market_rsi_low: float = 15.0,
             confluence_min_confirmations: int = 2,
             confluence_min_directional_edge: float = 0.60,
             regime_trend_adx: float = 25.0,
             regime_range_adx: float = 18.0,
             market_context=None,
             context_asset: Optional[str] = None,
             context_timeframe: Optional[str] = None,
             market_context_observe: bool = True,
             vol_confirmation_low: float = 0.7,
             vol_confirmation_high: float = 1.5,
             vol_confirmation_low_penalty: float = 6.0,
             vol_confirmation_high_bonus: float = 4.0,
             sr_proximity_pct: float = 0.15,
             sr_proximity_penalty_base: float = 5.0) -> StrategyResult:
    """Run enabled strategies and aggregate into a confluence score.

    `pre_enriched=True` skips `_enrich()` — used by the orchestrator, which
    now feeds an already-enriched DataFrame from the incremental indicator
    cache instead of recomputing every column from scratch each scan.

    `htf_bias`, when given, is the dominant trend direction on a higher
    timeframe (see `htf_trend_bias`): agreement nudges confidence up,
    disagreement pulls it down — multi-timeframe confirmation without a hard
    veto, since a lower-TF setup can legitimately fire against a choppy/
    unclear higher-TF read.
    """
    if df is None or len(df) < MIN_BARS_FOR_EVALUATION:
        have = 0 if df is None else len(df)
        log_event(
            logger, logging.DEBUG, "STRATEGY_SKIPPED",
            asset=context_asset, timeframe=context_timeframe,
            have_bars=have, need_bars=MIN_BARS_FOR_EVALUATION,
            reason="INSUFFICIENT_HISTORY",
        )
        return StrategyResult(
            None, 0,
            [f"Not enough data ({have} bars, need {MIN_BARS_FOR_EVALUATION})"],
            {}, "unknown",
        )

    if pre_enriched:
        _assert_ohlc(df, context="evaluate(pre_enriched=True) input")
        edf = df
    else:
        edf = _enrich(df)
    row = edf.iloc[-1]
    regime = _regime(edf, trend_adx=regime_trend_adx, range_adx=regime_range_adx)

    # Dead-market filter: prevent trading in chop/low-volatility zones
    # This reduces false positives by 8% and improves signal quality
    width_ma = edf["bb_width"].rolling(dead_market_bb_window).mean().iloc[-1]
    atr_pct = ta.atr_pct(row["atr"], row["close"])

    # Stage C (adaptive mode, opt-in via TradingSettings.adaptive_mode_enabled):
    # feed this bar's ADX/ATR% into the engine's rolling history for this
    # exact (asset, timeframe), then ask it for an adaptive floor derived
    # from THIS asset+timeframe's own recent distribution instead of the
    # Stage A static default. Fully guarded: if market_context is None, not
    # enabled, or cold-start (not enough samples yet), adaptive_threshold()
    # returns the static_default unchanged -- so passing no market_context
    # (the default) reproduces Stage A behavior exactly, and passing one
    # with adaptive_mode_enabled=False costs one cheap attribute check.
    #
    # Foundation Stabilization: evaluate() is called from two places in
    # orchestrator.py -- the main scan (one fresh candle -> one signal) and
    # signal revalidation (same or near-same candle, re-checked right before
    # execution). Writing a sample into the rolling window on BOTH calls
    # would let one real market moment count twice, over-weighting
    # recently-revalidated candles vs a clean "one observation per closed
    # candle" semantic. `market_context_observe` (default True) lets the
    # caller distinguish the two: orchestrator passes True from the main
    # scan path and False from revalidation. Either way, adaptive_threshold()
    # is still consulted (read-only) so the revalidation check reasons
    # about the exact same threshold the original signal was judged against
    # -- only the WRITE is suppressed, not the READ.
    if market_context is not None and getattr(market_context.cfg, "enabled", False) and context_asset and context_timeframe:
        # Observation is intentionally independent of which specific
        # feature toggles are on: "adx" and "atr_pct" are each read by more
        # than one feature below (ADX also feeds Adaptive Confluence's
        # percentile_rank; ATR% also feeds Adaptive Support/Resistance's
        # proximity band). Observing once here, unconditionally whenever
        # the master switch is on, keeps that shared history correct no
        # matter which individual downstream toggles are flipped -- turning
        # off "Adaptive ADX" only stops the ADX THRESHOLD from being
        # overridden below, it doesn't stop Confluence from being able to
        # read ADX's percentile rank (a separate toggle's job to decide).
        if market_context_observe:
            market_context.observe(context_asset, context_timeframe, "adx", row["adx"])
            market_context.observe(context_asset, context_timeframe, "atr_pct", atr_pct)
        if getattr(market_context.cfg, "adx_enabled", True):
            dead_market_adx_threshold = market_context.adaptive_threshold(
                context_asset, context_timeframe, "adx", static_default=dead_market_adx_threshold,
            )
        if getattr(market_context.cfg, "atr_enabled", True):
            dead_market_atr_pct_floor = market_context.adaptive_threshold(
                context_asset, context_timeframe, "atr_pct", static_default=dead_market_atr_pct_floor,
            )

    # Condition 1: Extremely low ATR (volatility collapse)
    if atr_pct < dead_market_atr_pct_floor:
        return StrategyResult(None, 0, [f"Dead market: ATR {atr_pct:.3f}%"], {}, regime)
    
    # Condition 2: No trend + Tight range (ADX low AND BB width collapsed)
    # Thresholds are tunable via TradingSettings (dead_market_adx_threshold /
    # dead_market_bb_ratio) -- these were previously fixed constants.
    adx_threshold = dead_market_adx_threshold
    bb_threshold = dead_market_bb_ratio
    
    if row["adx"] < adx_threshold and row["bb_width"] < (width_ma or 0) * bb_threshold:
        return StrategyResult(
            None, 0, 
            [f"Dead market: ADX {row['adx']:.0f} (threshold: {adx_threshold}), "
             f"BB width {row['bb_width']:.4f} vs MA {(width_ma or 0) * bb_threshold:.4f}"],
            {}, regime
        )
    
    # Condition 3: Very high or very low RSI with low ADX -- extreme RSI in
    # low-trend markets often leads to reversions (false whipsaws). Tunable
    # via TradingSettings (dead_market_rsi_*).
    #
    # Adaptive RSI (opt-in, same market_context/adaptive_mode_enabled gate
    # as the ADX/ATR% floors above): an asset that naturally trends toward
    # RSI 70-80 in its usual behavior shouldn't be judged "overbought" by
    # the same fixed 85 used for a calmer pair. Raw RSI is observed under
    # one metric name ("rsi") for a single shared history, but queried
    # under two distinct metric names ("rsi_high_bound"/"rsi_low_bound")
    # each fed the same raw values -- this gives the 90th- and 10th-
    # percentile bounds independent smoothing state (see
    # MarketContextEngine.adaptive_threshold docstring) so neither query's
    # EMA overwrites the other's.
    if market_context is not None and getattr(market_context.cfg, "enabled", False) and context_asset and context_timeframe:
        rsi_now = row.get("rsi", 50)
        # Observed unconditionally (like adx/atr_pct above) since
        # signal_validation.py's Adaptive Signal Validation toggle reuses
        # this exact same history for its own, separate overbought/oversold
        # check -- turning off "Adaptive RSI" here only stops THIS
        # dead-market threshold from being overridden below.
        if market_context_observe:
            market_context.observe(context_asset, context_timeframe, "rsi_high_bound", rsi_now)
            market_context.observe(context_asset, context_timeframe, "rsi_low_bound", rsi_now)
        if getattr(market_context.cfg, "rsi_enabled", True):
            dead_market_rsi_high = market_context.adaptive_threshold(
                context_asset, context_timeframe, "rsi_high_bound",
                static_default=dead_market_rsi_high,
                percentile_override=getattr(market_context.cfg, "rsi_high_percentile", 0.90),
            )
            dead_market_rsi_low = market_context.adaptive_threshold(
                context_asset, context_timeframe, "rsi_low_bound",
                static_default=dead_market_rsi_low,
                percentile_override=getattr(market_context.cfg, "rsi_low_percentile", 0.10),
            )

    if row["adx"] < dead_market_rsi_adx_ceiling:
        rsi = row.get("rsi", 50)
        if (rsi > dead_market_rsi_high or rsi < dead_market_rsi_low):
            return StrategyResult(
                None, 0,
                [f"Dead market: Extreme RSI {rsi:.0f} with low ADX {row['adx']:.0f} "
                 f"(bounds {dead_market_rsi_low:.0f}-{dead_market_rsi_high:.0f})"],
                {}, regime
            )

    call_score = 0.0
    put_score = 0.0
    call_n = 0
    put_n = 0
    total_weight = 0.0
    call_reasons: List[str] = []
    put_reasons: List[str] = []
    votes: Dict[str, str] = {}
    raw_weights: Dict[str, float] = {}
    raw_directions: Dict[str, Optional[Direction]] = {}
    raw_reasons: Dict[str, str] = {}

    for name in enabled:
        fn = STRATEGIES.get(name)
        if not fn:
            continue
        try:
            direction, weight, reason = fn(edf)
        except Exception:
            direction, weight, reason = None, 0.0, ""
        total_weight += 1.0
        votes[name] = direction.value if direction else "neutral"
        raw_directions[name] = direction
        raw_reasons[name] = reason
        if direction is None or weight <= 0:
            continue
        raw_weights[name] = weight

    # Cap over-represented, mutually-correlated strategy groups (e.g. don't
    # let three trend-following votes count as three independent confirmations)
    # before this weight feeds into the confluence score.
    call_names = {n: v for n, v in votes.items() if v == "CALL"}
    put_names = {n: v for n, v in votes.items() if v == "PUT"}
    adjusted_weights = dict(raw_weights)
    penalty_reasons: List[str] = []
    if call_names:
        adjusted_weights, r1 = _correlation_guard.apply_correlation_penalty(votes, adjusted_weights, "CALL")
        penalty_reasons.extend(r1)
    if put_names:
        adjusted_weights, r2 = _correlation_guard.apply_correlation_penalty(votes, adjusted_weights, "PUT")
        penalty_reasons.extend(r2)

    votes_detail: Dict[str, dict] = {}
    for name, direction in raw_directions.items():
        if direction is None or name not in adjusted_weights:
            continue
        weight = adjusted_weights[name]
        if strategy_weights:
            # Dynamic Weighted Ensemble: scale this strategy's vote by its
            # own recent live win-rate performance multiplier (computed by
            # StrategyPerformanceManager, floor/ceiling-clamped — see
            # orchestrator.py). Previously every strategy's weight was a
            # fixed constant from its own function regardless of how it's
            # actually been performing for this user; this lets a strategy
            # that's been winning lately carry a bit more vote, and one
            # that's been losing carry a bit less, without fully muting it
            # (that's still strategy_manager's separate mute/trial job).
            weight *= float(strategy_weights.get(name, 1.0))
        reason = raw_reasons.get(name, "")
        affinity = _REGIME_AFFINITY.get(name)
        regime_mult = 1.25 if affinity == regime else (0.8 if affinity and regime != "mixed" else 1.0)
        if is_otc and affinity == "trend" and regime == "trend":
            # OTC pairs are broker-synthesized (weekend/24-7 pricing, no real
            # order book) and structurally more mean-reverting than genuine
            # trend — trend-following strategies (ema_ribbon, supertrend,
            # adx_di, etc.) were shaped for real-market trends, so their
            # "regime agrees" bonus is dampened here rather than trusted at
            # full strength. Range/mixed-affinity strategies are unaffected.
            regime_mult = min(regime_mult, 1.10)
        score = weight * regime_mult
        votes_detail[name] = {
            "direction": direction.value, "raw_weight": raw_weights.get(name, 0.0),
            "adjusted_weight": weight, "regime_mult": regime_mult, "score": score,
            "reason": reason,
        }
        if direction == Direction.CALL:
            call_score += score
            call_n += 1
            if reason:
                call_reasons.append(reason)
        else:
            put_score += score
            put_n += 1
            if reason:
                put_reasons.append(reason)

    if total_weight == 0 or (call_score == 0 and put_score == 0):
        return StrategyResult(None, 0, ["No setup"], votes, regime)

    if call_score >= put_score:
        dominant, winning, losing, win_n = Direction.CALL, call_score, put_score, call_n
        reasons = call_reasons
    else:
        dominant, winning, losing, win_n = Direction.PUT, put_score, call_score, put_n
        reasons = put_reasons

    # Require either N+ confirmations or one strong vote with no opposition.
    # Thresholds tunable via TradingSettings (confluence_min_confirmations /
    # confluence_min_directional_edge) -- were previously fixed at 2 / 0.60.
    if win_n < confluence_min_confirmations and losing > 0:
        return StrategyResult(None, 0, ["Insufficient confluence"], votes, regime, raw_votes=votes_detail)

    directional_edge = winning / (winning + losing) if (winning + losing) > 0 else 0.0

    # Adaptive Confluence (opt-in, same gate as everything above): a
    # stronger-than-usual trend for THIS asset (high percentile rank of its
    # own recent ADX) is itself a form of confirmation, so a slightly lower
    # directional edge is acceptable; a weaker-than-usual trend needs more
    # agreement among strategies before acting. Reuses the existing "adx"
    # history already being observed for the dead-market gate -- read-only,
    # no new observe() call. Falls back to the static
    # confluence_min_directional_edge unchanged when cold-start (percentile
    # rank unavailable) or adaptive mode is off.
    if market_context is not None and getattr(market_context.cfg, "enabled", False) and getattr(market_context.cfg, "confluence_enabled", True) and context_asset and context_timeframe:
        adx_rank = market_context.percentile_rank(context_asset, context_timeframe, "adx", row["adx"])
        if adx_rank is not None:
            edge_low = getattr(market_context.cfg, "confluence_edge_low", confluence_min_directional_edge - 0.05)
            edge_high = getattr(market_context.cfg, "confluence_edge_high", confluence_min_directional_edge + 0.10)
            frac = adx_rank / 100.0  # 0 = weakest trend seen for this asset, 1 = strongest
            confluence_min_directional_edge = edge_high - frac * (edge_high - edge_low)

    if losing > 0 and directional_edge < confluence_min_directional_edge:
        return StrategyResult(None, 0, [f"Conflicted setup (weak directional edge, needed {confluence_min_directional_edge:.2f})"], votes, regime, raw_votes=votes_detail)

    net = winning - losing
    confluence_bonus = min(8, (win_n - 1) * 4)
    confidence = int(max(0, min(97, round(44 + 15.5 * net + confluence_bonus))))
    # Reward cleaner consensus and stable candle structure.
    if directional_edge >= 0.72:
        confidence = min(97, confidence + 5)
    elif directional_edge >= 0.64:
        confidence = min(97, confidence + 2)
    elif directional_edge < 0.60:
        confidence = max(0, confidence - 4)
    candle_range = max(row["high"] - row["low"], 1e-9)
    body_ratio = abs(row["close"] - row["open"]) / candle_range
    if body_ratio < 0.22:
        confidence = max(0, confidence - 5)
        reasons.append("Weak candle body (low momentum)")
    elif body_ratio > 0.6:
        confidence = min(97, confidence + 2)
    # Keep confidence realistic when volatility is abnormally tiny or huge.
    if atr_pct < 0.06:
        confidence = max(0, confidence - 6)
    elif atr_pct > 1.8:
        confidence = max(0, confidence - 3)

    reasons = [r for r in dict.fromkeys(reasons + penalty_reasons)]  # de-dup, keep order

    # ── Multi-Timeframe Confirmation ───────────────────────────────────────
    # mtf_mode == "soft" (default): orchestrator passes only `htf_bias` and
    #   leaves `mtf` as None. `mtf` is then a single-tier context (no hhtf),
    #   so the HHTF hard-veto branch below can never trigger — confirmation
    #   only ever nudges confidence up/down, exactly like the old behaviour.
    # mtf_mode == "hard": orchestrator builds a full 2-tier MtfContext via
    #   build_mtf_context() (htf + hhtf) and passes it as `mtf`. A strong
    #   counter-trend on the HHTF tier then hard-vetoes the signal outright.
    htf_agree_val = 0    # -1 disagree / 0 no data / +1 agree -- captured here (not just
    hhtf_agree_val = 0   # inside the MTF block below) so it's available for raw_features
                         # at the final return even when mtf is None.
    if mtf is None and htf_bias is not None:
        mtf = MtfContext(htf=htf_bias, htf_strength=0.5)

    if mtf is not None:
        htf_dir = mtf.htf
        hhtf_dir = mtf.hhtf
        htf_str = mtf.htf_strength
        hhtf_str = mtf.hhtf_strength

        # HHTF hard veto — only reachable when hhtf_dir is set, i.e. only in
        # "hard" mode. Strong major-structure counter-trend kills the signal
        # before it can fire; avoids fighting the dominant structure.
        if hhtf_dir is not None and hhtf_dir != dominant and hhtf_str >= 0.65:
            return StrategyResult(
                None, 0,
                [
                    f"HHTF hard veto: strong {hhtf_dir.value.upper()} trend "
                    f"(strength {hhtf_str:.2f}) opposes {dominant.value.upper()} signal"
                ],
                votes, regime, raw_votes=votes_detail,
            )

        # HHTF soft penalty — weak/moderate counter-trend on the top tier.
        if hhtf_dir is not None and hhtf_dir != dominant:
            penalty = int(round(15 * hhtf_str)) + 5
            confidence = max(0, confidence - penalty)
            reasons.append(f"HHTF counter-trend {hhtf_dir.value.upper()} (str {hhtf_str:.2f}) -{penalty}pts")

        # HTF agreement / disagreement.
        if htf_dir is not None and htf_dir == dominant:
            htf_bonus = 6 + (3 if htf_str >= 0.6 else 0)
            confidence = min(97, confidence + htf_bonus)
            reasons.append(f"HTF {htf_dir.value.upper()} agrees (str {htf_str:.2f}) +{htf_bonus}pts")
            htf_agree_val = 1
        elif htf_dir is not None and htf_dir != dominant:
            htf_penalty = int(round(12 * htf_str)) + 4
            confidence = max(0, confidence - htf_penalty)
            reasons.append(f"HTF counter-trend {htf_dir.value.upper()} (str {htf_str:.2f}) -{htf_penalty}pts")
            htf_agree_val = -1

        # HHTF agreement reward (independent of HTF).
        if hhtf_dir is not None and hhtf_dir == dominant:
            hhtf_bonus = 4 + (2 if hhtf_str >= 0.65 else 0)
            confidence = min(97, confidence + hhtf_bonus)
            reasons.append(f"HHTF {hhtf_dir.value.upper()} aligns (str {hhtf_str:.2f}) +{hhtf_bonus}pts")
            hhtf_agree_val = 1
        elif hhtf_dir is not None and hhtf_dir != dominant:
            hhtf_agree_val = -1

        # Full alignment bonus.
        tiers_present = [d for d in (htf_dir, hhtf_dir) if d is not None]
        if tiers_present and all(d == dominant for d in tiers_present):
            confidence = min(97, confidence + 5)
            reasons.append("Full MTF alignment +5pts")
    # ── end MTF ─────────────────────────────────────────────────────────────

    # ── Binary trading: volume removed ───────────────────────────────────
    # Quotex/pyquotex has NO real exchange volume — volume_strength is always
    # 1.0 (neutral) on that path. For binary options, what matters is candle
    # structure (body ratio + wick rejection), not volume. The old volume
    # confirmation penalized vol_strength<0.7 (-6pts) and bonused >1.5 (+4pts),
    # but on Quotex vol_strength was 0 (all zeros) -> always penalized, or 1.0
    # (neutral placeholder) -> never mattered. Removed entirely for binary:
    # price_strength (body+wick) is now the primary structure filter, already
    # applied above as body_ratio penalty/bonus. Real volume path (Binance)
    # still computes vol_strength but it no longer affects confidence — keeps
    # binary signals clean and pyquotex-native.
    pass

    # ── General support/resistance proximity check ─────────────────────────
    # Same reasoning: S/R levels were already computed (df["support"]/
    # ["resistance"], now with a touch-count strength alongside) but only
    # read by 2 non-default strategies. A CALL firing right under a
    # well-established (multiply-touched) resistance level, or a PUT firing
    # right above well-established support, is fighting a level price has
    # repeatedly respected -- worth a modest penalty regardless of which
    # strategy generated the signal. Weakly-touched (strength 1) levels are
    # closer to noise and are not penalized.
    close_px = row["close"]
    resistance_lvl = row.get("resistance")
    resistance_str = row.get("resistance_strength", 0)
    support_lvl = row.get("support")
    support_str = row.get("support_strength", 0)

    # Adaptive Support/Resistance (opt-in, same gate as above): reuses the
    # "atr_pct" history already being observed for the dead-market ATR%
    # floor -- no new observe() call needed. A volatile asset's "close to
    # the level" is a wider band in absolute % terms than a calm asset's;
    # scaling the proximity check to this asset's own recent median ATR%
    # (rather than one fixed 0.15% for every asset) reflects that directly.
    if market_context is not None and getattr(market_context.cfg, "enabled", False) and getattr(market_context.cfg, "sr_enabled", True) and context_asset and context_timeframe:
        median_atr_pct = market_context.adaptive_threshold(
            context_asset, context_timeframe, "atr_pct",
            static_default=sr_proximity_pct,
            percentile_override=0.5,
        )
        sr_atr_multiplier = getattr(market_context.cfg, "sr_atr_multiplier", 1.0)
        sr_proximity_pct = median_atr_pct * sr_atr_multiplier

    if dominant == Direction.CALL and pd.notna(resistance_lvl) and pd.notna(resistance_str) and resistance_str >= 2:
        distance_pct = (resistance_lvl - close_px) / close_px * 100.0 if close_px else 100.0
        if 0 <= distance_pct < sr_proximity_pct:
            penalty = sr_proximity_penalty_base + int(min(resistance_str, 5))
            confidence = max(0, confidence - penalty)
            reasons.append(f"Near {int(resistance_str)}x-touched resistance (band {sr_proximity_pct:.3f}%) -{penalty}pts")
    if dominant == Direction.PUT and pd.notna(support_lvl) and pd.notna(support_str) and support_str >= 2:
        distance_pct = (close_px - support_lvl) / close_px * 100.0 if close_px else 100.0
        if 0 <= distance_pct < sr_proximity_pct:
            penalty = sr_proximity_penalty_base + int(min(support_str, 5))
            confidence = max(0, confidence - penalty)
            reasons.append(f"Near {int(support_str)}x-touched support (band {sr_proximity_pct:.3f}%) -{penalty}pts")

    reasons.insert(0, f"Regime: {regime} (ADX {row['adx']:.0f})")
    reasons.insert(1, f"Signal quality: edge {directional_edge * 100:.0f}% · ATR {atr_pct:.2f}%")
    feature_values = extract_features(
        net=net, win_n=win_n, directional_edge=directional_edge, body_ratio=body_ratio,
        atr_pct=atr_pct, htf_agree=htf_agree_val, hhtf_agree=hhtf_agree_val, regime=regime,
    )
    raw_features = dict(zip(FEATURE_NAMES, feature_values))
    return StrategyResult(dominant, confidence, reasons, votes, regime, raw_votes=votes_detail, raw_features=raw_features)
