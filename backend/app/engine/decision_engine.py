"""Decision Intelligence Engine (Phase 1).

Assembles a structured `SignalDecision` record for every signal evaluation
-- accepted or rejected -- from data already computed elsewhere in the
pipeline. This module does NOT compute anything new: it reads the raw
per-strategy vote breakdown (`StrategyResult.raw_votes`, see strategies.py),
the active regime, the confidence-calibration curve, and per-strategy health
scores, and assembles them into one auditable object that answers, for a
single signal:

    - Why should this trade exist?          -> thesis
    - What evidence supports it?            -> supporting_evidence
    - What evidence rejects it?              -> rejecting_evidence
    - What historical probability supports it? -> base_rate
    - What market regime is active?          -> regime
    - How reliable has this setup been recently? -> recent_reliability
    - How uncertain is the prediction?       -> confidence_interval
    - How should confidence change?          -> raw_confidence -> calibrated_confidence

This is purely an assembly/instrumentation layer -- it changes NOTHING about
which signals fire or how confidence is computed. Wiring it into the live
scan loop (orchestrator.py) is additive and gated behind
`decision_engine_enabled` (default off during rollout, per the approved
migration plan) so it can run in shadow before anything downstream depends
on it.

Phase 2 (rejecting-evidence logging) extends `build_decision()`'s evidence
capture to include gate-level vetoes (precision_gate, MTF, signal_validation)
alongside the per-strategy dissent already captured here from
`StrategyResult.raw_votes`. Phase 3 (rejection reconciliation) reads
`DecisionStore` written by this module.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..schemas import Direction, EvidenceItem, RegimeSnapshot, SignalDecision
from .strategies import StrategyResult

logger = logging.getLogger("qat.decision_engine")


def _evidence_from_raw_votes(raw_votes: Dict[str, dict], dominant: Optional[Direction]) -> tuple[List[EvidenceItem], List[EvidenceItem]]:
    """Split a StrategyResult's raw per-strategy vote breakdown into
    supporting vs. rejecting evidence relative to whichever direction the
    confluence engine actually settled on. When `dominant` is None (no
    setup, or a vetoed signal with no clear winner), every directional
    vote that DID exist is still preserved as rejecting evidence -- there
    was evidence, it just didn't cohere into a trade, which is itself
    useful to know later (see rejection_reconciliation.py, Phase 3)."""
    supporting: List[EvidenceItem] = []
    rejecting: List[EvidenceItem] = []
    dominant_value = dominant.value if dominant is not None else None
    for name, detail in raw_votes.items():
        item = EvidenceItem(
            source=name,
            direction=detail.get("direction"),
            strength=float(detail.get("score", detail.get("adjusted_weight", 0.0))),
            kind="supporting" if (dominant_value is not None and detail.get("direction") == dominant_value) else "rejecting",
            detail=detail.get("reason", ""),
        )
        (supporting if item.kind == "supporting" else rejecting).append(item)
    return supporting, rejecting


def _build_thesis(result: StrategyResult, supporting: List[EvidenceItem]) -> str:
    """Short human-readable summary -- NOT a new computation, just a
    concise rendering of reasons already produced by strategies.evaluate()."""
    if result.direction is None:
        return "; ".join(result.reasons) if result.reasons else "No qualifying setup"
    lead = ", ".join(item.detail for item in supporting[:3] if item.detail)
    regime_note = f" in a {result.regime} regime" if result.regime else ""
    direction_word = "CALL" if result.direction == Direction.CALL else "PUT"
    return f"{direction_word} favored{regime_note}: {lead}" if lead else f"{direction_word} favored{regime_note}"


def compute_composite_confidence(
    decision: SignalDecision, *, historical_weight: float, regime_weight: float, calibration_weight: float,
) -> Optional[float]:
    """Blends base_rate / regime confidence / calibrated confidence using
    TradingSettings.decision_historical_probability_weight /
    decision_regime_weight / decision_calibration_weight -- purely
    informational (surfaced in the Monitoring page), never used as a gate
    or to adjust a trade's actual confidence. Returns None if none of the
    three inputs are available yet (e.g. no calibrator built, no regime
    transition result)."""
    parts = []
    if decision.base_rate is not None:
        parts.append((historical_weight, decision.base_rate))
    if decision.regime_transition_confidence is not None:
        parts.append((regime_weight, decision.regime_transition_confidence))
    if decision.calibrated_confidence is not None:
        parts.append((calibration_weight, decision.calibrated_confidence / 100.0))
    total_weight = sum(w for w, _ in parts)
    if total_weight <= 0:
        return None
    blended = sum(w * v for w, v in parts) / total_weight
    return round(blended * 100.0, 1)


def build_decision(
    result: StrategyResult,
    *,
    asset: str,
    timeframe: str,
    calibrator=None,          # calibration.ConfidenceCalibrator, optional
    health_manager=None,      # health_score.HealthScoreManager, optional
    regime_adx: Optional[float] = None,
    regime_adx_percentile: Optional[float] = None,
    volatility_ratio: Optional[float] = None,
) -> SignalDecision:
    """Build one SignalDecision from a completed strategies.evaluate() call.

    Called AFTER strategies.evaluate() but BEFORE the orchestrator's own
    gates (precision_gate, MTF hard veto path already folds into `result`
    via strategies.py itself; risk.can_trade, signal_validation are still
    downstream of this call in the orchestrator, and their vetoes are
    attached by the caller -- see Phase 2). This function only knows what
    strategies.evaluate() knew.
    """
    supporting, rejecting = _evidence_from_raw_votes(result.raw_votes, result.direction)

    recent_reliability = None
    if health_manager is not None and result.raw_votes:
        key = f"{asset}|{timeframe}"
        health = health_manager._cache.get(key, {})  # read-only lookup; no
        # public per-strategy getter exists today (only filter_strategies()
        # and the aggregate status_matrix()) -- using the same cache
        # filter_strategies() itself reads from, not reaching past it.
        scores = [health[name]["score"] for name in result.raw_votes if name in health and "score" in health[name]]
        if scores:
            recent_reliability = round(sum(scores) / len(scores), 1)

    base_rate = None
    confidence_interval = None
    if calibrator is not None and result.direction is not None:
        base_rate = calibrator.calibrate(result.confidence, regime=result.regime) / 100.0
        ci = calibrator.confidence_interval(result.confidence, regime=result.regime)
        if ci is not None:
            confidence_interval = [ci[0], ci[1]]

    regime_snapshot = RegimeSnapshot(
        regime=result.regime or "unknown",
        adx=regime_adx,
        adx_percentile=regime_adx_percentile,
        volatility_ratio=volatility_ratio,
    ) if result.regime else None

    final_decision = "accepted" if result.direction is not None else "rejected"
    rejection_reason = None if final_decision == "accepted" else (result.reasons[0] if result.reasons else "No qualifying setup")

    calibrated_confidence = None
    if calibrator is not None and result.direction is not None:
        calibrated_confidence = calibrator.calibrate(result.confidence, regime=result.regime)

    return SignalDecision(
        asset=asset,
        direction=result.direction,
        timeframe=timeframe,
        thesis=_build_thesis(result, supporting),
        supporting_evidence=supporting,
        rejecting_evidence=rejecting,
        base_rate=base_rate,
        regime=regime_snapshot,
        recent_reliability=recent_reliability,
        confidence_interval=confidence_interval,
        raw_confidence=result.confidence if result.direction is not None else None,
        calibrated_confidence=calibrated_confidence,
        final_decision=final_decision,
        rejection_reason=rejection_reason,
        raw_features=dict(result.raw_features) if result.raw_features else None,
    )


class DecisionStore:
    """Append-only, per-user JSONL log of every SignalDecision -- accepted
    AND rejected. Same storage shape as QueuePersistence/ShadowStore
    (proven-safe pattern in this codebase for exactly this kind of
    write-heavy, append-only record), deliberately NOT SQLite for this
    phase: this is a log, not a queryable relational table yet. Phase 3's
    reconciliation reads this file directly.

    Bounded by both a max-file-size rotation and an explicit retention
    trim, since this grows with every scan cycle (not just executed
    trades) and is expected to be the highest-volume store in this system.
    """

    def __init__(self, storage_dir: str, *, max_records: int = 50_000) -> None:
        self.storage_path = Path(storage_dir)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.log_file = self.storage_path / "decisions.jsonl"
        self.max_records = max_records
        self._count = self._count_lines() if self.log_file.exists() else 0

    def _count_lines(self) -> int:
        try:
            with open(self.log_file, "r") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def append(self, decision: SignalDecision) -> None:
        try:
            with open(self.log_file, "a") as f:
                f.write(decision.model_dump_json() + "\n")
            self._count += 1
        except OSError as e:
            logger.error(f"[decision_engine] Failed to persist SignalDecision {decision.id}: {e}")
            return
        if self._count > self.max_records:
            self._trim()

    def _trim(self) -> None:
        """Keep only the most recent max_records entries. Rewrites the file
        once every max_records/2 appends (amortized), not on every append."""
        try:
            with open(self.log_file, "r") as f:
                lines = f.readlines()
            keep = lines[-(self.max_records // 2):]
            tmp = self.log_file.with_suffix(".jsonl.tmp")
            with open(tmp, "w") as f:
                f.writelines(keep)
            os.replace(tmp, self.log_file)
            self._count = len(keep)
        except OSError as e:
            logger.error(f"[decision_engine] Failed to trim DecisionStore: {e}")

    def read_recent(self, limit: int = 500) -> List[SignalDecision]:
        """Read the most recent `limit` decisions, newest last (same order
        as the file). Used by Phase 3's reconciliation and any future
        reporting endpoint -- not on any hot path."""
        if not self.log_file.exists():
            return []
        try:
            with open(self.log_file, "r") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(SignalDecision.model_validate_json(line))
            except Exception as e:
                logger.warning(f"[decision_engine] Skipping malformed DecisionStore line: {e}")
        return out
