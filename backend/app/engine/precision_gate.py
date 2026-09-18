"""Precision Signal gate — an optional, opt-in extra filter layered on top
of the normal confluence/threshold/risk pipeline.

Philosophy: quality over quantity. A signal that clears the normal
confidence threshold can still be a "just barely" call where the strategy
votes were mixed and only won on weight. Precision mode adds three
additional requirements before such a signal is allowed to fire:

  1. Strong agreement  — at least `min_agreeing` strategies voted for the
     winning direction (not just "won on weighted score").
  2. Counter-vote veto  — at most `max_opposing` strategies voted for the
     opposite direction. A signal with several strategies actively voting
     against it is a conflicted setup, even if it cleared the confidence
     threshold on raw score.
  3. Regime alignment    — at least one of the agreeing strategies has a
     natural affinity for the current regime (trend/range) per
     strategies._REGIME_AFFINITY, i.e. the winning vote isn't coming
     entirely from strategies fighting the current market conditions.

This does not replace or modify strategies.evaluate() or the confidence/
risk pipeline — it's a strictly additive final check, off by default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from . import strategies as _strategies
from ..logging_setup import log_event
from ..schemas import EvidenceItem

logger = logging.getLogger("qat.precision_gate")


@dataclass
class PrecisionResult:
    passed: bool
    reason: str
    agreeing: int = 0
    opposing: int = 0
    evidence: Optional[EvidenceItem] = None  # Phase 2: this gate's own verdict as
    # structured evidence (kind="supporting" if passed, "rejecting" if it
    # vetoed) -- the caller (orchestrator.py) attaches this to the signal's
    # SignalDecision.rejecting_evidence (or supporting_evidence) alongside
    # the per-strategy votes StrategyResult.raw_votes already captured.


def evaluate_precision(
    direction_value: str,
    votes: Dict[str, str],
    regime: str,
    min_agreeing: int = 3,
    max_opposing: int = 0,
    require_regime_alignment: bool = True,
    market_context=None,
    context_asset: Optional[str] = None,
    context_timeframe: Optional[str] = None,
) -> PrecisionResult:
    """`direction_value` is res.direction.value ("call"/"put"), `votes` is
    res.votes (strategy name -> "call"/"put"/"neutral"), `regime` is
    res.regime. Pure function, no side effects — safe to call speculatively.

    Adaptive Precision Gate (opt-in, same gate as everything in
    market_context): when `market_context` is provided and enabled (both
    the master switch and its own `precision_gate_enabled` toggle), scales
    `min_agreeing` by this asset's own ADX percentile rank -- a
    stronger-than-usual trend for this asset needs fewer agreeing
    strategies to count as a precise setup, a weaker one needs more. Reuses
    the "adx" history strategies.evaluate() already observes -- read-only,
    no observe() call here, so this stays a pure function regardless of
    whether market_context is passed. Falls back to the caller-supplied
    `min_agreeing` unchanged when market_context is None, disabled, or
    cold-start (percentile rank unavailable) -- exact legacy behavior."""
    if (
        market_context is not None
        and getattr(market_context.cfg, "enabled", False)
        and getattr(market_context.cfg, "precision_gate_enabled", True)
        and context_asset and context_timeframe
    ):
        # Need the current bar's ADX to rank against history, but this
        # function only receives votes/regime, not the raw row -- percentile
        # rank against the MOST RECENTLY OBSERVED value for this asset
        # (the same value strategies.evaluate() just fed into history this
        # same scan pass) is what we want here, so read the engine's own
        # last-seen adx sample instead of requiring a duplicate row param.
        last_adx = market_context.last_value(context_asset, context_timeframe, "adx")
        adx_rank = (
            market_context.percentile_rank(context_asset, context_timeframe, "adx", last_adx)
            if last_adx is not None else None
        )
        if adx_rank is not None:
            agreeing_low = getattr(market_context.cfg, "precision_agreeing_low", 2)
            agreeing_high = getattr(market_context.cfg, "precision_agreeing_high", 4)
            frac = adx_rank / 100.0  # 0 = weakest trend seen for this asset, 1 = strongest
            min_agreeing = round(agreeing_high - frac * (agreeing_high - agreeing_low))

    opposite = "put" if direction_value == "call" else "call"

    agreeing_names = [n for n, v in votes.items() if v == direction_value]
    opposing_names = [n for n, v in votes.items() if v == opposite]
    agreeing = len(agreeing_names)
    opposing = len(opposing_names)

    if agreeing < min_agreeing:
        log_event(logger, logging.DEBUG, "precision_gate_rejected", reason="insufficient_agreement",
                  agreeing=agreeing, min_agreeing=min_agreeing, opposing=opposing, regime=regime)
        reason = f"Precision gate: only {agreeing} agreeing strategies (need {min_agreeing})"
        return PrecisionResult(
            False, reason, agreeing, opposing,
            evidence=EvidenceItem(source="precision_gate", direction=direction_value, strength=float(agreeing),
                                   kind="rejecting", detail=reason),
        )
    if opposing > max_opposing:
        log_event(logger, logging.DEBUG, "precision_gate_rejected", reason="counter_vote_veto",
                  agreeing=agreeing, opposing=opposing, max_opposing=max_opposing, regime=regime)
        reason = f"Precision gate: {opposing} strategies voted opposite (counter-vote veto, max {max_opposing})"
        return PrecisionResult(
            False, reason, agreeing, opposing,
            evidence=EvidenceItem(source="precision_gate", direction=direction_value, strength=float(opposing),
                                   kind="rejecting", detail=reason),
        )
    if require_regime_alignment:
        aligned = any(_strategies._REGIME_AFFINITY.get(n) == regime for n in agreeing_names)
        if not aligned and regime in ("trend", "range"):
            log_event(logger, logging.DEBUG, "precision_gate_rejected", reason="no_regime_affinity",
                      agreeing=agreeing, opposing=opposing, regime=regime)
            reason = f"Precision gate: no agreeing strategy has {regime}-regime affinity"
            return PrecisionResult(
                False, reason, agreeing, opposing,
                evidence=EvidenceItem(source="precision_gate", direction=direction_value, strength=float(agreeing),
                                       kind="rejecting", detail=reason),
            )
    log_event(logger, logging.DEBUG, "precision_gate_passed", agreeing=agreeing, opposing=opposing,
              min_agreeing=min_agreeing, regime=regime)
    pass_reason = f"Precision gate passed: {agreeing} agree, {opposing} oppose"
    return PrecisionResult(
        True, pass_reason, agreeing, opposing,
        evidence=EvidenceItem(source="precision_gate", direction=direction_value, strength=float(agreeing),
                               kind="supporting", detail=pass_reason),
    )
