"""Rejected signal reconciliation and analytics (Phase 3).

For every SignalDecision with final_decision == "rejected" that has a price,
duration, and payout attached (see decision_engine.build_decision() /
orchestrator.py), this module resolves what WOULD have happened had the
signal been taken anyway -- entry vs. price-at-expiry, exactly the way
shadow_mode.py resolves shadow trades and exactly the way real binary
options settle. Nothing here feeds back into any gate, threshold, or live
decision; it is a passive, after-the-fact observation, same discipline as
pattern_risk_validation.py.

This is what makes a gate's cost measurable for the first time in this
codebase: without it, a veto (precision_gate, MTF, confluence threshold,
signal_validation) can only be judged by intuition. With it, every gate has
a real, Wilson-bound win rate for the trades it turned away, broken down by
which gate rejected it and which regime was active -- directly answering
whether a gate is well-calibrated (correctly turning away bad setups) or
needlessly conservative (turning away setups that would have won).
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from .analytics_engine import wilson_interval
from ..config import user_data_dir
from ..logging_setup import log_event
from ..schemas import SignalDecision

logger = logging.getLogger("qat.rejection_reconciliation")

MAX_RECORDS = 5000
NOTIONAL_STAKE = 10.0  # same fixed reference stake as ShadowStore, for a comparable PnL view


def _gate_name(decision: SignalDecision) -> str:
    """Which gate rejected this signal, derived from rejection_reason /
    rejecting_evidence -- a stable, small taxonomy for grouping, not the raw
    free-text reason (which varies in exact wording per call)."""
    reason = (decision.rejection_reason or "").lower()
    if reason.startswith("precision_gate"):
        return "precision_gate"
    if "calibrated confidence" in reason:
        return "confidence_threshold"
    if decision.rejecting_evidence:
        # Fall back to the most prominent (highest-strength) rejecting
        # evidence source when the reason string itself doesn't map cleanly
        # (e.g. a strategies.evaluate()-level rejection: "No setup",
        # "Insufficient confluence", "Conflicted setup", HHTF hard veto).
        top = max(decision.rejecting_evidence, key=lambda e: e.strength)
        if top.source.startswith("signal_validation"):
            return "signal_validation"
        return "confluence_engine"
    return "unknown"


@dataclass
class RejectedOutcome:
    decision_id: str
    asset: str
    direction: Optional[str]
    timeframe: str
    regime: str
    rejected_by: str            # taxonomy from _gate_name()
    rejection_reason: str
    entry_price: float
    opened_at: float
    duration: float
    payout: float
    resolved: bool = False
    exit_price: Optional[float] = None
    status: Optional[str] = None            # "win" | "loss" | "draw"
    simulated_profit: Optional[float] = None


class RejectedOutcomeStore:
    """Per-user, bounded, disk-persisted -- same JSON-file pattern as
    ShadowStore. Deliberately NOT merged into ShadowStore: that store
    answers "how would an untaken STRATEGY have done broadly" (every
    qualifying signal, regardless of why); this store answers "was this
    SPECIFIC gate decision correct" (only signals a gate actually turned
    away, tagged with which gate and why)."""

    def __init__(self, storage_path: str) -> None:
        self._path = Path(storage_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._outcomes: Dict[str, RejectedOutcome] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for d in raw:
                self._outcomes[d["decision_id"]] = RejectedOutcome(**d)
        except Exception:
            logger.warning("[rejection-reconciliation] failed to load %s", self._path, exc_info=True)

    def _save(self) -> None:
        try:
            # Bound before persisting -- keep the most recently opened records.
            if len(self._outcomes) > MAX_RECORDS:
                keep = sorted(self._outcomes.values(), key=lambda o: o.opened_at, reverse=True)[:MAX_RECORDS]
                self._outcomes = {o.decision_id: o for o in keep}
            self._path.write_text(
                json.dumps([asdict(o) for o in self._outcomes.values()], default=str, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[rejection-reconciliation] failed to persist %s", self._path, exc_info=True)

    def track(self, decision: SignalDecision) -> Optional[RejectedOutcome]:
        """Register a rejected decision for later resolution. Silently
        skipped (returns None) if the decision is missing price/duration/
        payout -- can't resolve a hypothetical outcome without them, and
        this is instrumentation, not a hard requirement."""
        if decision.final_decision != "rejected":
            return None
        if decision.price is None or decision.duration is None or decision.payout is None:
            return None
        if decision.id in self._outcomes:
            return None
        outcome = RejectedOutcome(
            decision_id=decision.id, asset=decision.asset,
            direction=decision.direction.value if decision.direction else None,
            timeframe=decision.timeframe, regime=(decision.regime.regime if decision.regime else "unknown"),
            rejected_by=_gate_name(decision), rejection_reason=decision.rejection_reason or "",
            entry_price=decision.price, opened_at=decision.created_at,
            duration=decision.duration, payout=decision.payout,
        )
        self._outcomes[outcome.decision_id] = outcome
        self._save()
        log_event(logger, logging.DEBUG, "rejected_outcome_tracked",
                  decision_id=outcome.decision_id, asset=outcome.asset, rejected_by=outcome.rejected_by)
        return outcome

    def pending_due(self, now: float) -> List[RejectedOutcome]:
        return [o for o in self._outcomes.values() if not o.resolved and (o.opened_at + o.duration) <= now]

    def resolve(self, decision_id: str, exit_price: float) -> Optional[RejectedOutcome]:
        o = self._outcomes.get(decision_id)
        if o is None or o.resolved:
            return None
        o.exit_price = exit_price
        if o.direction is None:
            # No dominant direction ever emerged (e.g. "No setup",
            # "Insufficient confluence") -- there is no hypothetical
            # direction to resolve a win/loss against, so this stays
            # untracked as decisive but IS marked resolved so it stops
            # showing up in pending_due() forever.
            o.status = "draw"
            o.simulated_profit = 0.0
        elif exit_price == o.entry_price:
            o.status = "draw"
            o.simulated_profit = 0.0
        else:
            won = (exit_price > o.entry_price) if o.direction == "call" else (exit_price < o.entry_price)
            o.status = "win" if won else "loss"
            o.simulated_profit = round(NOTIONAL_STAKE * (o.payout / 100.0), 2) if won else -NOTIONAL_STAKE
        o.resolved = True
        self._save()
        log_event(logger, logging.DEBUG, "rejected_outcome_resolved",
                  decision_id=decision_id, status=o.status, rejected_by=o.rejected_by)
        return o

    def all_resolved(self, limit: int = 2000) -> List[RejectedOutcome]:
        resolved = [o for o in self._outcomes.values() if o.resolved]
        resolved.sort(key=lambda o: o.opened_at, reverse=True)
        return resolved[:limit]


@dataclass
class GateRegimeStat:
    gate: str
    regime: str
    total: int
    wins: int
    losses: int
    win_rate: float
    wilson_lower: float
    wilson_upper: float
    significant: bool
    total_pnl: float
    verdict: str   # plain-language, per-cell


@dataclass
class RejectionReport:
    sample_size: int                          # resolved, decisive (win/loss) outcomes with a real direction
    min_sample_size: int
    cells: List[GateRegimeStat] = field(default_factory=list)
    note: str = ""


def _cell_verdict(stat: GateRegimeStat, breakeven_win_rate: float, min_sample_size: int) -> str:
    """A gate is only worth trusting on a cell if we have enough resolved
    samples AND the rejected trades' win rate is genuinely below the
    payout-implied breakeven -- not just below 50%, which is the wrong bar
    for an instrument with a structural house edge (see decision_engine
    module docstring / earlier audit). Uses the Wilson UPPER bound (most
    optimistic plausible true rate) so a "well-calibrated" verdict requires
    real evidence, not a lucky small sample."""
    if not stat.significant:
        return f"Insufficient data ({stat.total} resolved, need {min_sample_size})"
    if stat.wilson_upper < breakeven_win_rate:
        return (f"Well-calibrated: even the most optimistic plausible win rate "
                f"({stat.wilson_upper}%) is below breakeven ({breakeven_win_rate:.1f}%) -- "
                f"this gate is correctly rejecting unprofitable setups here.")
    if stat.wilson_lower > breakeven_win_rate:
        return (f"Possibly too conservative: even the most pessimistic plausible win rate "
                f"({stat.wilson_lower}%) is above breakeven ({breakeven_win_rate:.1f}%) -- "
                f"this gate may be costing edge in this (gate, regime) cell.")
    return (f"Inconclusive: win rate ({stat.win_rate}%, CI [{stat.wilson_lower}, {stat.wilson_upper}]) "
            f"straddles breakeven ({breakeven_win_rate:.1f}%) -- more samples needed.")


def compute_rejection_report(
    outcomes: List[RejectedOutcome], *, min_sample_size: int = 30, typical_payout_pct: float = 85.0,
) -> RejectionReport:
    """Pure function -- no I/O. Breaks resolved, decisive (direction was
    known, outcome is win/loss not draw) rejected-signal outcomes down by
    (rejected_by gate, regime), computing a Wilson-bound win rate per cell
    and a verdict against the PAYOUT-IMPLIED breakeven rate (not 50%) --
    the same structural-edge correction flagged in the earlier audit.
    """
    decisive = [o for o in outcomes if o.resolved and o.status in ("win", "loss") and o.direction is not None]
    breakeven = 100.0 / (1.0 + typical_payout_pct / 100.0)  # e.g. payout=85 -> ~54.05%

    grouped: Dict[tuple, List[RejectedOutcome]] = defaultdict(list)
    for o in decisive:
        grouped[(o.rejected_by, o.regime)].append(o)

    cells: List[GateRegimeStat] = []
    for (gate, regime), subset in sorted(grouped.items()):
        wins = sum(1 for o in subset if o.status == "win")
        losses = sum(1 for o in subset if o.status == "loss")
        total = wins + losses
        win_rate = round(wins / total * 100.0, 1) if total else 0.0
        lower, upper = wilson_interval(wins, total)
        stat = GateRegimeStat(
            gate=gate, regime=regime, total=total, wins=wins, losses=losses,
            win_rate=win_rate, wilson_lower=round(lower * 100, 1), wilson_upper=round(upper * 100, 1),
            significant=total >= min_sample_size, total_pnl=round(sum(o.simulated_profit or 0.0 for o in subset), 2),
            verdict="",
        )
        stat.verdict = _cell_verdict(stat, breakeven, min_sample_size)
        cells.append(stat)

    return RejectionReport(
        sample_size=len(decisive), min_sample_size=min_sample_size, cells=cells,
        note=(
            f"{len(decisive)} resolved, decisive rejected-signal outcomes across {len(cells)} "
            f"(gate, regime) cells. Breakeven win rate at a {typical_payout_pct:.0f}% payout is "
            f"~{breakeven:.1f}% -- NOT 50%, since binary options have a structural house edge; "
            f"every verdict above is measured against that bar, not against a coin flip."
        ),
    )
