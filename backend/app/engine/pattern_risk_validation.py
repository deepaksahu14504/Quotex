"""Pattern Risk shadow-validation harness.

Records what PatternRiskEngine WOULD have recommended for every real trade,
at the moment that trade executes -- before its outcome is known -- then
fills in the actual outcome when the trade closes. Nothing here reads its
own output to influence a trade; the recommendation is computed and stored
purely as a passive observation, so a later comparison between "what the
engine said" and "what actually happened" is statistically unbiased (the
engine's score could not have affected the outcome it's being compared
against).

This is the mechanism the production rollout plan depends on: Pattern Risk
and Recovery stay pure advisory/read-only until THIS module shows, over a
statistically significant sample, that acting on their recommendations
would have measurably improved outcomes. Until that evidence exists,
pattern_risk_enforcement_enabled (config.py) stays False and nothing
downstream of a trade decision reads from this file.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Deque, Dict, List, Optional

from .analytics_engine import wilson_interval
from ..config import user_data_dir
from ..logging_setup import log_event

logger = logging.getLogger("qat.pattern_risk_validation")

MAX_RECORDS = 2000


@dataclass
class ValidationRecord:
    trade_id: str
    asset: str
    timeframe: str
    timestamp: float
    score: float
    evidence_count: int
    recommend_reject: bool
    stake_multiplier: float       # the RECOMMENDED value -- never applied to the real trade's actual stake
    confidence_delta: float       # the RECOMMENDED value -- never applied to the real trade's actual confidence
    outcome: Optional[str] = None   # "win" | "loss" | "draw", filled in when the trade closes
    profit: Optional[float] = None


@dataclass
class ScoreBandStat:
    band: str
    total: int
    wins: int
    losses: int
    win_rate: float
    wilson_lower: float
    wilson_upper: float
    significant: bool
    total_pnl: float


@dataclass
class ValidationReport:
    sample_size: int                       # resolved (outcome-known) records
    significant: bool
    flagged_reject_stat: Optional[ScoreBandStat]     # actual performance of trades the engine WOULD have rejected
    not_flagged_stat: Optional[ScoreBandStat]        # actual performance of trades it would NOT have rejected
    score_bands: List[ScoreBandStat] = field(default_factory=list)
    actual_total_pnl: float = 0.0
    counterfactual_pnl_excluding_flagged: float = 0.0   # what total PnL would have been had every recommend_reject=True trade been skipped
    actual_max_drawdown: float = 0.0
    counterfactual_max_drawdown_excluding_flagged: float = 0.0
    verdict: str = ""   # plain-language summary of whether the evidence currently supports enabling enforcement
    note: str = ""


class PatternRiskValidationStore:
    """Per-user, bounded, disk-persisted -- same pattern as TradeStore /
    ShadowStore. Records are added at trade-execution time (outcome=None)
    and updated in place when the trade closes."""

    def __init__(self, user_id: str, scope_dir: Optional[Path] = None):
        self._file: Path = (scope_dir / "pattern_risk_validation.json") if scope_dir is not None else user_data_dir(user_id) / "pattern_risk_validation.json"
        self._records: Deque[ValidationRecord] = deque(maxlen=MAX_RECORDS)
        self._load()

    def _load(self) -> None:
        if self._file.exists():
            try:
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                for r in raw:
                    self._records.append(ValidationRecord(**r))
            except Exception:
                logger.warning("[pattern-risk-validation] failed to load %s", self._file, exc_info=True)

    def _persist(self) -> None:
        try:
            self._file.write_text(
                json.dumps([asdict(r) for r in self._records], default=str, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[pattern-risk-validation] failed to persist %s", self._file, exc_info=True)

    def record(self, rec: ValidationRecord) -> None:
        self._records.append(rec)
        self._persist()
        log_event(
            logger, logging.DEBUG, "pattern_risk_validation_recorded",
            trade_id=rec.trade_id, asset=rec.asset, timeframe=rec.timeframe,
            score=rec.score, evidence_count=rec.evidence_count, recommend_reject=rec.recommend_reject,
        )

    def update_outcome(self, trade_id: str, outcome: str, profit: float) -> None:
        for r in self._records:
            if r.trade_id == trade_id:
                r.outcome = outcome
                r.profit = profit
                self._persist()
                log_event(logger, logging.DEBUG, "pattern_risk_validation_outcome",
                          trade_id=trade_id, outcome=outcome, profit=profit)
                return

    def all(self) -> List[ValidationRecord]:
        return list(self._records)


_SCORE_BANDS = [("<50", 0, 50), ("50-65", 50, 65), ("65-80", 65, 80), ("80+", 80, 101)]


def compute_validation_report(records: List[ValidationRecord], min_sample_size: int = 30) -> ValidationReport:
    """Pure function -- no I/O, no side effects. Compares actual outcomes
    of trades the engine WOULD have flagged for rejection (recommend_reject
    was True) against trades it would not have flagged, using the exact
    same Wilson-interval significance gate as every other engine in this
    codebase. Also computes a simple counterfactual: total PnL and max
    drawdown of the actual trade sequence vs. the same sequence with every
    recommend_reject=True trade removed -- a reasonable, if approximate,
    estimate of what skipping those trades would have done, since the
    recommendation was never allowed to influence the outcome it's being
    compared against.
    """
    resolved = sorted(
        [r for r in records if r.outcome in ("win", "loss", "draw")],
        key=lambda r: r.timestamp,
    )
    sample_size = len(resolved)
    significant = sample_size >= min_sample_size

    def _band_stat(label: str, subset: List[ValidationRecord]) -> ScoreBandStat:
        wins = sum(1 for r in subset if r.outcome == "win")
        losses = sum(1 for r in subset if r.outcome == "loss")
        total_decisive = wins + losses
        win_rate = (wins / total_decisive * 100.0) if total_decisive else 0.0
        lower, upper = wilson_interval(wins, total_decisive)
        return ScoreBandStat(
            band=label, total=len(subset), wins=wins, losses=losses,
            win_rate=round(win_rate, 1), wilson_lower=round(lower * 100, 1), wilson_upper=round(upper * 100, 1),
            significant=total_decisive >= min_sample_size,
            total_pnl=round(sum(r.profit or 0.0 for r in subset), 2),
        )

    flagged = [r for r in resolved if r.recommend_reject]
    not_flagged = [r for r in resolved if not r.recommend_reject]
    flagged_stat = _band_stat("recommend_reject=True", flagged) if flagged else None
    not_flagged_stat = _band_stat("recommend_reject=False", not_flagged) if not_flagged else None

    score_bands = []
    for label, lo, hi in _SCORE_BANDS:
        subset = [r for r in resolved if lo <= r.score < hi]
        if subset:
            score_bands.append(_band_stat(label, subset))

    actual_pnl_seq = [r.profit or 0.0 for r in resolved]
    counterfactual_pnl_seq = [r.profit or 0.0 for r in resolved if not r.recommend_reject]

    def _max_drawdown(pnl_seq: List[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnl_seq:
            equity += p
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return round(max_dd, 2)

    actual_total_pnl = round(sum(actual_pnl_seq), 2)
    counterfactual_pnl = round(sum(counterfactual_pnl_seq), 2)
    actual_dd = _max_drawdown(actual_pnl_seq)
    counterfactual_dd = _max_drawdown(counterfactual_pnl_seq)

    verdict = "Insufficient data -- keep collecting shadow observations."
    note = f"{sample_size} resolved trades observed; need {min_sample_size} for any verdict."
    if significant and flagged_stat and not_flagged_stat and flagged_stat.significant and not_flagged_stat.significant:
        # A verdict of "supports enabling" requires the flagged group's
        # Wilson UPPER bound to sit below the not-flagged group's Wilson
        # LOWER bound -- i.e. the confidence intervals don't overlap, not
        # just that the point estimates differ, which is a much higher and
        # more honest bar than comparing two raw percentages.
        if flagged_stat.wilson_upper < not_flagged_stat.wilson_lower and counterfactual_dd < actual_dd:
            verdict = ("Evidence currently SUPPORTS enabling enforcement: flagged trades' win rate "
                       "is significantly lower (non-overlapping confidence intervals) and the "
                       "counterfactual drawdown is lower than actual.")
        elif flagged_stat.wilson_lower > not_flagged_stat.wilson_upper:
            verdict = ("Evidence currently CONTRADICTS the engine's assumption: flagged trades "
                       "actually performed significantly BETTER, not worse. Do not enable "
                       "enforcement based on the current model.")
        else:
            verdict = ("Inconclusive: confidence intervals overlap. More samples needed before "
                       "this engine's recommend_reject signal can be trusted operationally.")
        note = f"{sample_size} resolved trades ({len(flagged)} flagged, {len(not_flagged)} not flagged)."

    return ValidationReport(
        sample_size=sample_size, significant=significant,
        flagged_reject_stat=flagged_stat, not_flagged_stat=not_flagged_stat,
        score_bands=score_bands, actual_total_pnl=actual_total_pnl,
        counterfactual_pnl_excluding_flagged=counterfactual_pnl,
        actual_max_drawdown=actual_dd, counterfactual_max_drawdown_excluding_flagged=counterfactual_dd,
        verdict=verdict, note=note,
    )
