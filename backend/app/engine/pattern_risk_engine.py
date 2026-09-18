"""Pattern Risk Engine (Loss Intelligence Modules 4, 5, 7, 10 -- consolidated).

READ-ONLY / ADVISORY ONLY. This module answers "how much does the current
market state resemble our historical LOSSES more than our historical WINS,
and what would a conservative response look like" -- it does not itself
change any stake, confidence, threshold, or reject/accept decision.
Nothing in orchestrator.py's scan/execute path calls this yet. Every
number this produces is a *recommendation* field on a report, not an
applied adjustment.

Why one module instead of four: the original spec described Pattern
Similarity (compare now vs. history), Intelligent Adaptive Response
(gradually adjust based on similarity), Pattern Memory (remember recurring
bad situations, self-heal when they stop being bad), and Self Optimization
(tighten/loosen automatically) as four separate systems. They are the same
underlying idea from four angles -- detect a recurring unfavorable
condition, respond proportionally, and let the response fade automatically
as new evidence arrives. Building four independent systems for one job is
exactly the "parallel adaptive systems" the brief said to avoid; this file
is that one job.

Core discipline (matching AnalyticsEngine): every signal is a comparison
against BOTH the win population and the loss population -- a feature that
is common in losses AND equally common in wins carries no weight. There is
no "loss-only" pattern matching anywhere in this file. Self-healing is a
direct, automatic consequence of this: as more wins accumulate under a
condition that used to be loss-leaning, the win population's mean shifts,
the comparison score for that condition shrinks – no separate "un-flagging"
mechanism or blacklist/whitelist bookkeeping is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import logging

from .analytics_engine import AnalyticsEngine
from ..logging_setup import log_event

logger = logging.getLogger("qat.pattern_risk")


# Continuous features compared via z-distance-to-population-mean. Each maps
# a human name to how to read it off a TradeFeatureSnapshot.
_CONTINUOUS_FEATURES = {
    "adx": "adx_at_entry",
    "atr_pct": "atr_pct_at_entry",
    "rsi": "rsi_at_entry",
    "volatility_percentile": "volatility_percentile",
    "volume_ratio": "volume_ratio_at_entry",
}
# Categorical features compared via AnalyticsEngine's existing significance-
# gated breakdowns (reused, not recomputed) -- these map to _dimension_values'
# dimension keys.
_CATEGORICAL_FEATURES = {
    "regime": "regime",
    "session": "session",
    "timeframe": "timeframe",
    "asset": "asset",
    "otc_or_live": "otc_or_live",
}

_MIN_FEATURE_SAMPLE = 5      # minimum count in EACH population before a continuous feature counts as evidence
_LEAN_EVIDENCE_THRESHOLD = 0.30  # |lean| above this counts as one independent piece of evidence


@dataclass
class SignalContribution:
    """One dimension's contribution to the overall score -- always
    reported, whether or not it ended up counting as "evidence" (below
    _LEAN_EVIDENCE_THRESHOLD), so the assessment is fully explainable."""
    dimension: str
    lean: float               # -1 (looks like historical wins) .. +1 (looks like historical losses), 0 = no signal
    detail: str
    is_evidence: bool


@dataclass
class RecommendedAdjustments:
    """ADVISORY ONLY. Nothing reads these values and applies them yet --
    they exist so a future, explicitly-approved integration stage has a
    concrete, bounded, already-designed target to wire in, rather than
    inventing the bounds at integration time. Every bound below is a
    conservative nudge in the same tight range the already-wired Adaptive
    Risk / Adaptive Precision Gate use (see engine/market_context.py's
    risk_stake_multiplier_low/high, precision_agreeing_low/high)."""
    confidence_delta: float = 0.0          # points to subtract from confidence, 0 to -20
    precision_agreeing_delta: int = 0      # additional agreeing strategies to require, 0 to +2
    stake_multiplier: float = 1.0          # 0.7 (max caution) to 1.0 (no change) -- never a bonus, only caution
    cooldown_multiplier: float = 1.0       # 1.0 (no change) to 1.8 (max extension)


@dataclass
class PatternRiskAssessment:
    score: float                                   # 0-100, 50=neutral/no-signal, 100=strongly resembles historical losses
    sample_size: int                               # decisive (win+loss) trades used
    significant: bool                              # sample_size >= min_sample_size
    evidence_count: int                            # number of dimensions independently exceeding the evidence threshold
    contributions: List[SignalContribution] = field(default_factory=list)
    recommended: RecommendedAdjustments = field(default_factory=RecommendedAdjustments)
    recommend_reject: bool = False                 # advisory only -- True requires MULTIPLE independent signals to agree, never a single feature
    note: str = ""


def _population_stats(values: List[float]) -> Optional[Dict[str, float]]:
    n = len(values)
    if n == 0:
        return None
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    std = variance ** 0.5
    return {"mean": mean, "std": std, "n": n}


class PatternRiskEngine:
    """Stateless, read-only, advisory. Construct with a shared
    AnalyticsEngine (reuses its significance-gated categorical breakdowns
    rather than recomputing them -- one source of truth for "does this
    bucket underperform"). Call assess() with a candidate/current snapshot
    and the trade history to compare against; nothing is retained between
    calls.
    """

    def __init__(self, analytics_engine: AnalyticsEngine, min_sample_size: int = 10):
        self.analytics = analytics_engine
        self.min_sample_size = min_sample_size

    def assess(self, current: Dict[str, Any], trades: List) -> PatternRiskAssessment:
        closed = [t for t in trades if str(getattr(getattr(t, "status", None), "value", getattr(t, "status", None))) in ("win", "loss")]
        wins = [t for t in closed if str(getattr(t.status, "value", t.status)) == "win"]
        losses = [t for t in closed if str(getattr(t.status, "value", t.status)) == "loss"]
        sample_size = len(wins) + len(losses)
        significant = sample_size >= self.min_sample_size

        contributions: List[SignalContribution] = []

        # --- Continuous features: z-distance-to-population-mean comparison
        for human_name, feat_attr in _CONTINUOUS_FEATURES.items():
            if human_name not in current or current[human_name] is None:
                continue
            cur_val = float(current[human_name])
            win_vals = [getattr(t.features, feat_attr) for t in wins if t.features and getattr(t.features, feat_attr) is not None]
            loss_vals = [getattr(t.features, feat_attr) for t in losses if t.features and getattr(t.features, feat_attr) is not None]
            win_pop = _population_stats(win_vals)
            loss_pop = _population_stats(loss_vals)
            if not win_pop or not loss_pop or win_pop["n"] < _MIN_FEATURE_SAMPLE or loss_pop["n"] < _MIN_FEATURE_SAMPLE:
                continue
            # Pooled std as a shared distance scale (floored to avoid div/0
            # when a population happens to be near-constant).
            pooled_std = max((win_pop["std"] + loss_pop["std"]) / 2.0, 1e-6)
            dist_to_win = abs(cur_val - win_pop["mean"]) / pooled_std
            dist_to_loss = abs(cur_val - loss_pop["mean"]) / pooled_std
            denom = dist_to_win + dist_to_loss
            lean = 0.0 if denom == 0 else (dist_to_win - dist_to_loss) / denom
            lean = max(-1.0, min(1.0, lean))
            contributions.append(SignalContribution(
                dimension=human_name, lean=round(lean, 3),
                detail=(f"current={cur_val:.2f}, win_mean={win_pop['mean']:.2f} (n={win_pop['n']}), "
                        f"loss_mean={loss_pop['mean']:.2f} (n={loss_pop['n']})"),
                is_evidence=abs(lean) >= _LEAN_EVIDENCE_THRESHOLD,
            ))

        # --- Categorical features: reuse AnalyticsEngine's significance-gated breakdowns
        report = self.analytics.compute(trades)
        overall_wr = report.overall.win_rate
        for human_name, dim_key in _CATEGORICAL_FEATURES.items():
            if human_name not in current or current[human_name] is None:
                continue
            bucket_stats = report.breakdowns.get(dim_key, {})
            bucket = bucket_stats.get(str(current[human_name]))
            if bucket is None or not bucket.significant:
                continue
            gap = overall_wr - bucket.win_rate   # positive = this bucket underperforms the overall rate
            # Normalize the win-rate-point gap to a comparable -1..1 lean scale (30pts = full-strength signal)
            lean = max(-1.0, min(1.0, gap / 30.0))
            contributions.append(SignalContribution(
                dimension=human_name, lean=round(lean, 3),
                detail=(f"{current[human_name]}: win_rate={bucket.win_rate:.1f}% (n={bucket.wins + bucket.losses}) "
                        f"vs overall {overall_wr:.1f}%"),
                is_evidence=abs(lean) >= _LEAN_EVIDENCE_THRESHOLD,
            ))

        evidence_count = sum(1 for c in contributions if c.is_evidence and c.lean > 0)
        if contributions:
            avg_lean = sum(c.lean for c in contributions) / len(contributions)
        else:
            avg_lean = 0.0
        score = max(0.0, min(100.0, 50.0 + 50.0 * avg_lean))

        recommended = self._recommend(score, evidence_count, significant)
        # Reject is advisory-only and deliberately hard to trigger: requires
        # BOTH overall sample significance AND at least 3 independent
        # dimensions (never a single feature) agreeing the pattern looks
        # like a loss, AND a high absolute score.
        recommend_reject = significant and evidence_count >= 3 and score >= 75.0
        if recommend_reject:
            log_event(
                logger, logging.WARNING, "pattern_risk_recommend_reject",
                candidate=current, score=round(score, 1), evidence_count=evidence_count,
                sample_size=sample_size,
                reason="3_or_more_independent_dimensions_agree_high_loss_resemblance",
            )

        note = "Advisory only -- not applied to any live trading decision."
        if not significant:
            note += f" Sample size ({sample_size}) below min_sample_size ({self.min_sample_size}); treat this score as low-confidence."

        log_event(
            logger, logging.DEBUG, "pattern_risk_assessed",
            candidate=current, score=round(score, 1), sample_size=sample_size, significant=significant,
            evidence_count=evidence_count, recommend_reject=recommend_reject,
            contributions=[{"dimension": c.dimension, "lean": c.lean, "is_evidence": c.is_evidence} for c in contributions],
            reason="weighted_average_of_win_vs_loss_population_lean",
        )
        return PatternRiskAssessment(
            score=round(score, 1), sample_size=sample_size, significant=significant,
            evidence_count=evidence_count, contributions=contributions,
            recommended=recommended, recommend_reject=recommend_reject, note=note,
        )

    def _recommend(self, score: float, evidence_count: int, significant: bool) -> RecommendedAdjustments:
        """Maps score -> bounded, advisory-only nudges. Below score=50
        (looks more like historical wins than losses) every adjustment is a
        no-op -- this module only ever recommends CAUTION, never a bonus
        for looking win-like, matching the "gradual" and "conservative"
        framing of the original spec rather than a symmetric reward/penalty
        system."""
        if not significant or score <= 50.0:
            return RecommendedAdjustments()
        frac = (score - 50.0) / 50.0  # 0 at score=50, 1 at score=100
        return RecommendedAdjustments(
            confidence_delta=-round(min(20.0, frac * 20.0), 1),
            precision_agreeing_delta=2 if score >= 80 else (1 if score >= 60 else 0),
            stake_multiplier=round(1.0 - min(0.3, frac * 0.3), 3),
            cooldown_multiplier=round(1.0 + min(0.8, frac * 0.8), 3),
        )
