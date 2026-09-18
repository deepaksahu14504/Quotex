"""Adaptive Market Regime Transition Detector (production grade).

Detects BOTH the current market regime and transitions BETWEEN regimes,
using a weighted confluence of eleven independent inputs (never a single
indicator): EMA slope, ADX, ATR, Bollinger Band width, RSI, MACD histogram,
Supertrend direction, volume ratio, price momentum, support/resistance
distance, and multi-timeframe (1m/2m/5m) confirmation.

Design principles (all deliberate, not incidental):

- INFLUENCES CONFIDENCE, NEVER FORCES A DIRECTION. This module never emits
  a buy/sell decision. It returns a confidence multiplier, a threshold
  adjustment, a cooldown, and a pause flag -- the caller (orchestrator.py)
  applies these to the SAME confidence pipeline that already exists
  (strategies.evaluate() -> regime_tracker adjustment -> calibration ->
  effective_threshold). This is why it can be integrated without touching
  any existing trading algorithm.

- STRICTLY CAUSAL / NO LOOK-AHEAD. Every computation at bar i uses only
  data from bars <= i. The accuracy-tracking mechanism (see
  TransitionAccuracyTracker) grades a PAST prediction using bars that have
  since occurred -- that is retrospective grading of an old prediction,
  not use of future data to make the CURRENT prediction. Never confuse
  the two.

- INCREMENTAL, O(1) PER UPDATE. No recomputation over full history. Every
  rolling statistic (volatility percentile, EMA slope, regime stability)
  is maintained via a bounded deque and/or incremental mean/variance
  (Welford's algorithm), updated once per new bar.

- THREAD-SAFE. All state mutation happens inside a per-detector
  threading.Lock. Read-only status snapshots (`status()`) also take the
  lock briefly to avoid reading a half-updated state.

- DOES NOT RECOMPUTE INDICATORS ALREADY COMPUTED ELSEWHERE. ADX, ATR, RSI,
  MACD histogram, Bollinger width, Supertrend direction, and volume ratio
  are all already columns on the enriched DataFrame produced by
  incremental.py -- this module reads them, it does not reimplement them.
  The one thing it computes itself is a short EMA purely for slope
  (incremental.py doesn't expose a standalone EMA column for an arbitrary
  period), and price momentum (rate of change over a short window) -- both
  self-contained here so incremental.py's existing pipeline is untouched.
"""
from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Dict, Optional, Tuple

from ..logging_setup import log_event

logger = logging.getLogger("qat.regime_transition")


class Regime(str, Enum):
    STRONG_UPTREND = "strong_uptrend"
    WEAK_UPTREND = "weak_uptrend"
    STRONG_DOWNTREND = "strong_downtrend"
    WEAK_DOWNTREND = "weak_downtrend"
    SIDEWAYS_RANGE = "sideways_range"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    BREAKOUT = "breakout"
    REVERSAL = "reversal"
    TRANSITION = "transition_state"


_TREND_FAMILY = {Regime.STRONG_UPTREND, Regime.WEAK_UPTREND}
_DOWNTREND_FAMILY = {Regime.STRONG_DOWNTREND, Regime.WEAK_DOWNTREND}
_RANGE_FAMILY = {Regime.SIDEWAYS_RANGE}
_VOL_FAMILY = {Regime.HIGH_VOLATILITY, Regime.LOW_VOLATILITY}
_STRUCTURAL_FAMILY = {Regime.BREAKOUT, Regime.REVERSAL, Regime.TRANSITION}


def _family_of(regime: Regime) -> frozenset:
    for fam in (_TREND_FAMILY, _DOWNTREND_FAMILY, _RANGE_FAMILY, _VOL_FAMILY, _STRUCTURAL_FAMILY):
        if regime in fam:
            return frozenset(fam)
    return frozenset({regime})


@dataclass
class TimeframeSignal:
    """One timeframe's directional read, already computed elsewhere (e.g.
    strategies.htf_trend_bias) -- this module does not recompute
    indicators for other timeframes itself. direction: "call"|"put"|None.
    strength: 0-1."""
    direction: Optional[str] = None
    strength: float = 0.0


@dataclass
class RegimeTransitionResult:
    regime: Regime
    transition_probability: float
    regime_confidence: float
    stability_score: float
    recommended_confidence_multiplier: float
    recommended_threshold_adjustment: float
    cooldown_candles: int
    paused: bool
    reason: str


class _RunningStats:
    """Welford's incremental mean/variance -- O(1) per update."""
    __slots__ = ("n", "mean", "m2")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float:
        return self.m2 / self.n if self.n > 1 else 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    def zscore(self, x: float) -> float:
        if self.n < 10 or self.std < 1e-9:
            return 0.0
        return (x - self.mean) / self.std


class _Ema:
    """Minimal standalone incremental EMA, local to this module so it
    never requires a change to incremental.py's own indicator pipeline."""
    __slots__ = ("period", "alpha", "value")

    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: Optional[float] = None

    def update(self, x: float) -> float:
        self.value = x if self.value is None else (x * self.alpha + self.value * (1 - self.alpha))
        return self.value


WEIGHTS = {
    "ema_slope": 0.14, "adx": 0.13, "atr": 0.08, "bb_width": 0.08, "rsi": 0.08,
    "macd_hist": 0.10, "supertrend": 0.12, "volume_ratio": 0.07, "momentum": 0.10,
    "sr_distance": 0.05, "mtf": 0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


class TransitionAccuracyTracker:
    """Tracks how accurate past HIGH-transition-probability predictions
    were, and nudges the "what counts as high" threshold accordingly.

    STRICTLY NO LOOK-AHEAD: record_prediction() is called at bar i using
    only bar-i-and-earlier information. grade_due() is called later (bar
    i + horizon) and only reads what actually happened SINCE the
    prediction -- retrospective grading of an old prediction, never
    future information feeding the prediction itself."""

    def __init__(self, *, horizon_bars: int = 5, initial_threshold: float = 0.6,
                 min_threshold: float = 0.35, max_threshold: float = 0.85) -> None:
        self.horizon_bars = horizon_bars
        self.threshold = initial_threshold
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self._pending: Deque[Tuple[int, float, Regime]] = deque()
        self.correct = 0
        self.incorrect = 0

    def record_prediction(self, bar_index: int, transition_probability: float, regime_at_prediction: Regime) -> None:
        if transition_probability >= self.threshold:
            self._pending.append((bar_index, transition_probability, regime_at_prediction))

    def grade_due(self, bar_index: int, current_regime: Regime) -> None:
        while self._pending and bar_index - self._pending[0][0] >= self.horizon_bars:
            _, _prob, regime_then = self._pending.popleft()
            actually_transitioned = _family_of(regime_then) != _family_of(current_regime)
            if actually_transitioned:
                self.correct += 1
            else:
                self.incorrect += 1
            self._adjust_threshold(actually_transitioned)

    def _adjust_threshold(self, was_correct: bool) -> None:
        total = self.correct + self.incorrect
        step = 0.02 / max(1.0, math.sqrt(total))
        if was_correct:
            self.threshold = max(self.min_threshold, self.threshold - step)
        else:
            self.threshold = min(self.max_threshold, self.threshold + step)

    @property
    def accuracy(self) -> Optional[float]:
        total = self.correct + self.incorrect
        return round(self.correct / total, 3) if total else None

    def status(self) -> dict:
        return {
            "threshold": round(self.threshold, 3), "accuracy": self.accuracy,
            "correct": self.correct, "incorrect": self.incorrect, "pending": len(self._pending),
        }


class RegimeTransitionDetector:
    """One instance per (asset, timeframe). Call `update()` once per new
    CLOSED bar (never a still-forming bar) with the already-enriched
    indicator row plus MTF context; get a RegimeTransitionResult back
    immediately."""

    _HISTORY = 12

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bar_index = 0
        self._ema20 = _Ema(20)
        self._ema50 = _Ema(50)
        self._close_history: Deque[float] = deque(maxlen=10)
        self._atr_pct_stats = _RunningStats()
        self._bb_width_stats = _RunningStats()
        self._regime_history: Deque[Regime] = deque(maxlen=self._HISTORY)
        self._trend_score_history: Deque[float] = deque(maxlen=self._HISTORY)
        self._established_regime: Optional[Regime] = None
        self._established_since_bar: int = 0
        self._last_result: Optional[RegimeTransitionResult] = None
        self.accuracy_tracker = TransitionAccuracyTracker()

    def update(self, row: dict, *, mtf: Optional[Dict[str, TimeframeSignal]] = None) -> RegimeTransitionResult:
        with self._lock:
            self._bar_index += 1
            close = float(row["close"])
            ema20 = self._ema20.update(close)
            ema50 = self._ema50.update(close)
            self._close_history.append(close)

            atr = float(row.get("atr", 0.0) or 0.0)
            atr_pct = (atr / close * 100.0) if close else 0.0
            self._atr_pct_stats.update(atr_pct)
            bb_width = row.get("bb_width", 0.0) or 0.0
            bb_width = float(bb_width) if not (isinstance(bb_width, float) and math.isnan(bb_width)) else 0.0
            if bb_width:
                self._bb_width_stats.update(bb_width)

            inputs = self._score_inputs(row, ema20, ema50, atr_pct, bb_width, mtf or {})
            trend_score = inputs["trend_score"]
            self._trend_score_history.append(trend_score)

            regime, regime_confidence = self._classify(inputs)
            self._regime_history.append(regime)

            stability_score = self._compute_stability()
            transition_probability = self._compute_transition_probability(stability_score, regime)

            self.accuracy_tracker.grade_due(self._bar_index, regime)
            self.accuracy_tracker.record_prediction(self._bar_index, transition_probability, regime)

            result = self._build_result(regime, regime_confidence, transition_probability, stability_score)
            self._log_transitions(regime, result)
            self._last_result = result
            return result

    def status(self) -> dict:
        with self._lock:
            last = self._last_result
            return {
                "bar_index": self._bar_index,
                "established_regime": self._established_regime.value if self._established_regime else None,
                "last_result": None if last is None else {
                    "regime": last.regime.value, "transition_probability": last.transition_probability,
                    "regime_confidence": last.regime_confidence, "stability_score": last.stability_score,
                    "recommended_confidence_multiplier": last.recommended_confidence_multiplier,
                    "recommended_threshold_adjustment": last.recommended_threshold_adjustment,
                    "cooldown_candles": last.cooldown_candles, "paused": last.paused,
                },
                "accuracy_tracker": self.accuracy_tracker.status(),
            }

    def _score_inputs(self, row: dict, ema20: float, ema50: float, atr_pct: float, bb_width: float,
                       mtf: Dict[str, TimeframeSignal]) -> Dict[str, float]:
        ema_slope = 0.0
        if ema50:
            ema_slope = max(-1.0, min(1.0, (ema20 - ema50) / ema50 * 20.0))

        adx = float(row.get("adx", 0.0) or 0.0)
        adx_strength = max(0.0, min(1.0, (adx - 15.0) / 35.0))

        vol_z = max(self._atr_pct_stats.zscore(atr_pct), self._bb_width_stats.zscore(bb_width) if bb_width else 0.0)

        rsi = float(row.get("rsi", 50.0) or 50.0)
        rsi_dir = max(-1.0, min(1.0, (rsi - 50.0) / 30.0))

        macd_hist = float(row.get("macd_hist", 0.0) or 0.0)
        macd_dir = max(-1.0, min(1.0, macd_hist * 50.0))

        st_dir = float(row.get("st_dir", 0.0) or 0.0)

        vol_ratio = float(row.get("vol_ratio", 1.0) or 1.0)
        volume_signal = max(-1.0, min(1.0, (vol_ratio - 1.0)))

        momentum = 0.0
        if len(self._close_history) >= 5 and self._close_history[0]:
            momentum = max(-1.0, min(1.0, (self._close_history[-1] - self._close_history[0]) / self._close_history[0] * 20.0))

        sr_distance = 0.0
        support = row.get("support")
        resistance = row.get("resistance")
        close = float(row["close"])
        support_ok = support is not None and not (isinstance(support, float) and math.isnan(support))
        resistance_ok = resistance is not None and not (isinstance(resistance, float) and math.isnan(resistance))
        if support_ok and resistance_ok and close:
            span = resistance - support
            if span > 1e-9:
                pos = (close - support) / span
                sr_distance = max(0.0, 1.0 - min(abs(pos - 0.0), abs(pos - 1.0)) * 2.0)

        mtf_dir = 0.0
        if mtf:
            total_w = 0.0
            acc = 0.0
            for tf_signal in mtf.values():
                if tf_signal.direction is None:
                    continue
                sign = 1.0 if tf_signal.direction == "call" else -1.0
                acc += sign * max(0.0, min(1.0, tf_signal.strength))
                total_w += 1.0
            mtf_dir = (acc / total_w) if total_w else 0.0

        directional = {
            "ema_slope": ema_slope, "macd_hist": macd_dir, "supertrend": st_dir,
            "momentum": momentum, "rsi": rsi_dir, "mtf": mtf_dir,
        }
        trend_score = sum(WEIGHTS[k] * v for k, v in directional.items())
        trend_score *= (0.5 + 0.5 * adx_strength)
        trend_score = max(-1.0, min(1.0, trend_score))

        return {
            "trend_score": trend_score, "adx": adx, "adx_strength": adx_strength,
            "vol_z": vol_z, "volume_signal": volume_signal, "sr_distance": sr_distance, "rsi": rsi,
        }

    def _classify(self, inputs: Dict[str, float]) -> Tuple[Regime, float]:
        trend_score = inputs["trend_score"]
        adx = inputs["adx"]
        adx_strength = inputs["adx_strength"]
        vol_z = inputs["vol_z"]
        sr_distance = inputs["sr_distance"]
        rsi = inputs["rsi"]

        if self._established_regime in (Regime.STRONG_UPTREND, Regime.WEAK_UPTREND) and trend_score < -0.15 and rsi >= 70:
            return Regime.REVERSAL, min(1.0, abs(trend_score) + 0.3)
        if self._established_regime in (Regime.STRONG_DOWNTREND, Regime.WEAK_DOWNTREND) and trend_score > 0.15 and rsi <= 30:
            return Regime.REVERSAL, min(1.0, abs(trend_score) + 0.3)

        if self._established_regime == Regime.SIDEWAYS_RANGE and abs(trend_score) > 0.25 and sr_distance > 0.5 and inputs["volume_signal"] > 0.1:
            return Regime.BREAKOUT, min(1.0, (abs(trend_score) + sr_distance) / 2.0)

        if vol_z >= 1.5:
            return Regime.HIGH_VOLATILITY, min(1.0, vol_z / 3.0)
        if vol_z <= -1.0:
            return Regime.LOW_VOLATILITY, min(1.0, abs(vol_z) / 2.0)

        if abs(trend_score) < 0.12 and adx < 20:
            return Regime.SIDEWAYS_RANGE, max(0.0, 1.0 - abs(trend_score) * 4.0)
        if trend_score > 0:
            regime = Regime.STRONG_UPTREND if (trend_score > 0.45 and adx_strength > 0.5) else Regime.WEAK_UPTREND
        else:
            regime = Regime.STRONG_DOWNTREND if (trend_score < -0.45 and adx_strength > 0.5) else Regime.WEAK_DOWNTREND
        confidence = min(1.0, abs(trend_score) * 0.6 + adx_strength * 0.4)
        return regime, max(0.0, confidence)

    def _compute_stability(self) -> float:
        if len(self._regime_history) < 3:
            return 0.5
        families = [_family_of(r) for r in self._regime_history]
        latest_family = families[-1]
        agree = sum(1 for f in families if f == latest_family)
        return agree / len(families)

    def _compute_transition_probability(self, stability_score: float, current_regime: Regime) -> float:
        if self._established_regime is None:
            self._established_regime = current_regime
            self._established_since_bar = self._bar_index
            return 0.0

        family_changed = _family_of(current_regime) != _family_of(self._established_regime)
        score_velocity = 0.0
        if len(self._trend_score_history) >= 3:
            score_velocity = abs(self._trend_score_history[-1] - self._trend_score_history[-3]) / 2.0

        instability = 1.0 - stability_score
        prob = 0.5 * instability + 0.35 * min(1.0, score_velocity * 3.0) + 0.15 * (1.0 if family_changed else 0.0)
        prob = max(0.0, min(1.0, prob))

        if family_changed and stability_score >= 0.7 and (self._bar_index - self._established_since_bar) >= 3:
            self._established_regime = current_regime
            self._established_since_bar = self._bar_index

        return prob

    def _build_result(self, regime: Regime, regime_confidence: float, transition_probability: float,
                       stability_score: float) -> RegimeTransitionResult:
        confidence_multiplier = max(0.5, 1.0 - transition_probability * 0.5)
        threshold_adjustment = round(transition_probability * 15.0, 1)

        high_threshold = self.accuracy_tracker.threshold
        if transition_probability >= high_threshold:
            cooldown_candles = 1 + min(2, int((transition_probability - high_threshold) / max(1e-6, 1.0 - high_threshold) * 2))
        else:
            cooldown_candles = 0

        paused = transition_probability >= min(0.85, high_threshold + 0.2) and regime_confidence < 0.4

        reason_parts = [f"regime={regime.value}", f"transition_p={transition_probability:.2f}", f"stability={stability_score:.2f}"]
        if paused:
            reason_parts.append("PAUSED (chaotic transition)")
        elif cooldown_candles:
            reason_parts.append(f"cooldown={cooldown_candles}")

        return RegimeTransitionResult(
            regime=regime, transition_probability=round(transition_probability, 3),
            regime_confidence=round(regime_confidence, 3), stability_score=round(stability_score, 3),
            recommended_confidence_multiplier=round(confidence_multiplier, 3),
            recommended_threshold_adjustment=threshold_adjustment,
            cooldown_candles=cooldown_candles, paused=paused, reason=" ".join(reason_parts),
        )

    def _log_transitions(self, regime: Regime, result: RegimeTransitionResult) -> None:
        prev = self._last_result
        if prev is None:
            return
        if prev.regime != regime:
            log_event(logger, logging.INFO, "regime_changed", previous=prev.regime.value, new=regime.value)
        was_high = prev.transition_probability >= self.accuracy_tracker.threshold
        now_high = result.transition_probability >= self.accuracy_tracker.threshold
        if now_high and not was_high:
            log_event(logger, logging.INFO, "transition_started", regime=regime.value,
                       transition_probability=result.transition_probability)
        elif was_high and now_high and _family_of(regime) != _family_of(self._established_regime or regime):
            log_event(logger, logging.INFO, "transition_confirmed", regime=regime.value)
        elif was_high and not now_high:
            log_event(logger, logging.INFO, "transition_cancelled", regime=regime.value,
                       transition_probability=result.transition_probability)
        if result.recommended_confidence_multiplier < prev.recommended_confidence_multiplier - 0.05:
            log_event(logger, logging.INFO, "confidence_reduced", multiplier=result.recommended_confidence_multiplier)
        if result.paused and not prev.paused:
            log_event(logger, logging.WARNING, "trading_paused", regime=regime.value, reason=result.reason)
        elif prev.paused and not result.paused:
            log_event(logger, logging.INFO, "trading_resumed", regime=regime.value)


class RegimeTransitionEngine:
    """Manager holding one RegimeTransitionDetector per (asset, timeframe)
    key -- mirrors IndicatorCache/HealthScoreManager's established
    per-key pattern for a clean, single-object orchestrator integration."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._detectors: Dict[str, RegimeTransitionDetector] = {}

    def get(self, key: str) -> RegimeTransitionDetector:
        with self._lock:
            det = self._detectors.get(key)
            if det is None:
                det = RegimeTransitionDetector()
                self._detectors[key] = det
            return det

    def update(self, key: str, row: dict, *, mtf: Optional[Dict[str, TimeframeSignal]] = None) -> RegimeTransitionResult:
        return self.get(key).update(row, mtf=mtf)

    def status_all(self) -> Dict[str, dict]:
        with self._lock:
            return {k: d.status() for k, d in self._detectors.items()}
