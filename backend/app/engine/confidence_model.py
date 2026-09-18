"""Data-driven confidence model -- Phase 5.

Replaces the hand-tuned linear formula in strategies.py
(`confidence = 44 + 15.5*net + confluence_bonus`, plus several more
hand-set adjustments for directional_edge tiers, candle body ratio, ATR,
and MTF agreement) with a logistic regression fit against REALIZED
outcomes -- same inputs, same shape (a linear combination feeding a
probability), coefficients estimated from data instead of guessed.

This is deliberately NOT meta-labeling (a separate bet-sizing model on top
of the direction call) -- it's narrower: refit the existing formula's own
inputs with justified coefficients. The output still passes through the
existing isotonic calibration layer (calibration.py) afterward; that layer
fixes any residual miscalibration of THIS model's output against outcomes,
while this model fixes the raw score's shape and which inputs matter how
much. The two are complementary, not redundant -- removing either leaves a
gap the other doesn't cover.

Training data comes from joining DecisionStore (Phase 1) accepted
decisions with their real TradeRecord outcomes via decision_id (Signal ->
TradeRecord.decision_id). The hand-tuned formula in strategies.py remains
in place as the fallback whenever this model doesn't have enough samples
to trust yet (MIN_SAMPLES_TO_TRAIN) -- there is always a usable confidence
score, never a gap.

Rollout: ship in shadow (compute both old and new confidence, log both,
trade on the OLD one) before cutting over -- same pattern shadow_mode.py
already established for "prove it before you trust it."
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..logging_setup import log_event

logger = logging.getLogger("qat.confidence_model")

# Same feature set the hand-tuned formula in strategies.py already uses --
# this model doesn't invent new inputs, it re-weights the existing ones.
# Same feature set the hand-tuned formula in strategies.py already uses --
# this model doesn't invent new inputs, it re-weights the existing ones.
# The last three (regime_transition_probability/regime_confidence/
# regime_stability) come from regime_transition_detector.py.
FEATURE_NAMES = [
    "net",                 # winning - losing weighted confluence score
    "win_n",                # count of agreeing strategies (replaces the
                            # hand-set confluence_bonus = min(8, (win_n-1)*4))
    "directional_edge",     # winning / (winning + losing)
    "body_ratio",           # |close-open| / (high-low)
    "atr_pct",              # ATR as % of price
    "htf_agree",            # +1 HTF agrees, -1 HTF disagrees, 0 no HTF data
    "hhtf_agree",           # same for HHTF (soft-penalty tier only; hard veto
                            # is a gate, untouched by this model)
    "is_trend",             # regime one-hot
    "is_range",             # regime one-hot (mixed = both 0)
    "regime_transition_probability",  # 0-1, from regime_transition_detector.py
    "regime_transition_confidence",   # 0-1, confidence in the current regime label
    "regime_stability_score",         # 0-1, consistency of recent regime classifications
]

MIN_SAMPLES_TO_TRAIN = 200   # below this, fit() refuses -- not enough data to
                             # estimate 9 coefficients + intercept reliably
MIN_SAMPLES_TO_TRUST = 300   # below this, predict_proba() returns None even if
                             # a model exists (a small-sample fit is noisy;
                             # this margin means retraining happens before
                             # the model is ever actually used)


def _standardize(X: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    safe_std = np.where(std < 1e-9, 1.0, std)
    return (X - mean) / safe_std


@dataclass
class ConfidenceModel:
    """Logistic regression over FEATURE_NAMES, fit via batch gradient
    descent with L2 regularization (dependency-free beyond numpy, already
    a project dependency). Features are standardized (zero mean, unit
    variance) before fitting for numerically stable convergence; the
    stored mean/std are needed at prediction time too, not just training.
    """
    weights: Optional[List[float]] = None      # length len(FEATURE_NAMES)+1, weights[0] = intercept
    feature_mean: Optional[List[float]] = None
    feature_std: Optional[List[float]] = None
    n_samples: int = 0
    trained_at: Optional[float] = None
    train_log_loss: Optional[float] = None      # in-sample -- NOT a held-out validation
                                                 # metric; Phase 6 adds proper calibration
                                                 # validation on fresh data, this is just
                                                 # a sanity number logged at fit time
    min_samples_to_train: int = MIN_SAMPLES_TO_TRAIN  # tunable via TradingSettings.confidence_model_min_training_samples
    min_samples_to_trust: int = MIN_SAMPLES_TO_TRUST  # tunable via TradingSettings.confidence_model_prediction_threshold

    def fit(self, X: List[List[float]], y: List[int], *, l2: float = 1.0, lr: float = 0.3, epochs: int = 1000) -> bool:
        """X: list of feature vectors (order matching FEATURE_NAMES), y:
        list of 0/1 outcomes (1 = win). Returns False (refuses to fit)
        below MIN_SAMPLES_TO_TRAIN -- too few samples to estimate this many
        coefficients without just fitting noise."""
        n = len(y)
        if n < self.min_samples_to_train:
            logger.info("[confidence_model] refusing to fit: %d samples < %d required", n, self.min_samples_to_train)
            return False
        X_arr = np.asarray(X, dtype=float)
        y_arr = np.asarray(y, dtype=float)
        mean = X_arr.mean(axis=0)
        std = X_arr.std(axis=0)
        X_std = _standardize(X_arr, mean, std)
        X_design = np.hstack([np.ones((n, 1)), X_std])  # intercept column

        w = np.zeros(X_design.shape[1])
        for _ in range(epochs):
            z = X_design @ w
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            grad = X_design.T @ (p - y_arr) / n
            # L2 regularization on every weight EXCEPT the intercept (index 0)
            reg = np.concatenate([[0.0], l2 * w[1:] / n])
            w -= lr * (grad + reg)

        z_final = X_design @ w
        p_final = 1.0 / (1.0 + np.exp(-np.clip(z_final, -30, 30)))
        eps = 1e-9
        log_loss = -np.mean(y_arr * np.log(p_final + eps) + (1 - y_arr) * np.log(1 - p_final + eps))

        self.weights = w.tolist()
        self.feature_mean = mean.tolist()
        self.feature_std = std.tolist()
        self.n_samples = n
        self.trained_at = time.time()
        self.train_log_loss = round(float(log_loss), 4)
        log_event(logger, logging.INFO, "confidence_model_fit", n_samples=n, train_log_loss=self.train_log_loss)
        return True

    def predict_proba(self, features: List[float]) -> Optional[float]:
        """Returns a win-probability estimate in [0,1], or None if there's
        no trustworthy fit yet (caller must fall back to the hand-tuned
        formula in strategies.py -- there is always a usable score).
        Also returns None if `features` doesn't match the length this
        model was actually fitted with (e.g. FEATURE_NAMES was extended
        since this model was last trained) -- safer to decline a
        prediction than to silently apply weights to the wrong features."""
        if self.weights is None or self.n_samples < self.min_samples_to_trust:
            return None
        if len(features) != len(self.weights) - 1:  # weights[0] is the intercept
            logger.warning(
                "[confidence_model] feature vector length %d does not match fitted "
                "model's %d -- refusing to predict until retrained",
                len(features), len(self.weights) - 1,
            )
            return None
        x = np.asarray(features, dtype=float)
        mean = np.asarray(self.feature_mean)
        std = np.asarray(self.feature_std)
        x_std = _standardize(x, mean, std)
        w = np.asarray(self.weights)
        z = w[0] + float(np.dot(w[1:], x_std))
        return float(1.0 / (1.0 + np.exp(-np.clip(z, -30, 30))))

    def coefficient_report(self) -> Dict[str, float]:
        """Human-readable mapping of feature name -> fitted (standardized)
        coefficient -- lets a user see which inputs the data actually
        supports weighting heavily, vs. the hand-tuned formula's guesses."""
        if self.weights is None:
            return {}
        return {"intercept": round(self.weights[0], 4), **{
            name: round(w, 4) for name, w in zip(FEATURE_NAMES, self.weights[1:])
        }}


class ConfidenceModelStore:
    """Persists one ConfidenceModel per user to disk -- small state (a
    couple dozen floats), same lightweight JSON pattern as the other new
    stores in this phase set."""

    def __init__(self, storage_path: str) -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.model = ConfidenceModel()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.model = ConfidenceModel(**raw)
        except Exception:
            logger.warning("[confidence_model] failed to load %s", self._path, exc_info=True)

    def save(self) -> None:
        try:
            self._path.write_text(json.dumps(self.model.__dict__, indent=2), encoding="utf-8")
        except Exception:
            logger.warning("[confidence_model] failed to persist %s", self._path, exc_info=True)


def extract_features(
    *, net: float, win_n: int, directional_edge: float, body_ratio: float, atr_pct: float,
    htf_agree: int = 0, hhtf_agree: int = 0, regime: str = "mixed",
    regime_transition_probability: float = 0.0, regime_transition_confidence: float = 0.0,
    regime_stability_score: float = 0.0,
) -> List[float]:
    """Single source of truth for building a feature vector in
    FEATURE_NAMES order -- used identically at both training-set
    construction time and live prediction time, so the two can never
    silently drift apart in feature ordering or meaning. The three
    regime_* parameters default to 0.0 (neutral/unknown) so existing
    callers that don't pass them keep working unchanged."""
    return [
        float(net), float(win_n), float(directional_edge), float(body_ratio), float(atr_pct),
        float(htf_agree), float(hhtf_agree),
        1.0 if regime == "trend" else 0.0, 1.0 if regime == "range" else 0.0,
        float(regime_transition_probability), float(regime_transition_confidence), float(regime_stability_score),
    ]


def build_training_set(
    decisions_by_id: Dict[str, "SignalDecision"], trades: List["TradeRecord"],
) -> Tuple[List[List[float]], List[int]]:
    """Joins DecisionStore records with real TradeRecord outcomes via
    decision_id. Only accepted decisions with a resolved win/loss trade are
    usable -- draws are excluded (same convention as every other win-rate
    computation in this codebase), and a decision needs its raw features
    attached (SignalDecision.raw_features, see decision_engine.py) or it's
    skipped. Pure function, no I/O -- caller assembles the inputs from
    whatever stores it has."""
    X: List[List[float]] = []
    y: List[int] = []
    for trade in trades:
        if trade.status.value not in ("win", "loss") or not trade.decision_id:
            continue
        decision = decisions_by_id.get(trade.decision_id)
        if decision is None or not decision.raw_features:
            continue
        try:
            features = [decision.raw_features[name] for name in FEATURE_NAMES]
        except KeyError:
            continue
        X.append(features)
        y.append(1 if trade.status.value == "win" else 0)
    return X, y
