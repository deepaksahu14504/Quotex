"""Confidence calibration.

The strategy engine's confidence score is a hand-tuned confluence formula
(how many strategies agree, weighted by higher-timeframe bias) — it was
never guaranteed to mean "this trade has an X% chance of winning" in a
statistically calibrated sense. This module checks that claim against real
trade history and corrects it: if signals the engine scored around 80%
actually won 65% of the time historically, a new 80%-scored signal gets
recalibrated down to ~65% before it's compared against your confidence
threshold — so the threshold you set means what it says.

Method: bucket closed trades by raw confidence (5-point bins), compute each
bucket's real win rate, then run isotonic regression (pool-adjacent-
violators) over the bucket sequence. Isotonic regression is the standard,
simple, assumption-light way to calibrate a score into a probability: it
finds the best-fitting *monotonically non-decreasing* curve through the
bucket win rates — enforcing the one thing that must always be true (higher
raw confidence can never calibrate to a lower probability) without assuming
any particular functional shape.

Needs a real sample before it does anything: with too little history it's a
pass-through (raw confidence used as-is) rather than overfitting noise from
a handful of trades.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..logging_setup import log_event
from .analytics_engine import wilson_interval

logger = logging.getLogger("qat.calibration")

MIN_SAMPLES_TOTAL = 30       # need at least this many closed trades before calibrating at all --
                             # same n>=30 standard now used consistently across strategy_manager.py's
                             # MIN_SAMPLE, pattern_risk_validation.py, and rejection_reconciliation.py (Phase 6)
BUCKET_SIZE = 5              # confidence grouped into 5-point buckets: 50-54, 55-59, ...
REFRESH_EVERY_N_TRADES = 10  # recompute the curve after this many new closed trades
REFRESH_MAX_AGE = 3600.0     # ...or after this long, whichever comes first


def _pava(values: List[float], weights: List[float]) -> List[float]:
    """Pool-Adjacent-Violators: the standard isotonic regression algorithm.
    Returns a monotonically non-decreasing sequence, same length as input,
    that's the best weighted least-squares fit to `values` subject to that
    ordering constraint."""
    blocks: List[List[float]] = []  # [weighted_value_sum, weight_sum, count]
    for v, w in zip(values, weights):
        blocks.append([v * w, w, 1])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            b2 = blocks.pop()
            b1 = blocks.pop()
            blocks.append([b1[0] + b2[0], b1[1] + b2[1], b1[2] + b2[2]])
    out: List[float] = []
    for total_v, total_w, count in blocks:
        out.extend([total_v / total_w] * count)
    return out


@dataclass
class BucketPoint:
    bucket_start: int
    raw_trades: int
    raw_win_rate: float
    calibrated_win_rate: float


def _build_curve(closed: list) -> Tuple[Dict[int, float], List[BucketPoint]]:
    """Shared bucket+PAVA logic used for both the global curve and each
    per-regime segment -- extracted so P4's segmentation doesn't duplicate
    this block three times."""
    buckets: Dict[int, List[int]] = {}
    for t in closed:
        _raw_conf = getattr(t, "raw_confidence", None)
        raw = _raw_conf if _raw_conf is not None else t.confidence
        b = (int(raw) // BUCKET_SIZE) * BUCKET_SIZE
        status = str(getattr(t.status, "value", t.status))
        buckets.setdefault(b, []).append(1 if status == "win" else 0)

    sorted_buckets = sorted(buckets.keys())
    raw_rates = [sum(buckets[b]) / len(buckets[b]) for b in sorted_buckets]
    weights = [float(len(buckets[b])) for b in sorted_buckets]
    calibrated = _pava(raw_rates, weights)

    curve = dict(zip(sorted_buckets, calibrated))
    points = [
        BucketPoint(bucket_start=b, raw_trades=len(buckets[b]),
                    raw_win_rate=round(raw_rates[i] * 100, 1),
                    calibrated_win_rate=round(calibrated[i] * 100, 1))
        for i, b in enumerate(sorted_buckets)
    ]
    return curve, points


def _interpolate(curve: Dict[int, float], raw_confidence: int) -> float:
    """Shared lookup/interpolation logic for a single (bucket -> calibrated
    rate) curve -- reused per-regime and for the global fallback."""
    b = (int(raw_confidence) // BUCKET_SIZE) * BUCKET_SIZE
    buckets = sorted(curve.keys())
    if b in curve:
        return curve[b]
    if b < buckets[0]:
        return curve[buckets[0]]
    if b > buckets[-1]:
        return curve[buckets[-1]]
    lower = max(x for x in buckets if x <= b)
    upper = min(x for x in buckets if x >= b)
    if lower == upper:
        return curve[lower]
    frac = (b - lower) / (upper - lower)
    return curve[lower] + frac * (curve[upper] - curve[lower])


def _brier_score(curve: Dict[int, float], trades: list) -> Optional[float]:
    """Mean squared error between predicted win probability (from `curve`,
    via the same interpolation calibrate() uses) and actual outcome (1=win,
    0=loss) -- the standard proper scoring rule for a probability
    calibration. Lower is better; 0 is perfect. Returns None if there's
    nothing to score against."""
    if not curve or not trades:
        return None
    errors = []
    for t in trades:
        _rc = getattr(t, "raw_confidence", None)
        raw = _rc if _rc is not None else t.confidence
        status = str(getattr(t.status, "value", t.status))
        outcome = 1.0 if status == "win" else 0.0
        predicted = _interpolate(curve, int(raw))
        errors.append((predicted - outcome) ** 2)
    return sum(errors) / len(errors)


@dataclass
class ConfidenceCalibrator:
    # Global curve -- every closed trade, regardless of regime. Used as-is
    # when a regime isn't provided, and as the fallback when a specific
    # regime segment doesn't have enough samples of its own yet (P4).
    _curve: Dict[int, float] = field(default_factory=dict)
    _points: List[BucketPoint] = field(default_factory=list)
    # Per-regime curves (P4): a trending 80%-confidence signal and a choppy
    # 80%-confidence signal can have substantially different true win rates;
    # blending them into one global curve averages that structure away.
    _regime_curves: Dict[str, Dict[int, float]] = field(default_factory=dict)
    _regime_points: Dict[str, List[BucketPoint]] = field(default_factory=dict)
    _built: bool = False
    _last_built_at: float = 0.0
    _last_built_count: int = 0
    total_samples: int = 0

    calibration_history: List[dict] = field(default_factory=list)  # bounded log of every
    # refresh attempt's train/holdout sizes, old vs. new Brier score, and the adopt/
    # rollback decision -- Phase 6 visibility into whether refreshes are actually helping.
    _MAX_HISTORY = 50

    # Tunable via TradingSettings (Admin Settings UI) -- see apply_settings() in
    # orchestrator.py for how these get updated live without a restart.
    min_samples_total: int = MIN_SAMPLES_TOTAL          # need at least this many closed
                                                         # trades before calibrating at all
    refresh_every_n_trades: int = REFRESH_EVERY_N_TRADES  # recompute after this many new closed trades
    refresh_max_age: float = REFRESH_MAX_AGE              # ...or after this long, whichever comes first
    holdout_validation_enabled: bool = True   # Phase 6's train/holdout Brier-score validation
                                               # before adopting a refresh. Off reverts to the
                                               # pre-Phase-6 behavior of always adopting directly.
    rollback_on_worse_enabled: bool = True    # only meaningful when holdout_validation_enabled is
                                               # True -- if False, a worse-scoring candidate is still
                                               # logged in calibration_history but adopted anyway

    def needs_refresh(self, current_closed_count: int) -> bool:
        if not self._built:
            return current_closed_count >= self.min_samples_total
        if current_closed_count - self._last_built_count >= self.refresh_every_n_trades:
            return True
        return (time.time() - self._last_built_at) >= self.refresh_max_age

    def refresh(self, trades) -> None:
        """`trades` is any iterable of objects with `.status` ("win"/"loss"/
        other), `.raw_confidence` or `.confidence` (int 0-100), and
        optionally `.regime` (str) for the per-regime segmentation (P4).

        Validates each candidate refresh against the CURRENT production
        curve on a held-out slice before adopting it (Phase 6) -- a
        refresh that would make calibration measurably worse is rolled
        back instead of blindly applied, closing the "trusting an
        unvalidated refresh" gap flagged in the earlier audit.
        """
        closed = [
            t for t in trades
            if getattr(t, "status", None) is not None
            and str(getattr(t.status, "value", t.status)) in ("win", "loss")
            and (getattr(t, "raw_confidence", None) or getattr(t, "confidence", None)) is not None
        ]
        self.total_samples = len(closed)
        self._last_built_at = time.time()
        self._last_built_count = len(closed)
        if len(closed) < self.min_samples_total:
            self._built = False
            self._curve = {}
            self._points = []
            self._regime_curves = {}
            self._regime_points = {}
            return

        # Deterministic 80/20 holdout split -- every 5th trade (by position
        # in the already-time-ordered `closed` list), not random, so this
        # is reproducible and doesn't need an RNG dependency.
        holdout = closed[4::5]
        train = [t for i, t in enumerate(closed) if i % 5 != 4]

        if self.holdout_validation_enabled and len(train) >= self.min_samples_total and holdout:
            candidate_curve, _candidate_points = _build_curve(train)
            new_brier = _brier_score(candidate_curve, holdout)
            old_brier = _brier_score(self._curve, holdout) if self._built else None
            # Adopt if there's no existing curve to compare against, or the
            # candidate isn't meaningfully worse (5% tolerance so two
            # near-identical curves don't thrash the decision on noise), or
            # rollback has been explicitly disabled (still logged either way).
            worse = old_brier is not None and new_brier is not None and new_brier > old_brier * 1.05
            adopt = (not worse) or (not self.rollback_on_worse_enabled)
            self.calibration_history.append({
                "at": self._last_built_at, "train_n": len(train), "holdout_n": len(holdout),
                "old_brier": round(old_brier, 4) if old_brier is not None else None,
                "new_brier": round(new_brier, 4) if new_brier is not None else None,
                "decision": "adopted" if adopt else "rolled_back",
            })
            self.calibration_history = self.calibration_history[-self._MAX_HISTORY:]
            if not adopt:
                log_event(
                    logger, logging.WARNING, "calibration_refresh_rolled_back",
                    old_brier=old_brier, new_brier=new_brier, train_n=len(train), holdout_n=len(holdout),
                )
                return  # keep the existing self._curve/_points/_built exactly as they were
            log_event(
                logger, logging.INFO, "calibration_refresh_adopted",
                old_brier=old_brier, new_brier=new_brier, train_n=len(train), holdout_n=len(holdout),
            )
        # Either validation wasn't possible (not enough for a holdout split
        # yet -- only happens right at the min_samples_total boundary) or it
        # passed: build the FINAL production curve on the FULL closed set
        # (train+holdout combined) -- the holdout's only job was validating
        # the decision, not permanently withholding data from production.
        self._curve, self._points = _build_curve(closed)
        self._built = True

        # Per-regime segments: only build a segment's own curve once it has
        # its own min_samples_total -- thin segments fall back to the global
        # curve in calibrate() rather than overfitting a handful of trades.
        by_regime: Dict[str, list] = {}
        for t in closed:
            regime = getattr(t, "regime", None)
            if regime:
                by_regime.setdefault(str(regime), []).append(t)

        regime_curves: Dict[str, Dict[int, float]] = {}
        regime_points: Dict[str, List[BucketPoint]] = {}
        for regime, regime_trades in by_regime.items():
            if len(regime_trades) < self.min_samples_total:
                continue
            regime_curves[regime], regime_points[regime] = _build_curve(regime_trades)
        self._regime_curves = regime_curves
        self._regime_points = regime_points
        log_event(
            logger, logging.INFO, "calibration_curve_rebuilt",
            total_samples=self.total_samples, regime_segments=list(regime_curves.keys()),
            reason="scheduled_refresh_sufficient_history",
        )

    def calibrate(self, raw_confidence: int, regime: Optional[str] = None) -> int:
        """Maps a raw confluence score to a calibrated 0-100 value. Falls
        back to the raw score untouched until there's enough real history,
        and falls back from a thin regime-specific segment to the global
        curve (P4) rather than the uncalibrated raw score."""
        if not self._built or not self._curve:
            log_event(
                logger, logging.DEBUG, "calibration_passthrough",
                previous_value=raw_confidence, new_value=raw_confidence,
                total_samples=self.total_samples, min_samples_required=self.min_samples_total,
                reason="not_yet_built_insufficient_history",
            )
            return raw_confidence
        curve = self._curve
        used_regime = None
        if regime and regime in self._regime_curves:
            curve = self._regime_curves[regime]
            used_regime = regime
        val = _interpolate(curve, raw_confidence)
        calibrated = int(round(max(0.0, min(100.0, val * 100))))
        log_event(
            logger, logging.DEBUG, "calibration_applied",
            previous_value=raw_confidence, new_value=calibrated,
            regime_requested=regime, regime_curve_used=used_regime,
            total_samples=self.total_samples,
            reason="regime_specific_curve" if used_regime else "global_curve",
        )
        return calibrated

    def confidence_interval(self, raw_confidence: int, regime: Optional[str] = None) -> Optional[Tuple[float, float]]:
        """Wilson bound (as percentages, 0-100 scale) for the bucket
        `raw_confidence` falls into, using that bucket's own sample count
        and raw win rate -- NOT the smoothed/interpolated PAVA output, since
        the interval should reflect actual observed evidence for that
        bucket, not the monotonic-regression fit. Returns None if no bucket
        has been built yet (same not-enough-history gate as calibrate())."""
        points = self._points
        if regime and regime in self._regime_points and self._regime_points[regime]:
            points = self._regime_points[regime]
        if not points:
            return None
        b = (int(raw_confidence) // BUCKET_SIZE) * BUCKET_SIZE
        # Nearest bucket by start (handles a raw_confidence outside any
        # populated bucket's exact range, same clamping spirit as _interpolate).
        closest = min(points, key=lambda p: abs(p.bucket_start - b))
        wins = int(round((closest.raw_win_rate / 100.0) * closest.raw_trades))
        lower, upper = wilson_interval(wins, closest.raw_trades)
        return (round(lower * 100, 1), round(upper * 100, 1))

    def status_for_ui(self) -> dict:
        return {
            "built": self._built,
            "total_samples": self.total_samples,
            "min_samples_required": self.min_samples_total,
            "points": [
                {"bucket": f"{p.bucket_start}-{p.bucket_start + BUCKET_SIZE - 1}",
                 "trades": p.raw_trades, "raw_win_rate": p.raw_win_rate,
                 "calibrated_win_rate": p.calibrated_win_rate}
                for p in self._points
            ],
            "regime_segments": {
                regime: [
                    {"bucket": f"{p.bucket_start}-{p.bucket_start + BUCKET_SIZE - 1}",
                     "trades": p.raw_trades, "raw_win_rate": p.raw_win_rate,
                     "calibrated_win_rate": p.calibrated_win_rate}
                    for p in pts
                ]
                for regime, pts in self._regime_points.items()
            },
            "calibration_history": self.calibration_history,
        }
