"""Trade Analytics Engine (Loss Intelligence Modules 2, 3, 8 -- consolidated).

READ-ONLY. This module computes statistics over already-closed trades. It
is never imported by orchestrator.py's execution path and never influences
a stake, threshold, or pause/resume decision -- it exists purely to answer
"how has the bot actually performed, broken down by X" and "which
conditions correlate with losses more than they do with wins," so that a
FUTURE Pattern Risk / Recovery stage can be designed against real data
instead of guesses.

Core principle honored throughout: every breakdown's denominator is
(wins + losses) together, never losses alone -- a bucket with 9 wins and 1
loss is not "the same win rate" as one with 0 wins and 0 losses just
because both have 1 loss. Every statistic is measured against BOTH
outcomes, weighted by sample size via a Wilson score interval rather than
a raw percentage, and small samples are explicitly flagged as not yet
statistically meaningful rather than silently presented as if they were.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import logging
import math

from ..logging_setup import log_event

logger = logging.getLogger("qat.analytics")
import time


# ─────────────────────────────────────────────────────────────────────── #
# Statistical significance
# ─────────────────────────────────────────────────────────────────────── #
def _inverse_normal_cdf(p: float) -> float:
    """Peter Acklam's rational approximation of the inverse standard normal
    CDF (probit function) -- accurate to ~1.15e-9, no scipy dependency.
    Used only to convert a user-configured confidence level into a z-score
    for wilson_interval(); not used anywhere performance-sensitive (called
    once per settings change, not per trade)."""
    if p <= 0.0 or p >= 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def z_for_confidence_level(confidence_level: float = 0.95) -> float:
    """Two-sided z-score for a given confidence level, e.g. 0.95 -> ~1.96,
    0.99 -> ~2.576. This is what TradingSettings.adaptive_wilson_confidence_level
    controls -- every Wilson-bound call in the codebase (strategy mute/trial,
    calibration intervals, rejection reconciliation) can pass this instead of
    a hardcoded z, so raising/lowering the confidence level moves the
    evidence bar for all of them consistently."""
    return _inverse_normal_cdf(0.5 + confidence_level / 2.0)


def wilson_interval(wins: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """95% Wilson score confidence interval for a win rate, expressed as a
    proportion of `total` (wins + losses -- draws are excluded from `total`
    by the caller before this is called). z=1.96 is the standard 95%
    two-sided value.

    Wilson's interval is preferred over a raw wins/total percentage or a
    naive normal-approximation interval because it behaves sensibly at
    small sample sizes (it can't go below 0 or above 1, and it correctly
    widens a lot when total is small) -- exactly the "before considering
    a pattern useful" check this engine needs. Returns (0.0, 1.0) for
    total=0 (maximally uninformative, not an error).
    """
    if total <= 0:
        return 0.0, 1.0
    p_hat = wins / total
    denom = 1.0 + (z * z) / total
    center = p_hat + (z * z) / (2 * total)
    margin = z * math.sqrt((p_hat * (1 - p_hat) / total) + (z * z) / (4 * total * total))
    lower = max(0.0, (center - margin) / denom)
    upper = min(1.0, (center + margin) / denom)
    return lower, upper


@dataclass
class BucketStat:
    """Win/loss statistics for one bucket of one breakdown dimension (e.g.
    weekday=2, or session="london"). `significant` is the single gate a
    caller should check before treating `win_rate` as meaningful -- an
    insignificant bucket's win_rate is still reported (never hidden), just
    flagged as not yet reliable."""
    total: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    win_rate: float = 0.0          # 0-100, wins / (wins+losses)
    wilson_lower: float = 0.0      # 0-100, 95% CI lower bound
    wilson_upper: float = 100.0    # 0-100, 95% CI upper bound
    significant: bool = False      # True only if closed-trade sample >= min_sample_size
    pnl: float = 0.0


@dataclass
class LossReasonStat:
    """One root-cause tag's prevalence in losses VS in wins. The gap
    between loss_rate and win_rate (`signal_strength`) is the actual
    root-cause signal -- a condition present in 40% of losses means
    nothing on its own if it's also present in 40% of wins; it matters
    when the two rates diverge."""
    tag: str
    loss_count: int = 0
    win_count: int = 0
    loss_rate_within_losses: float = 0.0   # % of all losses that have this tag
    win_rate_within_wins: float = 0.0      # % of all wins that have this tag
    signal_strength: float = 0.0           # loss_rate_within_losses - win_rate_within_wins (can be negative)
    significant: bool = False


@dataclass
class AnalyticsReport:
    generated_at: float
    total_trades: int
    total_closed: int              # wins + losses (draws excluded from win-rate math)
    overall: BucketStat
    breakdowns: Dict[str, Dict[str, BucketStat]] = field(default_factory=dict)
    loss_reasons: Dict[str, LossReasonStat] = field(default_factory=dict)
    best_worst: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)  # dimension -> {"best": bucket_key, "worst": bucket_key}
    min_sample_size: int = 10
    insufficient_data_note: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────── #
# Root-cause tagging
# ─────────────────────────────────────────────────────────────────────── #
# Every threshold below reuses a number already established elsewhere in
# this codebase (dead-market ADX/volume floors, the range-regime ADX
# boundary) rather than inventing a new arbitrary cutoff for this module.
# Honesty note: the original Loss Pattern Intelligence spec also asked for
# "Fake Breakout", "Against Higher Timeframe", "Momentum Failure", and
# "Late Entry" tags. TradeFeatureSnapshot does not capture the data those
# would need (post-entry price action, HTF bias at entry, a true
# order-to-fill latency distinction from generation latency) -- rather
# than fabricate a tag from data that isn't actually there, those four are
# left out until Module 1's snapshot is extended to capture them.
_WEAK_TREND_ADX = 18.0          # matches strategies.py's default range_adx boundary
_LOW_VOLUME_RATIO = 0.7         # matches config.py's vol_confirmation_low default
_LOW_CONFLUENCE_COUNT = 2       # matches config.py's confluence_min_confirmations default
_OVERCONFIDENT_QUALITY = 80.0   # a trade "should" have been reliable at this quality score
_HIGH_LATENCY_MS = 1000.0       # execution latency above which entry timing itself is suspect


def _tags_for_trade(trade) -> List[str]:
    """Returns every root-cause tag that honestly applies to this trade's
    captured features, regardless of whether it won or lost -- tag
    prevalence is computed separately for the win and loss populations by
    the caller, so this function itself must not know or care about the
    outcome."""
    tags: List[str] = []
    feats = getattr(trade, "features", None)
    if feats is None:
        return tags
    if feats.adx_at_entry is not None and feats.adx_at_entry < _WEAK_TREND_ADX:
        tags.append("weak_trend")
    if feats.volume_ratio_at_entry is not None and feats.volume_ratio_at_entry < _LOW_VOLUME_RATIO:
        tags.append("low_volume")
    if getattr(trade, "regime", None) == "range":
        tags.append("sideways_market")
    if feats.confluence_count is not None and feats.confluence_count < _LOW_CONFLUENCE_COUNT:
        tags.append("low_confluence")
    if feats.quality_score is not None and feats.quality_score >= _OVERCONFIDENT_QUALITY:
        tags.append("confidence_overestimation")
    if feats.execution_latency_ms is not None and feats.execution_latency_ms > _HIGH_LATENCY_MS:
        tags.append("high_latency_entry")
    return tags


# ─────────────────────────────────────────────────────────────────────── #
# Breakdown dimension extractors
# ─────────────────────────────────────────────────────────────────────── #
def _bucket_confidence(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value < 70:
        return "<70"
    if value < 80:
        return "70-80"
    if value < 90:
        return "80-90"
    return "90+"


def _bucket_trend(adx: Optional[float]) -> Optional[str]:
    if adx is None:
        return None
    if adx < _WEAK_TREND_ADX:
        return "weak"
    if adx < 25.0:  # matches strategies.py's default trend_adx boundary
        return "moderate"
    return "strong"


def _bucket_volatility_percentile(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct < 33:
        return "low"
    if pct < 66:
        return "medium"
    return "high"


def _dimension_values(trade) -> Dict[str, Optional[str]]:
    """One value per breakdown dimension for this trade (None = not
    applicable / not captured, excluded from that dimension's stats
    rather than counted as a fake bucket)."""
    feats = getattr(trade, "features", None)
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {
        "weekday": weekday_names[feats.weekday] if feats and feats.weekday is not None else None,
        "hour": f"{feats.hour:02d}:00" if feats and feats.hour is not None else None,
        "session": feats.session if feats else None,
        "asset": getattr(trade, "asset", None),
        "otc_or_live": ("OTC" if feats.is_otc else "Live") if feats and feats.is_otc is not None else None,
        "timeframe": getattr(trade, "timeframe", None),
        "regime": getattr(trade, "regime", None),
        "volatility_bucket": _bucket_volatility_percentile(feats.volatility_percentile) if feats else None,
        "confidence_bucket": _bucket_confidence(getattr(trade, "confidence", None)),
        "trend_bucket": _bucket_trend(feats.adx_at_entry) if feats else None,
    }
    # Note: "quality score bucket" from the original spec is intentionally
    # not a separate dimension here -- TradeFeatureSnapshot.quality_score
    # is captured as the same raw pre-calibration confidence value already
    # covered by "confidence_bucket" above. Building a second, separately-
    # bucketed dimension from the same underlying number would be exactly
    # the kind of duplicated logic the brief asked to avoid.


class AnalyticsEngine:
    """Stateless, read-only. Call compute() with the trade history you want
    analyzed (typically self.store.all() from an Orchestrator) whenever a
    report is requested -- there is no background task, no cache
    invalidation to worry about, and nothing here retains a reference to
    the trades passed in after compute() returns. Memory is bounded by the
    caller's own trade history retention (TradeStore's existing
    deque(maxlen=1000)), not by anything in this class.
    """

    def __init__(self, min_sample_size: int = 10):
        self.min_sample_size = min_sample_size

    def compute(self, trades: List) -> AnalyticsReport:
        closed = [t for t in trades if getattr(t, "status", None) is not None
                  and str(getattr(t.status, "value", t.status)) in ("win", "loss", "draw")]
        wins = [t for t in closed if str(getattr(t.status, "value", t.status)) == "win"]
        losses = [t for t in closed if str(getattr(t.status, "value", t.status)) == "loss"]
        draws = [t for t in closed if str(getattr(t.status, "value", t.status)) == "draw"]
        decisive = wins + losses  # draws excluded from win-rate math, never from counts

        overall = self._bucket_stat(len(wins), len(losses), len(draws),
                                     sum(getattr(t, "profit", 0.0) or 0.0 for t in closed))

        breakdowns: Dict[str, Dict[str, BucketStat]] = defaultdict(dict)
        # dimension -> bucket -> (wins, losses, draws, pnl)
        tally: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0.0]))
        for t in closed:
            status = str(getattr(t.status, "value", t.status))
            profit = getattr(t, "profit", 0.0) or 0.0
            for dim, bucket in _dimension_values(t).items():
                if bucket is None:
                    continue
                row = tally[dim][bucket]
                if status == "win":
                    row[0] += 1
                elif status == "loss":
                    row[1] += 1
                elif status == "draw":
                    row[2] += 1
                row[3] += profit

            # Per-strategy breakdown is separate from the single-value
            # dimensions above -- one trade can credit multiple strategies.
            for strat in (getattr(t, "strategies", None) or []):
                row = tally["strategy"][strat]
                if status == "win":
                    row[0] += 1
                elif status == "loss":
                    row[1] += 1
                elif status == "draw":
                    row[2] += 1
                row[3] += profit

        for dim, buckets in tally.items():
            for bucket, (w, l, d, pnl) in buckets.items():
                breakdowns[dim][bucket] = self._bucket_stat(int(w), int(l), int(d), pnl)

        loss_reasons = self._compute_loss_reasons(wins, losses)
        best_worst = self._compute_best_worst(breakdowns)

        note = None
        if len(decisive) < self.min_sample_size:
            note = (f"Only {len(decisive)} closed (win/loss) trades on record -- fewer than the "
                    f"minimum sample size ({self.min_sample_size}) this engine requires before "
                    f"treating any breakdown as statistically meaningful. Every bucket below is "
                    f"still shown, but flagged significant=False until more history accumulates.")

        log_event(
            logger, logging.DEBUG, "analytics_report_computed",
            total_trades=len(trades), total_closed=len(decisive),
            overall_win_rate=overall.win_rate, dimensions_computed=list(breakdowns.keys()),
            loss_reason_tags=list(loss_reasons.keys()), significant=len(decisive) >= self.min_sample_size,
            reason="on_demand_report_request",
        )
        return AnalyticsReport(
            generated_at=time.time(),
            total_trades=len(trades),
            total_closed=len(decisive),
            overall=overall,
            breakdowns=dict(breakdowns),
            loss_reasons=loss_reasons,
            best_worst=best_worst,
            min_sample_size=self.min_sample_size,
            insufficient_data_note=note,
        )

    def _bucket_stat(self, wins: int, losses: int, draws: int, pnl: float) -> BucketStat:
        total_decisive = wins + losses
        win_rate = (wins / total_decisive * 100.0) if total_decisive else 0.0
        lower, upper = wilson_interval(wins, total_decisive)
        return BucketStat(
            total=wins + losses + draws, wins=wins, losses=losses, draws=draws,
            win_rate=round(win_rate, 1),
            wilson_lower=round(lower * 100.0, 1), wilson_upper=round(upper * 100.0, 1),
            significant=total_decisive >= self.min_sample_size,
            pnl=round(pnl, 2),
        )

    def _compute_loss_reasons(self, wins: List, losses: List) -> Dict[str, LossReasonStat]:
        loss_tag_counts: Dict[str, int] = defaultdict(int)
        win_tag_counts: Dict[str, int] = defaultdict(int)
        for t in losses:
            for tag in _tags_for_trade(t):
                loss_tag_counts[tag] += 1
        for t in wins:
            for tag in _tags_for_trade(t):
                win_tag_counts[tag] += 1

        all_tags = set(loss_tag_counts) | set(win_tag_counts)
        result: Dict[str, LossReasonStat] = {}
        for tag in all_tags:
            loss_count = loss_tag_counts.get(tag, 0)
            win_count = win_tag_counts.get(tag, 0)
            loss_rate = (loss_count / len(losses) * 100.0) if losses else 0.0
            win_rate = (win_count / len(wins) * 100.0) if wins else 0.0
            result[tag] = LossReasonStat(
                tag=tag, loss_count=loss_count, win_count=win_count,
                loss_rate_within_losses=round(loss_rate, 1),
                win_rate_within_wins=round(win_rate, 1),
                signal_strength=round(loss_rate - win_rate, 1),
                significant=(len(losses) + len(wins)) >= self.min_sample_size,
            )
        return result

    def _compute_best_worst(self, breakdowns: Dict[str, Dict[str, BucketStat]]) -> Dict[str, Dict[str, Optional[str]]]:
        result: Dict[str, Dict[str, Optional[str]]] = {}
        for dim, buckets in breakdowns.items():
            sig_buckets = {k: v for k, v in buckets.items() if v.significant}
            if not sig_buckets:
                result[dim] = {"best": None, "worst": None}
                continue
            best = max(sig_buckets.items(), key=lambda kv: kv[1].win_rate)[0]
            worst = min(sig_buckets.items(), key=lambda kv: kv[1].win_rate)[0]
            result[dim] = {"best": best, "worst": worst}
        return result
