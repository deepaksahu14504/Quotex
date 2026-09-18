"""Recovery Engine (Loss Intelligence Module 6).

READ-ONLY / ADVISORY ONLY. Computes a Recovery Score from Shadow Mode
performance (and optionally current market-quality signals) and reports
whether resuming live trading looks statistically supported -- it does
NOT pause, resume, or otherwise touch orchestrator.py's trading state.
Nothing calls this from the scan/execute/pause path yet.

Deliberately separate from PatternRiskEngine: that module answers "does
this specific candidate trade resemble our losses," a per-trade question.
This module answers "has performance recovered broadly enough to trust
live trading again," an account-level, binary-outcome question with a
different, and higher, evidentiary bar -- getting this one wrong has a
much larger blast radius (every trade after an incorrect resume) than
getting one trade's pattern score wrong.

"Never resume solely because a timer expired": this class takes no time-
since-pause input at all. Its only inputs are actual shadow trade outcomes
and (optionally) current market-quality signals. A resume recommendation
requires BOTH a statistically-supported win rate (Wilson lower bound, not
a raw average) AND consistency across the sample (a recent hot streak
riding on top of an equally recent cold streak does not qualify) --
matching "when recovery score remains consistently high," not "was high
once."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
import logging

from .analytics_engine import wilson_interval
from ..logging_setup import log_event

logger = logging.getLogger("qat.recovery")


@dataclass
class RecoveryAssessment:
    recovery_score: float                    # 0-100
    sample_size: int                         # resolved shadow trades considered
    significant: bool                        # sample_size >= min_shadow_sample
    wilson_lower: float                      # 0-100, conservative win-rate estimate
    wilson_upper: float
    consistency_ok: bool                     # both halves of the recent window clear the baseline, and don't diverge wildly
    older_half_win_rate: Optional[float]
    newer_half_win_rate: Optional[float]
    market_quality_score: Optional[float]    # 0-100, only if a market snapshot was supplied
    resume_recommended: bool                 # advisory only -- nothing acts on this
    note: str = ""


class RecoveryEngine:
    """Stateless, read-only, advisory.

    min_shadow_sample: minimum resolved shadow trades before ANY
        recommendation is made (below this, resume_recommended is always
        False and the report says so explicitly).
    consistency_window: how many of the most recent resolved shadow trades
        to split into two halves for the consistency check.
    win_rate_baseline: the Wilson lower bound must clear this (percentage
        points) for a resume to be recommended -- deliberately set at a
        level a genuinely-recovered system should clear comfortably, not
        merely "better than a coin flip."
    score_threshold: the combined recovery_score must also clear this.
    """

    def __init__(
        self,
        min_shadow_sample: int = 20,
        consistency_window: int = 20,
        win_rate_baseline: float = 50.0,
        score_threshold: float = 65.0,
        max_half_divergence: float = 25.0,
    ):
        self.min_shadow_sample = min_shadow_sample
        self.consistency_window = consistency_window
        self.win_rate_baseline = win_rate_baseline
        self.score_threshold = score_threshold
        self.max_half_divergence = max_half_divergence

    def assess(self, shadow_trades: List, market_quality_score: Optional[float] = None) -> RecoveryAssessment:
        """`shadow_trades` should be ShadowStore's resolved trades (any
        object with .status in {"win","loss","draw"} and .opened_at for
        recency ordering works -- this module has no import-time
        dependency on shadow_mode.py's exact class, only its shape).
        `market_quality_score`, if supplied, should already be a 0-100
        composite (e.g. an average of MarketContextSnapshot's
        trend_strength/volume_quality/noise_level/historical_stability) --
        this module does not compute that itself, to avoid a second,
        parallel definition of "market quality" alongside
        MarketContextEngine's."""
        resolved = [t for t in shadow_trades if str(getattr(t, "status", None)) in ("win", "loss")]
        resolved.sort(key=lambda t: getattr(t, "opened_at", 0.0), reverse=True)
        recent = resolved[: self.consistency_window * 2]  # enough for two full halves

        wins = sum(1 for t in recent if t.status == "win")
        losses = sum(1 for t in recent if t.status == "loss")
        sample_size = wins + losses
        significant = sample_size >= self.min_shadow_sample

        lower, upper = wilson_interval(wins, sample_size) if sample_size else (0.0, 1.0)
        wilson_lower_pct = round(lower * 100.0, 1)
        wilson_upper_pct = round(upper * 100.0, 1)

        # Consistency: split the recent window into an older and a newer
        # half (by recency-sorted order, so "newer" is index 0..window-1),
        # and require both halves to clear the baseline without diverging
        # wildly from each other -- a single lucky streak stacked on a
        # losing one should not read as "recovered."
        newer_half = recent[: self.consistency_window]
        older_half = recent[self.consistency_window: self.consistency_window * 2]
        newer_wr = self._win_rate(newer_half)
        older_wr = self._win_rate(older_half)
        consistency_ok = False
        if newer_wr is not None and older_wr is not None:
            both_clear_baseline = newer_wr >= self.win_rate_baseline and older_wr >= self.win_rate_baseline
            divergence = abs(newer_wr - older_wr)
            consistency_ok = both_clear_baseline and divergence <= self.max_half_divergence
        elif newer_wr is not None and older_wr is None:
            # Not enough history yet for a real two-half comparison --
            # never treat a single half as "consistent" on its own.
            consistency_ok = False

        # Combine: Wilson lower bound (the conservative estimate) is the
        # dominant input since it's the direct evidence requested; market
        # quality (if supplied) is a secondary signal, weighted lower.
        if market_quality_score is not None:
            recovery_score = 0.7 * wilson_lower_pct + 0.3 * float(market_quality_score)
        else:
            recovery_score = wilson_lower_pct
        recovery_score = round(max(0.0, min(100.0, recovery_score)), 1)

        resume_recommended = (
            significant
            and consistency_ok
            and wilson_lower_pct >= self.win_rate_baseline
            and recovery_score >= self.score_threshold
        )

        note = "Advisory only -- does not pause, resume, or otherwise change live trading state."
        if not significant:
            note += f" Only {sample_size} resolved shadow trades (need {self.min_shadow_sample}); no recommendation possible yet."
        elif not consistency_ok:
            note += " Recent shadow performance is not yet consistent across both halves of the window."

        log_event(
            logger, logging.INFO if resume_recommended else logging.DEBUG, "recovery_assessed",
            recovery_score=recovery_score, sample_size=sample_size, significant=significant,
            wilson_lower=wilson_lower_pct, wilson_upper=wilson_upper_pct, consistency_ok=consistency_ok,
            older_half_win_rate=older_wr, newer_half_win_rate=newer_wr,
            market_quality_score=market_quality_score, resume_recommended=resume_recommended,
            reason="wilson_lower_bound_plus_two_half_consistency_check",
        )
        return RecoveryAssessment(
            recovery_score=recovery_score, sample_size=sample_size, significant=significant,
            wilson_lower=wilson_lower_pct, wilson_upper=wilson_upper_pct,
            consistency_ok=consistency_ok, older_half_win_rate=older_wr, newer_half_win_rate=newer_wr,
            market_quality_score=float(market_quality_score) if market_quality_score is not None else None,
            resume_recommended=resume_recommended, note=note,
        )

    @staticmethod
    def _win_rate(half: List) -> Optional[float]:
        if not half:
            return None
        wins = sum(1 for t in half if t.status == "win")
        losses = sum(1 for t in half if t.status == "loss")
        total = wins + losses
        if total == 0:
            return None
        return round(wins / total * 100.0, 1)
