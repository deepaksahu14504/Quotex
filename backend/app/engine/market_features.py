"""Price-action / microstructure / sentiment features for binary-options signals.

Design rules this module exists to enforce:

1.  **Tick count is never volume.** Quotex/pyquotex publishes no order-book volume
    (`vendor/old-pyquotex/pyquotex/api.py:790` — the tick frame is
    `[asset, ts, price, direction]`). Every activity measure produced here is named
    `tick_*` / `activity_*` / `price_update_*` and is deliberately NOT called
    `real_volume`, `volume`, or anything a volume indicator could pick up.

2.  **Nothing here replaces existing indicators.** These are *additional* columns.
    `indicators.py` / `strategies.py` keep their own mathematics untouched.

3.  **Every feature degrades to a neutral value** when the input is missing,
    malformed, or too short. A feature must never raise into the signal engine and
    must never invent information: neutral means "no evidence", not "bullish".

4.  **Sentiment is bounded confirmation, not a signal source.** It can nudge
    confidence by a configurable few points when it is fresh, strong and agrees with
    the direction the OHLC engine already chose. It cannot pick or reverse a
    direction, and its absence is not evidence against a trade.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Neutral constants — a single source of truth so "no evidence" never drifts.
# ────────────────────────────────────────────────────────────────────────────
NEUTRAL_RATIO = 0.5      # a doji-ish body/range, mid-range close
NEUTRAL_INTENSITY = 1.0  # activity exactly at its own average
NEUTRAL_BALANCE = 0.0    # equal up/down ticks, zero momentum


# ────────────────────────────────────────────────────────────────────────────
# Data quality
# ────────────────────────────────────────────────────────────────────────────
class DataQuality:
    """String constants for the structured `data_quality` log field.

    Kept as plain strings rather than an enum so they serialise straight into JSON
    logs and into the existing `log_event` helper without a custom encoder.
    """

    OK = "ok"
    MISSING_OHLC = "missing_ohlc"
    MALFORMED_OHLC = "malformed_ohlc"
    INSUFFICIENT_HISTORY = "insufficient_history"
    STALE_PRICE = "stale_price"
    STALE_SENTIMENT = "stale_sentiment"
    MISSING_SENTIMENT = "missing_sentiment"
    DUPLICATE_TICK = "duplicate_tick"
    OUT_OF_ORDER_TICK = "out_of_order_tick"
    TIMEFRAME_MISMATCH = "timeframe_mismatch"


@dataclass
class QualityReport:
    """Accumulated data-quality flags for one evaluation.

    `ok` is True only when no flag was raised, so callers can log a single
    `data_quality` value instead of a list.
    """

    flags: List[str] = field(default_factory=list)

    def add(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    @property
    def ok(self) -> bool:
        return not self.flags

    @property
    def value(self) -> str:
        return DataQuality.OK if self.ok else ",".join(self.flags)

    def __str__(self) -> str:  # pragma: no cover - logging convenience
        return self.value


# ────────────────────────────────────────────────────────────────────────────
# OHLC price-action features (P3)
# ────────────────────────────────────────────────────────────────────────────
# Minimum rows before a feature is considered meaningful. Below this the feature
# is filled with its neutral value rather than a number computed from noise.
MIN_ROWS_CANDLE_SHAPE = 1     # per-candle geometry needs only the candle itself
MIN_ROWS_RETURN = 2           # a return needs two closes
MIN_ROWS_MOMENTUM = 11        # momentum(n) needs n+1 closes; default n=10
MIN_ROWS_ATR = 15             # ATR(14) needs 15 rows to have one valid value
MIN_ROWS_RANGE_POSITION = 10  # "position in recent range" needs a window


def _safe_div(num: pd.Series, den: pd.Series, fill: float) -> pd.Series:
    """`num / den`, mapping non-finite and zero denominators to `fill`."""
    den = den.replace(0.0, np.nan)
    out = num / den
    return out.replace([np.inf, -np.inf], np.nan).fillna(fill)


def _finite(series: pd.Series, fill: float) -> pd.Series:
    """Coerce to numeric, drop non-finite, fill with a neutral constant."""
    out = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return out.fillna(fill)


def validate_ohlc(df: pd.DataFrame, report: QualityReport) -> pd.DataFrame:
    """Return a copy with numeric OHLC, flagging unusable rows.

    Never raises and never drops rows silently: a malformed row becomes NaN, which
    every feature below already maps to its neutral value. Rows whose high<low or
    whose extremes fall outside the open/close envelope are flagged
    `malformed_ohlc`; they are not repaired, because inventing prices would be worse
    than admitting the bar is unusable.
    """
    # A list, not a tuple: `df[("a", "b")]` is read as ONE column key by pandas.
    required = ["timestamp", "open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        report.add(DataQuality.MISSING_OHLC)
        return df.copy()

    out = df.copy()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    bad = (
        out[required].isna().any(axis=1)
        | (out["high"] < out["low"])
        | (out["high"] < out[["open", "close"]].max(axis=1))
        | (out["low"] > out[["open", "close"]].min(axis=1))
    )
    if bool(bad.any()):
        report.add(DataQuality.MALFORMED_OHLC)
        # Neutralise only the offending bars so one bad frame cannot poison a
        # rolling window for the whole series.
        out.loc[bad, ["open", "high", "low", "close"]] = np.nan
    return out


def add_price_action_features(
    df: pd.DataFrame,
    *,
    atr_period: int = 14,
    momentum_period: int = 10,
    range_window: int = 20,
    breakout_window: int = 20,
    sr_window: int = 50,
) -> pd.DataFrame:
    """Append price-action feature columns to `df` and return it.

    All features are computed from OHLC alone, so they work identically when
    `volume` is 0, synthetic, or entirely absent — which is the pyquotex case.
    Columns that already exist on `df` are never overwritten.
    """
    n = len(df)
    out = df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
    if n == 0:
        return out

    idx = out.index
    high = pd.to_numeric(out.get("high"), errors="coerce")
    low = pd.to_numeric(out.get("low"), errors="coerce")
    open_ = pd.to_numeric(out.get("open"), errors="coerce")
    close = pd.to_numeric(out.get("close"), errors="coerce")

    def put(name: str, series: pd.Series, neutral: float, min_rows: int) -> None:
        """Write `name`, falling back to `neutral` when history is too short."""
        if name in out.columns:
            return  # never clobber an existing column
        if n < min_rows:
            out[name] = pd.Series([neutral] * n, index=idx)
        else:
            out[name] = _finite(series, neutral)

    # ── candle geometry ────────────────────────────────────────────────────
    candle_range = (high - low).replace(0.0, np.nan)
    body = (close - open_).abs()
    body_top = pd.concat([open_, close], axis=1).max(axis=1)
    body_bot = pd.concat([open_, close], axis=1).min(axis=1)
    upper_wick = high - body_top
    lower_wick = body_bot - low

    put("candle_range", (high - low), 0.0, MIN_ROWS_CANDLE_SHAPE)
    put("candle_body_ratio", _safe_div(body, candle_range, NEUTRAL_RATIO),
        NEUTRAL_RATIO, MIN_ROWS_CANDLE_SHAPE)
    put("upper_wick_ratio", _safe_div(upper_wick, candle_range, 0.0),
        0.0, MIN_ROWS_CANDLE_SHAPE)
    put("lower_wick_ratio", _safe_div(lower_wick, candle_range, 0.0),
        0.0, MIN_ROWS_CANDLE_SHAPE)
    # +1 bullish / -1 bearish / 0 doji. A tie is 0, NOT bullish: the existing
    # strategy tie-break (strategies.py) already decides CALL, and silently
    # encoding that bias into a feature would double-count it.
    direction = pd.Series(
        np.sign((close - open_).fillna(0.0).to_numpy()), index=idx, dtype=float
    )
    put("candle_direction", direction, 0.0, MIN_ROWS_CANDLE_SHAPE)
    # Close Location Value: where the close sits inside the bar, -1..+1.
    clv = ((close - low) - (high - close)) / candle_range
    put("close_location_value", clv, 0.0, MIN_ROWS_CANDLE_SHAPE)

    # ── ATR and ATR-normalised movement ────────────────────────────────────
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / max(1, atr_period), adjust=False, min_periods=atr_period).mean()
    atr_safe = atr.replace(0.0, np.nan)

    # `put` is a no-op when the column already exists, so a frame that has a real
    # ATR from `strategies._enrich()` / `SymbolState.step()` keeps it.
    put("atr", atr, 0.0, MIN_ROWS_ATR)
    put("range_vs_atr", _safe_div(high - low, atr_safe, 1.0), 1.0, MIN_ROWS_ATR)
    put("atr_normalized_move", _safe_div(close - open_, atr_safe, 0.0),
        0.0, MIN_ROWS_ATR)

    # ── returns / momentum ─────────────────────────────────────────────────
    put("short_term_return", _safe_div(close - close.shift(1), close.shift(1), 0.0),
        0.0, MIN_ROWS_RETURN)
    put("momentum", close - close.shift(momentum_period), 0.0, MIN_ROWS_MOMENTUM)
    put("atr_normalized_momentum",
        _safe_div(close - close.shift(momentum_period), atr_safe, 0.0),
        0.0, MIN_ROWS_MOMENTUM)

    # ── consecutive direction streaks ──────────────────────────────────────
    put("consecutive_bullish", _streak(direction, 1.0), 0.0, MIN_ROWS_CANDLE_SHAPE)
    put("consecutive_bearish", _streak(direction, -1.0), 0.0, MIN_ROWS_CANDLE_SHAPE)

    # ── rejection strength ─────────────────────────────────────────────────
    # How hard the bar was pushed back from its own extreme, normalised by the
    # bar's range. High on a pin bar, ~0 on a marubozu.
    put("bullish_rejection", _safe_div(lower_wick, candle_range, 0.0),
        0.0, MIN_ROWS_CANDLE_SHAPE)
    put("bearish_rejection", _safe_div(upper_wick, candle_range, 0.0),
        0.0, MIN_ROWS_CANDLE_SHAPE)

    # ── breakout / structure distances ─────────────────────────────────────
    # shift(1) throughout: the level is built from *prior* bars only, so a bar
    # can never measure its distance to a level it is itself creating.
    prior_high = high.shift(1).rolling(breakout_window, min_periods=2).max()
    prior_low = low.shift(1).rolling(breakout_window, min_periods=2).min()
    put("breakout_distance",
        _safe_div(close - prior_high, atr_safe, 0.0), 0.0, MIN_ROWS_ATR)
    put("breakdown_distance",
        _safe_div(prior_low - close, atr_safe, 0.0), 0.0, MIN_ROWS_ATR)

    sr_high = high.shift(1).rolling(sr_window, min_periods=5).max()
    sr_low = low.shift(1).rolling(sr_window, min_periods=5).min()
    put("resistance_distance",
        _safe_div(sr_high - close, atr_safe, 0.0), 0.0, MIN_ROWS_ATR)
    put("support_distance",
        _safe_div(close - sr_low, atr_safe, 0.0), 0.0, MIN_ROWS_ATR)

    # ── position within the recent range ───────────────────────────────────
    win_high = high.rolling(range_window, min_periods=2).max()
    win_low = low.rolling(range_window, min_periods=2).min()
    pos = _safe_div(close - win_low, (win_high - win_low).replace(0.0, np.nan), NEUTRAL_RATIO)
    put("price_position_in_range", pos, NEUTRAL_RATIO, MIN_ROWS_RANGE_POSITION)

    return out


def _streak(direction: pd.Series, sign: float) -> pd.Series:
    """Count of consecutive bars whose direction equals `sign`, ending at each bar."""
    vals = direction.fillna(0.0).to_numpy()
    out = np.zeros(len(vals), dtype=float)
    run = 0
    for i, v in enumerate(vals):
        run = run + 1 if v == sign else 0
        out[i] = run
    return pd.Series(out, index=direction.index)


# Feature names added above — used by tests and by the incremental engine to make
# sure the warm path carries the same columns as the cold path.
PRICE_ACTION_FEATURES: Tuple[str, ...] = (
    "candle_range",
    "candle_body_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "candle_direction",
    "close_location_value",
    "range_vs_atr",
    "atr_normalized_move",
    "short_term_return",
    "momentum",
    "atr_normalized_momentum",
    "consecutive_bullish",
    "consecutive_bearish",
    "bullish_rejection",
    "bearish_rejection",
    "breakout_distance",
    "breakdown_distance",
    "resistance_distance",
    "support_distance",
    "price_position_in_range",
)


# ────────────────────────────────────────────────────────────────────────────
# Tick / microstructure activity features (P3)
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class TickActivity:
    """Activity measures derived from raw price updates for ONE (asset, timeframe).

    These describe *how often the price moved*, never *how much was traded*.
    There is deliberately no `real_volume` field: nothing in this class may be
    handed to a volume indicator.
    """

    tick_count: int = 0
    price_update_count: int = 0
    price_update_frequency: float = 0.0
    tick_activity: float = NEUTRAL_INTENSITY
    activity_intensity: float = NEUTRAL_INTENSITY
    tick_direction_balance: float = NEUTRAL_BALANCE
    intrabar_momentum: float = 0.0
    intrabar_price_change: float = 0.0
    intrabar_range: float = 0.0
    window_seconds: float = 0.0
    last_tick_ts: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {
            "tick_count": float(self.tick_count),
            "price_update_count": float(self.price_update_count),
            "price_update_frequency": float(self.price_update_frequency),
            "tick_activity": float(self.tick_activity),
            "activity_intensity": float(self.activity_intensity),
            "tick_direction_balance": float(self.tick_direction_balance),
            "intrabar_momentum": float(self.intrabar_momentum),
            "intrabar_price_change": float(self.intrabar_price_change),
            "intrabar_range": float(self.intrabar_range),
        }


def compute_tick_activity(
    prices: Sequence[float],
    timestamps: Sequence[float],
    *,
    period_seconds: float,
    bar_open: Optional[float] = None,
    bar_high: Optional[float] = None,
    bar_low: Optional[float] = None,
    baseline_counts: Optional[Iterable[float]] = None,
    now: Optional[float] = None,
) -> TickActivity:
    """Derive activity features from the ticks belonging to one forming bar.

    `baseline_counts` is the recent history of per-bar tick counts; when supplied,
    `activity_intensity` is the current count relative to that average. Without a
    baseline the intensity stays neutral at 1.0 rather than guessing.

    `tick_direction_balance` is derived from signed price changes, NOT from the
    broker's `direction` field: the installed vendor drops that field when it stores
    ticks (`api.py:785` unpacks 3 of the 4 wire elements), so it is unavailable here.
    """
    act = TickActivity(window_seconds=float(period_seconds or 0.0))
    n = len(prices)
    if n == 0 or n != len(timestamps):
        return act

    try:
        act.tick_count = n
        act.last_tick_ts = float(timestamps[-1])
    except (TypeError, ValueError, IndexError):
        return act

    # Price updates = ticks that actually moved the price. A repeated print is
    # still an update event for `tick_count`, but carries no directional content.
    ups = downs = 0
    for i in range(1, n):
        try:
            delta = float(prices[i]) - float(prices[i - 1])
        except (TypeError, ValueError):
            continue
        if delta > 0:
            ups += 1
        elif delta < 0:
            downs += 1
    act.price_update_count = ups + downs
    total_moves = ups + downs
    act.tick_direction_balance = (
        (ups - downs) / total_moves if total_moves else NEUTRAL_BALANCE
    )

    # Frequency: updates per second over the bar window.
    if period_seconds and period_seconds > 0:
        act.price_update_frequency = total_moves / float(period_seconds)

    # Intrabar price change / momentum, anchored on the bar open when known.
    try:
        first = float(bar_open) if bar_open is not None else float(prices[0])
        last = float(prices[-1])
        act.intrabar_price_change = last - first
        if first:
            act.intrabar_momentum = (last - first) / first
    except (TypeError, ValueError):
        act.intrabar_momentum = 0.0
        act.intrabar_price_change = 0.0

    # Intrabar range: honour the caller's running high/low, else derive it.
    try:
        hi = float(bar_high) if bar_high is not None else max(float(p) for p in prices)
        lo = float(bar_low) if bar_low is not None else min(float(p) for p in prices)
        act.intrabar_range = hi - lo
    except (TypeError, ValueError):
        act.intrabar_range = 0.0

    # `tick_activity` is a raw normalised rate; `activity_intensity` is the same
    # count expressed against the recent per-bar average so it is comparable
    # across assets with wildly different tick rates.
    counts = list(baseline_counts) if baseline_counts is not None else []
    baseline = [float(c) for c in counts if _is_finite(c) and float(c) > 0]
    if baseline:
        avg = sum(baseline) / len(baseline)
        act.activity_intensity = (float(n) / avg) if avg > 0 else NEUTRAL_INTENSITY
    act.tick_activity = act.activity_intensity
    return act


def _is_finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


TICK_ACTIVITY_FIELDS: Tuple[str, ...] = (
    "tick_count",
    "price_update_count",
    "price_update_frequency",
    "tick_activity",
    "activity_intensity",
    "tick_direction_balance",
    "intrabar_momentum",
    "intrabar_price_change",
    "intrabar_range",
)


# ────────────────────────────────────────────────────────────────────────────
# Sentiment normalisation (P4)
# ────────────────────────────────────────────────────────────────────────────
# The installed vendor delivers the raw broker payload unchanged
# (`api.py:363-369`), and its own CLI reads `call`/`put` with `bulls`/`bears` as
# aliases (`cli/commands/realtime.py:59-60`). Both spellings are accepted here.
_BUY_KEYS = ("call", "bulls", "buy", "buying", "buy_percent")
_SELL_KEYS = ("put", "bears", "sell", "selling", "sell_percent")


@dataclass
class SentimentState:
    """Normalised trader sentiment for ONE (asset, timeframe)."""

    buy: Optional[float] = None
    sell: Optional[float] = None
    bias: float = 0.0
    strength: float = 0.0
    timestamp: float = 0.0
    stale: bool = True
    available: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        if not self.timestamp:
            return float("inf")
        return max(0.0, time.time() - float(self.timestamp))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sentiment_buy": self.buy,
            "sentiment_sell": self.sell,
            "sentiment_bias": self.bias,
            "sentiment_strength": self.strength,
            "sentiment_timestamp": self.timestamp,
            "sentiment_stale": self.stale,
            "sentiment_available": self.available,
        }


def _coerce_percent(value: Any) -> Optional[float]:
    """Broker percentages arrive as numbers or strings like `"62"` / `"62%"`."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
        if not value:
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    # A fraction (0..1) is scaled up; anything already in percent is left alone.
    if 0.0 <= out <= 1.0 and value not in ("0", "1", "0.0", "1.0"):
        out *= 100.0
    return max(0.0, min(100.0, out))


def _first_present(payload: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        if k in payload:
            v = _coerce_percent(payload[k])
            if v is not None:
                return v
    return None


def normalize_sentiment(
    payload: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    max_age_seconds: float = 120.0,
) -> SentimentState:
    """Normalise a raw broker sentiment payload into buy/sell percentages + bias.

    `sentiment_bias = (buy_pct - sell_pct) / 100`, so bias is in -1..+1 and is
    directly comparable to a confidence fraction. A payload that cannot be parsed
    yields `available=False` with a zero bias — i.e. "no evidence", never "neutral
    market", so a caller must not treat absence as a signal in either direction.
    """
    now = float(now if now is not None else time.time())
    state = SentimentState(timestamp=now, stale=True, available=False)
    if not isinstance(payload, dict) or not payload:
        return state

    buy = _first_present(payload, _BUY_KEYS)
    sell = _first_present(payload, _SELL_KEYS)
    if buy is None and sell is None:
        return state

    # If the broker sends only one side, infer the other: these are complementary
    # shares of the same trader population, so the missing side is 100 - known.
    if buy is None:
        buy = max(0.0, 100.0 - (sell or 0.0))
    if sell is None:
        sell = max(0.0, 100.0 - buy)

    state.buy = buy
    state.sell = sell
    state.bias = (buy - sell) / 100.0
    state.strength = abs(state.bias)
    state.available = True
    state.raw = dict(payload)

    ts = _coerce_percent(payload.get("timestamp") or payload.get("ts") or payload.get("time"))
    # The timestamp is not a percentage — re-read it raw so the 0..1 scaling in
    # `_coerce_percent` cannot corrupt an epoch value.
    raw_ts = payload.get("timestamp") or payload.get("ts") or payload.get("time")
    try:
        state.timestamp = float(raw_ts) if raw_ts is not None else now
    except (TypeError, ValueError):
        state.timestamp = now
    del ts
    state.stale = (now - state.timestamp) > float(max_age_seconds)
    return state


@dataclass
class SentimentAdjustment:
    """The bounded confidence nudge produced from sentiment (P6)."""

    adjustment: float = 0.0
    applied: bool = False
    reason: str = "disabled"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sentiment_adjustment": self.adjustment,
            "sentiment_applied": self.applied,
            "sentiment_reason": self.reason,
        }


def sentiment_confidence_adjustment(
    sentiment: Optional[SentimentState],
    *,
    direction: Optional[str],
    enabled: bool = True,
    max_age_seconds: float = 120.0,
    min_strength: float = 0.10,
    max_adjustment: float = 4.0,
    disagreement_penalty: Optional[float] = None,
    now: Optional[float] = None,
) -> SentimentAdjustment:
    """Return a bounded confidence adjustment in *confidence points*.

    Rules, all of which are hard requirements:

    *  Disabled, missing, stale, or weak sentiment → **0.0**. Absence of evidence
       is never penalised; the engine simply proceeds on OHLC alone.
    *  Agreement (sentiment bias points the same way as the chosen direction) adds
       at most `max_adjustment` points, scaled by how strong the bias is.
    *  Disagreement applies at most `disagreement_penalty` (default: half of
       `max_adjustment`) and **never reverses the signal** — it can only shave a
       few points off confidence, and the caller still clamps to [0, 100].
    *  The result is always inside [-cap, +cap].
    """
    cap = abs(float(max_adjustment))
    if disagreement_penalty is None:
        disagreement_penalty = cap / 2.0
    disagreement_penalty = abs(float(disagreement_penalty))

    if not enabled:
        return SentimentAdjustment(0.0, False, "disabled")
    if sentiment is None or not sentiment.available:
        return SentimentAdjustment(0.0, False, "missing")

    now = float(now if now is not None else time.time())
    if sentiment.stale or (now - sentiment.timestamp) > float(max_age_seconds):
        return SentimentAdjustment(0.0, False, "stale")
    if sentiment.strength < float(min_strength):
        return SentimentAdjustment(0.0, False, "weak")
    if direction is None:
        return SentimentAdjustment(0.0, False, "no_direction")

    bias = float(sentiment.bias)
    is_call = str(direction).upper() in ("CALL", "BUY", "UP", "HIGHER", "1")
    agrees = (bias > 0) if is_call else (bias < 0)

    if agrees:
        # Scale linearly from min_strength up to a full-strength bias, then cap.
        span = max(1e-9, 1.0 - float(min_strength))
        scale = (sentiment.strength - float(min_strength)) / span
        scale = max(0.0, min(1.0, scale))
        return SentimentAdjustment(round(cap * scale, 4), True, "agrees")

    # Disagreement: small bounded penalty only. It can never flip the direction.
    span = max(1e-9, 1.0 - float(min_strength))
    scale = (sentiment.strength - float(min_strength)) / span
    scale = max(0.0, min(1.0, scale))
    return SentimentAdjustment(round(-disagreement_penalty * scale, 4), True, "disagrees")


# ────────────────────────────────────────────────────────────────────────────
# The combined feature bundle
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class MarketFeatures:
    """Everything the signal engine needs beyond the existing indicator frame.

    Extends the existing data flow rather than replacing it: `indicators` stays the
    enriched DataFrame produced by `IndicatorCache.get_enriched()`, and this bundle
    only adds the activity/sentiment/quality context that has no column to live in.
    """

    asset: str
    timeframe: str
    candle_ts: float = 0.0
    price: float = 0.0
    indicators: Optional[pd.DataFrame] = None
    tick: TickActivity = field(default_factory=TickActivity)
    sentiment: SentimentState = field(default_factory=SentimentState)
    quality: QualityReport = field(default_factory=QualityReport)

    def price_action(self) -> Dict[str, float]:
        """Latest row of the price-action columns, as plain floats."""
        df = self.indicators
        if df is None or len(df) == 0:
            return {}
        row = df.iloc[-1]
        out: Dict[str, float] = {}
        for name in PRICE_ACTION_FEATURES:
            if name in df.columns:
                try:
                    v = float(row[name])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(v):
                    out[name] = v
        return out

    def flat(self) -> Dict[str, Any]:
        """Flat, JSON-safe view for structured logging."""
        out: Dict[str, Any] = {
            "asset": self.asset,
            "timeframe": self.timeframe,
            "candle_ts": self.candle_ts,
            "price": self.price,
            "data_quality": self.quality.value,
        }
        out.update(self.tick.as_dict())
        out.update(self.sentiment.as_dict())
        return out
